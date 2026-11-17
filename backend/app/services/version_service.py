# 版本管理与差异对比服务（第十二批规格实现）
# 统一 ContentVersion：大纲 / 章节正文 / 知识库资料快照、草稿与正式版本、
# 恢复旧版本（不覆盖旧号）、正文段落差异、大纲结构差异、AI 候选版本、
# 版本锁定（最新版本承载 locked）、版本清理策略。
# 第一阶段（E1 治理）：ai_replace / ai_insert / ai_apply_blocks 直接改正文后
# 补生命周期收尾（确定性校验 + 元数据 + 后续章节 needs_recheck）。
import difflib
import json
import logging
import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.character import Character
from app.models.content_version import (
    CHANGE_TYPES,
    ContentVersion,
    SOURCES,
    TARGET_TYPES,
)
from app.models.outline import Outline
from app.models.world_setting import WorldSetting
from app.services import audit_service
from app.services.chapter_lifecycle import after_content_saved

logger = logging.getLogger(__name__)

# 目标模型字段（快照使用）
_CHARACTER_FIELDS = [
    "identity",
    "personality",
    "appearance",
    "background",
    "goal",
    "conflict",
    "relationship",
    "character_status",
]

_HTTP_404 = status.HTTP_404_NOT_FOUND
_HTTP_400 = status.HTTP_400_BAD_REQUEST
_HTTP_403 = status.HTTP_403_FORBIDDEN


# ============ 目标加载与校验（三重权限：Project 归属 + target 归属） ============

async def get_target(db: AsyncSession, project_id, target_type: str, target_id):
    """加载目标内容并校验 target.project_id == project_id。

    chapter 返回 (ChapterContent, Outline) 二元组；其余返回模型实例。
    """
    if target_type not in TARGET_TYPES:
        raise HTTPException(status_code=_HTTP_400, detail="不支持的目标类型")

    if target_type == "outline":
        target = await db.get(Outline, target_id)
        if not target or target.project_id != project_id:
            raise HTTPException(status_code=_HTTP_404, detail="目标内容不存在")
        return target

    if target_type == "chapter":
        cc = await db.get(ChapterContent, target_id)
        if not cc:
            raise HTTPException(status_code=_HTTP_404, detail="章节正文不存在")
        outline = await db.get(Outline, cc.outline_id)
        if not outline or outline.project_id != project_id:
            raise HTTPException(status_code=_HTTP_404, detail="章节正文不存在")
        return cc, outline

    # knowledge_item：人物资料 / 世界观资料
    ch = await db.get(Character, target_id)
    if ch and ch.project_id == project_id:
        return ch
    ws = await db.get(WorldSetting, target_id)
    if ws and ws.project_id == project_id:
        return ws
    raise HTTPException(status_code=_HTTP_404, detail="知识库资料不存在")


# ============ 快照组装 / 回写 ============

def _word_count(text: str) -> int:
    return len("".join(text.split()))


def build_snapshot(target_type: str, target):
    """组装 (title_snapshot, content_snapshot_json, metadata_snapshot_json)。"""
    if target_type == "outline":
        title = target.title
        content = json.dumps(
            {"summary": target.summary, "content": target.content},
            ensure_ascii=False,
        )
        metadata = json.dumps(
            {
                "outline_type": target.outline_type,
                "parent_id": str(target.parent_id) if target.parent_id else None,
                "sort_order": target.sort_order,
                "status": target.status,
            },
            ensure_ascii=False,
        )
        return title, content, metadata

    if target_type == "chapter":
        cc, outline = target
        title = outline.title
        content = json.dumps({"content": cc.content}, ensure_ascii=False)
        metadata = json.dumps(
            {
                "outline_id": str(cc.outline_id),
                "word_count": cc.word_count,
                "version": cc.version,
            },
            ensure_ascii=False,
        )
        return title, content, metadata

    # knowledge_item
    if isinstance(target, Character):
        title = target.name
        content = json.dumps(
            {f: getattr(target, f) for f in _CHARACTER_FIELDS},
            ensure_ascii=False,
        )
        metadata = json.dumps({"kind": "character"}, ensure_ascii=False)
        return title, content, metadata

    title = target.name
    content = json.dumps({"content": target.content}, ensure_ascii=False)
    metadata = json.dumps(
        {"kind": "world_setting", "category": target.category, "importance": target.importance},
        ensure_ascii=False,
    )
    return title, content, metadata


