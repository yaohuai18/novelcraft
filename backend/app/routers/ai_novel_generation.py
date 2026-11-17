# AI 小说生成路由（prefix=/api/ai）
# 模式：已配置真实密钥（DEEPSEEK_API_KEY > AI_API_KEY > OPENAI_API_KEY）→ mode=real 真实调用；
#       未配置/占位密钥 → mode=mock 动态模拟（正文标注「演示生成」）。
# 能力：生成「全书大纲+第一章」/ 生成下一章 / 重新生成 / 扩写 / 润色 /
#       整本生成（非流式 + SSE 流式）/ AI 状态查询；全部接口带每用户限流。
import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.services import collaboration_service, generation_service as gs
from app.services.outline_plan_version_service import (
    ChapterNotBoundError,
    NoConfirmedVersionError,
)
from app.schemas.ai_novel import (
    AiConfigResponse,
    AiStatusResponse,
    ChapterEditRequest,
    ChapterEditResponse,
    NextChapterRequest,
    NovelChapter,
    NovelGenerateRequest,
    NovelGenerateResponse,
    NovelPlan,
    OutlineFirstResponse,
    PlanNovelRequest,
    PlanNovelResponse,
    PlanUpdateRequest,
    PlanUpdateResponse,
)
from app.services.ai_service import (
    ai_expand_chapter,
    build_ai_config_payload,
    ai_generate_next_chapter,
    ai_generate_outline_first,
    ai_plan_novel,
    ai_plan_update,
    ai_polish_chapter,
    ai_regenerate_chapter,
    assemble_stream_chunks,
    chat_with_ai_novel,
    check_ai_rate_limit,
    count_words,
    evaluate_chapter_result,
    generate_mock_novel,
    get_effective_ai_config,
    is_ai_configured,
    normalize_chapter_title,
    take_last_ai_meta,
    take_last_ai_prompt,
    _smart_truncate,
)
from app.services.context_builder import (
    build_confirmed_continuation_context,
    build_continuation_context,
)

router = APIRouter(
    prefix="/api/ai",
    tags=["AI 小说生成"],
)


def _ai_error_response(code: str, message: str, status: int = 502):
    """结构化 AI 错误：{ detail: { code, message } }。"""
    return JSONResponse(
        status_code=status,
        content={
            "detail": {
                "code": code,
                "message": message,
            }
        },
    )



def _build_ai_override(data) -> dict:
    """从请求体提取请求级模型覆盖参数（model_name/temperature/max_tokens）。"""
    ov = {}
    if getattr(data, "model_name", None):
        ov["model_name"] = data.model_name
    if getattr(data, "temperature", None) is not None:
        ov["temperature"] = data.temperature
    if getattr(data, "max_tokens", None):
        ov["max_tokens"] = data.max_tokens
    return ov


def _resolve_chapter_index(data) -> int:
    """解析目标章节序号：优先 chapter_index 字段，兜底从标题「第X章」提取。"""
    idx = getattr(data, "chapter_index", None)
    if idx and idx > 0:
        return idx
    title = getattr(data, "chapter_title", "") or ""
    m = re.search(r"第\s*(\d+)\s*章", title)
    if m:
        return int(m.group(1))
    return 1


async def _record_ai_generation(
    db: AsyncSession,
    *,
    real: bool,
    action_type: str,
    project_id: uuid.UUID | None = None,
    chapter_id: uuid.UUID | None = None,
    outline_plan_version_id: uuid.UUID | None = None,
    chapter_plan_id: str | None = None,
    chapter_plan_snapshot: dict | None = None,
    title_snapshot: str | None = None,
    context_snapshot: dict | None = None,
    response_content: str | None = None,
    error_message: str | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    override: dict | None = None,
    raw_content: str | None = None,
    processed_content: str | None = None,
    final_content: str | None = None,
    postprocess_actions: str | None = None,
    finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    raw_content_length: int | None = None,
    final_content_length: int | None = None,
    was_truncated: int | None = None,
    truncation_reason: str | None = None,
    review_flags: str | None = None,
    status: str | None = None,
):
    """第一期「生成不丢失」：每个 AI 生成入口执行后写 generation_record。

    real=True 时记录生效的 provider/model（resolve_ai_provider）；
    演示（mock）模式 provider 标记为 mock，保证演示模式同样可追溯。
    第二期「章节能被记住」：携带 Context Builder 组装后的完整 system_prompt /
    user_prompt 落库，供后续审计与一致性检查使用。
    「已确认大纲版本化」：携带 project_id / chapter_id / outline_plan_version_id /
    chapter_plan_id / chapter_plan_snapshot / title_snapshot，建立正文与已确认版本
    的稳定追溯关系。
    第三批「生成链路数据一致性/可追溯性」：携带 raw_content（模型原始响应，只追加
    不改写）、processed_content（清洗后结果）、final_content（最终正文）及
    finish_reason / prompt_tokens / completion_tokens / postprocess_actions /
    review_flags / was_truncated / truncation_reason / 长度统计等元数据。
    finish_reason / prompt_tokens / completion_tokens 未显式传入时，
    record_generation 内部会从最近一次 AI 调用 contextvar 取真实值。
    """
    _ov = override or {}
    if real:
        provider, model_name = gs.resolve_ai_provider()
    else:
        provider, model_name = "mock", "mock-model"
    if _ov.get("model_name"):
        model_name = _ov["model_name"]
    if status is None:
        status = "succeeded" if error_message is None else "failed"
    await gs.record_generation(
        db,
        attribution_status=("project_bound" if project_id is not None else "pre_project"),
        project_id=project_id,
        chapter_id=chapter_id,
        outline_plan_version_id=outline_plan_version_id,
        chapter_plan_id=chapter_plan_id,
        chapter_plan_snapshot=(
            gs.serialize_context(chapter_plan_snapshot)
            if chapter_plan_snapshot
            else None
        ),
        title_snapshot=title_snapshot,
        action_type=action_type,
        model_provider=provider,
        model_name=model_name,
        temperature=_ov.get("temperature"),
        max_tokens=_ov.get("max_tokens"),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context_snapshot=gs.serialize_context(context_snapshot or {}),
        response_content=response_content,
        raw_content=raw_content,
        processed_content=processed_content,
        final_content=final_content,
        postprocess_actions=postprocess_actions,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw_content_length=raw_content_length,
        final_content_length=final_content_length,
        was_truncated=was_truncated,
        truncation_reason=truncation_reason,
        review_flags=review_flags,
        status=status,
        error_message=error_message,
    )

    # 2026-11-17 批次 0：生成失败自动沉淀为失败样本（source='auto'），
    # 后续生成时 _inject_failure_samples 会自动把教训注入提示词，防止同类错误反复犯。
    # 只沉淀「对模型有意义的教训」：截断/输出格式类；超时、网络等基础设施错误
    # 对小说模型无意义，不落库（避免污染提示词）。同类别+同项目只留一条，防样本库膨胀。
    if real and error_message:
        await _auto_sink_failure_sample(
            db,
            project_id=project_id,
            action_type=action_type,
            error_message=error_message,
        )


