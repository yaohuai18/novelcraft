# 章节正文路由（第七批规格实现 + 第一期「生成不丢失」扩展）
# 1) GET  /api/projects/{project_id}/chapters/{outline_id}/content  读取正文，不存在自动初始化空正文（version=1）
# 2) PUT  /api/projects/{project_id}/chapters/{outline_id}/content  保存正文：version+1 并写入 ChapterVersion 历史
#    第一期扩展：支持 source=manual/ai_draft/ai_continue/ai_rewrite/ai_polish/restore；
#    AI 来源需携带 generation_record_id；版本链 parent_version_id 自动指向当前 is_current 版本。
# 3) GET  /api/projects/{project_id}/chapters/{outline_id}/versions 版本列表（按 version desc）
# 4) POST /api/projects/{project_id}/chapters/{outline_id}/versions/{version_id}/restore 恢复历史版本（新建 restore 版本，不覆盖旧版本）
# 5) POST /api/projects/{project_id}/chapters/{outline_id}/ai/continue   AI 续写预览（先写 generation_record，不覆盖正文）
# 6) POST /api/projects/{project_id}/chapters/{outline_id}/ai/rewrite    AI 改写预览（先写 generation_record，不覆盖正文）
# 7) POST /api/projects/{project_id}/chapters/{outline_id}/ai/polish     AI 润色预览（先写 generation_record，不覆盖正文）
# 8) POST /api/projects/{project_id}/chapters/{outline_id}/ai/plan-compliance 计划符合度检查（不调 AI，本地规则，落 plan_check 记录）
# 所有接口需 JWT 鉴权，并校验章节归属当前作品且 outline_type=chapter。
import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import AsyncSessionLocal, get_db
from app.models.chapter_content import ChapterContent
from app.models.chapter_version import ChapterVersion, VERSION_SOURCES
from app.models.character import Character
from app.models.generation import GenerationRecord
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.models.world_setting import WorldSetting
from app.schemas.writing_ai import (
    ContinueWritingRequest,
    PolishWritingRequest,
    RewriteWritingRequest,
    WritingPreviewResponse
)
from app.schemas.chapter_content import (
    ChapterContentResponse,
    ChapterContentUpdate,
    ChapterVersionResponse,
    ConflictResponse,
    LifecycleResponse,
)
from app.schemas.chapter_plan import PlanComplianceResponse
from app.services.ai_service import (
    chat_with_ai,
    get_effective_ai_config,
    is_ai_configured,
    take_last_ai_meta,
)
from app.services.model_policy import TaskType
from app.services.chapter_memory_service import trigger_chapter_summary
from app.services.consistency_service import trigger_auto_check
from app.services.state_extraction_service import (
    trigger_state_extraction,
    trigger_state_facts,
)
from app.services.chapter_lifecycle import (
    SAVEABLE_GENERATION_STATUSES,
    after_content_saved,
    get_effective_generation_record,
)
from app.services.context_builder import build_continuation_context
from app.services.chapter_plan_service import plan_compliance_of
from app.services import (
    collaboration_service,
    event_service,
    failure_sample_service,
    generation_service,
    index_service,
    translation_service,
)


# 第一批数据完整性：请求 source 与 generation_record.action_type 的合法对应。
# 未在此表登记的 ai_* 来源不做类型匹配（但仍受章节绑定与消费标记约束）。
AI_SOURCE_ACTION_TYPES: dict[str, tuple[str, ...]] = {
    "ai_draft": ("draft",),
    "ai_continue": ("continue",),
    "ai_rewrite": ("rewrite",),
    "ai_polish": ("polish",),
    "ai_expand": ("expand",),
}

router = APIRouter(
    prefix="/api/projects/{project_id}/chapters",
    tags=["章节正文"]
)


class SelectionRewriteRequest(BaseModel):
    """选段改写请求（2026-11-16 第三档升级）：只改写选中段落，不整章重写。"""

    selected_text: str = Field(..., min_length=1, max_length=8000)
    instruction: str = Field(default="", max_length=2000)


@router.post("/{outline_id}/ai/rewrite-selection")
async def rewrite_selection(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: SelectionRewriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """选段改写预览：对选中文本按指令改写（保持字数相当），返回新文本，不落库。"""
    from app.services.ai_service import chat_with_ai, is_ai_configured
    from app.services.model_policy import TaskType

    chapter = await get_chapter(project_id, outline_id, current_user, db)
    if not is_ai_configured():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 未配置，无法选段改写",
        )
    instruction_text = data.instruction.strip() or "改写得更流畅、更有画面感"
    system_prompt = (
        "你是小说文字编辑。只改写用户给出的选中段落，要求：\n"
        "1. 输出改写后的段落原文，不要任何解释、标题或引号包裹；\n"
        "2. 保持原意、人物口吻与叙事视角不变，长度与原段落大致相当；\n"
        "3. 严格执行用户的具体指令。"
    )
    user_prompt = (
        f"【用户指令】{instruction_text}\n\n"
        f"【选中段落】\n{data.selected_text}"
    )
    try:
        rewritten = await chat_with_ai(
            system_prompt,
            user_prompt,
            max_tokens=max(600, len(data.selected_text) * 2),
            task_type=TaskType.NOVEL_BODY.value,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"选段改写失败：{str(exc)[:200]}",
        ) from exc
    rewritten = rewritten.strip()
    if not rewritten:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="选段改写返回为空，请重试",
        )
    return {
        "chapter_id": str(outline_id),
        "rewritten_text": rewritten,
        "original_length": len(data.selected_text),
        "rewritten_length": len(rewritten),
    }


