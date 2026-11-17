# AI 任务执行服务（第八批规格实现 + P0 任务系统补齐 + P1 统一 AI 任务）
# run_ai_task 由 FastAPI BackgroundTasks 触发，使用独立数据库会话
# （不能复用请求结束后已关闭的请求会话），状态流转：
# pending -> running -> succeeded / failed；超时任务标记 timeout；运行中取消保持 cancelled。
# P0 补齐：超时判定（timeout_at）、token 用量落库（take_last_ai_usage）、错误信息记录。
# P1 补齐：
#  - 并发保护：原子抢占 pending -> running（UPDATE ... WHERE status='pending'），
#    同一任务只会被一个 worker 执行，重试不会重复扣费/重复写入；
#  - 任务分派：按 task_type 分派到章节续写/改写/润色、大纲生成、一致性检查、
#    变更建议生成等执行器，使 AI 操作统一进入 ai_tasks（任务中心可见、可取消/重试）。
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ai_task import AITask
from app.models.chapter_content import ChapterContent
from app.models.project import Project
from app.models.user import User
from app.services import event_service, generation_service
from app.services.ai_service import (
    chat_with_ai,
    chat_with_ai_json_dict,
    take_last_ai_usage,
)
from app.services.context_builder import build_continuation_context
from app.services.consistency_service import run_consistency_check
from app.services.model_policy import TaskType
from app.services.token_usage_service import (
    UsageLedgerError,
    install_usage_ledger_hook,
    uninstall_usage_ledger_hook,
)
from app.services.review_repair_service import (
    build_repair_candidate,
    run_llm_consistency_review,
)

logger = logging.getLogger(__name__)

# 章节续写/改写/润色默认最大生成长度（与同步接口默认一致）
DEFAULT_MAX_LENGTH = 800

# ---- 阶段D「异步任务增强」常量 ----
# repair 类任务自动修复次数上限（默认 2 次/问题）：同一问题（issue_id）
# 在同一作品下最多发起 AUTO_REPAIR_MAX_PER_ISSUE 次 repair 任务，超出拒绝。
AUTO_REPAIR_MAX_PER_ISSUE = 2

# 阶段I：受「自动修复次数上限」约束的任务类型（补丁候选生成同样计入上限，
# 避免绕过限次守卫反复调用模型）。repair 为应用补丁、repair_candidate 为只读候选。
REPAIR_TASK_TYPES = ("repair", "repair_candidate")


def _should_auto_retry(task: "AITask") -> bool:
    """是否允许自动重试：仅幂等任务（idempotency_key 非空）且 max_retries > 0
    且未超过重试上限。max_retries 默认 0 = 不自动重试（新策略默认关闭）。"""
    return bool(
        task.max_retries and task.max_retries > 0
        and task.idempotency_key
        and task.retry_count < task.max_retries
    )


def _known_token_floor(task: "AITask") -> tuple[int, bool]:
    """未知 usage 安全口径（阶段 1 冻结期最小修复）：返回 (已知 token 下界, 是否存在未知侧)。

    input_tokens / output_tokens 为 NULL 表示「供应商未返回真实 usage，用量未知」。
    未知**不得**折算为 0 —— 否则「未知用量」会被当成「零消耗」，预算守卫被静默绕过
    （历史缺陷：`(input or 0) + (output or 0)`）。
    本函数只累加已取得的真实值，作为累计用量的**下界**：token 非负，故下界永不高估，
    用它判定超限不会误杀（只是可能在信息不足时来不及拦截）。
    """
    known = [v for v in (task.input_tokens, task.output_tokens) if v is not None]
    has_unknown = task.input_tokens is None or task.output_tokens is None
    return sum(known), has_unknown


def _budget_unknown_warning(task: "AITask", phase: str, floor: int) -> None:
    """未知 usage 的可观测记录：仅写日志（不落库、不改 schema、不阻断任务、不改任务终态）。"""
    logger.warning(
        "cost_budget 预算判定遇到未知 token 用量（%s）：task_id=%s cost_budget=%s，"
        "input_tokens=%r output_tokens=%r，按已知下界 %s 判定（未知侧不折算为 0）",
        phase,
        task.id,
        task.cost_budget,
        task.input_tokens,
        task.output_tokens,
        floor,
    )