async def _auto_sink_failure_sample(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    action_type: str,
    error_message: str,
) -> None:
    """把生成失败沉淀为 failure_samples 自动样本（source='auto'）。"""
    from sqlalchemy import select

    from app.models.failure_sample import FailureSample

    err = error_message or ""
    if "ai_output_truncated" in err or "max_tokens" in err:
        category = "style"
        bad_example = (
            f"（{action_type}）单次生成因触及 max_tokens 上限被截断，正文不完整，"
            f"被判定失败：{err[:200]}"
        )
        correct_way = (
            "正文必须一次写完整章：若接近单次输出上限，须在自然段落处收尾并保持叙事完整，"
            "严禁输出半截句子；后端检测到截断会判失败并要求重来。"
        )
    elif "bad_response" in err or "格式异常" in err or "JSON" in err:
        category = "style"
        bad_example = (
            f"（{action_type}）模型输出不是合法 JSON/不符合指定格式，被判定失败：{err[:200]}"
        )
        correct_way = (
            "输出必须严格遵循指定的格式（首行标题、正文分段），不得输出解释、元信息"
            "或多余内容；格式错误会被后端拒绝。"
        )
    else:
        return  # 基础设施类错误（超时/网络/网关）对模型无意义，不沉淀

    existing = (
        await db.execute(
            select(FailureSample.id).where(
                FailureSample.category == category,
                FailureSample.source == "auto",
                FailureSample.project_id.is_(None) if project_id is None
                else FailureSample.project_id == project_id,
            )
        )
    ).first()
    if existing is not None:
        return  # 同类教训已存在，不重复

    db.add(
        FailureSample(
            project_id=project_id,
            category=category,
            bad_example=bad_example,
            correct_way=correct_way,
            source="auto",
            enabled=1,
        )
    )


def _build_generation_eval(
    content: str,
    title: str,
    body: str,
    target_words: int | None,
    finish_reason: str | None = None,
    stream_completed: bool | None = None,
) -> dict:
    """正文生成完成后计算第三批落库字段（状态机 + 完成性校验 + 追溯元数据）。

    - raw_content 恒为模型原始完整响应（只追加不改写）；
    - processed_content / final_content 均为后处理后的最终正文（当前链路不做截断，
      清洗仅提取标题/规范化，因此二者一致；后续若引入正文清洗可拆开）；
    - finish_reason / prompt_tokens / completion_tokens 未显式传入时取最近一次
      AI 调用 contextvar 的真实值（chat_with_ai_novel 已写入）；
    - status 由 evaluate_chapter_result 按完成性校验规则判定
      （completed / over_target / incomplete / postprocess_failed / failed）。
    """
    meta_finish, meta_prompt, meta_completion = take_last_ai_meta()
    if finish_reason is None:
        finish_reason = meta_finish
    if meta_prompt is None and meta_completion is None:
        prompt_tokens = completion_tokens = None
    else:
        prompt_tokens, completion_tokens = meta_prompt, meta_completion
    eval_dict = evaluate_chapter_result(
        raw_content=content,
        title=title,
        body=body,
        target_words=target_words,
        finish_reason=finish_reason,
        stream_completed=stream_completed,
    )
    return {
        "raw_content": content,
        "processed_content": body,
        "final_content": body,
        "postprocess_actions": json.dumps(
            eval_dict["postprocess_actions"], ensure_ascii=False
        ),
        "finish_reason": finish_reason,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "raw_content_length": eval_dict["raw_content_length"],
        "final_content_length": eval_dict["final_content_length"],
        "was_truncated": 1 if eval_dict["was_truncated"] else 0,
        "truncation_reason": eval_dict["truncation_reason"],
        "review_flags": json.dumps(
            eval_dict["review_flags"], ensure_ascii=False
        ),
        "status": eval_dict["status"],
    }


async def _build_ctx_or_none(
    db: AsyncSession,
    project_id: uuid.UUID | None,
    chapter_id: uuid.UUID | None,
    max_length: int,
    instruction: str = "",
) -> dict | None:
    """第二期「章节能被记住」：优先用 Context Builder 组装续写上下文。

    仅当请求携带 project_id + chapter_id 时启用；组装失败（如章节不存在）回退
    旧字段拼接逻辑，不阻塞生成主流程。
    """
    if not project_id or not chapter_id:
        return None
    try:
        return await build_continuation_context(
            db,
            project_id,
            chapter_id,
            max_length=max_length,
            instruction=instruction,
        )
    except Exception:  # noqa: BLE001 - Context Builder 失败回退旧逻辑
        return None