@router.get("/{outline_id}/rewrite-impact")
async def rewrite_impact(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重写影响面（2026-11-16 第三档升级）：返回修改本章后可能受影响的后续章节。

    依赖图存在时按实体/事实依赖精确计算；无依赖记录时回退为全部后续章节。
    只读计算，不修改任何章节。
    """
    from app.services.chapter_lifecycle import (
        compute_dependency_impact_set,
        _collect_later_chapters,
    )

    chapter = await get_chapter(project_id, outline_id, current_user, db)
    impacted = await compute_dependency_impact_set(db, chapter)
    if impacted is None:
        impacted = await _collect_later_chapters(db, chapter)
    items = [
        {
            "chapter_id": str(o.id),
            "title": o.title or "",
            "sort_order": o.sort_order,
            "has_validation": bool(o.validation_report),
            "needs_recheck": bool(
                o.validation_report and "needs_recheck" in (o.validation_report or "")
            ),
        }
        for o in impacted
    ]
    return {
        "mode": "dependency_graph" if impacted is not None else "fallback_all_later",
        "count": len(items),
        "items": items,
    }


@router.get("/validation-summary")
async def validation_summary(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """全书章节校验状态摘要（2026-11-15 第二档升级：让一致性校验可见）。

    返回每章的 validated_at / blocking 数 / warning 数 / needs_recheck / lifecycle_status。
    注意：必须定义在任何 /{outline_id} 路由之前，避免被路径参数吞掉。
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="作品不存在或无权访问"
            )

    rows = (
        await db.execute(
            select(Outline)
            .where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            )
            .order_by(Outline.sort_order)
        )
    ).scalars().all()

    items = []
    for ch in rows:
        report = {}
        if ch.validation_report:
            try:
                report = json.loads(ch.validation_report)
            except Exception:  # noqa: BLE001
                report = {}
        issues = report.get("issues") or []
        blocking = sum(1 for i in issues if i.get("severity") == "error")
        warnings = len(issues) - blocking
        items.append(
            {
                "chapter_id": str(ch.id),
                "title": ch.title or "",
                "sort_order": ch.sort_order,
                "validated_at": report.get("validated_at") or None,
                "blocking": blocking,
                "warnings": warnings,
                "needs_recheck": bool(report.get("needs_recheck")),
                "lifecycle_status": ch.lifecycle_status,
            }
        )
    total = len(items)
    validated = sum(1 for i in items if i["validated_at"])
    return {
        "total": total,
        "validated": validated,
        "coverage": round(validated / total, 3) if total else 0.0,
        "items": items,
    }

logger = logging.getLogger(__name__)


async def _run_post_save_ai_triggers(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> None:
    """保存正文后的 AI 增强链路（章节记忆/状态提取/一致性检查）后台执行。

    同步等待 AI 会拖慢保存接口（实测每章 4.5~5.4s，多章串行保存极易触发
    前端 15s 请求超时）。改为独立 session 后台执行：每个 trigger 自带开关
    与异常兜底，失败不影响正文保存，成功时结果异步落库。
    """
    try:
        async with AsyncSessionLocal() as db:
            await trigger_chapter_summary(db, project_id, chapter_id, chapter_title, content)
    except Exception:  # noqa: BLE001
        pass
    try:
        async with AsyncSessionLocal() as db:
            await trigger_state_extraction(db, project_id, chapter_id, chapter_title, content)
    except Exception:  # noqa: BLE001
        pass
    try:
        # 第一批「事实数据库与规则校验」：结构化事实提取并按章节幂等落库
        # （items/item_possessions/secret_knowers/快照/事件/章节元数据），
        # 提取失败不阻断正文保存，也不影响既有状态变更链路。
        async with AsyncSessionLocal() as db:
            await trigger_state_facts(db, project_id, chapter_id, chapter_title, content)
    except Exception:  # noqa: BLE001
        pass
    try:
        async with AsyncSessionLocal() as db:
            await trigger_auto_check(db, project_id, chapter_id, chapter_title, content)
    except Exception:  # noqa: BLE001
        pass


def _build_ai_override(data) -> dict:
    """从请求体提取请求级模型覆盖参数（model_name/temperature/max_tokens）。

    第六阶段：密钥仅存服务端环境变量，此处只透传模型选择，不含任何密钥。
    """
    ov = {}
    if getattr(data, "model_name", None):
        ov["model_name"] = data.model_name
    if getattr(data, "temperature", None) is not None:
        ov["temperature"] = data.temperature
    if getattr(data, "max_tokens", None):
        ov["max_tokens"] = data.max_tokens
    return ov


async def get_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Outline:
    """校验作品归属与章节大纲（outline_type=chapter）。

    第十五批：作者本人或项目活跃成员（owner/editor/reviewer/commenter/viewer）可读；
    非作者非成员统一 404。
    """
    project = await db.get(Project, project_id)

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )

    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="作品不存在或无权访问"
            )

    result = await db.execute(
        select(Outline).where(
            Outline.id == outline_id,
            Outline.project_id == project_id,
            Outline.outline_type == "chapter"
        )
    )

    chapter = result.scalar_one_or_none()

    if not chapter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在"
        )

    return chapter