def _budget_exceeded_before_run(task: "AITask") -> bool:
    """执行前成本预算检查：任务已累计 token 用量达到/超过预算则直接失败，
    避免继续烧钱（cost_budget 为 NULL 时不检查，默认关闭）。

    未知 usage 安全口径：以「已知 token 下界」判定。下界已达预算说明**确定**超限才阻断；
    存在未知侧时只记 warning，既不把未知当 0 静默放行，也不因未知而误杀任务。
    """
    if task.cost_budget is None:
        return False
    floor, has_unknown = _known_token_floor(task)
    if has_unknown:
        _budget_unknown_warning(task, "执行前", floor)
    return floor >= task.cost_budget


def _budget_exceeded_after_run(task: "AITask") -> bool:
    """执行后成本预算检查：本次落账 token 用量超过预算则判失败并提示。

    与执行前一致：以已知 token 下界判定（未知侧不折算为 0、不参与比较）。
    """
    if task.cost_budget is None:
        return False
    floor, has_unknown = _known_token_floor(task)
    if has_unknown:
        _budget_unknown_warning(task, "执行后", floor)
    return floor > task.cost_budget


def _budget_exceeded_message(task: "AITask") -> str:
    """预算超限提示：未知侧一律显示为「未知」，不得显示成 0（会误导为「零消耗」）。"""
    if task.input_tokens is None or task.output_tokens is None:
        floor, _ = _known_token_floor(task)
        input_text = "未知" if task.input_tokens is None else task.input_tokens
        output_text = "未知" if task.output_tokens is None else task.output_tokens
        return (
            f"任务成本预算已超限（cost_budget={task.cost_budget}，"
            f"累计 token 下界={input_text} + {output_text}"
            f"（合计 {floor}，未知侧未计入）），已直接失败"
        )
    return (
        f"任务成本预算已超限（cost_budget={task.cost_budget}，"
        f"累计 token={task.input_tokens} + {task.output_tokens}），已直接失败"
    )


async def checkpoint_progress(
    db: AsyncSession,
    task: "AITask",
    payload: dict,
) -> None:
    """记录任务进度快照（resume_token），供取消后恢复断点续跑。
    仅 running 状态可写；写入后立即提交，取消时进度不丢失。"""
    if task.status != "running":
        return
    task.resume_token = json.dumps(payload, ensure_ascii=False)
    await db.commit()


async def count_repair_attempts(
    db: AsyncSession,
    project_id: uuid.UUID,
    issue_id: str,
) -> int:
    """统计某作品下同一问题已发起的 repair/补丁候选任务数（自动修复次数上限依据）。
    repair 类任务的 issue_id 位于 request_snapshot 中，逐条解析统计。
    阶段I：repair_candidate（只读候选生成）同样计入上限，防止绕过限次守卫。"""
    rows = (await db.execute(
        select(AITask).where(
            AITask.project_id == project_id,
            AITask.task_type.in_(REPAIR_TASK_TYPES),
        )
    )).scalars().all()
    count = 0
    for row in rows:
        if not row.request_snapshot:
            continue
        try:
            snap = json.loads(row.request_snapshot)
        except Exception:
            continue
        if snap.get("issue_id") == issue_id:
            count += 1
    return count