async def _ensure_project_editor(
    db: AsyncSession,
    project_id: uuid.UUID | None,
    current_user: User,
) -> JSONResponse | None:
    """正文生成写操作校验：项目归属（作者或 owner/editor），失败返回 403/404。

    与大纲版本化路由一致的权限模型：项目不存在 → 404；非成员且非作者 → 403。
    未携带 project_id（旧自由创作路径）不校验。
    """
    if project_id is None:
        return None
    project = await db.get(Project, project_id)
    if project is None:
        return _ai_error_response(
            "project_not_found",
            "作品不存在或无权访问",
            status=404,
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role not in ("owner", "editor"):
            return _ai_error_response(
                "forbidden",
                "仅项目作者或编辑可生成正文",
                status=403,
            )
    return None


async def _build_confirmed_ctx(
    db: AsyncSession,
    project_id: uuid.UUID | None,
    chapter_id: uuid.UUID | None,
    max_length: int,
    instruction: str = "",
) -> tuple[dict | None, JSONResponse | None]:
    """「已确认大纲版本化」：生成接口的唯一事实来源组装。

    携带 project_id + chapter_id 时，正文生成必须只读取该项目 active confirmed
    OutlinePlanVersion 下与该 chapterId 稳定绑定的 ChapterPlan：
      - 成功 → (ctx, None)，ctx 附带 outline_plan_version_id / chapter_plan_id /
        chapter_plan_snapshot / title_snapshot 追溯字段；
      - 无 confirmed 版本或当前章节未绑定已确认计划 → (None, 409 NO_CONFIRMED_PLAN)，
        拒绝生成，不做任何降级；
      - 其余异常 → (None, 502 CONFIRMED_CTX_FAILED)。
    不携带 project_id/chapter_id 的旧调用（自由创作路径）由调用方另行兼容。
    """
    if not project_id or not chapter_id:
        return None, None
    try:
        ctx = await build_confirmed_continuation_context(
            db,
            project_id,
            chapter_id,
            max_length=max_length,
            instruction=instruction,
        )
        # 第三档升级（2026-11-16）：注入失败样本——本项目过去犯过的错成为每次生成的固定约束
        ctx["system_prompt"] = await _inject_failure_samples(
            db, project_id, ctx.get("system_prompt") or ""
        )
        return ctx, None
    except ValueError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        if code in ("no_confirmed_plan", "chapter_not_bound"):
            return None, _ai_error_response(
                code,
                (
                    "当前项目尚未确认大纲版本，或本章节未绑定已确认章节计划。"
                    "请先在目录页确认大纲后再生成正文；如需无大纲自由创作，请显式选择该模式。"
                ),
                status=409,
            )
        return None, _ai_error_response(
            "confirmed_ctx_failed",
            f"读取已确认大纲版本失败：{message or raw}",
            status=502,
        )
    except NoConfirmedVersionError as error:
        return None, _ai_error_response(
            "no_confirmed_plan",
            f"{error}",
            status=409,
        )
    except ChapterNotBoundError as error:
        return None, _ai_error_response(
            "chapter_not_bound",
            f"{error}",
            status=409,
        )
    except Exception:  # noqa: BLE001 - confirmed 组装失败禁止降级
        return None, _ai_error_response(
            "confirmed_ctx_failed",
            "读取已确认大纲版本失败，请稍后重试",
            status=502,
        )


def _trace_fields(
    ctx: dict | None,
    project_id: uuid.UUID | None,
    chapter_id: uuid.UUID | None,
) -> dict:
    """从 confirmed 上下文提取生成记录追溯字段（无 ctx 时全 None）。"""
    return {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "outline_plan_version_id": (
            ctx.get("outline_plan_version_id") if ctx else None
        ),
        "chapter_plan_id": ctx.get("chapter_plan_id") if ctx else None,
        "chapter_plan_snapshot": ctx.get("chapter_plan_snapshot") if ctx else None,
        "title_snapshot": ctx.get("title_snapshot") if ctx else None,
    }


async def _inject_failure_samples(
    db: AsyncSession, project_id: uuid.UUID, system_prompt: str
) -> str:
    """把失败样本追加到 system prompt（本项目 + 全局样本，最多 8 条）。

    第三档升级（2026-11-16）：把「本项目过去犯过的错」变成每章生成的固定约束，
    同类错误不再反复犯（如人物瞳色漂移、伏笔提前揭露）。
    """
    from app.models.failure_sample import FailureSample
    from sqlalchemy import or_, select

    rows = (
        await db.execute(
            select(FailureSample)
            .where(
                or_(
                    FailureSample.project_id == project_id,
                    FailureSample.project_id.is_(None),
                ),
                FailureSample.enabled == 1,
            )
            .order_by(FailureSample.created_at.asc())
            .limit(8)
        )
    ).scalars().all()
    if not rows:
        return system_prompt
    block = "\n\n【本项目过去犯过的错（写正文前必须逐条对照，严禁再犯）】\n"
    for s in rows:
        block += (
            f"- {s.category}：错误示例「{s.bad_example[:120]}」"
            f"→ 正确做法：{s.correct_way[:150]}\n"
        )
    return system_prompt + block


async def _enforce_rate_limit(current_user: User):
    """每用户 AI 调用限流；超出返回 429 可读错误（由路由调用）。"""
    allowed, retry_after = check_ai_rate_limit(str(current_user.id))
    if not allowed:
        return _ai_error_response(
            "rate_limited",
            f"AI 调用过于频繁，请在 {retry_after} 秒后重试",
            status=429,
        )
    return None


def _resolve_real(requested_mode: str) -> bool:
    """解析生成模式：auto 由密钥配置决定；mock/real 显式指定（real 但未配置 → 抛 RuntimeError）。"""
    mode = (requested_mode or "auto").strip().lower()
    configured = is_ai_configured()
    if mode == "real":
        if not configured:
            raise RuntimeError("ai_not_configured: 尚未配置 AI 服务，请在 backend/.env 填写 DEEPSEEK_API_KEY")
        return True
    if mode == "mock":
        return False
    # auto
    return configured


def _plan_snapshot(data, extra: dict | None = None) -> dict:
    """规划/整本生成类入口的 context_snapshot：展开核心上下文字段，而非仅嵌套 request。

    第二期「章节能被记住」：补全 plan/generate/outline 入口快照，
    使 context_snapshot 直接包含 设置/大纲/前情摘要/人物状态/续写起点/章节数 等组装上下文。
    """
    snap: dict = {"request": data.model_dump(mode="json")}
    for field in (
        "setting",
        "outline",
        "previous_summary",
        "character_state",
        "continue_from",
        "chapter_count",
        "words_per_chapter",
        "genre",
        "title",
        "instruction",
    ):
        if hasattr(data, field):
            value = getattr(data, field)
            snap[field] = (
                value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            )
    if extra:
        snap.update(extra)
    return snap


def _extract_title_and_body(content: str, index: int) -> tuple[str, str]:
    """从模型输出提取 (标题, 正文)：第一行为标题并清洗为「第X章：简短标题」，其余为正文。"""
    text = content.strip()
    lines = text.splitlines()
    if not lines:
        return normalize_chapter_title("", index), text
    title = normalize_chapter_title(lines[0].strip(), index)
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else text
    return title, body


async def _finalize_chapter(
    content: str,
    index: int,
    *,
    target_words: int | None = None,
    allow_truncate: bool = False,
    allow_retry: bool = False,
    retry_factory=None,
) -> tuple[str, str, int]:
    """统一后处理：从模型输出提取标题/正文 → 字数统计 → 超长保留完整正文 / 不足重试。

    返回 (title, body, word_count)：
    - target_words 为 None 时只统计字数，不做截断与重试（用于扩写）；
    - 正文超过 target_words*1.2 时【不再静默截断】：默认保留完整正文，
      是否标记 over_target 由调用方 evaluate_chapter_result 判定（数据可追溯）；
    - 仅当显式传入 allow_truncate=True 时仍走 _smart_truncate（兼容旧调用，正文链路不使用）；
    - 正文不足 target_words*0.7 且 allow_retry=True 时调用 retry_factory 重试一次
      （同类重试上限 1 次），重试后仍不足则如实返回当前字数。
    """
    title, body = _extract_title_and_body(content, index)
    if not body:
        body = content
    word_count = count_words(body)
    if target_words is None or target_words <= 0:
        return title, body, word_count

    if word_count > target_words * 1.2 and allow_truncate:
        body = _smart_truncate(body, target_words)
        word_count = count_words(body)
    elif word_count < target_words * 0.7 and allow_retry and retry_factory is not None:
        try:
            retried_content = await retry_factory()
        except RuntimeError:
            # 重试失败不阻断：保留首次生成结果，如实报告字数
            retried_content = content
        retried_title, retried_body = _extract_title_and_body(retried_content, index)
        if not retried_body:
            retried_body = retried_content
        title, body = retried_title, retried_body
        word_count = count_words(body)
    return title, body, word_count


def _finalize_with_confirmed_title(
    content: str,
    title: str,
    body: str,
    word_count: int,
    ctx: dict | None,
    index: int,
) -> tuple[str, str, int]:
    """ctx 路径的正文不含标题行：标题以已确认章节计划为准，并恢复被误剥的正文首行。

    2026-11-16 修复：generate / regenerate / expand / polish 四个端点共用。
    非 ctx 路径（默认提示词带标题行）不受影响，原样返回。
    """
    if not (ctx and ctx.get("title_snapshot")):
        return title, body, word_count
    title = normalize_chapter_title(ctx["title_snapshot"], index)
    text = content.strip()
    lines = text.splitlines()
    if lines and re.search(r"第\s*\d+\s*章", lines[0]):
        body = "\n".join(lines[1:]).strip() or text
    else:
        body = text
    return title, body, count_words(body)


async def _build_chapter(
    setting: NovelGenerateRequest,
    index: int,
    total: int,
    real: bool,
    previous_chapter: str | None = None,
    override: dict | None = None,
) -> NovelChapter:
    """生成第 index 章：real=True 走真实模型，否则走动态 mock。

    生成后统一做字数后处理：超长保留完整正文（不再截断）、不足 70% 重试一次
    （仅真实模式），并在响应中返回去空白字数 word_count。
    """
    if real:
        async def _retry():
            return await chat_with_ai_novel(
                setting,
                index=index,
                total=total,
                previous_chapter=previous_chapter,
                override=override,
            )

        content = await chat_with_ai_novel(
            setting,
            index=index,
            total=total,
            previous_chapter=previous_chapter,
            override=override,
        )
        title, body, word_count = await _finalize_chapter(
            content,
            index,
            target_words=setting.words_per_chapter,
            allow_retry=True,
            retry_factory=_retry,
        )
    else:
        content = generate_mock_novel(
            setting,
            index=index,
            total=total,
            previous_chapter=previous_chapter,
        )
        title, body, word_count = await _finalize_chapter(
            content,
            index,
            target_words=setting.words_per_chapter,
        )

    eval_fields = _build_generation_eval(
        content,
        title,
        body,
        target_words=setting.words_per_chapter,
    )
    return (
        NovelChapter(
            index=index,
            title=title,
            content=body,
            word_count=word_count,
        ),
        eval_fields,
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _generate_all_chapters(
    setting: NovelGenerateRequest,
    real: bool,
    override: dict | None = None,
) -> tuple[list[NovelChapter], list[dict]]:
    chapters: list[NovelChapter] = []
    evals: list[dict] = []
    total = setting.chapter_count
    start_index = 1
    if setting.continue_from:
        # 续写模式：基于已有文本继续，默认补 1 章
        chapters_count = max(1, total) if total > 0 else 1
        start_index = 1
        for i in range(1, chapters_count + 1):
            prev = chapters[-1].content if chapters else setting.continue_from
            chapter, _eval = await _build_chapter(
                setting, i, chapters_count, real, previous_chapter=prev,
                override=override,
            )
            chapters.append(chapter)
            evals.append(_eval)
        return chapters, evals
    for i in range(1, total + 1):
        prev = chapters[-1].content if chapters else None
        chapter, _eval = await _build_chapter(
            setting, i, total, real, previous_chapter=prev, override=override
        )
        chapters.append(chapter)
        evals.append(_eval)
    return chapters, evals


@router.get("/config", response_model=AiConfigResponse)
async def ai_config(
    current_user: User = Depends(get_current_user),
):
    """模型配置接口：返回当前生效 provider/model/temperature/max_tokens 与可选项。

    安全约束：密钥仅存于服务端环境变量（DEEPSEEK_API_KEY/AI_API_KEY/OPENAI_API_KEY），
    本接口与响应模型均不包含、不返回任何 API Key 字段。
    """
    return AiConfigResponse(**build_ai_config_payload())


@router.get("/status", response_model=AiStatusResponse)
async def ai_status(
    current_user: User = Depends(get_current_user),
):
    """AI 服务状态：当前模式、生效的 base_url 与模型（不返回密钥）。"""
    configured = is_ai_configured()
    _, base, model = get_effective_ai_config()
    return AiStatusResponse(
        configured=configured,
        mode="real" if configured else "mock",
        base_url=base or "",
        model=model or "",
    )


@router.post("/generate-outline-first", response_model=OutlineFirstResponse)
async def generate_outline_first(
    data: NovelGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成「全书大纲 + 第一章正文」（不一次生成整本）。"""
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    try:
        outline, first_text = await ai_generate_outline_first(
            data, real, outline=data.outline, override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="outline",
            context_snapshot=_plan_snapshot(data),
            error_message=raw,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    # 大纲提示词由 ai_service 统一写入 contextvar（真实/演示一致），在此显式取用，
    # 避免随后 _build_chapter 生成的正文提示词覆盖或顺序错位。
    outline_system, outline_user = take_last_ai_prompt()
    chapter, chapter_eval = await _build_chapter(
        data, 1, max(1, data.chapter_count), real, override=override
    )
    mode = "real" if real else "mock"
    await _record_ai_generation(
        db,
        real=real,
        action_type="outline",
        context_snapshot=_plan_snapshot(data),
        response_content=outline,
        system_prompt=outline_system,
        user_prompt=outline_user,
    override=override
    )
    await _record_ai_generation(
        db,
        real=real,
        action_type="draft",
        context_snapshot=_plan_snapshot(data, {"index": 1}),
        response_content=chapter.content,
        **chapter_eval,
    override=override
    )
    return OutlineFirstResponse(
        mode=mode,
        outline=outline,
        chapter=chapter,
        message=(
            "已由真实模型生成大纲与第一章" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/generate-next-chapter", response_model=ChapterEditResponse)
async def generate_next_chapter(
    data: NextChapterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成下一章：携带前文摘要/人物状态/章节大纲，基于上下文续写。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)

    # 「已确认大纲版本化」：生成唯一事实来源 = active confirmed OutlinePlanVersion
    # 快照中与该 chapterId 稳定绑定的 ChapterPlan；无 confirmed/未绑定 → 409 拒绝生成。
    # 已移除旧逻辑「前端 data.outline 无条件注入 Prompt 并标最高优先级」——
    # 前端大纲文本不再作为生成依据，章节标题/正文只按已确认版本执行。
    ctx, ctx_error = await _build_confirmed_ctx(
        db,
        data.project_id,
        data.chapter_id,
        max_length=data.setting.words_per_chapter,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "draft"}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "outline": data.outline,
            "previous_summary": data.previous_summary,
            "character_state": data.character_state,
            "chapter_index": data.chapter_index,
            "previous_chapter_tail": (data.previous_chapter or "")[-6000:],
        }
    )
    try:
        content = await ai_generate_next_chapter(
            data.setting,
            real,
            outline=data.outline,
            previous_summary=data.previous_summary,
            character_state=data.character_state,
            chapter_index=data.chapter_index,
            previous_chapter=data.previous_chapter,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
        override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="draft",
            context_snapshot=ctx_snapshot,
            error_message=raw,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            **trace,
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    async def _retry_next():
        return await ai_generate_next_chapter(
            data.setting,
            real,
            outline=data.outline if ctx is None else "",
            previous_summary=data.previous_summary,
            character_state=data.character_state,
            chapter_index=data.chapter_index,
            previous_chapter=data.previous_chapter,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
        override=override
        )

    title, body, word_count = await _finalize_chapter(
        content,
        data.chapter_index,
        target_words=data.setting.words_per_chapter,
        allow_retry=real,
        retry_factory=_retry_next if real else None,
    )
    # 2026-11-16 修复：ctx 路径正文不含标题行，标题以已确认章节计划为准
    title, body, word_count = _finalize_with_confirmed_title(
        content, title, body, word_count, ctx, data.chapter_index
    )
    chapter_eval = _build_generation_eval(
        content,
        title,
        body,
        target_words=data.setting.words_per_chapter,
    )

    mode = "real" if real else "mock"
    await _record_ai_generation(
        db,
        real=real,
        action_type="draft",
        context_snapshot=ctx_snapshot,
        response_content=body,
        system_prompt=ctx["system_prompt"] if ctx else None,
        user_prompt=ctx["user_prompt"] if ctx else None,
        override=override,
        **trace,
        **chapter_eval,
    )
    return ChapterEditResponse(
        mode=mode,
        chapter=NovelChapter(
            index=data.chapter_index,
            title=title,
            content=body,
            word_count=word_count,
        ),
        message=(
            "已由真实模型续写下一章" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/regenerate-chapter", response_model=ChapterEditResponse)
async def regenerate_chapter(
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重新生成本章：基于设定重写本章正文。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)

    # 「已确认大纲版本化」：重写同样以 active confirmed 版本绑定的 ChapterPlan 为唯一事实来源
    ctx, ctx_error = await _build_confirmed_ctx(
        db,
        data.project_id,
        data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.instruction,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "rewrite", "instruction": data.instruction}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "instruction": data.instruction,
        }
    )
    try:
        content = await ai_regenerate_chapter(
            data.setting,
            real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            instruction=data.instruction,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            outline=data.outline,
            previous_chapter_tail=data.previous_chapter_tail,
            next_chapter_head=data.next_chapter_head,
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="draft",
            context_snapshot=ctx_snapshot,
            error_message=raw,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    async def _retry_regenerate():
        return await ai_regenerate_chapter(
            data.setting,
            real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            instruction=data.instruction,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            outline=data.outline,
            previous_chapter_tail=data.previous_chapter_tail,
            next_chapter_head=data.next_chapter_head,
        )

    title, body, word_count = await _finalize_chapter(
        content,
        chapter_index,
        target_words=data.setting.words_per_chapter,
        allow_retry=real,
        retry_factory=_retry_regenerate if real else None,
    )
    title, body, word_count = _finalize_with_confirmed_title(
        content, title, body, word_count, ctx, chapter_index
    )
    chapter_eval = _build_generation_eval(
        content,
        title,
        body,
        target_words=data.setting.words_per_chapter,
    )

    mode = "real" if real else "mock"
    await _record_ai_generation(
        db,
        real=real,
        action_type="draft",
        context_snapshot=ctx_snapshot,
        response_content=body,
        system_prompt=ctx["system_prompt"] if ctx else None,
        user_prompt=ctx["user_prompt"] if ctx else None,
        **trace,
        **chapter_eval,
    override=override
    )
    return ChapterEditResponse(
        mode=mode,
        chapter=NovelChapter(
            index=chapter_index,
            title=title,
            content=body,
            word_count=word_count,
        ),
        message=(
            "已由真实模型重新生成本章" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/expand-chapter", response_model=ChapterEditResponse)
async def expand_chapter(
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """扩写本章：保留情节扩充细节，返回扩写后完整章节。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)

    # 「已确认大纲版本化」：扩写同样以 active confirmed 版本绑定的 ChapterPlan 为唯一事实来源
    ctx, ctx_error = await _build_confirmed_ctx(
        db,
        data.project_id,
        data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.instruction,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "expand", "instruction": data.instruction}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "instruction": data.instruction,
        }
    )
    try:
        content = await ai_expand_chapter(
            data.setting,
            real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            instruction=data.instruction,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            outline=data.outline,
            previous_chapter_tail=data.previous_chapter_tail,
            next_chapter_head=data.next_chapter_head,
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="expand",
            context_snapshot=ctx_snapshot,
            error_message=raw,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    # 扩写本身是加长操作，不参与字数截断/重试，仅如实统计字数并做完成性标记
    title, body, word_count = await _finalize_chapter(
        content,
        chapter_index,
        target_words=None,
        allow_retry=False,
    )
    title, body, word_count = _finalize_with_confirmed_title(
        content, title, body, word_count, ctx, chapter_index
    )
    chapter_eval = _build_generation_eval(
        content,
        title,
        body,
        target_words=None,
    )

    mode = "real" if real else "mock"
    await _record_ai_generation(
        db,
        real=real,
        action_type="expand",
        context_snapshot=ctx_snapshot,
        response_content=body,
        system_prompt=ctx["system_prompt"] if ctx else None,
        user_prompt=ctx["user_prompt"] if ctx else None,
        **trace,
        **chapter_eval,
    override=override
    )
    return ChapterEditResponse(
        mode=mode,
        chapter=NovelChapter(
            index=chapter_index,
            title=title,
            content=body,
            word_count=word_count,
        ),
        message=(
            "已由真实模型扩写本章" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/polish-chapter", response_model=ChapterEditResponse)
async def polish_chapter(
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """润色本章：按目标风格优化语言表达，返回润色后完整章节。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)

    # 「已确认大纲版本化」：润色同样以 active confirmed 版本绑定的 ChapterPlan 为唯一事实来源
    ctx, ctx_error = await _build_confirmed_ctx(
        db,
        data.project_id,
        data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.style,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "polish", "style": data.style}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "style": data.style,
        }
    )
    try:
        content = await ai_polish_chapter(
            data.setting,
            real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            style=data.style,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
        override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="polish",
            context_snapshot=ctx_snapshot,
            error_message=raw,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    # 润色保持字数接近原文：以原文正文字数为目标，超长不截断、不重试，仅标记
    title, body, word_count = await _finalize_chapter(
        content,
        chapter_index,
        target_words=count_words(data.chapter_content),
        allow_retry=False,
    )
    title, body, word_count = _finalize_with_confirmed_title(
        content, title, body, word_count, ctx, chapter_index
    )
    chapter_eval = _build_generation_eval(
        content,
        title,
        body,
        target_words=count_words(data.chapter_content),
    )

    mode = "real" if real else "mock"
    await _record_ai_generation(
        db,
        real=real,
        action_type="polish",
        context_snapshot=ctx_snapshot,
        response_content=body,
        system_prompt=ctx["system_prompt"] if ctx else None,
        user_prompt=ctx["user_prompt"] if ctx else None,
        **trace,
        **chapter_eval,
    override=override
    )
    return ChapterEditResponse(
        mode=mode,
        chapter=NovelChapter(
            index=chapter_index,
            title=title,
            content=body,
            word_count=word_count,
        ),
        message=(
            "已由真实模型润色本章" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/generate-novel", response_model=NovelGenerateResponse)
async def generate_novel(
    data: NovelGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """非流式：一次请求生成全部章节（真实模式逐章调用模型）。"""
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    try:
        chapters, chapter_evals = await _generate_all_chapters(
            data, real, override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="draft",
            context_snapshot=_plan_snapshot(data),
            error_message=raw,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_generation_failed", raw)

    mode = "real" if real else "mock"
    for index, chapter in enumerate(chapters, start=1):
        await _record_ai_generation(
            db,
            real=real,
            action_type="draft",
            context_snapshot=_plan_snapshot(data, {"index": index}),
            response_content=chapter.content,
            **chapter_evals[index - 1],
        override=override
        )
    return NovelGenerateResponse(
        mode=mode,
        chapters=chapters,
        message=(
            "已由真实模型生成" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/generate-novel/stream")
async def generate_novel_stream(
    data: NovelGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 流式：按章节逐步下发生成结果，前端可实时显示进度与正文。"""
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)

    async def event_stream():
        request_id = uuid.uuid4().hex[:12]
        yield _sse({
            "type": "start",
            "mode": "real" if real else "mock",
            "total": data.chapter_count,
            "request_id": request_id,
        })
        chapters: list[NovelChapter] = []
        total = data.chapter_count
        for i in range(1, total + 1):
            prev = chapters[-1].content if chapters else data.continue_from
            try:
                chapter, chapter_eval = await _build_chapter(
                    data, i, total, real, previous_chapter=prev, override=override
                )
            except RuntimeError as error:
                raw = str(error)
                code, _, message = raw.partition(": ")
                await _record_ai_generation(
                    db,
                    real=real,
                    action_type="draft",
                    context_snapshot=_plan_snapshot(data, {"index": i}),
                    error_message=raw,
                override=override
                )
                if not code.startswith("ai_"):
                    code, message = "ai_generation_failed", raw
                yield _sse({
                    "type": "error",
                    "code": code,
                    "message": message or raw,
                })
                return
            chapters.append(chapter)
            await _record_ai_generation(
                db,
                real=real,
                action_type="draft",
                context_snapshot=_plan_snapshot(data, {"index": i}),
                response_content=chapter.content,
                **chapter_eval,
            override=override
            )
            if not real:
                await asyncio.sleep(settings.ai_mock_delay_seconds)
            yield _sse({
                "type": "chapter",
                "index": i,
                "total": total,
                "title": chapter.title,
                "content": chapter.content,
            })
        yield _sse({
            "type": "done",
            "mode": "real" if real else "mock",
            "message": (
                "已由真实模型生成" if real
                else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
            ),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------- 2026-11-17 批次 3：编辑类端点流式化 + 生成可中途取消 ----------

_CANCEL_EVENTS: dict[str, asyncio.Event] = {}


def _register_cancel_event(request_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    _CANCEL_EVENTS[request_id] = ev
    return ev


def _unregister_cancel_event(request_id: str) -> None:
    _CANCEL_EVENTS.pop(request_id, None)


class CancelGenerationRequest(BaseModel):
    request_id: str


@router.post("/cancel-generation")
async def cancel_generation(
    data: CancelGenerationRequest,
    current_user: User = Depends(get_current_user),
):
    """取消进行中的生成：设置取消信号，生成循环在下一个阶段边界停下。

    只对带 request_id 的流式生成有效（批次 3 起编辑类端点均走流式）。
    """
    ev = _CANCEL_EVENTS.get(data.request_id)
    if ev is None:
        return {"ok": False, "message": "未找到进行中的生成任务"}
    ev.set()
    return {"ok": True, "message": "已发送取消信号"}


@router.get("/usage-summary")
async def usage_summary(
    days: int = Query(default=14, ge=1, le=90),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批次 4：token 用量摘要（近 N 天，按日/按模型/按项目），供自费 API 成本可见。

    只统计真实模型调用（provider != mock），数字来自 generation_records 的
    prompt_tokens / completion_tokens，量级与 DeepSeek 后台一致。
    """
    from sqlalchemy import select, func

    from app.models.generation import GenerationRecord
    from app.models.project import Project

    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(GenerationRecord).where(
                GenerationRecord.created_at >= since,
                GenerationRecord.model_provider != "mock",
            )
        )
    ).scalars().all()

    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    total_in = 0
    total_out = 0
    for r in rows:
        p = int(r.prompt_tokens or 0)
        c = int(r.completion_tokens or 0)
        if p == 0 and c == 0:
            continue
        total_in += p
        total_out += c
        day = r.created_at.strftime("%m-%d")
        d = by_day.setdefault(day, {"input": 0, "output": 0, "records": 0})
        d["input"] += p
        d["output"] += c
        d["records"] += 1
        m = by_model.setdefault(r.model_name or "unknown", {"input": 0, "output": 0})
        m["input"] += p
        m["output"] += c
        if r.project_id is not None:
            k = str(r.project_id)
            if k not in by_project:
                by_project[k] = {"project_id": k, "input": 0, "output": 0, "title": ""}
            by_project[k]["input"] += p
            by_project[k]["output"] += c
    if by_project:
        names = (
            await db.execute(
                select(Project.id, Project.title).where(
                    Project.id.in_([uuid.UUID(k) for k in by_project])
                )
            )
        ).all()
        for pid, title in names:
            if str(pid) in by_project:
                by_project[str(pid)]["title"] = title
    return {
        "days": days,
        "since": since.isoformat(),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "records": len(rows),
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_model": [{"model": k, **v} for k, v in by_model.items()],
        "by_project": sorted(
            by_project.values(), key=lambda x: -(x["input"] + x["output"])
        )[:5],
    }


async def _run_edit_stream(
    request: Request,
    *,
    request_id: str,
    mode: str,
    action_label: str,
    index: int,
    pipeline,
    record_success,
    record_failure,
):
    """编辑类流式端点共用骨架：start/status/进度心跳 + 取消信号与客户端断连检测。

    pipeline() 返回 (title, body, word_count, chapter_eval)，失败抛 RuntimeError。
    取消/断连时任务被 cancel，后端不再发起新的模型调用（停止烧钱），
    并落一条 status=cancelled 的生成记录。
    """
    cancel_ev = _register_cancel_event(request_id)
    started = time.monotonic()
    yield _sse({"type": "start", "mode": mode, "request_id": request_id})
    yield _sse({"type": "status", "message": f"正在调用 AI {action_label}（思考模型较慢，请耐心等待）…"})
    task = asyncio.create_task(pipeline())
    cancel_reason: str | None = None
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            break
        except asyncio.TimeoutError:
            if cancel_ev.is_set():
                cancel_reason = "用户取消"
                break
            if await request.is_disconnected():
                cancel_reason = "客户端断开"
                break
            yield _sse({
                "type": "progress",
                "elapsed": round(time.monotonic() - started),
            })
    if cancel_reason is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await record_failure(f"ai_cancelled: {cancel_reason}", status="cancelled")
        yield _sse({"type": "cancelled", "message": f"生成已取消（{cancel_reason}），已停止消耗额度"})
        _unregister_cancel_event(request_id)
        return
    try:
        result = task.result()
    except asyncio.CancelledError:
        await record_failure("ai_cancelled: 服务端取消", status="cancelled")
        yield _sse({"type": "cancelled", "message": "生成已取消"})
        _unregister_cancel_event(request_id)
        return
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await record_failure(raw, status=None)
        yield _sse({
            "type": "error",
            "code": code if code.startswith("ai_") else "ai_generation_failed",
            "message": message or raw,
        })
        _unregister_cancel_event(request_id)
        return
    except Exception as error:  # 兜底：任何未预期异常都转成流内错误事件
        raw = f"{type(error).__name__}: {error}"
        await record_failure(raw, status=None)
        yield _sse({"type": "error", "code": "ai_generation_failed", "message": raw})
        _unregister_cancel_event(request_id)
        return
    title, body, word_count, chapter_eval = result
    await record_success(title, body, word_count, chapter_eval)
    _unregister_cancel_event(request_id)
    yield _sse({
        "type": "chapter",
        "index": index,
        "title": title,
        "content": body,
        "word_count": word_count,
    })
    yield _sse({"type": "done", "message": f"已完成{action_label}"})


def _edit_stream_response(request: Request, **kwargs):
    return StreamingResponse(
        _run_edit_stream(request, **kwargs),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/regenerate-chapter/stream")
async def regenerate_chapter_stream(
    request: Request,
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重新生成本章（SSE 流式，可取消）。事件：start/status/progress/chapter/done/error/cancelled。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)
    ctx, ctx_error = await _build_confirmed_ctx(
        db, data.project_id, data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.instruction,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "rewrite", "instruction": data.instruction}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "instruction": data.instruction,
        }
    )

    async def pipeline():
        content = await ai_regenerate_chapter(
            data.setting, real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            instruction=data.instruction,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            outline=data.outline,
            previous_chapter_tail=data.previous_chapter_tail,
            next_chapter_head=data.next_chapter_head,
        )
        async def _retry():
            return await ai_regenerate_chapter(
                data.setting, real,
                chapter_title=data.chapter_title,
                chapter_content=data.chapter_content,
                instruction=data.instruction,
                system_prompt=ctx["system_prompt"] if ctx else None,
                user_prompt=ctx["user_prompt"] if ctx else None,
                override=override,
                outline=data.outline,
                previous_chapter_tail=data.previous_chapter_tail,
                next_chapter_head=data.next_chapter_head,
            )
        title, body, word_count = await _finalize_chapter(
            content, chapter_index,
            target_words=data.setting.words_per_chapter,
            allow_retry=real,
            retry_factory=_retry if real else None,
        )
        title, body, word_count = _finalize_with_confirmed_title(
            content, title, body, word_count, ctx, chapter_index
        )
        chapter_eval = _build_generation_eval(
            content, title, body, target_words=data.setting.words_per_chapter
        )
        return title, body, word_count, chapter_eval

    async def record_success(title, body, word_count, chapter_eval):
        await _record_ai_generation(
            db, real=real, action_type="draft",
            context_snapshot=ctx_snapshot, response_content=body,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, **chapter_eval, override=override,
        )

    async def record_failure(message, status=None):
        await _record_ai_generation(
            db, real=real, action_type="draft",
            context_snapshot=ctx_snapshot, error_message=message,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, status=status, override=override,
        )

    return _edit_stream_response(
        request,
        request_id=uuid.uuid4().hex[:12],
        mode="real" if real else "mock",
        action_label="重新生成本章",
        index=chapter_index,
        pipeline=pipeline,
        record_success=record_success,
        record_failure=record_failure,
    )


@router.post("/expand-chapter/stream")
async def expand_chapter_stream(
    request: Request,
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """扩写本章（SSE 流式，可取消）。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)
    ctx, ctx_error = await _build_confirmed_ctx(
        db, data.project_id, data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.instruction,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "expand", "instruction": data.instruction}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "instruction": data.instruction,
        }
    )

    async def pipeline():
        content = await ai_expand_chapter(
            data.setting, real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            instruction=data.instruction,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
            outline=data.outline,
            previous_chapter_tail=data.previous_chapter_tail,
            next_chapter_head=data.next_chapter_head,
        )
        title, body, word_count = await _finalize_chapter(
            content, chapter_index, target_words=None, allow_retry=False,
        )
        title, body, word_count = _finalize_with_confirmed_title(
            content, title, body, word_count, ctx, chapter_index
        )
        chapter_eval = _build_generation_eval(content, title, body, target_words=None)
        return title, body, word_count, chapter_eval

    async def record_success(title, body, word_count, chapter_eval):
        await _record_ai_generation(
            db, real=real, action_type="expand",
            context_snapshot=ctx_snapshot, response_content=body,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, **chapter_eval, override=override,
        )

    async def record_failure(message, status=None):
        await _record_ai_generation(
            db, real=real, action_type="expand",
            context_snapshot=ctx_snapshot, error_message=message,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, status=status, override=override,
        )

    return _edit_stream_response(
        request,
        request_id=uuid.uuid4().hex[:12],
        mode="real" if real else "mock",
        action_label="扩写本章",
        index=chapter_index,
        pipeline=pipeline,
        record_success=record_success,
        record_failure=record_failure,
    )


@router.post("/polish-chapter/stream")
async def polish_chapter_stream(
    request: Request,
    data: ChapterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """润色本章（SSE 流式，可取消）。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    chapter_index = _resolve_chapter_index(data)
    ctx, ctx_error = await _build_confirmed_ctx(
        db, data.project_id, data.chapter_id,
        max_length=len(data.chapter_content or "") or 800,
        instruction=data.instruction,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "polish", "instruction": data.instruction}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "chapter_title": data.chapter_title,
            "chapter_content": data.chapter_content,
            "instruction": data.instruction,
        }
    )

    async def pipeline():
        content = await ai_polish_chapter(
            data.setting, real,
            chapter_title=data.chapter_title,
            chapter_content=data.chapter_content,
            style=data.style,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
        )
        title, body, word_count = await _finalize_chapter(
            content, chapter_index, target_words=None, allow_retry=False,
        )
        title, body, word_count = _finalize_with_confirmed_title(
            content, title, body, word_count, ctx, chapter_index
        )
        chapter_eval = _build_generation_eval(content, title, body, target_words=None)
        return title, body, word_count, chapter_eval

    async def record_success(title, body, word_count, chapter_eval):
        await _record_ai_generation(
            db, real=real, action_type="polish",
            context_snapshot=ctx_snapshot, response_content=body,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, **chapter_eval, override=override,
        )

    async def record_failure(message, status=None):
        await _record_ai_generation(
            db, real=real, action_type="polish",
            context_snapshot=ctx_snapshot, error_message=message,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, status=status, override=override,
        )

    return _edit_stream_response(
        request,
        request_id=uuid.uuid4().hex[:12],
        mode="real" if real else "mock",
        action_label="润色本章",
        index=chapter_index,
        pipeline=pipeline,
        record_success=record_success,
        record_failure=record_failure,
    )


@router.post("/generate-next-chapter/stream")
async def generate_next_chapter_stream(
    request: Request,
    data: NextChapterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成下一章（SSE 流式，可取消）。"""
    access_error = await _ensure_project_editor(db, data.project_id, current_user)
    if access_error is not None:
        return access_error
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.setting.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    ctx, ctx_error = await _build_confirmed_ctx(
        db, data.project_id, data.chapter_id,
        max_length=data.setting.words_per_chapter,
    )
    if ctx_error is not None:
        return ctx_error
    trace = _trace_fields(ctx, data.project_id, data.chapter_id)
    ctx_snapshot = (
        {**ctx["snapshot"], "action": "draft"}
        if ctx else {
            "setting": data.setting.model_dump(mode="json"),
            "outline": data.outline,
            "previous_summary": data.previous_summary,
            "character_state": data.character_state,
            "chapter_index": data.chapter_index,
            "previous_chapter_tail": (data.previous_chapter or "")[-6000:],
        }
    )

    async def pipeline():
        content = await ai_generate_next_chapter(
            data.setting, real,
            outline=data.outline,
            previous_summary=data.previous_summary,
            character_state=data.character_state,
            chapter_index=data.chapter_index,
            previous_chapter=data.previous_chapter,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            override=override,
        )
        async def _retry():
            return await ai_generate_next_chapter(
                data.setting, real,
                outline=data.outline if ctx is None else "",
                previous_summary=data.previous_summary,
                character_state=data.character_state,
                chapter_index=data.chapter_index,
                previous_chapter=data.previous_chapter,
                system_prompt=ctx["system_prompt"] if ctx else None,
                user_prompt=ctx["user_prompt"] if ctx else None,
                override=override,
            )
        title, body, word_count = await _finalize_chapter(
            content, data.chapter_index,
            target_words=data.setting.words_per_chapter,
            allow_retry=real,
            retry_factory=_retry if real else None,
        )
        title, body, word_count = _finalize_with_confirmed_title(
            content, title, body, word_count, ctx, data.chapter_index
        )
        chapter_eval = _build_generation_eval(
            content, title, body, target_words=data.setting.words_per_chapter
        )
        return title, body, word_count, chapter_eval

    async def record_success(title, body, word_count, chapter_eval):
        await _record_ai_generation(
            db, real=real, action_type="draft",
            context_snapshot=ctx_snapshot, response_content=body,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, **chapter_eval, override=override,
        )

    async def record_failure(message, status=None):
        await _record_ai_generation(
            db, real=real, action_type="draft",
            context_snapshot=ctx_snapshot, error_message=message,
            system_prompt=ctx["system_prompt"] if ctx else None,
            user_prompt=ctx["user_prompt"] if ctx else None,
            **trace, status=status, override=override,
        )

    return _edit_stream_response(
        request,
        request_id=uuid.uuid4().hex[:12],
        mode="real" if real else "mock",
        action_label="生成下一章",
        index=data.chapter_index,
        pipeline=pipeline,
        record_success=record_success,
        record_failure=record_failure,
    )


@router.post("/plan-novel", response_model=PlanNovelResponse)
async def plan_novel(
    data: PlanNovelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """灵感 → 完整创作规划：AI 补全书名/卖点/简介/世界观/人物/关系/主线/分卷/章节大纲/伏笔。"""
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    try:
        plan = await ai_plan_novel(
            idea=data.idea,
            genre=data.genre or "",
            writing_style=data.writing_style or "",
            perspective=data.perspective or "",
            chapter_count=max(1, data.chapter_count or 10),
            words_per_chapter=max(500, data.words_per_chapter or 1000),
            ending_type=data.ending_type or "",
            has_romance=bool(data.has_romance),
            content_restrictions=data.content_restrictions or "",
            must_keep_settings=data.must_keep_settings or "",
            existing_settings=data.existing_settings,
            real=real,
        override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="outline",
            context_snapshot=_plan_snapshot(data),
            error_message=raw,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_plan_failed", raw)

    mode = "real" if real else "mock"
    meta_finish, meta_prompt, meta_completion = take_last_ai_meta()
    await _record_ai_generation(
        db,
        real=real,
        action_type="outline",
        context_snapshot=_plan_snapshot(data),
        response_content=plan.model_dump_json(),
        finish_reason=meta_finish,
        prompt_tokens=meta_prompt,
        completion_tokens=meta_completion,
    override=override
    )
    return PlanNovelResponse(
        mode=mode,
        plan=plan,
        message=(
            "已由真实模型生成完整创作规划" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )


@router.post("/plan-update", response_model=PlanUpdateResponse)
async def plan_update(
    data: PlanUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """局部修改规划：只更新与指令相关的章节并返回 changed_chapter_ids，不重置整本。"""
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error
    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)
    override = _build_ai_override(data)
    try:
        plan, changed_chapter_ids = await ai_plan_update(
            instruction=data.instruction,
            current_plan=data.current_plan,
            locked=data.locked or [],
            real=real,
        override=override
        )
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        await _record_ai_generation(
            db,
            real=real,
            action_type="outline",
            context_snapshot={
                "instruction": data.instruction,
                "locked": data.locked or [],
                "current_plan_summary": (data.current_plan or {})[:2000],
            },
            error_message=raw,
        override=override
        )
        if code.startswith("ai_"):
            return _ai_error_response(code, message or raw)
        return _ai_error_response("ai_plan_update_failed", raw)

    mode = "real" if real else "mock"
    meta_finish, meta_prompt, meta_completion = take_last_ai_meta()
    await _record_ai_generation(
        db,
        real=real,
        action_type="outline",
        context_snapshot={
            "instruction": data.instruction,
            "locked": data.locked or [],
            "current_plan_summary": (data.current_plan or {})[:2000],
        },
        response_content=plan.model_dump_json(),
        finish_reason=meta_finish,
        prompt_tokens=meta_prompt,
        completion_tokens=meta_completion,
    override=override
    )
    return PlanUpdateResponse(
        mode=mode,
        plan=plan,
        changed_chapter_ids=changed_chapter_ids,
        message=(
            "已由真实模型完成局部修改，仅刷新受影响章节" if real
            else "未配置真实 AI 密钥，本次为动态模拟结果（mode=mock），配置 DEEPSEEK_API_KEY 后可切换真实模型"
        ),
    )