async def get_chapter_ai_context(
    project_id: uuid.UUID,
    chapter: Outline,
    db: AsyncSession
) -> tuple[str, str]:
    """组装 AI 写作上下文：人物资料 + 世界观条目（初期限制版上下文）。

    返回 (character_text, world_text)。
    """
    character_result = await db.execute(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at.asc())
    )

    characters = list(character_result.scalars().all())

    world_result = await db.execute(
        select(WorldSetting)
        .where(WorldSetting.project_id == project_id)
        .order_by(WorldSetting.created_at.asc())
    )

    world_settings = list(world_result.scalars().all())

    character_text = "\n".join(
        (
            f"- {item.name}\n"
            f"  身份：{item.identity or '未设定'}\n"
            f"  性格：{item.personality or '未设定'}\n"
            f"  目标：{item.goal or '未设定'}\n"
            f"  冲突：{item.conflict or '未设定'}"
        )
        for item in characters
    )

    world_text = "\n".join(
        (
            f"- [{item.category}] {item.name}："
            f"{item.content}"
        )
        for item in world_settings
    )

    return character_text, world_text


@router.get(
    "/{outline_id}/content",
    response_model=ChapterContentResponse
)
async def get_chapter_content(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """读取章节正文；从未保存过时自动初始化空正文（content="", word_count=0, version=1）。"""
    outline = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    result = await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == outline_id
        )
    )

    content = result.scalar_one_or_none()

    if not content:
        content = ChapterContent(
            outline_id=outline_id,
            content="",
            word_count=0,
            version=1
        )

        db.add(content)
        await db.commit()
        await db.refresh(content)

    return content


