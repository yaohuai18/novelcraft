"""「整本规划」后台作业服务（持久化租约 + 原子领取 + 幂等提交 + 晚到结果）。

背景
----
`POST /api/ai/plan-novel` 是同步接口，一次整本大纲 240~322 秒、2 万~2.8 万 token。
浏览器 240 秒先断开，响应被丢弃 —— 后端跑完了但结果没落到任何地方。

第二批把执行方式从「请求里 fire-and-forget」升级为「数据库租约 + 原子领取」：
任务来源、状态、租约、结果全部持久化；进程重启后 queued 仍可被领取，
租约过期的 running 才转 interrupted；晚到的成功结果只写 result_review_status，
不静默丢弃、也不伪装成成功。

边界
----
- 不碰 `ai_tasks`（任务中心那套补丁仍处归档状态），两套合并属第三批。
- 不改既有表，只在 novel_plan_jobs 上增列（v7）。
- 仍是**单机 in-process worker**：领取靠数据库 CAS，因此多进程也不会重复执行，
  但目前没有跨机调度、没有优先级、没有重试队列。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.novel_plan_job import NovelPlanJob
from app.services import generation_service as gs
from app.services import novel_plan_job_state as st
from app.services.ai_service import ai_plan_novel, take_last_ai_meta

logger = logging.getLogger(__name__)

# 租约：worker 领取时写 lease_expires_at = now + LEASE_SECONDS，每 15 秒续一次。
# 15 秒心跳 / 120 秒租约 = 允许连续丢 8 次心跳才判死，足够扛一次 GC 或慢查询。
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 15
# worker 扫描间隔（只在有 queued 时才真正抢）
WORKER_POLL_SECONDS = 3
# 单次作业硬上限：正常 5~6 分钟
MAX_RUNTIME_SECONDS = 1800
# 单进程并发执行上限（整本规划是重任务，不值得并行太多）
MAX_CONCURRENT_JOBS = 2
DEFAULT_LIST_LIMIT = 10
# 整本规划默认不自动重试：重跑一次就是两万多 token，必须由人决定
DEFAULT_MAX_ATTEMPTS = 1

# 保持后台任务引用，避免被垃圾回收（asyncio 只持弱引用）
_RUNNING: set[asyncio.Task] = set()


def worker_id() -> str:
    return os.environ.get("NOVELCRAFT_WORKER_ID") or f"worker-{os.getpid()}"


def _now() -> datetime:
    return datetime.utcnow()


def _lease_deadline(seconds: int = LEASE_SECONDS) -> datetime:
    return _now() + timedelta(seconds=seconds)


def scope_of(request_payload: dict, *, project_id: str | None = None) -> str:
    """幂等作用域。

    - 有 project_id：按项目隔离；
    - 没有 project_id（整本规划发生在作品创建之前）：按「请求实质内容」的指纹隔离，
      这样同一份草稿重复提交会合并到同一个任务，而不同创意可以并行。
    """
    if project_id:
        return f"project:{project_id}"
    material = {
        "idea": request_payload.get("idea") or "",
        "genre": request_payload.get("genre") or "",
        "writing_style": request_payload.get("writing_style") or "",
        "perspective": request_payload.get("perspective") or "",
        "chapter_count": request_payload.get("chapter_count"),
        "words_per_chapter": request_payload.get("words_per_chapter"),
        "ending_type": request_payload.get("ending_type") or "",
        "has_romance": bool(request_payload.get("has_romance")),
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "draft:" + hashlib.sha256(blob).hexdigest()[:24]


async def _find_by_key(
    db: AsyncSession, *, user_id: uuid.UUID, scope: str, key: str
) -> NovelPlanJob | None:
    return (
        await db.execute(
            select(NovelPlanJob).where(
                NovelPlanJob.user_id == user_id,
                NovelPlanJob.idempotency_scope == scope,
                NovelPlanJob.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


async def _find_active(
    db: AsyncSession, *, user_id: uuid.UUID, scope: str
) -> NovelPlanJob | None:
    return (
        await db.execute(
            select(NovelPlanJob)
            .where(
                NovelPlanJob.user_id == user_id,
                NovelPlanJob.idempotency_scope == scope,
                NovelPlanJob.status.in_(st.ACTIVE_STATUSES),
            )
            .order_by(NovelPlanJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_job(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    request_payload: dict,
    mode: str,
    idempotency_key: str | None = None,
    scope: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[NovelPlanJob, bool]:
    """落库为 queued。返回 (job, created)。

    - 同一 (user, scope, key) 已存在 → 直接返回那条（跨请求幂等）；
    - 同一 (user, scope) 已有活跃任务 → 返回那条（默认最多一个活跃规划任务）；
    - 都没有才新建。unique 索引兜住并发同 key 的竞态。
    """
    scope = scope or scope_of(request_payload)

    if idempotency_key:
        existing = await _find_by_key(
            db, user_id=user_id, scope=scope, key=idempotency_key
        )
        if existing is not None:
            return existing, False

    active = await _find_active(db, user_id=user_id, scope=scope)
    if active is not None:
        return active, False

    job = NovelPlanJob(
        id=uuid.uuid4(),
        user_id=user_id,
        status=st.STATUS_QUEUED,
        mode=mode,
        request_json=json.dumps(request_payload, ensure_ascii=False),
        progress=0,
        created_at=_now(),
        attempt_count=0,
        max_attempts=max(1, int(max_attempts)),
        idempotency_key=idempotency_key,
        idempotency_scope=scope,
    )
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        # 并发同 key：另一个请求先插进去了，回读它
        await db.rollback()
        if idempotency_key:
            existing = await _find_by_key(
                db, user_id=user_id, scope=scope, key=idempotency_key
            )
            if existing is not None:
                return existing, False
        raise
    await db.refresh(job)
    return job, True


async def claim_job(
    db: AsyncSession, job_id: uuid.UUID, worker: str, *, lease_seconds: int = LEASE_SECONDS
) -> bool:
    """queued → running（单条 CAS）。返回 False = 已被别人领走 / 已终态 / 达领取上限。"""
    now = _now()
    result = await db.execute(
        update(NovelPlanJob)
        .where(
            NovelPlanJob.id == job_id,
            NovelPlanJob.status == st.STATUS_QUEUED,
            NovelPlanJob.attempt_count < NovelPlanJob.max_attempts,
        )
        .values(
            status=st.STATUS_RUNNING,
            started_at=func.coalesce(NovelPlanJob.started_at, now),
            worker_id=worker,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            progress=10,
            attempt_count=NovelPlanJob.attempt_count + 1,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def claim_next_job(
    db: AsyncSession, worker: str, *, lease_seconds: int = LEASE_SECONDS
) -> uuid.UUID | None:
    """扫描并领取一个 queued（数据库条件更新，不做 SELECT-then-UPDATE 判断）。"""
    candidate = (
        await db.execute(
            select(NovelPlanJob.id)
            .where(
                NovelPlanJob.status == st.STATUS_QUEUED,
                NovelPlanJob.attempt_count < NovelPlanJob.max_attempts,
            )
            .order_by(NovelPlanJob.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate is None:
        return None
    if await claim_job(db, candidate, worker, lease_seconds=lease_seconds):
        return candidate
    return None


async def renew_lease(db: AsyncSession, job_id: uuid.UUID, worker: str) -> bool:
    """续租：只在「仍是自己的 running」时生效。返回 False 表示已失租。"""
    now = _now()
    result = await db.execute(
        update(NovelPlanJob)
        .where(
            NovelPlanJob.id == job_id,
            NovelPlanJob.status == st.STATUS_RUNNING,
            NovelPlanJob.worker_id == worker,
        )
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
    )
    await db.commit()
    return result.rowcount == 1


async def _heartbeat_loop(
    job_id: uuid.UUID, worker: str, lost: asyncio.Event, interval: float = HEARTBEAT_SECONDS
) -> None:
    """独立会话续租。续不上（行数 0）→ 置 lost 并退出，主流程据此放弃写结果。"""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                async with AsyncSessionLocal() as db:
                    ok = await renew_lease(db, job_id, worker)
            except Exception as exc:  # noqa: BLE001 单次数据库抖动不算失租
                logger.warning("novel_plan_jobs 续租失败 job=%s：%r", job_id, exc)
                continue
            if not ok:
                logger.warning("novel_plan_jobs 已失租 job=%s worker=%s", job_id, worker)
                lost.set()
                return
    except asyncio.CancelledError:
        raise


async def _current_error_code(job_id: uuid.UUID) -> str | None:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(NovelPlanJob.error_code).where(NovelPlanJob.id == job_id)
            )
        ).first()
    return row[0] if row else None


async def _current_status(job_id: uuid.UUID) -> str | None:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(NovelPlanJob.status).where(NovelPlanJob.id == job_id)
            )
        ).scalar_one_or_none()


async def _finalize(
    job_id: uuid.UUID,
    *,
    worker: str,
    status: str,
    result_payload: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> bool:
    """写终态（CAS：running + 本 worker + 租约未过期）。

    返回 False 表示这次写回被拒 —— 调用方必须走晚到结果路径，不能直接丢弃。
    """
    now = _now()
    values: dict = {
        "status": status,
        "progress": 100,
        "completed_at": now,
        "finished_at": now,
        "heartbeat_at": now,
        "lease_expires_at": None,
        "error_code": error_code,
        "error_message": (error_message or None) and error_message[:2000],
    }
    if result_payload is not None:
        values["result_json"] = json.dumps(result_payload, ensure_ascii=False)
    if prompt_tokens is not None:
        values["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        values["completion_tokens"] = completion_tokens

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.id == job_id,
                NovelPlanJob.status == st.STATUS_RUNNING,
                NovelPlanJob.worker_id == worker,
                NovelPlanJob.lease_expires_at.is_not(None),
                NovelPlanJob.lease_expires_at > now,
            )
            .values(**values)
        )
        await db.commit()
        return result.rowcount == 1


async def store_late_result(
    job_id: uuid.UUID,
    *,
    result_payload: dict,
    reason: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> bool:
    """保存「晚到的成功结果」，**不改写 status**。

    2026-11-12 语义修正：原实现把 interrupted/cancelled 改成 result_pending_review，
    等于抹掉「任务为何结束」。现在：
        status               保持 interrupted / cancelled / cancel_requested
        result_review_status = pending_review
        late_result_json     保存结果（不写入 result_json，避免被当成正常成功结果消费）
        late_result_reason   晚到原因（lease_lost / cancelled / service_shutdown ...）

    worker_id 保留原执行者，便于追责与复盘。条件带 result_review_status 判定，
    重复保存幂等（第二次 rowcount=0）。
    """
    now = _now()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.id == job_id,
                NovelPlanJob.status.in_(list(st.LATE_RESULT_ELIGIBLE_STATUSES)),
                NovelPlanJob.result_review_status.is_(None),
            )
            .values(
                result_review_status=st.RESULT_REVIEW_PENDING,
                late_result_json=json.dumps(result_payload, ensure_ascii=False),
                late_result_reason=reason[:64],
                late_result_at=now,
                # 兼容字段：老前端/老报表按此字段判断冲突原因
                result_conflict_reason=reason[:64],
                lease_expires_at=None,
            )
        )
        await db.commit()
        return result.rowcount == 1


def _parse_ai_error(exc: BaseException) -> tuple[str, str]:
    """把 `RuntimeError("ai_xxx: 人话")` 拆成 (code, message)；其余异常按类名归类。"""
    raw = str(exc) or exc.__class__.__name__
    code, sep, message = raw.partition(": ")
    if sep and code.startswith("ai_"):
        return code[:64], (message or raw)
    return exc.__class__.__name__[:64], raw


async def _record_generation(
    *,
    job_id: uuid.UUID,
    request_payload: dict,
    mode: str,
    response_content: str | None,
    error_message: str | None,
    finish_reason: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    status: str = "succeeded",
) -> None:
    """写 AI 审计流水（generation_records）。失败不影响任务终态。"""
    if mode == "real":
        provider, model_name = gs.resolve_ai_provider()
    else:
        provider, model_name = "mock", "mock-model"
    override_model = request_payload.get("model_name")
    if override_model:
        model_name = override_model
    context = {
        "source": "novel_plan_job",
        "job_id": str(job_id),
        "request": request_payload,
    }
    try:
        async with AsyncSessionLocal() as db:
            await gs.record_generation(
                db,
                attribution_status="pre_project",
                project_id=None,
                action_type="outline",
                model_provider=provider,
                model_name=model_name,
                temperature=request_payload.get("temperature"),
                max_tokens=request_payload.get("max_tokens"),
                context_snapshot=gs.serialize_context(context),
                response_content=response_content or "",
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status=status,
                error_message=error_message,
            )
    except Exception as exc:  # noqa: BLE001 审计失败不影响任务结果
        logger.warning("novel_plan_jobs 写 generation_records 失败 job=%s：%r", job_id, exc)


def _override_of(request_payload: dict) -> dict:
    """还原请求级模型覆盖参数（与同步端点 _build_ai_override 等价）。"""
    override: dict = {}
    if request_payload.get("model_name"):
        override["model_name"] = request_payload["model_name"]
    if request_payload.get("temperature") is not None:
        override["temperature"] = request_payload["temperature"]
    if request_payload.get("max_tokens"):
        override["max_tokens"] = request_payload["max_tokens"]
    return override


def _plan_request(payload: dict, *, real: bool):
    return dict(
        idea=payload.get("idea", ""),
        genre=payload.get("genre") or "",
        writing_style=payload.get("writing_style") or "",
        perspective=payload.get("perspective") or "",
        chapter_count=max(1, int(payload.get("chapter_count") or 10)),
        words_per_chapter=max(500, int(payload.get("words_per_chapter") or 1000)),
        ending_type=payload.get("ending_type") or "",
        has_romance=bool(payload.get("has_romance")),
        content_restrictions=payload.get("content_restrictions") or "",
        must_keep_settings=payload.get("must_keep_settings") or "",
        existing_settings=payload.get("existing_settings"),
        real=real,
        override=_override_of(payload),
    )


def _looks_empty(plan) -> bool:
    """空结果判定：模型返回了对象但没有标题候选，等于没东西可给用户。"""
    titles = getattr(plan, "title_candidates", None)
    return not titles


async def run_job(job_id: uuid.UUID) -> None:
    """执行一个已入库的规划作业（先 CAS 领取，未领到直接退出）。"""
    worker = worker_id()
    async with AsyncSessionLocal() as db:
        claimed = await claim_job(db, job_id, worker)
    if not claimed:
        logger.info("novel_plan_jobs 未领到作业（已领走/已终态/达上限）job=%s", job_id)
        return

    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_loop(job_id, worker, lost))
    ai_task: asyncio.Task | None = None
    lost_waiter: asyncio.Task | None = None
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(select(NovelPlanJob).where(NovelPlanJob.id == job_id))
            ).scalar_one_or_none()
        if row is None:
            logger.warning("novel_plan_jobs 作业消失 job=%s", job_id)
            return

        payload = json.loads(row.request_json or "{}")
        mode = row.mode or "auto"
        real = mode == "real"

        ai_task = asyncio.create_task(ai_plan_novel(**_plan_request(payload, real=real)))
        lost_waiter = asyncio.create_task(lost.wait())

        try:
            done, _ = await asyncio.wait(
                {ai_task, lost_waiter},
                timeout=MAX_RUNTIME_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            raise

        if not done:
            # 超时：先尝试正常写 failed；若已被取消/中断，则按当前状态收尾
            ai_task.cancel()
            status = await _current_status(job_id)
            if status == st.STATUS_CANCEL_REQUESTED:
                await _mark_cancelled(job_id, worker)
            else:
                await _finalize(
                    job_id,
                    worker=worker,
                    status=st.STATUS_FAILED,
                    error_code=st.ERR_TIMEOUT,
                    error_message=f"规划生成超过 {MAX_RUNTIME_SECONDS // 60} 分钟仍未完成，已停止等待，请重试",
                )
            await _record_generation(
                job_id=job_id,
                request_payload=payload,
                mode=mode,
                response_content=None,
                error_message=st.ERR_TIMEOUT,
                finish_reason=None,
                prompt_tokens=None,
                completion_tokens=None,
                status="failed",
            )
            return

        if ai_task not in done:
            # 先失租：结果不再写入，等模型自然结束也无意义
            ai_task.cancel()
            logger.warning("novel_plan_jobs 失租中止 job=%s worker=%s", job_id, worker)
            return

        try:
            plan = ai_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 业务失败要落库给人看
            code, message = _parse_ai_error(exc)
            logger.warning("novel_plan_jobs 生成失败 job=%s code=%s", job_id, code)
            if await _current_status(job_id) == st.STATUS_CANCEL_REQUESTED:
                await _mark_cancelled(job_id, worker)
                await _record_generation(
                    job_id=job_id,
                    request_payload=payload,
                    mode=mode,
                    response_content=None,
                    error_message=st.ERR_CANCELLED_BY_USER,
                    finish_reason=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    status="failed",
                )
                return
            await _finalize(
                job_id,
                worker=worker,
                status=st.STATUS_FAILED,
                error_code=code,
                error_message=message,
            )
            await _record_generation(
                job_id=job_id,
                request_payload=payload,
                mode=mode,
                response_content=None,
                error_message=f"{code}: {message}",
                finish_reason=None,
                prompt_tokens=None,
                completion_tokens=None,
                status="failed",
            )
            return

        finish_reason, prompt_tokens, completion_tokens = take_last_ai_meta()
        if _looks_empty(plan):
            await _finalize(
                job_id,
                worker=worker,
                status=st.STATUS_FAILED,
                error_code=st.ERR_EMPTY_RESULT,
                error_message="模型没有返回可用的规划内容，请重试或调整创意描述",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            await _record_generation(
                job_id=job_id,
                request_payload=payload,
                mode=mode,
                response_content="",
                error_message=st.ERR_EMPTY_RESULT,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status="failed",
            )
            return

        result_payload = {
            "mode": "real" if real else "mock",
            "plan": plan.model_dump(mode="json"),
            "message": (
                "已由真实模型生成完整创作规划"
                if real
                else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
            ),
        }
        if await _current_status(job_id) == st.STATUS_CANCEL_REQUESTED:
            # 用户在执行中点了取消：先落终态 cancelled，再把已生成的结果作为
            # 晚到结果保留（result_review_status=pending_review），
            # 既不违背取消意图，也不白烧一次模型调用。
            await _mark_cancelled(job_id, worker)
            saved = await store_late_result(
                job_id,
                result_payload=result_payload,
                reason=st.ERR_CANCELLED_BY_USER,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            logger.info("novel_plan_jobs 取消后保留晚到结果 job=%s saved=%s", job_id, saved)
            await _record_generation(
                job_id=job_id,
                request_payload=payload,
                mode=mode,
                response_content=json.dumps(result_payload["plan"], ensure_ascii=False),
                error_message=None,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status="succeeded",
            )
            return

        ok = await _finalize(
            job_id,
            worker=worker,
            status=st.STATUS_SUCCEEDED,
            result_payload=result_payload,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        if not ok:
            # 关键：不丢弃结果，转为待人工复核
            status = await _current_status(job_id)
            reason = (await _current_error_code(job_id)) or status or st.ERR_LEASE_LOST
            saved = await store_late_result(
                job_id,
                result_payload=result_payload,
                reason=reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            logger.warning(
                "novel_plan_jobs 结果晚到 job=%s 原状态=%s 已保存=%s", job_id, status, saved
            )

        await _record_generation(
            job_id=job_id,
            request_payload=payload,
            mode=mode,
            response_content=json.dumps(result_payload["plan"], ensure_ascii=False),
            error_message=None,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status="succeeded",
        )
    finally:
        for task in (ai_task, lost_waiter):
            if task is not None and not task.done():
                task.cancel()
        heartbeat.cancel()
        for task in (ai_task, lost_waiter, heartbeat):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _mark_cancelled(job_id: uuid.UUID, worker: str) -> bool:
    """cancel_requested → cancelled（worker 在安全点停下的落点）。"""
    now = _now()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.id == job_id,
                NovelPlanJob.status == st.STATUS_CANCEL_REQUESTED,
                NovelPlanJob.worker_id == worker,
            )
            .values(
                status=st.STATUS_CANCELLED,
                error_code=st.ERR_CANCELLED_BY_USER,
                error_message="任务已按请求取消，未写入任何结果",
                progress=0,
                completed_at=now,
                finished_at=now,
                lease_expires_at=None,
            )
        )
        await db.commit()
        return result.rowcount == 1


def dispatch(job_id: uuid.UUID) -> None:
    """快速路径：提交后立刻在本进程抢一次，不等扫描周期。"""
    _spawn(run_job(job_id))


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def worker_loop(
    stop: asyncio.Event, *, interval: float = WORKER_POLL_SECONDS
) -> None:
    """进程内 worker 循环：扫 queued → CAS 领取 → 执行。

    单机 in-process；多进程同时跑也安全（领取是数据库 CAS），但每个进程各自扫描，
    没有跨机协调。停止信号由 main 的 shutdown 事件驱动。
    """
    worker = worker_id()
    logger.info("novel_plan_jobs worker 启动 worker=%s", worker)
    try:
        while not stop.is_set():
            claimed_any = False
            try:
                while len(_RUNNING) < MAX_CONCURRENT_JOBS:
                    async with AsyncSessionLocal() as db:
                        job_id = await claim_next_job(db, worker)
                    if job_id is None:
                        break
                    claimed_any = True
                    _spawn(run_job(job_id))
            except Exception:  # noqa: BLE001 worker 不因单次异常退出
                logger.exception("novel_plan_jobs worker 扫描异常")
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2 if claimed_any else interval)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("novel_plan_jobs worker 退出 worker=%s", worker)


async def recover_stale_jobs(*, grace_seconds: int = 0) -> dict:
    """启动恢复：只把「租约已过期」的 running 转 interrupted。

    第二批的关键修正：
    - `queued` **不再**被中断 —— 它还没被任何人领取，重启后应当继续被领取；
    - `running` 只有租约过期（或从未写过租约）才判定执行者消失；
    - 不做自动重试（max_attempts 默认 1），重跑由人决定。
    """
    now = _now()
    cutoff = now - timedelta(seconds=grace_seconds)
    expired_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(NovelPlanJob.id).where(
                    NovelPlanJob.status == st.STATUS_RUNNING,
                    or_(
                        NovelPlanJob.lease_expires_at.is_(None),
                        NovelPlanJob.lease_expires_at < cutoff,
                    ),
                )
            )
        ).scalars().all()
        for job_id in rows:
            result = await db.execute(
                update(NovelPlanJob)
                .where(
                    NovelPlanJob.id == job_id,
                    NovelPlanJob.status == st.STATUS_RUNNING,
                    or_(
                        NovelPlanJob.lease_expires_at.is_(None),
                        NovelPlanJob.lease_expires_at < cutoff,
                    ),
                )
                .values(
                    status=st.STATUS_INTERRUPTED,
                    error_code=st.ERR_LEASE_EXPIRED,
                    error_message="执行者失联（租约已过期），任务已安全中断；结果如晚到会进入待复核",
                    progress=0,
                    completed_at=now,
                    finished_at=now,
                    lease_expires_at=None,
                )
            )
            if result.rowcount == 1:
                expired_ids.append(job_id)
        await db.commit()
        queued = (
            await db.execute(
                select(func.count())
                .select_from(NovelPlanJob)
                .where(NovelPlanJob.status == st.STATUS_QUEUED)
            )
        ).scalar_one()

    if expired_ids:
        logger.warning(
            "novel_plan_jobs 启动恢复：%d 个租约过期的 running 已转 interrupted",
            len(expired_ids),
        )
    return {"expired": len(expired_ids), "queued": int(queued or 0)}


async def shutdown_mark_own_jobs(worker: str | None = None) -> int:
    """关闭时把本 worker 持有的 running 标 interrupted（不伪造 succeeded/failed）。"""
    worker = worker or worker_id()
    now = _now()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.status == st.STATUS_RUNNING,
                NovelPlanJob.worker_id == worker,
            )
            .values(
                status=st.STATUS_INTERRUPTED,
                error_code=st.ERR_SERVICE_SHUTDOWN,
                error_message="服务正在关闭，任务已中断；结果如晚到会进入待复核",
                completed_at=now,
                finished_at=now,
                lease_expires_at=None,
            )
        )
        await db.commit()
        count = result.rowcount or 0
    if count:
        logger.warning("novel_plan_jobs 关闭：%d 个本机运行中作业已标 interrupted", count)
    return count


async def request_cancel(
    db: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[str | None, bool]:
    """用户取消。返回 (当前状态, 是否发生了状态变化)。

    - queued → cancelled（还没开始，直接把结果定为不会产生）；
    - running → cancel_requested（worker 在安全点停；已产生的模型输出会落待复核）；
    - 终态 → 原样返回，不报错（幂等）。
    """
    row = await get_job(db, job_id=job_id, user_id=user_id)
    if row is None:
        return None, False

    now = _now()
    if row.status == st.STATUS_QUEUED:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.id == job_id,
                NovelPlanJob.status == st.STATUS_QUEUED,
            )
            .values(
                status=st.STATUS_CANCELLED,
                error_code=st.ERR_CANCELLED_BY_USER,
                error_message="任务在开始前被取消",
                completed_at=now,
                finished_at=now,
            )
        )
        await db.commit()
        if result.rowcount == 1:
            return st.STATUS_CANCELLED, True
    elif row.status == st.STATUS_RUNNING:
        result = await db.execute(
            update(NovelPlanJob)
            .where(
                NovelPlanJob.id == job_id,
                NovelPlanJob.status == st.STATUS_RUNNING,
            )
            .values(
                status=st.STATUS_CANCEL_REQUESTED,
                cancel_requested_at=now,
            )
        )
        await db.commit()
        if result.rowcount == 1:
            return st.STATUS_CANCEL_REQUESTED, True
    elif row.status == st.STATUS_CANCEL_REQUESTED:
        return st.STATUS_CANCEL_REQUESTED, False

    await db.refresh(row)
    return row.status, False


async def decide_late_result(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    decision: str,
    note: str | None = None,
) -> bool:
    """把 pending_review 的晚到结果判为 accepted / discarded。

    第三批 g：原实现只定义了 accepted/discarded 两个值，没有任何写入路径，
    等于公开 API 暴露了一个永远不会发生的状态。这里补上人工处置入口：

    - 只允许任务所属用户处置（`user_id` 参与 WHERE，越权返回 False）；
    - 只允许 `pending_review → accepted|discarded` 单向迁移（CAS）；
    - 不改 `status` / `error_code`：任务终态与原因是既有事实，处置只描述「结果怎么处理了」；
    - 留痕：result_reviewed_at / result_reviewed_by / result_review_note。
    """
    if decision not in (st.RESULT_REVIEW_ACCEPTED, st.RESULT_REVIEW_DISCARDED):
        raise ValueError(f"不支持的结果处置：{decision}")
    values = {
        "result_review_status": decision,
        "result_reviewed_at": _now(),
        "result_reviewed_by": user_id,
        "result_review_note": (note or "")[:500] or None,
    }
    stmt = (
        update(NovelPlanJob)
        .where(
            NovelPlanJob.id == job_id,
            NovelPlanJob.user_id == user_id,
            NovelPlanJob.result_review_status == st.RESULT_REVIEW_PENDING,
        )
        .values(**values)
    )
    result = await db.execute(stmt)
    return result.rowcount == 1


async def get_job(
    db: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID
) -> NovelPlanJob | None:
    row = (
        await db.execute(select(NovelPlanJob).where(NovelPlanJob.id == job_id))
    ).scalar_one_or_none()
    if row is None or row.user_id != user_id:
        return None
    return row


async def list_jobs(
    db: AsyncSession, *, user_id: uuid.UUID, limit: int = DEFAULT_LIST_LIMIT
) -> list[NovelPlanJob]:
    rows = (
        await db.execute(
            select(NovelPlanJob)
            .where(NovelPlanJob.user_id == user_id)
            .order_by(NovelPlanJob.created_at.desc())
            .limit(max(1, min(50, limit)))
        )
    ).scalars().all()
    return list(rows)


def serialize_job(job: NovelPlanJob, *, include_plan: bool = True) -> dict:
    """前端可直接消费的结构。

    兼容：老字段（progress / error_code / completed_at）保持不变；
    新增 heartbeat_at / lease_expires_at / finished_at / attempt_count / can_retry /
    result_review_status / late_result_reason，老前端忽略即可。

    晚到结果语义（2026-11-12）：
        status 仍是任务真实终态（interrupted / cancelled ...），
        result_review_status == "pending_review" 表示「有一份成功结果待人工处置」。
        `result_pending_review` 仅为老前端保留的布尔镜像，不参与判断。
    """
    elapsed: int | None = None
    if job.started_at:
        end = job.finished_at or job.completed_at or _now()
        elapsed = max(0, int((end - job.started_at).total_seconds()))

    review_status = job.result_review_status or st.RESULT_REVIEW_NONE
    payload: dict = {
        "job_id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "progress": job.progress,
        "elapsed_seconds": elapsed,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": job.completion_tokens,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "finished_at": (
            job.finished_at.isoformat()
            if job.finished_at
            else (job.completed_at.isoformat() if job.completed_at else None)
        ),
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "lease_expires_at": (
            job.lease_expires_at.isoformat() if job.lease_expires_at else None
        ),
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "can_retry": st.can_retry(job.status),
        "can_cancel": job.status in (st.STATUS_QUEUED, st.STATUS_RUNNING),
        "result_review_status": review_status,
        "result_reviewed_at": (
            job.result_reviewed_at.isoformat() if job.result_reviewed_at else None
        ),
        "result_review_note": job.result_review_note,
        "late_result_reason": job.late_result_reason,
        "late_result_at": (
            job.late_result_at.isoformat() if job.late_result_at else None
        ),
        "has_late_result": review_status == st.RESULT_REVIEW_PENDING,
        # 兼容镜像（老前端只认这个布尔值）
        "result_pending_review": review_status == st.RESULT_REVIEW_PENDING,
        "result_conflict_reason": job.result_conflict_reason,
        "idempotency_key": job.idempotency_key,
    }

    if not include_plan:
        return payload

    if job.status == st.STATUS_SUCCEEDED and job.result_json:
        _merge_result(payload, job.result_json, job_id=job.id)
    elif review_status == st.RESULT_REVIEW_PENDING and job.late_result_json:
        # 晚到结果放在 plan 里给前端预填草稿，但标记来源是 late_result
        _merge_result(payload, job.late_result_json, job_id=job.id)
        payload["result_source"] = "late_result"
    return payload


def _merge_result(payload: dict, raw: str, *, job_id: uuid.UUID) -> None:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("novel_plan_jobs 结果 JSON 损坏 job=%s", job_id)
        return
    if isinstance(result, dict):
        payload["mode"] = result.get("mode", payload["mode"])
        payload["message"] = result.get("message", "")
        payload["plan"] = result.get("plan")


def is_terminal(job: NovelPlanJob) -> bool:
    return st.is_terminal(job.status)


def stale_cutoff(minutes: int = 60) -> datetime:
    """仅供诊断：超过该时间仍在 running 通常意味着执行者异常退出。"""
    return _now() - timedelta(minutes=minutes)