def apply_snapshot(target_type: str, target, version: ContentVersion):
    """把版本快照回写到目标当前内容（恢复旧版本时使用）。"""
    content = json.loads(version.content_snapshot or "{}")
    metadata = json.loads(version.metadata_snapshot or "{}")

    if target_type == "outline":
        target.title = version.title_snapshot or target.title
        target.summary = content.get("summary")
        target.content = content.get("content")
        target.outline_type = metadata.get("outline_type", target.outline_type)
        parent_raw = metadata.get("parent_id")
        target.parent_id = uuid.UUID(parent_raw) if parent_raw else None
        target.sort_order = int(metadata.get("sort_order", target.sort_order))
        target.status = metadata.get("status", target.status)
        return

    if target_type == "chapter":
        cc, _outline = target
        cc.content = content.get("content", "")
        cc.word_count = _word_count(cc.content)
        cc.version += 1
        return

    if isinstance(target, Character):
        target.name = version.title_snapshot or target.name
        for f in _CHARACTER_FIELDS:
            setattr(target, f, content.get(f))
        return

    target.name = version.title_snapshot or target.name
    target.content = content.get("content", "")
    target.category = metadata.get("category", target.category)
    target.importance = metadata.get("importance", target.importance)


def apply_ai_content(target_type: str, target, candidate_content: str):
    """把 AI 候选内容应用到目标当前内容（accept / replace / insert / apply_blocks）。"""
    if target_type == "chapter":
        cc, _outline = target
        cc.content = candidate_content
        cc.word_count = _word_count(candidate_content)
        cc.version += 1
        return

    if target_type == "outline":
        target.content = candidate_content
        return

    if isinstance(target, Character):
        target.background = candidate_content
    else:
        target.content = candidate_content


async def _finalize_chapter_lifecycle(
    db: AsyncSession,
    target_type: str,
    target,
    *,
    operation: str | None = None,
    actor_id=None,
    before_state: dict | None = None,
    metadata: dict | None = None,
) -> None:
    """AI 覆盖/插入/逐段采纳章节正文后，补生命周期收尾（E1 治理）。

    直接改正文的路径原本不触发 _run_post_save_ai_triggers，本函数至少保证：
    确定性校验落库 validation_report/validation_summary、生命周期状态更新、
    后续章节标记 needs_recheck。后台事实/记忆重算交由既有 trigger 链路。

    阶段J：同时写入内容级审计（action=ai_apply，含字段级 diff），
    before_state 由调用方在改正文前用 audit_service.snapshot_content_state 抓取。
    """
    if target_type != "chapter":
        return
    cc, outline = target
    try:
        await after_content_saved(
            db,
            outline,
            cc,
            record=None,
            audit_action=audit_service.ACTION_AI_APPLY,
            audit_source="ai",
            audit_before=before_state,
            audit_actor_id=actor_id,
            audit_actor_type="user",
            audit_operation=operation or "ai_apply",
            audit_metadata=metadata,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "lifecycle finalize failed after ai edit for chapter %s", cc.outline_id
        )


def _snapshot_for_audit(target_type: str, target) -> dict | None:
    """AI 改动章节正文前的只读快照（非章节目标返回 None，不参与内容审计）。"""
    if target_type != "chapter" or target is None:
        return None
    cc, outline = target
    return audit_service.snapshot_content_state(cc, outline)


# ============ 版本记录创建 ============

async def next_version_number(
    db: AsyncSession, project_id, target_type: str, target_id
) -> int:
    cur = (
        await db.execute(
            select(func.max(ContentVersion.version_number)).where(
                ContentVersion.project_id == project_id,
                ContentVersion.target_type == target_type,
                ContentVersion.target_id == target_id,
            )
        )
    ).scalar()
    return (cur or 0) + 1


