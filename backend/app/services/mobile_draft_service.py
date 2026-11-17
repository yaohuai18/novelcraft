# 移动端本地草稿服务（第十九批「移动端适配与开放 API」规格五/六/七）
# 自动保存 / 同步 / 冲突处理：
# - 保存草稿：计算 content_hash，与服务器当前版本比对
#   · base_version 与服务器一致 → 同步成功（写入 ChapterContent，version+1，synced）
#   · base_version 不一致 → 冲突（返回本地/服务器版本信息，不覆盖服务器）
# - 冲突解决：keep_server（保留服务器）/ keep_local（保留本地覆盖）/ save_as_new（另存为新章节）
# 冲突信息：本地版本时间、服务器版本时间、本地修改字数、服务器修改字数、最近编辑设备。
import hashlib
import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.models.chapter_content import ChapterContent
from app.models.mobile_draft import MobileDraft
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _word_count(content: str) -> int:
    return len(re.sub(r"\s", "", content or ""))


async def check_project_access(
    db: AsyncSession,
    project_id: uuid.UUID,
    user: User,
) -> Project:
    """归属校验：作者本人或管理员可访问。"""
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise ApiError("not_found", "项目不存在", status_code=404)
    if project.user_id != user.id and user.role not in ("operator", "super_admin"):
        raise ApiError("not_found", "项目不存在", status_code=404)
    return project