@router.put(
    "/{outline_id}/content",
    response_model=ChapterContentResponse
)
async def update_chapter_content(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: ChapterContentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """保存章节正文：每次保存 version+1，并将新版本写入 ChapterVersion 历史。

    - 第十五批：编辑权限放宽给项目 owner/editor 角色（协作者编辑正文）；
    - 乐观锁：携带 base_version 且与当前版本不一致时返回 409 冲突
      （conflict/current_version/submitted_base_version），避免覆盖他人修改；
    - 协作者修改/新版本创建会通过事件机制触发站内通知。

    第一期「生成不丢失」：
    - source 区分 manual / ai_draft / ai_continue / ai_rewrite / ai_polish；
      AI 来源版本必须携带 generation_record_id 且该记录属于本项目且 status=succeeded；
    - parent_version_id 默认自动指向当前 is_current 版本，保证版本链连续；
    - 新版本置 is_current=True，旧 current 版本自动降级；
    - word_count 按去除空格与换行后的字符数统计。
    """
    outline = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    project = await db.get(Project, project_id)
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可修改正文"
            )

    # 第十三批：归档章节不允许普通编辑（可恢复为草稿后再编辑）
    if outline.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="章节已归档，不允许编辑；请先恢复为草稿"
        )

    result = await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == outline_id
        )
    )

    chapter_content = result.scalar_one_or_none()

    if not chapter_content:
        chapter_content = ChapterContent(
            outline_id=outline_id
        )
        db.add(chapter_content)
        await db.flush()

    # ===== 第一批数据完整性：AI 结果必须绑定到"这一章" =====
    # 一条 generation_record 只在它自己所属的章节、以匹配的操作类型、且未被消费时
    # 才能被接受为正式正文。旧实现只校验 project_id，同作品内章节 A 的结果可以被
    # 接受到章节 B。
    record: GenerationRecord | None = None
    record_id = None
    if data.source.startswith("ai_"):
        if data.generation_record_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="AI 来源版本必须携带 generation_record_id",
            )
        record = await db.get(GenerationRecord, data.generation_record_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="生成记录不存在或不属于当前作品",
            )
        record_id = record.id
        if record.chapter_id != outline_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "generation_record_chapter_mismatch",
                    "detail": "该生成记录属于其他章节，不能接受到当前章节",
                    "generation_record_id": str(record.id),
                    "record_chapter_id": (
                        str(record.chapter_id) if record.chapter_id else None
                    ),
                    "chapter_id": str(outline_id),
                },
            )
        allowed_actions = AI_SOURCE_ACTION_TYPES.get(data.source)
        # 已登记的 ai_* 来源严格匹配；未登记来源保持放行，避免误伤后续新增入口，
        # 但章节绑定与消费标记对它们同样生效。
        if allowed_actions is not None and record.action_type not in allowed_actions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "generation_record_action_mismatch",
                    "detail": "生成记录的操作类型与本次接受来源不匹配",
                    "generation_record_id": str(record.id),
                    "expected_actions": list(allowed_actions),
                    "actual_action": record.action_type,
                },
            )
        if record.status not in SAVEABLE_GENERATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "生成记录未完成（incomplete/failed/postprocess_failed），"
                    "不能接受为正式章节正文；请在预览中重新生成或使用续写补齐"
                ),
            )
        if record.consumed_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "generation_record_already_consumed",
                    "detail": "该生成结果已被接受过，不能重复接受",
                    "generation_record_id": str(record.id),
                    "consumed_at": record.consumed_at.isoformat(),
                    "consumed_chapter_id": (
                        str(record.consumed_chapter_id)
                        if record.consumed_chapter_id else None
                    ),
                    "consumed_version_id": (
                        str(record.consumed_version_id)
                        if record.consumed_version_id else None
                    ),
                    "chapter_id": str(outline_id),
                },
            )

    # 阶段J「内容级审计」：修改正文之前先抓取变更前状态（只读，不落库）。
    # 该快照用于 after_content_saved 生成字段级 diff；必须在 CAS 写库之前取，
    # 否则拿到的是修改后的内容。
    from app.services import audit_service as _audit_service

    content_state_before = _audit_service.snapshot_content_state(
        chapter_content, outline
    )

    # ===== 第一批并发安全：数据库原子 CAS =====
    # 旧实现是"读 version → 应用层比较 → ORM 赋值"，两个并发请求可以同时通过
    # 比较并写出同一个 next_version。此处改为单条条件 UPDATE：
    #   WHERE outline_id = ? [AND version = ?]
    # 受影响行数为 0 说明版本已被别人推进（或行不存在）→ 409。
    word_count = len(data.content.replace(" ", "").replace("\n", ""))
    from app.core.text_utils import bigrams as _bigrams

    cas_stmt = (
        update(ChapterContent)
        .where(ChapterContent.outline_id == outline_id)
        .values(
            content=data.content,
            content_bigrams=_bigrams(data.content),
            word_count=word_count,
            version=ChapterContent.version + 1,
        )
    )
    if data.base_version is not None:
        cas_stmt = cas_stmt.where(ChapterContent.version == data.base_version)
    cas_result = await db.execute(cas_stmt)
    if cas_result.rowcount != 1:
        await db.rollback()
        current_version = (
            await db.execute(
                select(ChapterContent.version).where(
                    ChapterContent.outline_id == outline_id
                )
            )
        ).scalar_one_or_none()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ConflictResponse(
                conflict=True,
                current_version=current_version or 0,
                submitted_base_version=data.base_version or 0,
            ).model_dump(),
        )

    # CAS 已写库，重新加载内存对象（后续审计/生命周期读到的是新正文）
    await db.refresh(chapter_content)
    next_version = chapter_content.version
    new_version_id = uuid.uuid4()

    # 版本链：未指定 parent 时自动指向当前 is_current 版本
    parent_version_id = data.parent_version_id
    if parent_version_id is None:
        current = (
            await db.execute(
                select(ChapterVersion)
                .where(
                    ChapterVersion.outline_id == outline_id,
                    ChapterVersion.is_current.is_(True),
                )
                .order_by(ChapterVersion.version.desc())
            )
        ).scalar_one_or_none()
        parent_version_id = current.id if current else None

    # 旧 current 降级，新版本置为 current（章节正文永远是当前版本的投影）
    await db.execute(
        update(ChapterVersion)
        .where(
            ChapterVersion.outline_id == outline_id,
            ChapterVersion.is_current.is_(True),
        )
        .values(is_current=False)
    )

    version = ChapterVersion(
        id=new_version_id,
        outline_id=outline_id,
        version=next_version,
        content=data.content,
        word_count=word_count,
        source=data.source,
        generation_record_id=data.generation_record_id,
        parent_version_id=parent_version_id,
        is_current=True,
        change_summary=data.change_summary
    )
    db.add(version)

    # ===== 第一批消费标记：条件更新，竞争请求只有一个能成功 =====
    # consumed_at 一旦写入，这条生成记录不能再被接受；同时把用户真正接受的正文
    # 写进 final_content（预览阶段 final_content 一直为空，不伪造"已接受"）。
    if record_id is not None:
        consume_result = await db.execute(
            update(GenerationRecord)
            .where(
                GenerationRecord.id == record_id,
                GenerationRecord.consumed_at.is_(None),
            )
            .values(
                consumed_at=datetime.utcnow(),
                consumed_chapter_id=outline_id,
                consumed_version_id=new_version_id,
                final_content=data.content,
                final_content_length=word_count,
            )
        )
        if consume_result.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "generation_record_already_consumed",
                    "detail": "该生成结果已被其他请求接受，请刷新后重试",
                    "generation_record_id": str(record_id),
                    "chapter_id": str(outline_id),
                },
            )

    # 正文保存后同步搜索索引
    await index_service.sync_chapter_content_index(
        db, project_id, outline_id, outline.title or "", data.content
    )

    # 第十五批事件通知：协作者修改内容 → 通知作者；新版本创建 → 通知协作者成员
    if project.user_id != current_user.id:
        await event_service.emit_content_updated(
            db, project, current_user,
            target_type="chapter",
            target_id=str(outline_id),
            payload={"version": next_version, "chapter_id": str(outline_id)},
        )
    await event_service.emit_version_created(
        db, project, current_user,
        version=next_version,
        chapter_id=str(outline_id),
    )

    # 第二十一批：原文更新后自动把该章节译文标记 outdated（提示重新翻译/校对）
    await translation_service.on_source_content_updated(db, outline_id)

    await db.commit()
    await db.refresh(chapter_content)

    # 第二/三/四期 AI 增强链路（章节记忆/状态提取/一致性检查）改为后台执行，
    # 避免同步等待 AI 拖慢保存接口导致前端请求超时（各 trigger 自带开关与异常兜底）
    asyncio.create_task(
        _run_post_save_ai_triggers(
            project_id, outline_id, outline.title or "", data.content
        )
    )

    # 触发链路内部可能 commit 使 ORM 对象过期；返回前重新加载，避免响应序列化触发异步 IO（MissingGreenlet）
    try:
        await db.refresh(chapter_content)
    except Exception:  # noqa: BLE001
        pass

    # 第一阶段「生命周期与可追溯元数据」：保存后统一收尾——
    # 生成元数据（generation_meta/outline_version_id）落库、确定性校验
    # （复用 fact_validator，blocking→validation_failed / warning→pending_review）、
    # validation_summary / validation_updated_at 真实落库（修复 R4）、
    # 后续章节 needs_recheck 标记（编辑/重写/续写后既有事实可能失效）。
    # 校验失败不阻断正文保存，但 lifecycle_status 会阻止 approve/publish。
    record = await get_effective_generation_record(
        db, project_id, data.generation_record_id
    )
    try:
        await after_content_saved(
            db,
            outline,
            chapter_content,
            record=record,
            audit_action=_audit_service.ACTION_CONTENT_SAVE,
            audit_source=data.source or ("ai" if record is not None else "manual"),
            audit_before=content_state_before,
            audit_actor_id=current_user.id,
            audit_actor_type="user",
            audit_operation="update_chapter_content",
            audit_metadata={
                "chapter_version": next_version,
                "previous_version": chapter_content.version - 1,
                "change_summary": data.change_summary,
                "request_source": "PUT /chapter-contents/{outline_id}",
            },
        )
    except Exception:  # noqa: BLE001
        # 生命周期收尾失败不阻塞正文保存（保持“保存成功、元数据尽力而为”语义）
        logger.exception(
            "lifecycle finalize failed after save for chapter %s", outline_id
        )

    try:
        await db.refresh(chapter_content)
    except Exception:  # noqa: BLE001
        pass

    # 批次 4：作者大幅重写 AI 生成结果时，把原稿/改稿差集沉淀为失败样本
    # （source='edit_diff'），后续生成自动注入教训。失败不阻断保存。
    try:
        if await failure_sample_service.sink_edit_diff_if_rewritten(
            db,
            project_id=project_id,
            outline_id=outline_id,
            new_content=data.content,
        ):
            await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "edit-diff sample sink failed for chapter %s", outline_id
        )

    return chapter_content