async def run_ai_task(
    task_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession]
):
    async with session_factory() as db:
        now = datetime.utcnow()

        # P1 并发保护：原子抢占 pending -> running。
        # rowcount == 0 表示任务已被其他 worker 抢占、已取消或不存在，直接返回。
        claim = await db.execute(
            update(AITask)
            .where(AITask.id == task_id, AITask.status == "pending")
            .values(status="running", started_at=now)
        )
        if claim.rowcount == 0:
            return

        task = (await db.execute(
            select(AITask).where(AITask.id == task_id)
        )).scalar_one_or_none()

        if task is None:
            return

        # P0：超时判定——进入执行前已超时的任务直接标记 timeout，不再调用 AI
        if task.timeout_at is not None and now >= task.timeout_at:
            task.status = "timeout"
            task.completed_at = now
            task.error_message = "任务在开始执行前已超时"
            await db.commit()
            return

        # 阶段D：成本预算预检——累计 token 已超预算直接失败，不再调用 AI
        if _budget_exceeded_before_run(task):
            task.status = "failed"
            task.error_message = _budget_exceeded_message(task)
            task.completed_at = datetime.utcnow()
            await db.commit()
            await _emit_task_status(db, task)
            return

        # B-2：安装逐次 token 账目钩子（本执行轮内每次供应商调用前/后落账）。
        # 钩子绑定当前任务与当前会话，执行结束（含异常/取消/提前返回）必须卸载，
        # 否则 contextvar 会污染同一 worker 上下文中的后续任务。
        ledger_hook = install_usage_ledger_hook(db, task)
        try:
            while True:
                # 自动重试轮：上一轮失败后状态被置回 pending，本轮重新抢占 running。
                # 首轮已在函数开头抢占，此处仅处理重试轮。
                if task.status == "pending":
                    reclaim = await db.execute(
                        update(AITask)
                        .where(AITask.id == task_id, AITask.status == "pending")
                        .values(status="running")
                    )
                    if reclaim.rowcount == 0:
                        return
                    task.status = "running"

                try:
                    result_text = await _execute_task(db, task)

                    # 重新读取最新状态：AI 调用期间任务可能已被取消
                    await db.refresh(task)

                    if task.status == "cancelled":
                        return

                    # P0：执行期间再次判定超时（AI 调用耗时超过 timeout_at）
                    if task.timeout_at is not None and datetime.utcnow() >= task.timeout_at:
                        task.status = "timeout"
                        task.error_message = "任务执行超时"
                        task.completed_at = datetime.utcnow()
                        await db.commit()
                        await _emit_task_status(db, task)
                        return

                    # P0：token 用量落库（ai_service 的 contextvar 提供最近一次调用用量）。
                    # 重试场景：仅最终成功那次落账，失败尝试不落账——重试不重复扣费。
                    input_tokens, output_tokens = take_last_ai_usage()
                    task.input_tokens = input_tokens
                    task.output_tokens = output_tokens

                    # B-2：本次调用结清为 partial / unverifiable（费用口径不确定）。
                    # 与既有口径的边界（不得回归阶段1 冻结契约）：
                    #   - 正文已取得 -> 本次结果照常交付（未知用量不得折算成 0 伪造），
                    #     任务不因「账目口径未知」而作废，token 字段仍留 NULL；
                    #   - 「禁后续调用」由账目闸门实现：本执行轮内的下一次派发
                    #     （before_dispatch）以及 resume / retry 端点会被 409/422 拦截。
                    if ledger_hook.blocked_error is not None:
                        logger.warning(
                            "本次调用账目口径不确定（已阻断后续派发），正文照常交付："
                            "task_id=%s %s",
                            task.id,
                            ledger_hook.blocked_error.message,
                        )

                    # 阶段D：成本预算检查——本次落账后超限直接失败并提示（不进成功态、不再重试）
                    if _budget_exceeded_after_run(task):
                        task.status = "failed"
                        task.error_message = _budget_exceeded_message(task)
                        task.result = result_text
                        task.completed_at = datetime.utcnow()
                        await db.commit()
                        await _emit_task_status(db, task)
                        return

                    task.status = "succeeded"
                    task.result = result_text
                    task.completed_at = datetime.utcnow()

                except Exception as error:
                    await db.refresh(task)

                    if task.status == "cancelled":
                        return

                    # B-2：账目闸门 / 预算闸门拦截或落账失败 -> 直接失败，禁止自动重试
                    # （费用口径未知时重试只会重复烧钱；异常本身携带 409/422 契约）
                    if isinstance(error, UsageLedgerError):
                        task.status = "failed"
                        task.error_message = str(error)
                        task.completed_at = datetime.utcnow()
                        await db.commit()
                        await _emit_task_status(db, task)
                        return

                    # 阶段D：失败自动重试——仅幂等任务、未超上限，置回 pending 继续循环
                    if _should_auto_retry(task):
                        task.retry_count += 1
                        task.status = "pending"
                        task.error_message = (
                            f"执行失败（第 {task.retry_count} 次），任务将自动重试：{error}"
                        )
                        await db.commit()
                        continue

                    task.status = "failed"
                    task.error_message = str(error)
                    task.completed_at = datetime.utcnow()

                await db.commit()
                await _emit_task_status(db, task)
                return
        finally:
            uninstall_usage_ledger_hook()


async def _emit_task_status(db: AsyncSession, task: "AITask") -> None:
    """第十五批事件通知：AI 任务完成/失败 → 通知任务发起人（AI 任务通知）。"""
    project = await db.get(Project, task.project_id)
    actor = await db.get(User, task.user_id)
    if project is not None and actor is not None:
        await event_service.emit_ai_task_status(
            db, project, actor,
            status=task.status,
            task_title=task.task_type,
            target_id=str(task.id),
        )
        # 通知在 emit 链路只 flush 不提交：此处显式 commit，
        # 否则调用方已 commit 后退出会话，通知行被回滚（通知永远不会落库）
        await db.commit()