async def check_chapter_access(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> Outline:
    """章节存在且属于项目。"""
    result = await db.execute(
        select(Outline).where(
            Outline.id == chapter_id,
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
        )
    )
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise ApiError("not_found", "章节不存在", status_code=404)
    return chapter


async def get_server_snapshot(
    db: AsyncSession,
    chapter_id: uuid.UUID,
) -> dict:
    """服务器当前版本快照：{version, content, word_count, updated_at}。"""
    result = await db.execute(
        select(ChapterContent).where(ChapterContent.outline_id == chapter_id)
    )
    cc = result.scalar_one_or_none()
    if cc is not None:
        return {
            "version": str(cc.version),
            "content": cc.content or "",
            "word_count": cc.word_count,
            "updated_at": cc.updated_at,
        }
    return {
        "version": "0",
        "content": "",
        "word_count": 0,
        "updated_at": None,
    }


async def _apply_to_server(
    db: AsyncSession,
    chapter_id: uuid.UUID,
    content: str,
) -> dict:
    """将内容写入服务器（无 ChapterContent 则创建，有则 version+1）。"""
    result = await db.execute(
        select(ChapterContent).where(ChapterContent.outline_id == chapter_id)
    )
    cc = result.scalar_one_or_none()
    now = datetime.utcnow()
    if cc is None:
        cc = ChapterContent(
            outline_id=chapter_id,
            content=content or "",
            word_count=_word_count(content),
            version=1,
        )
        db.add(cc)
        await db.flush()
        return {"version": "1", "content": content or "", "updated_at": now}
    cc.content = content or ""
    cc.word_count = _word_count(content)
    cc.version += 1
    cc.updated_at = now
    await db.flush()
    return {"version": str(cc.version), "content": content or "", "updated_at": now}


async def upsert_and_sync(
    db: AsyncSession,
    *,
    user: User,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    content: str,
    base_version: str | None,
    device_info: str | None,
) -> tuple[MobileDraft, dict | None]:
    """保存草稿并尝试同步。返回 (draft, conflict_info|None)。"""
    await check_project_access(db, project_id, user)
    await check_chapter_access(db, project_id, chapter_id)

    now = datetime.utcnow()
    result = await db.execute(
        select(MobileDraft).where(
            MobileDraft.user_id == user.id,
            MobileDraft.chapter_id == chapter_id,
        )
    )
    draft = result.scalar_one_or_none()

    if draft is None:
        draft = MobileDraft(
            user_id=user.id,
            project_id=project_id,
            chapter_id=chapter_id,
            base_version=base_version,
            content=content or "",
            content_hash=_content_hash(content),
            device_info=device_info,
            last_edited_at=now,
            sync_status="local_only",
        )
        db.add(draft)
    else:
        draft.content = content or ""
        draft.content_hash = _content_hash(content)
        draft.base_version = base_version
        draft.device_info = device_info
        draft.last_edited_at = now
        draft.sync_error = None

    server = await get_server_snapshot(db, chapter_id)

    # 同步条件：本地 base_version 与服务器当前版本一致（或服务器无版本且本地无 base_version）
    server_version = server["version"]
    if base_version is not None and base_version != server_version:
        draft.sync_status = "conflict"
        draft.sync_error = "服务器版本已更新，存在编辑冲突"
        conflict_info = {
            "conflict": True,
            "local_version_time": now.isoformat(),
            "server_version_time": (
                server["updated_at"].isoformat()
                if server["updated_at"] else None
            ),
            "local_word_count": _word_count(content or ""),
            "server_word_count": server["word_count"],
            "last_editing_device": device_info,
            "base_version": base_version,
            "server_version": server_version,
        }
        await db.commit()
        await db.refresh(draft)
        return draft, conflict_info

    # 无冲突：应用内容到服务器
    new_server = await _apply_to_server(db, chapter_id, content)
    draft.sync_status = "synced"
    draft.base_version = new_server["version"]
    draft.sync_error = None
    await db.commit()
    await db.refresh(draft)
    return draft, None


async def sync_draft(
    db: AsyncSession,
    draft: MobileDraft,
) -> tuple[MobileDraft, dict | None]:
    """强制同步草稿（网络恢复后自动同步入口）。"""
    server = await get_server_snapshot(db, draft.chapter_id)
    server_version = server["version"]

    if draft.base_version is not None and draft.base_version != server_version:
        draft.sync_status = "conflict"
        draft.sync_error = "服务器版本已更新，存在编辑冲突"
        conflict_info = {
            "conflict": True,
            "local_version_time": draft.last_edited_at.isoformat(),
            "server_version_time": (
                server["updated_at"].isoformat()
                if server["updated_at"] else None
            ),
            "local_word_count": _word_count(draft.content),
            "server_word_count": server["word_count"],
            "last_editing_device": draft.device_info,
            "base_version": draft.base_version,
            "server_version": server_version,
        }
        await db.commit()
        await db.refresh(draft)
        return draft, conflict_info

    new_server = await _apply_to_server(db, draft.chapter_id, draft.content)
    draft.sync_status = "synced"
    draft.base_version = new_server["version"]
    draft.sync_error = None
    await db.commit()
    await db.refresh(draft)
    return draft, None


async def resolve_conflict(
    db: AsyncSession,
    draft: MobileDraft,
    strategy: str,
) -> tuple[MobileDraft, dict]:
    """冲突解决。
    keep_server：丢弃本地内容，回退到服务器版本（synced）。
    keep_local：用本地内容覆盖服务器（写入服务器，synced）。
    save_as_new：本地内容另存为新章节副本（draft 指向新章节，synced）。
    """
    server = await get_server_snapshot(db, draft.chapter_id)
    server_version = server["version"]

    if strategy == "keep_server":
        draft.content = server["content"]
        draft.content_hash = _content_hash(server["content"])
        draft.base_version = server_version
        draft.sync_status = "synced"
        draft.sync_error = None
        await db.commit()
        await db.refresh(draft)
        return draft, {"strategy": "keep_server", "chapter_id": str(draft.chapter_id)}

    if strategy == "keep_local":
        new_server = await _apply_to_server(db, draft.chapter_id, draft.content)
        draft.base_version = new_server["version"]
        draft.sync_status = "synced"
        draft.sync_error = None
        await db.commit()
        await db.refresh(draft)
        return draft, {"strategy": "keep_local", "chapter_id": str(draft.chapter_id)}

    if strategy == "save_as_new":
        # 复制原章节为新章节，内容用本地草稿
        original = await db.get(Outline, draft.chapter_id)
        sort_result = await db.execute(
            select(Outline.sort_order)
            .where(Outline.project_id == draft.project_id)
            .order_by(Outline.sort_order.desc())
            .limit(1)
        )
        max_sort = sort_result.scalar_one_or_none() or 0
        new_chapter = Outline(
            project_id=draft.project_id,
            parent_id=original.parent_id if original else None,
            title=(original.title if original else "章节") + "（副本）",
            outline_type="chapter",
            content="",
            sort_order=max_sort + 1,
            status="draft",
        )
        db.add(new_chapter)
        await db.flush()
        # 新章节正文 = 本地内容
        await _apply_to_server(db, new_chapter.id, draft.content)
        draft.chapter_id = new_chapter.id
        draft.base_version = "1"
        draft.sync_status = "synced"
        draft.sync_error = None
        await db.commit()
        await db.refresh(draft)
        return draft, {"strategy": "save_as_new", "chapter_id": str(new_chapter.id)}

    raise ApiError(
        "validation_failed",
        "冲突解决策略非法（keep_server / keep_local / save_as_new）",
        status_code=422,
    )