async def get_latest_version(
    db: AsyncSession, project_id, target_type: str, target_id
) -> ContentVersion | None:
    return (
        await db.execute(
            select(ContentVersion)
            .where(
                ContentVersion.project_id == project_id,
                ContentVersion.target_type == target_type,
                ContentVersion.target_id == target_id,
            )
            .order_by(ContentVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def is_target_locked(
    db: AsyncSession, project_id, target_type: str, target_id
) -> bool:
    latest = await get_latest_version(db, project_id, target_type, target_id)
    return bool(latest and latest.locked)


async def create_version_record(
    db: AsyncSession,
    *,
    project_id,
    target_type: str,
    target_id,
    target,
    change_type: str,
    source: str,
    change_summary: str | None,
    created_by,
    is_draft: bool = False,
    content_snapshot: str | None = None,
    title_snapshot: str | None = None,
    metadata_snapshot: str | None = None,
    generation_id=None,
    parent_version_id=None,
    candidate_content: str | None = None,
    locked: bool | None = None,
) -> ContentVersion:
    """创建一条 ContentVersion 记录（version_number 自动递增、created_by 服务端生成）。

    locked 未显式传入时继承当前目标最新版本的锁定状态。
    """
    if content_snapshot is None or title_snapshot is None or metadata_snapshot is None:
        title_snapshot, content_snapshot, metadata_snapshot = build_snapshot(
            target_type, target
        )

    vnum = await next_version_number(db, project_id, target_type, target_id)
    latest = await get_latest_version(db, project_id, target_type, target_id)
    if locked is None:
        locked = bool(latest and latest.locked)

    version = ContentVersion(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        version_number=vnum,
        title_snapshot=title_snapshot,
        content_snapshot=content_snapshot,
        metadata_snapshot=metadata_snapshot,
        change_type=change_type,
        change_summary=change_summary,
        source=source,
        status="draft" if is_draft else "stable",
        created_by=created_by,
        generation_id=generation_id,
        parent_version_id=parent_version_id,
        candidate_content=candidate_content,
        locked=locked,
    )
    db.add(version)
    await db.flush()
    return version


def _validate_source(source: str):
    if source not in SOURCES:
        raise HTTPException(status_code=_HTTP_400, detail="不支持的版本来源")


def _validate_change_type(change_type: str):
    if change_type not in CHANGE_TYPES:
        raise HTTPException(status_code=_HTTP_400, detail="不支持的变更类型")


# ============ 列表 / 详情 ============

async def list_versions(
    db: AsyncSession,
    project_id,
    *,
    target_type: str | None = None,
    target_id=None,
    source: str | None = None,
    created_by=None,
    from_dt=None,
    to_dt=None,
    page: int = 1,
    page_size: int = 20,
    include_drafts: bool = False,
    per_target: bool = False,
):
    if target_type is not None and target_type not in TARGET_TYPES:
        raise HTTPException(status_code=_HTTP_400, detail="不支持的目标类型")
    if source is not None:
        _validate_source(source)
    if created_by is not None:
        try:
            created_by = uuid.UUID(str(created_by))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=_HTTP_400, detail="created_by 格式错误")

    conditions = [ContentVersion.project_id == project_id]
    if target_type:
        conditions.append(ContentVersion.target_type == target_type)
    if target_id:
        conditions.append(ContentVersion.target_id == target_id)
    if source:
        conditions.append(ContentVersion.source == source)
    if created_by:
        conditions.append(ContentVersion.created_by == created_by)
    if from_dt:
        conditions.append(ContentVersion.created_at >= from_dt)
    if to_dt:
        conditions.append(ContentVersion.created_at <= to_dt)
    if not include_drafts:
        conditions.append(ContentVersion.status != "draft")

    total = (
        await db.execute(
            select(func.count()).select_from(ContentVersion).where(*conditions)
        )
    ).scalar() or 0

    order = (
        ContentVersion.version_number.desc()
        if per_target
        else ContentVersion.created_at.desc()
    )
    rows = (
        await db.execute(
            select(ContentVersion)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return rows, total


async def get_version(
    db: AsyncSession, project_id, target_type: str, target_id, version_id
) -> ContentVersion:
    version = await db.get(ContentVersion, version_id)
    if (
        not version
        or version.project_id != project_id
        or version.target_type != target_type
        or version.target_id != target_id
    ):
        raise HTTPException(status_code=_HTTP_404, detail="版本不存在")
    return version


# ============ 版本恢复 ============

async def restore_version(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    current_user,
) -> dict:
    """恢复旧版本：先保存当前版本（backup）→ 应用旧快照 → 创建新版本（不覆盖旧号）。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await get_version(db, project_id, target_type, target_id, version_id)

    # 1. 保存当前内容为新版本（恢复前自动备份）
    backup = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="update",
        source="restore",
        change_summary="恢复前自动保存当前内容",
        created_by=current_user.id,
        is_draft=False,
    )

    # 2. 把旧版本快照回写到目标当前内容
    # 阶段J：回写前抓取 before 快照（供内容级审计做字段级 diff）
    before_state = _snapshot_for_audit(target_type, target)
    apply_snapshot(target_type, target, version)

    # 3. 使用旧版本内容创建新版本（版本历史保持连续，可再次撤销恢复）
    new_version = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="restore",
        source="restore",
        change_summary=f"从版本 {version.version_number} 恢复",
        created_by=current_user.id,
        is_draft=False,
        content_snapshot=version.content_snapshot,
        title_snapshot=version.title_snapshot,
        metadata_snapshot=version.metadata_snapshot,
    )

    # 阶段J「内容级审计」：版本恢复改动了正文，记录内容级追溯（action=content_restore）。
    if target_type == "chapter":
        try:
            cc, outline = target
            await audit_service.record_content_change(
                db,
                outline=outline,
                chapter_content=cc,
                action=audit_service.ACTION_CONTENT_RESTORE,
                source="manual",
                actor_id=current_user.id,
                actor_type="user",
                before=before_state,
                operation="restore_version",
                reason=f"从版本 {version.version_number} 恢复",
                metadata={
                    "restored_from_version_id": str(version.id),
                    "restored_from_version": version.version_number,
                    "backup_version_id": str(backup.id),
                    "new_version_id": str(new_version.id),
                },
            )
        except Exception:  # noqa: BLE001 — 审计失败不影响恢复动作
            logger.exception("content audit failed after restore_version for %s", target_id)

    return {
        "restored": True,
        "new_version_id": new_version.id,
        "restored_from": version.id,
        "backup_version_id": backup.id,
    }


# ============ 差异对比 ============

def _paragraph_diff(base_text: str, compare_text: str) -> list[dict]:
    a = base_text.split("\n")
    b = compare_text.split("\n")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in a[i1:i2]:
                blocks.append({"type": "unchanged", "text": line, "new_text": None})
        elif tag == "delete":
            for line in a[i1:i2]:
                blocks.append({"type": "removed", "text": line, "new_text": None})
        elif tag == "insert":
            for line in b[j1:j2]:
                blocks.append({"type": "added", "text": line, "new_text": None})
        elif tag == "replace":
            for idx, line in enumerate(a[i1:i2]):
                new_line = b[j1 + idx] if (j1 + idx) < j2 else None
                blocks.append({
                    "type": "modified",
                    "text": line,
                    "new_text": new_line,
                })
    return blocks


def _outline_diff(base: ContentVersion, compare: ContentVersion) -> dict:
    base_content = json.loads(base.content_snapshot or "{}")
    comp_content = json.loads(compare.content_snapshot or "{}")
    base_meta = json.loads(base.metadata_snapshot or "{}")
    comp_meta = json.loads(compare.metadata_snapshot or "{}")

    structure_changes: list[dict] = []
    field_changes: list[dict] = []
    content_changes: list[dict] = []

    bp = base_meta.get("parent_id")
    cp = comp_meta.get("parent_id")
    if bp != cp:
        structure_changes.append({
            "change": "parent_changed",
            "field": "parent_id",
            "old": bp,
            "new": cp,
        })
    if base_meta.get("sort_order") != comp_meta.get("sort_order"):
        structure_changes.append({
            "change": "order_changed",
            "field": "sort_order",
            "old": str(base_meta.get("sort_order")),
            "new": str(comp_meta.get("sort_order")),
        })
    if base_meta.get("outline_type") != comp_meta.get("outline_type"):
        structure_changes.append({
            "change": "type_changed",
            "field": "outline_type",
            "old": base_meta.get("outline_type"),
            "new": comp_meta.get("outline_type"),
        })

    if base.title_snapshot != compare.title_snapshot:
        field_changes.append({
            "field": "title",
            "old": base.title_snapshot,
            "new": compare.title_snapshot,
        })
    if base_content.get("summary") != comp_content.get("summary"):
        field_changes.append({
            "field": "summary",
            "old": base_content.get("summary"),
            "new": comp_content.get("summary"),
        })
    if base_meta.get("status") != comp_meta.get("status"):
        field_changes.append({
            "field": "status",
            "old": base_meta.get("status"),
            "new": comp_meta.get("status"),
        })

    if base_content.get("content") != comp_content.get("content"):
        content_changes.append({
            "field": "content",
            "old": base_content.get("content"),
            "new": comp_content.get("content"),
        })

    return {
        "structure_changes": structure_changes,
        "field_changes": field_changes,
        "content_changes": content_changes,
    }


def _knowledge_diff(base: ContentVersion, compare: ContentVersion) -> list[dict]:
    base_content = json.loads(base.content_snapshot or "{}")
    comp_content = json.loads(compare.content_snapshot or "{}")
    base_meta = json.loads(base.metadata_snapshot or "{}")
    comp_meta = json.loads(compare.metadata_snapshot or "{}")

    changes: list[dict] = []
    if base.title_snapshot != compare.title_snapshot:
        changes.append({
            "field": "name",
            "old": base.title_snapshot,
            "new": compare.title_snapshot,
        })
    if base_meta.get("kind") == "character":
        for f in _CHARACTER_FIELDS:
            if base_content.get(f) != comp_content.get(f):
                changes.append({"field": f, "old": base_content.get(f), "new": comp_content.get(f)})
    else:
        if base_content.get("content") != comp_content.get("content"):
            changes.append({
                "field": "content",
                "old": base_content.get("content"),
                "new": comp_content.get("content"),
            })
        if base_meta.get("category") != comp_meta.get("category"):
            changes.append({
                "field": "category",
                "old": base_meta.get("category"),
                "new": comp_meta.get("category"),
            })
        if base_meta.get("importance") != comp_meta.get("importance"):
            changes.append({
                "field": "importance",
                "old": base_meta.get("importance"),
                "new": comp_meta.get("importance"),
            })
    return changes


async def diff_versions(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    base_version_id,
    compare_version_id,
) -> dict:
    base = await get_version(db, project_id, target_type, target_id, base_version_id)
    compare = await get_version(db, project_id, target_type, target_id, compare_version_id)
    base_content = json.loads(base.content_snapshot or "{}")
    comp_content = json.loads(compare.content_snapshot or "{}")

    if target_type == "outline":
        result = _outline_diff(base, compare)
        result.update({
            "diff_type": "outline",
            "source_version": base.version_number,
            "target_version": compare.version_number,
        })
        return result

    if target_type == "knowledge_item":
        return {
            "diff_type": "knowledge",
            "source_version": base.version_number,
            "target_version": compare.version_number,
            "field_changes": _knowledge_diff(base, compare),
        }

    return {
        "diff_type": "paragraph",
        "source_version": base.version_number,
        "target_version": compare.version_number,
        "blocks": _paragraph_diff(
            base_content.get("content", ""),
            comp_content.get("content", ""),
        ),
    }


# ============ AI 候选版本 ============

def _check_candidate_editable(version: ContentVersion):
    if version.change_type != "ai_generate":
        raise HTTPException(status_code=_HTTP_400, detail="仅 AI 候选版本支持该操作")
    if not version.candidate_content:
        raise HTTPException(status_code=_HTTP_400, detail="候选内容为空")
    if version.accepted_at is not None or version.rejected_at is not None:
        raise HTTPException(status_code=_HTTP_400, detail="候选版本已处理")


async def create_ai_candidate(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    body,
    current_user,
) -> ContentVersion:
    """AI 生成结果保留为候选版本（change_type=ai_generate, source=ai）。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="ai_generate",
        source="ai",
        change_summary=body.change_summary or "AI 生成候选",
        created_by=current_user.id,
        is_draft=True,
        content_snapshot=json.dumps({"content": body.candidate_content}, ensure_ascii=False),
        title_snapshot=(
            target.title
            if target_type == "outline"
            else (target[1].title if target_type == "chapter" else target.name)
        ),
        metadata_snapshot=json.dumps({"candidate": True}, ensure_ascii=False),
        generation_id=body.generation_id,
        parent_version_id=body.parent_version_id,
        candidate_content=body.candidate_content,
    )
    return version


async def ai_accept(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    current_user,
) -> dict:
    """接受全部：应用候选内容并创建正式版本，标记候选 accepted_at。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await get_version(db, project_id, target_type, target_id, version_id)
    _check_candidate_editable(version)
    if await is_target_locked(db, project_id, target_type, target_id):
        raise HTTPException(
            status_code=_HTTP_403,
            detail="目标内容已锁定，禁止 AI 覆盖；请先解锁或手动创建新版本",
        )

    # 阶段J：改动正文前抓取 before 快照（供内容级审计做字段级 diff）
    before_state = _snapshot_for_audit(target_type, target)

    apply_ai_content(target_type, target, version.candidate_content)
    version.accepted_at = datetime.utcnow()

    new_version = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="ai_generate",
        source="ai",
        change_summary=version.change_summary or "接受 AI 生成结果",
        created_by=current_user.id,
        is_draft=False,
        content_snapshot=json.dumps({"content": version.candidate_content}, ensure_ascii=False),
        title_snapshot=version.title_snapshot,
        metadata_snapshot=version.metadata_snapshot,
        generation_id=version.generation_id,
        parent_version_id=version.id,
    )

    # 阶段J「内容级审计」：接受 AI 候选同样改动了正文，需记录内容级追溯
    # （该路径不经过 after_content_saved，故此处显式记录，action=ai_apply）。
    try:
        await audit_service.record_content_change(
            db,
            outline=(target[1] if target_type == "chapter" else None),
            chapter_content=(target[0] if target_type == "chapter" else None),
            action=audit_service.ACTION_AI_APPLY,
            source="ai",
            actor_id=current_user.id,
            actor_type="user",
            before=before_state,
            operation="ai_accept",
            reason=version.change_summary or "接受 AI 生成结果",
            metadata={
                "candidate_version_id": str(version.id),
                "new_version_id": str(new_version.id),
                "generation_id": str(version.generation_id) if version.generation_id else None,
            },
        )
    except Exception:  # noqa: BLE001 — 审计失败不影响接受动作
        logger.exception("content audit failed after ai_accept for %s", target_id)

    return {
        "status": "accepted",
        "version_id": version.id,
        "accepted_at": version.accepted_at,
        "new_version_id": new_version.id,
    }


async def ai_reject(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    current_user,
) -> dict:
    """拒绝候选：仅标记 rejected_at，不修改目标内容。"""
    version = await get_version(db, project_id, target_type, target_id, version_id)
    _check_candidate_editable(version)
    version.rejected_at = datetime.utcnow()
    return {
        "status": "rejected",
        "version_id": version.id,
        "rejected_at": version.rejected_at,
    }


async def ai_replace(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    current_user,
) -> dict:
    """替换当前正文：直接覆盖（不创建新版本），锁定目标禁止。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await get_version(db, project_id, target_type, target_id, version_id)
    _check_candidate_editable(version)
    if await is_target_locked(db, project_id, target_type, target_id):
        raise HTTPException(
            status_code=_HTTP_403,
            detail="目标内容已锁定，禁止 AI 覆盖；请先解锁或手动创建新版本",
        )

    # 阶段J：改动正文前抓取 before 快照（供内容级审计做字段级 diff）
    before_state = _snapshot_for_audit(target_type, target)

    apply_ai_content(target_type, target, version.candidate_content)

    # 第一阶段（E1 治理）：AI 覆盖正文后补生命周期收尾
    await _finalize_chapter_lifecycle(
        db,
        target_type,
        target,
        operation="ai_replace",
        actor_id=current_user.id,
        before_state=before_state,
        metadata={"candidate_version_id": str(version.id)},
    )

    return {
        "status": "replaced",
        "version_id": version.id,
        "current_version": (
            target[0].version if target_type == "chapter" else None
        ),
    }


async def ai_insert(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    body,
    current_user,
) -> dict:
    """插入到当前位置：候选内容按行插入正文指定锚点段落前/后（仅章节正文）。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await get_version(db, project_id, target_type, target_id, version_id)
    _check_candidate_editable(version)
    if await is_target_locked(db, project_id, target_type, target_id):
        raise HTTPException(
            status_code=_HTTP_403,
            detail="目标内容已锁定，禁止 AI 覆盖；请先解锁或手动创建新版本",
        )
    if target_type != "chapter":
        raise HTTPException(status_code=_HTTP_400, detail="仅章节正文支持插入操作")

    cc, _outline = target
    lines = cc.content.split("\n")
    if body.anchor_index < 0 or body.anchor_index >= len(lines):
        raise HTTPException(status_code=_HTTP_400, detail="锚点段落超出范围")
    if body.position not in ("before", "after"):
        raise HTTPException(status_code=_HTTP_400, detail="position 仅支持 before / after")

    cand_lines = version.candidate_content.split("\n")
    # 阶段J：插入前抓取 before 快照（供内容级审计做字段级 diff）
    before_state = _snapshot_for_audit(target_type, target)
    if body.position == "before":
        new_lines = lines[:body.anchor_index] + cand_lines + lines[body.anchor_index:]
    else:
        new_lines = (
            lines[: body.anchor_index + 1]
            + cand_lines
            + lines[body.anchor_index + 1:]
        )
    cc.content = "\n".join(new_lines)
    cc.word_count = _word_count(cc.content)
    cc.version += 1

    new_version = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="ai_generate",
        source="ai",
        change_summary="AI 插入到当前位置",
        created_by=current_user.id,
        is_draft=False,
        content_snapshot=json.dumps({"content": cc.content}, ensure_ascii=False),
        title_snapshot=version.title_snapshot,
        metadata_snapshot=version.metadata_snapshot,
        generation_id=version.generation_id,
        parent_version_id=version.id,
    )

    # 第一阶段（E1 治理）：AI 插入正文后补生命周期收尾
    await _finalize_chapter_lifecycle(
        db,
        target_type,
        target,
        operation="ai_insert",
        actor_id=current_user.id,
        before_state=before_state,
        metadata={
            "candidate_version_id": str(version.id),
            "new_version_id": str(new_version.id),
            "anchor_index": body.anchor_index,
            "position": body.position,
        },
    )

    return {"status": "inserted", "version_id": version.id, "new_version_id": new_version.id}


async def ai_apply_blocks(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    version_id,
    body,
    current_user,
) -> dict:
    """逐段采纳：对每个差异块选择 采用AI/保留原内容/手动编辑（仅章节正文）。"""
    target = await get_target(db, project_id, target_type, target_id)
    version = await get_version(db, project_id, target_type, target_id, version_id)
    _check_candidate_editable(version)
    if await is_target_locked(db, project_id, target_type, target_id):
        raise HTTPException(
            status_code=_HTTP_403,
            detail="目标内容已锁定，禁止 AI 覆盖；请先解锁或手动创建新版本",
        )
    if target_type != "chapter":
        raise HTTPException(status_code=_HTTP_400, detail="仅章节正文支持逐段采纳")

    cc, _outline = target
    lines = cc.content.split("\n")
    cand_lines = version.candidate_content.split("\n")

    decision_map = {}
    for d in body.decisions:
        if d.paragraph_index < 0 or d.paragraph_index >= len(lines):
            raise HTTPException(status_code=_HTTP_400, detail="段落索引超出范围")
        if d.decision not in ("ai", "keep", "edit"):
            raise HTTPException(status_code=_HTTP_400, detail="decision 仅支持 ai / keep / edit")
        if d.decision == "edit" and d.text is None:
            raise HTTPException(status_code=_HTTP_400, detail="手动编辑需要提供 text")
        decision_map[d.paragraph_index] = d

    new_lines = []
    for i, line in enumerate(lines):
        d = decision_map.get(i)
        if d is None:
            new_lines.append(line)
        elif d.decision == "keep":
            new_lines.append(line)
        elif d.decision == "ai":
            new_lines.append(cand_lines[i] if i < len(cand_lines) else "")
        else:
            new_lines.append(d.text or "")
    # 阶段J：改动正文前抓取 before 快照（供内容级审计做字段级 diff）
    before_state = _snapshot_for_audit(target_type, target)
    cc.content = "\n".join(new_lines)
    cc.word_count = _word_count(cc.content)
    cc.version += 1

    new_version = await create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type="ai_generate",
        source="ai",
        change_summary="逐段采纳 AI 内容",
        created_by=current_user.id,
        is_draft=False,
        content_snapshot=json.dumps({"content": cc.content}, ensure_ascii=False),
        title_snapshot=version.title_snapshot,
        metadata_snapshot=version.metadata_snapshot,
        generation_id=version.generation_id,
        parent_version_id=version.id,
    )

    # 第一阶段（E1 治理）：逐段采纳正文后补生命周期收尾
    await _finalize_chapter_lifecycle(
        db,
        target_type,
        target,
        operation="ai_apply_blocks",
        actor_id=current_user.id,
        before_state=before_state,
        metadata={
            "candidate_version_id": str(version.id),
            "new_version_id": str(new_version.id),
            "decision_count": len(body.decisions),
        },
    )

    return {"status": "applied", "version_id": version.id, "new_version_id": new_version.id}


# ============ 版本锁定 ============

async def set_locked(
    db: AsyncSession,
    project_id,
    target_type: str,
    target_id,
    locked: bool,
    current_user,
) -> dict:
    """设置目标内容锁定状态（写入最新版本 locked 字段；无版本时先创建初始版本）。"""
    target = await get_target(db, project_id, target_type, target_id)
    latest = await get_latest_version(db, project_id, target_type, target_id)

    if latest is None:
        version = await create_version_record(
            db,
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            target=target,
            change_type="create",
            source="manual",
            change_summary="初始化版本并设置锁定",
            created_by=current_user.id,
            is_draft=False,
            locked=locked,
        )
    else:
        latest.locked = locked
        version = latest

    return {
        "target_type": target_type,
        "target_id": target_id,
        "locked": version.locked,
        "version_id": version.id,
    }


# ============ 版本清理策略 ============

async def cleanup_versions(
    db: AsyncSession,
    project_id,
    keep_drafts: int,
    keep_ai_candidates: int,
) -> dict:
    """清理策略（规格十五）：保留所有正式版本；草稿限最近 N 条；AI 候选限最近 N 条。

    按 (target_type, target_id) 分组独立计算保留数量。
    """
    rows = (
        await db.execute(
            select(ContentVersion)
            .where(ContentVersion.project_id == project_id)
            .order_by(ContentVersion.target_type, ContentVersion.target_id, ContentVersion.created_at.desc())
        )
    ).scalars().all()

    groups: dict[tuple, list[ContentVersion]] = {}
    for v in rows:
        groups.setdefault((v.target_type, str(v.target_id)), []).append(v)

    to_delete: list[ContentVersion] = []
    kept_stable = kept_drafts = kept_ai_candidates = 0

    for _key, versions in groups.items():
        drafts = [v for v in versions if v.status == "draft" and v.change_type != "ai_generate"]
        ai_candidates = [v for v in versions if v.status == "draft" and v.change_type == "ai_generate"]
        stable = [v for v in versions if v.status != "draft"]

        kept_stable += len(stable)
        kept_drafts += min(len(drafts), keep_drafts)
        kept_ai_candidates += min(len(ai_candidates), keep_ai_candidates)

        # drafts / ai_candidates 已按 created_at desc 排序
        for v in drafts[keep_drafts:]:
            to_delete.append(v)
        for v in ai_candidates[keep_ai_candidates:]:
            to_delete.append(v)

    deleted_drafts = sum(
        1 for v in to_delete if v.change_type != "ai_generate"
    )
    deleted_ai_candidates = sum(
        1 for v in to_delete if v.change_type == "ai_generate"
    )

    for v in to_delete:
        await db.delete(v)

    return {
        "deleted_drafts": deleted_drafts,
        "deleted_ai_candidates": deleted_ai_candidates,
        "kept_stable": kept_stable,
        "kept_drafts": kept_drafts,
        "kept_ai_candidates": kept_ai_candidates,
    }