# ---------------------------------------------------------------- 任务分派

async def _execute_task(db: AsyncSession, task: AITask) -> str:
    """按 task_type 分派到对应执行器，返回任务结果文本（成功时为 JSON 或正文）。"""
    snapshot = {}
    if task.request_snapshot:
        try:
            snapshot = json.loads(task.request_snapshot)
        except Exception:
            snapshot = {}

    # 阶段D：取消恢复——把 resume_token 进度快照合并进 snapshot，执行器从断点继续。
    # 已完成的阶段（stage）不重复执行，仅重跑未完成部分。
    if task.resume_token:
        try:
            resume = json.loads(task.resume_token)
            if isinstance(resume, dict):
                snapshot["_resume"] = resume
        except Exception:
            pass

    # 阶段D：记录进度快照——任务已进入分派阶段（取消后恢复时可定位）
    await checkpoint_progress(
        db, task,
        {"stage": "dispatch", "task_type": task.task_type},
    )

    if task.task_type in {"continue", "rewrite", "polish"}:
        return await _execute_chapter_edit(db, task, snapshot)
    if task.task_type in {"generate_summary", "generate_volumes", "generate_chapters"}:
        return await _execute_outline_generation(db, task, snapshot)
    if task.task_type == "consistency_check":
        return await _execute_consistency_check(db, task, snapshot)
    if task.task_type == "generate_suggestions":
        return await _execute_generate_suggestions(db, task, snapshot)
    if task.task_type == "repair":
        return await _execute_repair(db, task, snapshot)
    # 阶段I：独立 LLM 连续性审稿（只读报告，不改正文）
    if task.task_type == "consistency_review":
        return await _execute_llm_consistency_review(db, task, snapshot)
    # 阶段I：修复补丁候选生成（纯只读，不落正文）
    if task.task_type == "repair_candidate":
        return await _execute_repair_candidate(db, task, snapshot)

    # 兜底：未匹配的执行器走通用 AI 对话（兼容旧任务）
    return await chat_with_ai(
        system_prompt="你是一名专业的小说创作助手。",
        user_prompt=task.prompt,
        override=({"model_name": task.model_name} if task.model_name else None),
        task_type=TaskType.GENERAL.value,
    )


