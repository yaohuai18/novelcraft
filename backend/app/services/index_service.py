# 搜索索引服务（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# 每次正文/大纲/人物/设定/标签/发布状态保存后更新 SearchDocument 索引，删除时软删或移除。
# 索引更新失败不影响正文保存：sync_* 系列函数内部捕获异常，仅记录到内存失败队列。
import logging
import re
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outline import Outline
from app.models.search_document import SEARCH_TARGET_TYPES, SearchDocument
from app.models.tag import Tag

logger = logging.getLogger(__name__)

# 需要剥除的 HTML 标签（保留文本内容）
_HTML_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_OTHER_RE = re.compile(r"<[^>]+>")
# Markdown 标记：标题、引用、列表、粗斜体、行内代码、分隔线等
_MD_TITLE_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_SYMBOL_RE = re.compile(r"[>*_`~#\[\]()!\-]{1,3}\s?", re.MULTILINE)
_MD_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WS_RE = re.compile(r"[ \t\u3000]+")
_NL_RE = re.compile(r"\n{3,}")

# 索引失败任务队列（内存实现：失败不影响正文保存，支持重试）
index_failures: list[dict] = []


def to_plain_text(content: str | None) -> str:
    """正文清洗：去 HTML 标签、去 Markdown 标记、规范化空白与换行。"""
    if not content:
        return ""
    text = _HTML_TAG_RE.sub("", content)
    text = _HTML_OTHER_RE.sub("", text)
    text = _MD_CODE_FENCE_RE.sub("", text)
    text = _MD_TITLE_RE.sub("", text)
    text = _MD_SYMBOL_RE.sub("", text)
    text = _NL_RE.sub("\n\n", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def record_failure(kind: str, project_id, target_type: str, target_id, error: str) -> None:
    """记录索引失败任务，正文保存不受影响。"""
    index_failures.append({
        "kind": kind,
        "project_id": str(project_id),
        "target_type": target_type,
        "target_id": str(target_id),
        "error": str(error),
        "retried": False,
    })
    logger.warning("index sync failed kind=%s target=%s/%s: %s", kind, target_type, target_id, error)


async def _safe_sync(db: AsyncSession, kind: str, project_id, target_type: str, target_id, coro):
    """索引同步安全包裹：失败只回滚 savepoint，绝不影响业务事务（规格六）。"""
    try:
        async with db.begin_nested():
            await coro
    except Exception as exc:  # 索引失败不影响业务保存
        record_failure(kind, project_id, target_type, target_id, exc)


async def sync_outline_index(db: AsyncSession, outline: Outline) -> None:
    """大纲保存后更新/插入大纲索引文档。"""
    plain_text = to_plain_text(
        "\n".join(part for part in (outline.summary, outline.content) if part)
    )
    doc = await _get_or_create_doc(db, outline.project_id, "outline", outline.id)
    doc.title = outline.title or ""
    doc.plain_text = plain_text
    doc.summary = to_plain_text(outline.summary or "")
    doc.status = outline.status or "active"
    doc.deleted_at = None
    doc.volume_title = None
    await db.flush()


async def sync_chapter_content_index(
    db: AsyncSession, project_id: uuid.UUID, outline_id: uuid.UUID,
    title: str, content: str, status: str | None = None,
    visibility: str | None = None, volume_title: str | None = None,
    summary: str | None = None,
) -> None:
    """章节正文保存后更新/插入正文索引文档（target_id 使用 outline_id 保证唯一）。"""
    doc = await _get_or_create_doc(db, project_id, "chapter_content", outline_id)
    doc.title = title or ""
    doc.plain_text = to_plain_text(content)
    if summary is not None:
        doc.summary = to_plain_text(summary)
    if status is not None:
        doc.status = status
    if visibility is not None:
        doc.visibility = visibility
    if volume_title is not None:
        doc.volume_title = volume_title
    doc.deleted_at = None
    await db.flush()


async def sync_character_index(
    db: AsyncSession, project_id: uuid.UUID, character_id: uuid.UUID,
    name: str, description: str | None,
) -> None:
    """人物资料保存后更新/插入人物索引文档。"""
    doc = await _get_or_create_doc(db, project_id, "character", character_id)
    doc.title = name or ""
    doc.plain_text = to_plain_text(description)
    doc.summary = to_plain_text(description)
    doc.deleted_at = None
    await db.flush()


async def sync_world_setting_index(
    db: AsyncSession, project_id: uuid.UUID, setting_id: uuid.UUID,
    name: str, content: str | None,
) -> None:
    """世界观规则保存后更新/插入设定索引文档。"""
    doc = await _get_or_create_doc(db, project_id, "world_setting", setting_id)
    doc.title = name or ""
    doc.plain_text = to_plain_text(content)
    doc.summary = to_plain_text(content)
    doc.deleted_at = None
    await db.flush()


async def sync_project_index(db: AsyncSession, project) -> None:
    """作品信息保存后更新/插入作品索引文档（标题/简介/背景）。"""
    doc = await _get_or_create_doc(db, project.id, "project", project.id)
    doc.title = project.title or ""
    doc.summary = to_plain_text(project.description or "")
    doc.plain_text = to_plain_text(
        "\n".join(p for p in (project.description, project.background) if p)
    )
    doc.status = project.status or "active"
    doc.visibility = "public" if project.publication_status == "published" else "private"
    doc.deleted_at = None
    await db.flush()


async def sync_tag_index(db: AsyncSession, tag: Tag) -> None:
    """标签创建/修改后更新/插入标签索引文档。"""
    doc = await _get_or_create_doc(db, tag.project_id, "tag", tag.id)
    doc.title = tag.name or ""
    doc.plain_text = to_plain_text(
        " ".join(p for p in (tag.name, tag.description) if p)
    )
    doc.summary = to_plain_text(tag.description or "")
    doc.deleted_at = None
    await db.flush()


async def update_doc_visibility(
    db: AsyncSession, project_id: uuid.UUID, target_type: str, target_id: uuid.UUID,
    status: str | None = None, visibility: str | None = None,
) -> None:
    """发布/撤回/归档/恢复后更新索引文档状态（不创建新文档）。"""
    values = {}
    if status is not None:
        values["status"] = status
    if visibility is not None:
        values["visibility"] = visibility
    if not values:
        return
    await db.execute(
        update(SearchDocument)
        .where(
            SearchDocument.project_id == project_id,
            SearchDocument.target_type == target_type,
            SearchDocument.target_id == target_id,
        )
        .values(**values)
    )


async def refresh_tags_text_for_outline(db: AsyncSession, outline_id: uuid.UUID) -> None:
    """大纲条目（及其正文文档）标签变化后，重算该目标 tags_text。"""
    stmt = select(Tag.name).join(
        SearchDocument,
        SearchDocument.target_type == "tag",
        isouter=True
    ).where(SearchDocument.project_id.is_not(None))
    # 直接用 outline_tags 关联
    from sqlalchemy import text
    sql = text(
        "SELECT t.name FROM tags t JOIN outline_tags ot ON ot.tag_id = t.id "
        "WHERE ot.outline_id = :oid"
    )
    names = [row[0] for row in (await db.execute(sql, {"oid": str(outline_id)}))]
    tags_text = " ".join(names)
    await db.execute(
        update(SearchDocument)
        .where(
            SearchDocument.target_type.in_(["outline", "chapter_content"]),
            SearchDocument.target_id == outline_id,
        )
        .values(tags_text=tags_text)
    )


async def refresh_tags_text_for_target(
    db: AsyncSession, project_id: uuid.UUID, target_type: str, target_id: uuid.UUID
) -> None:
    """通用关系表标签变化后，重算该目标（含 Tag 文档自身）的 tags_text。"""
    from sqlalchemy import text
    sql = text(
        "SELECT t.name FROM tags t JOIN tag_relations tr ON tr.tag_id = t.id "
        "WHERE tr.target_type = :tt AND tr.target_id = :tid"
    )
    names = [row[0] for row in (await db.execute(sql, {"tt": target_type, "tid": str(target_id)}))]
    tags_text = " ".join(names)
    await db.execute(
        update(SearchDocument)
        .where(
            SearchDocument.project_id == project_id,
            SearchDocument.target_type == target_type,
            SearchDocument.target_id == target_id,
        )
        .values(tags_text=tags_text)
    )
    # 该目标若为章节/大纲，同步其正文文档
    if target_type in ("chapter", "volume", "scene"):
        await db.execute(
            update(SearchDocument)
            .where(
                SearchDocument.project_id == project_id,
                SearchDocument.target_type == "chapter_content",
                SearchDocument.target_id == target_id,
            )
            .values(tags_text=tags_text)
        )
        await db.execute(
            update(SearchDocument)
            .where(
                SearchDocument.project_id == project_id,
                SearchDocument.target_type == "outline",
                SearchDocument.target_id == target_id,
            )
            .values(tags_text=tags_text)
        )


async def delete_index(
    db: AsyncSession, project_id: uuid.UUID, target_type: str, target_id: uuid.UUID,
    soft: bool = False,
) -> None:
    """删除指定索引文档（软删标记 deleted_at 或物理删除）。"""
    if target_type not in SEARCH_TARGET_TYPES:
        return
    if soft:
        await db.execute(
            update(SearchDocument)
            .where(
                SearchDocument.project_id == project_id,
                SearchDocument.target_type == target_type,
                SearchDocument.target_id == target_id,
            )
            .values(deleted_at=__import__("datetime").datetime.utcnow())
        )
    else:
        await db.execute(
            delete(SearchDocument).where(
                SearchDocument.project_id == project_id,
                SearchDocument.target_type == target_type,
                SearchDocument.target_id == target_id,
            )
        )


async def delete_indexes_for_outline(
    db: AsyncSession, project_id: uuid.UUID, outline_id: uuid.UUID
) -> None:
    """删除大纲及其正文对应的索引文档。"""
    for target_type in ("outline", "chapter_content"):
        await delete_index(db, project_id, target_type, outline_id)


async def get_index_failure_count() -> int:
    """返回待重试的索引失败任务数。"""
    return len(index_failures)


async def _get_or_create_doc(
    db: AsyncSession, project_id: uuid.UUID, target_type: str, target_id: uuid.UUID
) -> SearchDocument:
    stmt = select(SearchDocument).where(
        SearchDocument.project_id == project_id,
        SearchDocument.target_type == target_type,
        SearchDocument.target_id == target_id,
    )
    doc = (await db.execute(stmt)).scalar_one_or_none()
    if doc is None:
        doc = SearchDocument(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            title="",
            plain_text="",
        )
        db.add(doc)
    return doc
