# 生成记录服务（第一期「生成不丢失」规格实现）
# 负责 generation_records 的写入与查询；同时提供 AI 配置解析 helper，
# 供各 AI 生成入口（plan-novel / generate / next / regenerate / expand / polish /
# 正文级 continue / rewrite / polish）统一落库。
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.generation import ATTRIBUTION_STATUSES, GenerationRecord
from app.services.ai_service import (
    get_effective_ai_config,
    take_last_ai_meta,
    take_last_ai_prompt,
    take_last_ai_usage,
)


def resolve_ai_provider() -> tuple[str | None, str | None]:
    """按生效配置解析 (model_provider, model_name)。

    provider 规则：DEEPSEEK_* → deepseek；AI_* → openai；OPENAI_* → custom。
    演示（mock）模式由调用方显式传 provider="mock"。
    """
    _, _, model = get_effective_ai_config()
    if settings.deepseek_api_key:
        return "deepseek", model
    if settings.ai_api_key:
        return "openai", model
    if settings.openai_api_key:
        return "custom", model
    return None, model


def serialize_context(context: dict | None) -> str | None:
    """把上下文快照 dict 序列化为 JSON 字符串；空/None 返回 None。"""
    if not context:
        return None
    return json.dumps(context, ensure_ascii=False, default=str)