async def _execute_chapter_edit(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """续写 / 改写 / 润色：复用上下文构建器与生成记录，与同步接口行为一致。"""
    if not task.outline_id:
        raise ValueError("续写/改写/润色任务需要 outline_id")

    max_length = int(snapshot.get("max_length") or DEFAULT_MAX_LENGTH)
    instruction = snapshot.get("instruction") or ""

    context = await build_continuation_context(
        db,
        task.project_id,
        task.outline_id,
        max_length=max_length,
        instruction=instruction,
    )
    system_prompt = context["system_prompt"]
    user_prompt = context["user_prompt"]

    override = {}
    if task.model_name:
        override["model_name"] = task.model_name

    provider, model_name = generation_service.resolve_ai_provider()
    if override.get("model_name"):
        model_name = override["model_name"]

    content = await chat_with_ai(
        system_prompt,
        user_prompt,
        override=override,
        task_type=TaskType.NOVEL_BODY.value,
    )

    # 与同步接口一致：成功先写 generation_record（供前端“接受为新版本”回传）
    await generation_service.record_generation(
        db,
        attribution_status="project_bound",
        project_id=task.project_id,
        chapter_id=task.outline_id,
        action_type=task.task_type,
        model_provider=provider,
        model_name=model_name,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context_snapshot=generation_service.serialize_context(
            context.get("snapshot")
        ),
        response_content=content,
        status="succeeded",
    )
    return content


async def _execute_outline_generation(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """大纲生成（简介/分卷/分章）：读取作品设定数据，AI 生成结构化 JSON。"""
    project = await db.get(Project, task.project_id)
    if project is None:
        raise ValueError("作品不存在")

    prompt = task.prompt or ""
    if not prompt:
        prompt = (
            f"作品《{project.title}》"
            + (f"（简介：{project.summary}）" if getattr(project, "summary", None) else "")
        )

    result = await chat_with_ai_json_dict(
        system_prompt=(
            "你是资深小说策划编辑。请根据作品设定生成结构化大纲 JSON，"
            "必须只输出合法 JSON 对象，不要输出多余文字。"
        ),
        user_prompt=prompt,
        override=({"model_name": task.model_name} if task.model_name else None),
        task_type=TaskType.OUTLINE_PLAN.value,
    )

    # 记录生成痕迹，便于追溯模型与提示词
    await generation_service.record_generation(
        db,
        attribution_status="project_bound",
        project_id=task.project_id,
        action_type=task.task_type,
        model_provider=generation_service.resolve_ai_provider()[0],
        model_name=task.model_name,
        system_prompt="你是资深小说策划编辑。请根据作品设定生成结构化大纲 JSON，"
                      "必须只输出合法 JSON 对象，不要输出多余文字。",
        user_prompt=prompt,
        response_content=json.dumps(result, ensure_ascii=False),
        status="succeeded",
    )
    return json.dumps(result, ensure_ascii=False)


async def _execute_repair(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """repair 类任务（阶段D 预置自动修复次数上限；修复候选只读生成，不写正文）。

    阶段D 只落地「自动修复上限守卫 + 修复候选只读生成」：基于校验问题（issue）
    生成修复建议 JSON，不修改任何正文/事实。完整的 repair 候选 / 局部复检
    闭环（阶段I）在此基础上扩展。
    """
    resume = snapshot.get("_resume") or {}
    if resume.get("stage") == "repair_done":
        # 取消后恢复：该阶段已完成，直接复用快照中的结果，不重复调用 AI
        return resume.get("result") or ""

    issue = snapshot.get("issue") or {}
    issue_id = snapshot.get("issue_id") or issue.get("id")
    if not issue_id:
        raise ValueError("repair 任务需要 issue_id（request_snapshot.issue_id）")

    # 阶段D：进度快照——AI 调用前记录断点（取消后恢复时从此处继续）
    await checkpoint_progress(
        db, task,
        {"stage": "ai_call_started", "issue_id": issue_id, "task_type": "repair"},
    )

    result = await chat_with_ai_json_dict(
        system_prompt=(
            "你是小说编辑，负责针对作品校验问题给出修复建议。"
            "输出 JSON：{\"repair_plan\": [{\"location\": 问题位置, "
            "\"suggestion\": 修复建议文本, \"reason\": 理由}]}。"
            "必须只输出合法 JSON。"
        ),
        user_prompt=json.dumps(
            {"issue_id": issue_id, "issue": issue},
            ensure_ascii=False,
        ),
        override=({"model_name": task.model_name} if task.model_name else None),
        task_type=TaskType.REPAIR_CANDIDATE.value,
    )

    result_text = json.dumps(result, ensure_ascii=False)

    # 阶段D：进度快照——阶段完成，取消后恢复可直接复用结果
    await checkpoint_progress(
        db, task,
        {"stage": "repair_done", "issue_id": issue_id, "result": result_text},
    )
    return result_text


async def _execute_llm_consistency_review(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """阶段I：独立 LLM 连续性审稿执行器（异步、可取消、只读报告）。

    - 读取章节正文（只读）→ 调独立审稿服务产出报告（落 consistency_review）；
    - 审稿问题只入 change_suggestions 建议队列（pending），人工确认后才可应用；
    - **不修改正文、不修改事实库**；
    - 支持取消后恢复：review_done 阶段已完成时直接复用快照结果，不重复调用模型。
    """
    resume = snapshot.get("_resume") or {}
    if resume.get("stage") == "review_done":
        return resume.get("result") or ""

    chapter_id = task.outline_id or snapshot.get("chapter_id")
    if not chapter_id:
        raise ValueError(
            "独立审稿任务需要 chapter_id（outline_id 或 request_snapshot.chapter_id）"
        )

    content = ""
    row = (await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == chapter_id
        )
    )).scalar_one_or_none()
    if row is not None:
        content = row.content or ""

    await checkpoint_progress(
        db, task,
        {
            "stage": "review_ai_started",
            "task_type": "consistency_review",
            "chapter_id": str(chapter_id),
        },
    )

    result = await run_llm_consistency_review(
        db,
        project_id=task.project_id,
        chapter_id=chapter_id,
        chapter_title=snapshot.get("chapter_title") or "",
        content=content,
        outline_text=snapshot.get("outline_text") or "",
        model_name=task.model_name,
        review_id=resume.get("review_id") or snapshot.get("review_id"),
        trigger=snapshot.get("trigger") or "manual",
    )
    result_text = json.dumps(result, ensure_ascii=False)

    await checkpoint_progress(
        db, task,
        {
            "stage": "review_done",
            "review_id": result.get("review_id"),
            "result": result_text,
        },
    )
    return result_text


async def _execute_repair_candidate(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """阶段I：修复补丁候选生成执行器（纯只读，不落正文）。

    限次守卫（阶段D AUTO_REPAIR_MAX_PER_ISSUE=2 次/问题）在任务创建侧校验；
    本执行器只生成候选并写入任务结果供人工审阅，绝不应用补丁、不写正文。
    """
    resume = snapshot.get("_resume") or {}
    if resume.get("stage") == "candidate_done":
        return resume.get("result") or ""

    issue = snapshot.get("issue") or {}
    issue_id = snapshot.get("issue_id") or issue.get("id")
    if not issue_id:
        raise ValueError(
            "repair_candidate 任务需要 issue_id（request_snapshot.issue_id）"
        )

    await checkpoint_progress(
        db, task,
        {
            "stage": "candidate_ai_started",
            "issue_id": issue_id,
            "task_type": "repair_candidate",
        },
    )

    candidate = await build_repair_candidate(
        db,
        project_id=task.project_id,
        issue=issue,
        chapter_id=task.outline_id or snapshot.get("chapter_id"),
        model_name=task.model_name,
    )
    result_text = json.dumps(candidate, ensure_ascii=False)

    await checkpoint_progress(
        db, task,
        {
            "stage": "candidate_done",
            "issue_id": issue_id,
            "result": result_text,
        },
    )
    return result_text


async def _execute_consistency_check(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """一致性检查：读取章节正文，执行本地规则 + AI 检查，结果落库并返回 JSON。"""
    if not task.outline_id:
        raise ValueError("一致性检查任务需要 outline_id")

    content = ""
    row = (await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == task.outline_id
        )
    )).scalar_one_or_none()
    if row is not None:
        content = row.content or ""

    chapter_title = snapshot.get("chapter_title") or "本章"
    outline_text = snapshot.get("outline_text") or ""

    result = await run_consistency_check(
        db,
        project_id=task.project_id,
        chapter_id=task.outline_id,
        chapter_title=chapter_title,
        content=content,
        outline_text=outline_text,
    )
    if result.get("status") == "failed":
        # 检查失败要让异步任务进入 failed，而不是伪装 succeeded 返回“100 分”
        raise RuntimeError(
            f"一致性检查失败: {(result.get('error_message') or '')[:200]}"
        )
    return json.dumps(result, ensure_ascii=False)


async def _execute_generate_suggestions(
    db: AsyncSession,
    task: AITask,
    snapshot: dict,
) -> str:
    """变更建议生成：基于章节正文与一致性结果，AI 产出建议列表 JSON。"""
    if not task.outline_id:
        raise ValueError("变更建议生成任务需要 outline_id")

    content = ""
    row = (await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == task.outline_id
        )
    )).scalar_one_or_none()
    if row is not None:
        content = row.content or ""

    if not content.strip():
        return json.dumps(
            {"suggestions": [], "note": "章节暂无正文，未生成建议"},
            ensure_ascii=False,
        )

    result = await chat_with_ai_json_dict(
        system_prompt=(
            "你是小说编辑，负责给作者提出可操作的修改建议。"
            "请阅读章节正文，输出 JSON：{\"suggestions\": [{\"original\": 原文片段, "
            "\"suggested\": 建议文本, \"reason\": 理由, \"severity\": \"suggestion|warning|critical\"}]}。"
            "必须只输出合法 JSON。"
        ),
        user_prompt=content[:12000],
        override=({"model_name": task.model_name} if task.model_name else None),
        task_type=TaskType.REPAIR_CANDIDATE.value,
    )

    await generation_service.record_generation(
        db,
        attribution_status="project_bound",
        project_id=task.project_id,
        chapter_id=task.outline_id,
        action_type="suggest",
        model_provider=generation_service.resolve_ai_provider()[0],
        model_name=task.model_name,
        system_prompt="你是小说编辑，负责给作者提出可操作的修改建议。",
        user_prompt=content[:12000],
        response_content=json.dumps(result, ensure_ascii=False),
        status="succeeded",
    )
    return json.dumps(result, ensure_ascii=False)