@router.get(
    "/{outline_id}/versions",
    response_model=list[ChapterVersionResponse]
)
async def list_chapter_versions(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """章节版本历史列表，按版本号降序返回（新版本在前）。"""
    outline = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    result = await db.execute(
        select(ChapterVersion)
        .where(ChapterVersion.outline_id == outline_id)
        .order_by(ChapterVersion.version.desc())
    )

    return list(result.scalars().all())


@router.post(
    "/{outline_id}/versions/{version_id}/restore",
    response_model=ChapterContentResponse
)
async def restore_chapter_version(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    version_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """恢复历史版本（第一期「生成不丢失」规格）。

    关键规则：不覆盖旧版本，而是基于目标版本创建新的 restore 版本，
    历史链不被破坏；新版本置 is_current=True 并更新当前章节正文。
    """
    outline = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    project = await db.get(Project, project_id)
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可恢复版本"
            )

    if outline.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="章节已归档，不允许编辑；请先恢复为草稿"
        )

    target = (
        await db.execute(
            select(ChapterVersion).where(
                ChapterVersion.id == version_id,
                ChapterVersion.outline_id == outline_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="目标版本不存在",
        )

    result = await db.execute(
        select(ChapterContent).where(
            ChapterContent.outline_id == outline_id
        )
    )
    chapter_content = result.scalar_one_or_none()
    if not chapter_content:
        chapter_content = ChapterContent(
            outline_id=outline_id,
            content="",
            word_count=0,
            version=1,
        )
        db.add(chapter_content)
        await db.flush()

    next_version = chapter_content.version + 1

    # 阶段J「内容级审计」：恢复前抓取当前状态，用于版本恢复的内容级追溯。
    from app.services import audit_service as _audit_service

    content_state_before = _audit_service.snapshot_content_state(
        chapter_content, outline
    )

    # 原子认领版本号：条件 UPDATE（WHERE version=读到的 vN）保证并发恢复/保存
    # 只有一个请求胜出，防止产生重复版本号和多个 is_current（与保存路径的 CAS 一致）
    claimed = await db.execute(
        update(ChapterContent)
        .where(
            ChapterContent.outline_id == outline_id,
            ChapterContent.version == chapter_content.version,
        )
        .values(
            version=next_version,
            content=target.content,
            content_bigrams=_bigrams(target.content),
            word_count=target.word_count,
        )
    )
    if claimed.rowcount != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="正文在别处已被更新，请刷新后重试恢复",
        )

    # 旧 current 降级，新建 restore 版本并置为 current
    await db.execute(
        update(ChapterVersion)
        .where(
            ChapterVersion.outline_id == outline_id,
            ChapterVersion.is_current.is_(True),
        )
        .values(is_current=False)
    )

    version = ChapterVersion(
        outline_id=outline_id,
        version=next_version,
        content=target.content,
        word_count=target.word_count,
        source="restore",
        generation_record_id=None,
        parent_version_id=target.id,
        is_current=True,
        change_summary=f"从版本 v{target.version} 恢复"
    )

    db.add(version)

    # 恢复后同步搜索索引
    await index_service.sync_chapter_content_index(
        db, project_id, outline_id, outline.title or "", target.content
    )

    await db.commit()
    await db.refresh(chapter_content)

    # 第一阶段（E1 治理）：restore 直接改正文，补生命周期收尾——
    # 校验当前恢复结果、落库 validation_summary、把后续章节标记 needs_recheck
    # （恢复的旧正文可能让既有事实/记忆失效）。
    try:
        await after_content_saved(
            db,
            outline,
            chapter_content,
            record=None,
            audit_action=_audit_service.ACTION_CONTENT_RESTORE,
            audit_source="manual",
            audit_before=content_state_before,
            audit_actor_id=current_user.id,
            audit_actor_type="user",
            audit_operation="restore_chapter_version",
            audit_reason=f"从版本 v{target.version} 恢复",
            audit_metadata={
                "chapter_version": next_version,
                "restored_from_version": target.version,
                "restored_from_version_id": str(target.id),
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "lifecycle finalize failed after restore for chapter %s", outline_id
        )

    try:
        await db.refresh(chapter_content)
    except Exception:  # noqa: BLE001
        pass

    return chapter_content


@router.post(
    "/{outline_id}/ai/continue",
    response_model=WritingPreviewResponse
)
async def continue_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: ContinueWritingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """AI 续写：基于章节大纲与已有正文末尾，生成续写内容预览。

    第一期「生成不丢失」：调用成功/失败均先写 generation_record，
    响应携带 generation_record_id 供前端「接受为新版本」时回传；预览不覆盖正文。
    """
    chapter = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    # 第二期「章节能被记住」：续写上下文由 Context Builder 按优先级组装
    # （写作规范→卷目标/细纲→场景→剧情承接摘要→人物状态→地点/势力/时间线→伏笔约束→前章末尾→用户指令）
    context = await build_continuation_context(
        db,
        project_id,
        outline_id,
        max_length=data.max_length,
        instruction=data.instruction or "",
    )
    system_prompt = context["system_prompt"]
    user_prompt = context["user_prompt"]
    context_snapshot = generation_service.serialize_context(
        context["snapshot"]
    )

    provider, model_name = generation_service.resolve_ai_provider()
    # 第六阶段：请求级模型覆盖（前端可选 provider/model/温度/max_tokens）
    override = _build_ai_override(data)
    if override.get("model_name"):
        model_name = override["model_name"]
    try:
        content = await chat_with_ai(
            system_prompt,
            user_prompt,
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        # 正文类记录按 finish_reason 落库：触达 max_tokens 记为 incomplete，否则 completed。
        # take_last_ai_meta 会清空 contextvar，所以 finish_reason 必须显式回传。
        finish_reason, _prompt_tokens, _completion_tokens = take_last_ai_meta()
        body_status = (
            "incomplete"
            if (finish_reason or "").lower() == "length"
            else "completed"
        )
        record = await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="continue",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            response_content=content,
            # 这三个入口不做二次清洗：模型返回什么，就是 raw/processed。
            # final_content 只在用户真正接受时写入（见保存端点消费标记）。
            raw_content=content,
            processed_content=content,
            raw_content_length=len(
                content.replace(" ", "").replace("\n", "")
            ),
            finish_reason=finish_reason,
            attribution_status="project_bound",
            status=body_status,
        )
    except RuntimeError as error:
        await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="continue",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            attribution_status="project_bound",
            status="failed",
            error_message=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="生成记录写入失败"
        )

    return WritingPreviewResponse(
        content=content,
        generation_record_id=record.id
    )


@router.post(
    "/{outline_id}/ai/rewrite",
    response_model=WritingPreviewResponse
)
async def rewrite_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: RewriteWritingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """AI 改写：按用户要求重写原文，返回预览。

    第一期「生成不丢失」：调用成功/失败均先写 generation_record，
    响应携带 generation_record_id；预览不覆盖正文。
    """
    chapter = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    # 第二期「章节能被记住」：改写同样携带 Context Builder 组装的前情上下文（承接摘要/人物状态/伏笔约束）
    context = await build_continuation_context(
        db,
        project_id,
        outline_id,
        max_length=len(data.content or "") or 800,
        instruction=data.instruction or "",
    )
    system_prompt = (
        "你是资深网络小说写作助手。基于作品写作规范、人物状态、伏笔约束与前情承接摘要，"
        "按用户要求重写本章原文。\n"
        "要求：\n"
        "1. 严格遵循 user 消息中【当前章节计划（最高优先级约束）】只重写当前章，"
        "不得写入后续章节才应发生的剧情，不得提前揭露 forbiddenReveals 内容。\n"
        "2. 保持人物性格、叙事视角和时态一致，不违背已确认人物状态与伏笔约束。\n"
        "3. 保留原文关键情节和设定。\n"
        "4. 严格按照用户的改写指令调整表达方式与节奏。\n"
        "5. 只输出改写后的正文，不要添加说明。"
    )
    user_prompt = (
        context["user_prompt"]
        + "\n【需要改写的本章原文】\n"
        + (data.content or "")
        + "\n\n【改写要求】\n"
        + (data.instruction or "无")
        + "\n\n请只输出改写后的完整正文。"
    )
    context_snapshot = generation_service.serialize_context({
        **context["snapshot"],
        "action": "rewrite",
        "source_content": data.content,
        "instruction": data.instruction,
    })

    provider, model_name = generation_service.resolve_ai_provider()
    # 第六阶段：请求级模型覆盖
    override = _build_ai_override(data)
    if override.get("model_name"):
        model_name = override["model_name"]
    try:
        content = await chat_with_ai(
            system_prompt,
            user_prompt,
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        # 正文类记录按 finish_reason 落库：触达 max_tokens 记为 incomplete，否则 completed。
        # take_last_ai_meta 会清空 contextvar，所以 finish_reason 必须显式回传。
        finish_reason, _prompt_tokens, _completion_tokens = take_last_ai_meta()
        body_status = (
            "incomplete"
            if (finish_reason or "").lower() == "length"
            else "completed"
        )
        record = await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="rewrite",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            response_content=content,
            # 这三个入口不做二次清洗：模型返回什么，就是 raw/processed。
            # final_content 只在用户真正接受时写入（见保存端点消费标记）。
            raw_content=content,
            processed_content=content,
            raw_content_length=len(
                content.replace(" ", "").replace("\n", "")
            ),
            finish_reason=finish_reason,
            attribution_status="project_bound",
            status=body_status,
        )
    except RuntimeError as error:
        await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="rewrite",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            attribution_status="project_bound",
            status="failed",
            error_message=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="生成记录写入失败"
        )

    return WritingPreviewResponse(
        content=content,
        generation_record_id=record.id
    )