def _coerce_uuid(value):
    """UUID 字段归一化：uuid.UUID 原样返回；str 尝试转换；非法/None 返回 None。

    多个生成入口经 context_builder 传回的 outline_plan_version_id 等追溯字段
    为字符串（str(version.id)），而 GenerationRecord 的 UUID 列（SQLAlchemy 原生
    Uuid 类型）要求 uuid.UUID 对象，直接落库会触发
    'str' object has no attribute 'hex' 被 record_generation 吞掉导致追溯缺失。
    """
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def record_generation(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    volume_id: uuid.UUID | None = None,
    chapter_id: uuid.UUID | None = None,
    scene_id: uuid.UUID | None = None,
    outline_plan_version_id: uuid.UUID | None = None,
    chapter_plan_id: str | None = None,
    chapter_plan_snapshot: str | None = None,
    title_snapshot: str | None = None,
    action_type: str,
    attribution_status: str | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    system_prompt: str = "",
    user_prompt: str = "",
    context_snapshot: str | None = None,
    response_content: str = "",
    raw_content: str = "",
    processed_content: str = "",
    final_content: str = "",
    postprocess_actions: str | None = None,
    finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    raw_content_length: int | None = None,
    final_content_length: int | None = None,
    was_truncated: int | None = None,
    truncation_reason: str | None = None,
    review_flags: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    status: str = "succeeded",
    error_message: str | None = None,
) -> GenerationRecord | None:
    """写入一条 generation_record 并提交。

    任何异常都不会向上抛出（不影响主流程），返回 None 表示落库失败；
    调用方如需强校验可自行捕获后记录日志。

    token 用量优先级：显式传入 > 最近一次 AI 调用真实 usage（contextvar） > NULL。
    阶段 1 token 口径修复：无供应商真实 usage 时 token 字段一律写 NULL，
    禁止用字符数按折半估算伪装成 token；字符数只保留在
    raw_content_length / final_content_length 等独立元数据字段，两者不得互换写入。
    prompt 优先级：显式传入 > 最近一次 AI 调用实际提示词（contextvar）。

    「已确认大纲版本化」追溯字段：
      outline_plan_version_id（外键 outline_plan_versions.id）、
      chapter_plan_id（ChapterPlan 稳定标识，字符串型）、
      chapter_plan_snapshot / title_snapshot（生成时依据的已确认计划快照与标题）。

    v6 归属语义（必须显式传入，不接受隐式 None）：
      project_bound —— 记录明确属于某个作品，必须提供 project_id；
      pre_project   —— 作品创建前的规划类调用，允许 project_id 为空；
      unattributed  —— 仅用于无法可靠归属的历史或异常数据。
    非法组合（缺 project_id 的 project_bound、未知取值、有 project_id 的
    unattributed）在落库前直接抛 ValueError，不被下面的吞异常逻辑掩盖。

    「第三批生成链路一致性」字段：
      raw_content —— 模型完整原始响应，只追加不改写；
      processed_content —— 清洗后结果；
      final_content —— 最终章节正文（response_content 保持历史语义兼容）；
      postprocess_actions / review_flags —— JSON 文本；
      finish_reason / prompt_tokens / completion_tokens —— 模型返回的完成原因与用量；
      raw_content_length / final_content_length —— 去空白字数；
      was_truncated / truncation_reason —— 是否截断及原因；
      未显式传入 finish_reason/tokens 时，从最近一次 AI 调用 contextvar 取用。
    """
    # 归属校验必须发生在 try 之外：这里是编程错误，不能被落库异常兜底吞掉。
    if attribution_status is None:
        raise ValueError(
            "record_generation 必须显式传入 attribution_status"
            "（project_bound / pre_project / unattributed）"
        )
    if attribution_status not in ATTRIBUTION_STATUSES:
        raise ValueError(
            "未知的 attribution_status：%r（允许值：%s）"
            % (attribution_status, ", ".join(ATTRIBUTION_STATUSES))
        )
    if attribution_status == "project_bound" and project_id is None:
        raise ValueError(
            "attribution_status=project_bound 时必须提供 project_id"
        )
    if attribution_status == "unattributed" and project_id is not None:
        raise ValueError(
            "attribution_status=unattributed 不允许同时提供 project_id"
            "（有 project_id 就应写 project_bound）"
        )

    try:
        if not system_prompt and not user_prompt:
            system_prompt, user_prompt = take_last_ai_prompt()
        if input_tokens is None and output_tokens is None:
            input_tokens, output_tokens = take_last_ai_usage()
        if finish_reason is None or (prompt_tokens is None and completion_tokens is None):
            _meta_finish, _meta_prompt, _meta_completion = take_last_ai_meta()
            if finish_reason is None:
                finish_reason = _meta_finish
            if prompt_tokens is None:
                prompt_tokens = _meta_prompt
            if completion_tokens is None:
                completion_tokens = _meta_completion
        # 阶段 1 token 口径最小修复（2026-10）：无真实供应商 usage 时不再估算写入 token 字段。
        # 字符数（提示词长度 / 正文长度）与 token 数口径必须分离，故此处保持 NULL 落库；
        # 需要字符维度时读取 raw_content_length / final_content_length 等独立字段。
        record = GenerationRecord(
            project_id=_coerce_uuid(project_id),
            volume_id=_coerce_uuid(volume_id),
            chapter_id=_coerce_uuid(chapter_id),
            scene_id=_coerce_uuid(scene_id),
            outline_plan_version_id=_coerce_uuid(outline_plan_version_id),
            chapter_plan_id=chapter_plan_id,
            chapter_plan_snapshot=chapter_plan_snapshot,
            title_snapshot=title_snapshot,
            action_type=action_type,
            attribution_status=attribution_status,
            model_provider=model_provider,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt or "",
            user_prompt=user_prompt or "",
            context_snapshot=context_snapshot,
            response_content=response_content or "",
            raw_content=raw_content or "",
            processed_content=processed_content or "",
            final_content=final_content or "",
            postprocess_actions=postprocess_actions,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            raw_content_length=raw_content_length,
            final_content_length=final_content_length,
            was_truncated=was_truncated,
            truncation_reason=truncation_reason,
            review_flags=review_flags,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status,
            error_message=error_message,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def list_generations(
    db: AsyncSession,
    project_id: uuid.UUID,
    action_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[GenerationRecord], int]:
    """按 project 过滤的分页生成记录列表，可选 action_type 筛选；按创建时间倒序。"""
    page = max(1, page)
    page_size = max(1, min(page_size, settings.page_size_max or 100))

    base = select(GenerationRecord).where(
        GenerationRecord.project_id == project_id
    )
    if action_type:
        base = base.where(GenerationRecord.action_type == action_type)
    count_stmt = (
        select(func.count())
        .select_from(base.subquery())
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        base
        .order_by(GenerationRecord.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def get_generation(
    db: AsyncSession,
    project_id: uuid.UUID,
    record_id: uuid.UUID,
) -> GenerationRecord | None:
    """按 project + id 取单条生成记录详情。"""
    stmt = select(GenerationRecord).where(
        GenerationRecord.project_id == project_id,
        GenerationRecord.id == record_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()