@router.post(
    "/{outline_id}/ai/polish",
    response_model=WritingPreviewResponse
)
async def polish_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: PolishWritingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """AI 润色：按目标风格润色原文，返回预览。

    第一期「生成不丢失」：调用成功/失败均先写 generation_record，
    响应携带 generation_record_id；预览不覆盖正文。
    """
    chapter = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    # 第二期「章节能被记住」：润色同样携带 Context Builder 组装的前情上下文
    context = await build_continuation_context(
        db,
        project_id,
        outline_id,
        max_length=len(data.content or "") or 800,
        instruction=data.style or "语言流畅、表达精准",
    )
    system_prompt = (
        "你是资深网络小说写作助手。基于作品写作规范、人物状态、伏笔约束与前情承接摘要，"
        "对本章原文进行润色。\n"
        "要求：\n"
        "1. 严格遵循 user 消息中【当前章节计划（最高优先级约束）】只润色当前章，"
        "不得引入后续章节才应发生的剧情，不得提前揭露 forbiddenReveals 内容。\n"
        "2. 保持人物性格、叙事视角和时态一致，不违背已确认人物状态与伏笔约束。\n"
        "3. 保留原文内容与情节，只优化语言表达。\n"
        "4. 严格按照用户指定的风格润色。\n"
        "5. 只输出润色后的正文，不要添加说明。"
    )
    user_prompt = (
        context["user_prompt"]
        + "\n【需要润色的本章原文】\n"
        + (data.content or "")
        + "\n\n【目标风格】\n"
        + (data.style or "语言流畅、表达精准")
        + "\n\n请只输出润色后的完整正文。"
    )
    context_snapshot = generation_service.serialize_context({
        **context["snapshot"],
        "action": "polish",
        "source_content": data.content,
        "style": data.style,
    })

    provider, model_name = generation_service.resolve_ai_provider()
    # 第六阶段：请求级模型覆盖
    override = _build_ai_override(data)
    if override.get("model_name"):
        model_name = override["model_name"]
    try:
        content = await chat_with_ai(
            system_prompt,
            user_prompt,
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        # 正文类记录按 finish_reason 落库：触达 max_tokens 记为 incomplete，否则 completed。
        # take_last_ai_meta 会清空 contextvar，所以 finish_reason 必须显式回传。
        finish_reason, _prompt_tokens, _completion_tokens = take_last_ai_meta()
        body_status = (
            "incomplete"
            if (finish_reason or "").lower() == "length"
            else "completed"
        )
        record = await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="polish",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            response_content=content,
            # 这三个入口不做二次清洗：模型返回什么，就是 raw/processed。
            # final_content 只在用户真正接受时写入（见保存端点消费标记）。
            raw_content=content,
            processed_content=content,
            raw_content_length=len(
                content.replace(" ", "").replace("\n", "")
            ),
            finish_reason=finish_reason,
            attribution_status="project_bound",
            status=body_status,
        )
    except RuntimeError as error:
        await generation_service.record_generation(
            db,
            project_id=project_id,
            chapter_id=chapter.id,
            action_type="polish",
            model_provider=provider,
            model_name=model_name,
            temperature=override.get("temperature"),
            max_tokens=override.get("max_tokens"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=context_snapshot,
            attribution_status="project_bound",
            status="failed",
            error_message=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error)
        )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="生成记录写入失败"
        )

    return WritingPreviewResponse(
        content=content,
        generation_record_id=record.id
    )


@router.post(
    "/{outline_id}/ai/plan-compliance",
    response_model=PlanComplianceResponse
)
async def check_plan_compliance(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlanComplianceResponse:
    """计划符合度检查（章节计划强约束配套）。

    对本章当前正文执行本地规则检查：标题/核心情境体现、requiredEvents 覆盖、
    expectedOutcome 达成、forbiddenReveals 是否违反、是否推进到后续章节剧情。
    不修改正文，只返回结构化问题供「按计划修订/重新生成」参考；
    检查结果自动落一条 generation_record（action_type=plan_check），便于追溯。
    """
    from datetime import datetime, timezone

    chapter = await get_chapter(
        project_id,
        outline_id,
        current_user,
        db
    )

    result = await plan_compliance_of(db, project_id, outline_id, chapter.content or "")

    plan_summary = {
        "title": (result["plan"] or {}).get("title"),
        "summary": (result["plan"] or {}).get("summary"),
        "requiredEvents": (result["plan"] or {}).get("requiredEvents") or [],
        "expectedOutcome": (result["plan"] or {}).get("expectedOutcome"),
        "forbiddenReveals": (result["plan"] or {}).get("forbiddenReveals") or [],
        "followingChapters": [
            {
                "chapterNumber": f.get("chapterNumber"),
                "title": f.get("title"),
            }
            for f in result["following_plans"]
        ],
    }

    # 落库 plan_check 记录（不调用 AI；response_content 存检查结论 JSON）
    record_payload = {
        "score": result["score"],
        "issues": [
            {
                "severity": i.severity,
                "type": i.type,
                "message": i.message,
                "evidence": i.evidence,
                "suggestion": i.suggestion,
            }
            for i in result["issues"]
        ],
    }
    await generation_service.record_generation(
        db,
        project_id=project_id,
        chapter_id=chapter.id,
        action_type="plan_check",
        attribution_status="project_bound",
        model_provider="",
        model_name="",
        system_prompt="",
        user_prompt="",
        context_snapshot=generation_service.serialize_context(
            {
                "plan": result["plan"],
                "plan_source": result.get("plan_source", "none"),
                "following_plans": result["following_plans"],
                "checked_content_length": len(chapter.content or ""),
            }
        ),
        response_content=json.dumps(record_payload, ensure_ascii=False),
        status="succeeded",
    )

    return PlanComplianceResponse(
        projectId=project_id,
        chapterId=outline_id,
        chapterNumber=await _chapter_number_of(db, project_id, chapter),
        title=chapter.title or "",
        score=result["score"],
        issues=result["issues"],
        planSummary=plan_summary,
        checkedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


async def _chapter_number_of(
    db: AsyncSession, project_id: uuid.UUID, chapter: Outline
) -> int:
    """当前章在项目内 chapter 列表中的 1-based 序号（与 Context Builder 一致）。"""
    from app.services.chapter_plan_service import chapter_number_of

    return await chapter_number_of(db, project_id, chapter)


# ============ 第一阶段：生命周期最小状态转换（approve / publish） ============


@router.post(
    "/{outline_id}/lifecycle/approve",
    response_model=LifecycleResponse,
)
async def approve_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """审批通过（最小状态转换）：lifecycle_status → approved。

    有 blocking 校验问题（validation_failed / validation_report.blocking）
    时拒绝审批，仅作状态阻断与可审计记录，不实现全自动 AI 重写。
    """
    from app.services.chapter_lifecycle import approve_chapter as _approve
    from app.services.chapter_lifecycle import LifecycleRejected

    chapter = await get_chapter(project_id, outline_id, current_user, db)
    project = await db.get(Project, project_id)
    if project is not None and project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可审批章节"
            )
    outline = await db.get(Outline, outline_id)
    if outline is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    try:
        result = await _approve(db, outline)
    except LifecycleRejected as exc:
        # A2：纯门禁拒绝的审计行只在 audit_pending 时由路由提交一次；
        # blocking 拒绝已随 validation_failed 在 service 内提交，这里不得重复提交。
        # U1a 补正：审计提交失败只降级为「审计未持久化」，不得把 409 变成 500。
        if exc.audit_pending:
            from app.services import audit_service

            await audit_service.commit_pending_rejection_audit(db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await db.commit()
    return LifecycleResponse(
        project_id=project_id,
        chapter_id=outline_id,
        lifecycle_status=result["lifecycle_status"],
        approved=result["approved"],
        blocking=result["blocking"],
        message=result["message"],
    )


@router.post(
    "/{outline_id}/lifecycle/publish",
    response_model=LifecycleResponse,
)
async def publish_chapter(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """发布（最小状态转换）：lifecycle_status → published。

    同样受确定性校验阻断：存在 blocking 问题时不批准发布。
    """
    from app.services.chapter_lifecycle import LifecycleRejected
    from app.services.publish_orchestrator import (
        GATE_PROFILE_FULL,
        PublishCommand,
        publish_chapter as _orchestrated_publish,
    )

    chapter = await get_chapter(project_id, outline_id, current_user, db)
    outline = await db.get(Outline, outline_id)
    if outline is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可发布章节"
            )
    try:
        # U2a（D-U2A-1/2/3）：E3 统一经编排器发布——保留正文/报告/锚点/blocking
        # 全量门禁，同时把 Publication 快照、发布三列与 lifecycle_publish 审计
        # 收敛进同一次业务提交，消除「生命周期已发布但无 Publication」的半发布状态。
        # 路由不再直接调用 生命周期发布服务旧入口，也不再自行提交。
        outcome = await _orchestrated_publish(
            db,
            PublishCommand(
                project=project,
                outline=outline,
                write_snapshot=True,
                gate_profile=GATE_PROFILE_FULL,
            ),
        )
    except LifecycleRejected as exc:
        # A2：与 approve 同一控制流——纯门禁拒绝仅在 audit_pending 时提交一次；
        # blocking 拒绝的 validation_failed 与拒绝审计也由这一次提交同事务落库。
        # U1a 补正：审计提交失败只降级为「审计未持久化」，不得把 409 变成 500。
        if exc.audit_pending:
            from app.services import audit_service

            await audit_service.commit_pending_rejection_audit(db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    # U2a：业务事务已由编排器唯一一次 commit 收口，路由不得二次提交。
    return LifecycleResponse(
        project_id=project_id,
        chapter_id=outline_id,
        lifecycle_status=outcome.lifecycle_status,
        approved=True,
        blocking=0,
        message=outcome.message,
    )
