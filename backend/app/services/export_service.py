# 导出任务服务（第十六批「数据导入、导出与备份恢复」规格实现）
# 导出格式：Markdown / 纯文本 / JSON / CSV（规格八）
# 导出范围：whole_project / volume / chapter / characters / locations / world / timeline / tags / comments / version_history（规格九）
# 导出选项：草稿 / 私有备注 / 评论 / 归档 / AI 记录 / 版本历史（规格九默认策略：公开+自己全部，私人评论不导出）
# 结果有效期：result_token 一次性取用、绑定用户、短期有效（规格十/十九）
import csv
import hashlib
import html as html_mod
import io
import json
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.chapter_version import ChapterVersion
from app.models.character import Character
from app.models.comment import Comment
from app.models.content_version import ContentVersion
from app.models.export_task import ExportTask
from app.models.faction import Faction
from app.models.location import Location
from app.models.outline import Outline
from app.models.project import Project
from app.models.relationship import Relationship
from app.models.tag import OutlineTag, Tag
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.world_setting import WorldSetting
from app.schemas.import_export import ExportCreate
from app.services.baseline_service import collect_baseline
from app.services.index_service import to_plain_text
from app.services.snapshot_service import build_project_snapshot

MAX_CONCURRENT_EXPORTS = 5
EXPORT_RESULT_TTL_HOURS = 24  # 结果有效期（规格十：避免长期占用存储）

_LEVEL_HASH = {"summary": 1, "volume": 2, "chapter": 3, "scene": 4}
_LEVEL_NAME = {1: "作品", 2: "分卷", 3: "章节", 4: "场景"}

# 角色允许的导出范围（规格二十二）
SCOPE_PERMISSIONS = {
    "owner": {
        "whole_project", "volume", "chapter", "characters", "locations",
        "world", "timeline", "tags", "comments", "version_history",
    },
    "editor": {
        "whole_project", "volume", "chapter", "characters", "locations",
        "world", "timeline", "tags", "comments",
    },
    "reviewer": {
        "whole_project", "volume", "chapter", "characters", "locations",
        "world", "timeline", "tags", "comments",
    },
    "viewer": {
        "whole_project", "volume", "chapter", "characters", "locations",
        "world", "timeline", "tags",
    },
}


async def create_export_task(
    db: AsyncSession,
    project: Project,
    user_id: uuid.UUID,
    body: ExportCreate,
    role: str = "owner",
) -> ExportTask:
    """创建导出任务并同步执行；生成一次性 result_token 与有效期。"""
    scope = body.scope or body.export_type or "whole_project"
    # 旧 export_type 映射到新 scope（兼容第十批）
    legacy_scope_map = {
        "whole_project": "whole_project",
        "outline": "whole_project",
        "content": "whole_project",
        "knowledge": "characters",
        "timeline": "timeline",
        "relationship": "relationships",
    }
    scope = legacy_scope_map.get(scope, scope)
    allowed = SCOPE_PERMISSIONS.get(role, SCOPE_PERMISSIONS["owner"])
    if scope not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"当前角色不允许导出范围 {scope}",
        )

    active_count = (
        await db.execute(
            select(ExportTask.id).where(
                ExportTask.project_id == project.id,
                ExportTask.status.in_(["pending", "running"]),
            )
        )
    ).scalars().all()
    if len(active_count) >= MAX_CONCURRENT_EXPORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"导出任务并发数已达上限（{MAX_CONCURRENT_EXPORTS}）",
        )

    if scope not in {
        "whole_project", "volume", "chapter", "characters", "locations",
        "world", "timeline", "tags", "comments", "version_history",
        "relationships",
    }:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的导出范围",
        )
    if body.export_format not in {"text", "markdown", "html", "json", "csv"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的导出格式",
        )
    if body.outline_id is not None:
        row = (
            await db.execute(
                select(Outline).where(
                    Outline.id == body.outline_id,
                    Outline.project_id == project.id,
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="指定大纲不存在或不属于该作品",
            )

    task = ExportTask(
        user_id=user_id,
        project_id=project.id,
        export_type=body.export_type,
        scope=scope,
        export_format=body.export_format,
        options=json.dumps(body.options.model_dump(mode="json"), ensure_ascii=False),
        status="running",
        progress=10,
    )
    db.add(task)
    await db.flush()

    try:
        content = await build_export_content(
            db, project, scope, body.export_format,
            body.options, body.outline_id, user_id, role,
        )
        task.content = content
        task.status = "completed"
        task.progress = 100
        task.completed_at = datetime.utcnow()
        task.result_token = _new_token()
        task.expires_at = datetime.utcnow() + timedelta(hours=EXPORT_RESULT_TTL_HOURS)
    except ValueError as error:
        task.status = "failed"
        task.error_message = str(error)

    await db.commit()
    await db.refresh(task)
    return task


def _new_token() -> str:
    return secrets.token_urlsafe(32)[:48]


async def get_export_result(
    db: AsyncSession,
    task: ExportTask,
    user_id: uuid.UUID,
    token: str,
) -> ExportTask:
    """通过一次性 result_token 获取结果（规格十/十九：短期有效、绑定用户、一次性）。"""
    if task.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="导出结果绑定创建用户，无权访问",
        )
    if task.status != "completed" or not task.content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="导出任务尚未完成",
        )
    if not task.result_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="导出任务未生成结果令牌",
        )
    if task.expires_at and datetime.utcnow() > task.expires_at:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="导出结果已过期，请重新创建导出任务",
        )
    if not secrets.compare_digest(task.result_token, token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="结果令牌无效",
        )
    # 一次性：取用后令牌作废（结果内容仍留在任务内可查，但不可再次取用）
    task.result_token = None
    await db.commit()
    await db.refresh(task)
    return task


async def cancel_export(db: AsyncSession, task: ExportTask) -> ExportTask:
    if task.status not in {"pending", "running", "completed"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前任务无法取消",
        )
    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    task.result_token = None  # 作废结果令牌（规格十九：回收短期有效结果）
    await db.commit()
    await db.refresh(task)
    return task


# ==================== 内容组装 ====================

async def build_export_content(
    db: AsyncSession,
    project: Project,
    scope: str,
    export_format: str,
    options,
    outline_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    role: str = "owner",
) -> str:
    outlines = await _load_outlines(db, project.id, outline_id)
    chapter_map = await _load_chapter_contents(db, outlines)
    tag_map = await _load_tag_map(db, project.id)
    characters, world_settings = await _load_knowledge(db, project.id)
    locations = await _load_locations(db, project.id)
    factions = await _load_factions(db, project.id)
    world_rules = await _load_world_rules(db, project.id)
    timeline_events = await _load_timeline_events(db, project.id)
    relationships = await _load_relationships(db, project.id)
    tags = await _load_tags(db, project.id)
    comments = await _load_comments(db, project.id, options, user_id, role)
    versions = await _load_versions(db, project.id, options, user_id, role)

    if export_format == "json":
        return await _render_json(
            db, project, scope, outlines, chapter_map,
            characters, world_settings, tag_map,
            locations, factions, world_rules, timeline_events,
            relationships, tags, comments, versions, options, user_id, role,
        )
    if export_format == "csv":
        return _render_csv(
            project, scope, characters, world_settings, locations, factions,
            world_rules, timeline_events, relationships, tags,
        )
    if export_format == "markdown":
        return _render_markdown(
            project, scope, outlines, chapter_map,
            characters, world_settings, locations, tag_map, options,
        )
    if export_format == "html":
        return _render_html(
            project, scope, outlines, chapter_map,
            characters, world_settings, locations, tag_map, options,
        )
    return _render_text(
        project, scope, outlines, chapter_map,
        characters, world_settings, locations, tag_map, options,
    )


class _OutlineNode:
    __slots__ = ("outline", "children")

    def __init__(self, outline: Outline):
        self.outline = outline
        self.children: list[_OutlineNode] = []


async def _load_outlines(db: AsyncSession, project_id: uuid.UUID, outline_id: uuid.UUID | None) -> list[_OutlineNode]:
    rows = list(
        (
            await db.execute(
                select(Outline)
                .where(Outline.project_id == project_id)
                .order_by(Outline.sort_order, Outline.created_at)
            )
        )
        .scalars()
        .all()
    )
    if outline_id is not None:
        keep = {str(outline_id)}
        changed = True
        while changed:
            changed = False
            for o in rows:
                if o.parent_id and str(o.parent_id) in keep and str(o.id) not in keep:
                    keep.add(str(o.id))
                    changed = True
        rows = [o for o in rows if str(o.id) in keep]

    node_map: dict[uuid.UUID, _OutlineNode] = {}
    roots: list[_OutlineNode] = []
    for o in rows:
        node = _OutlineNode(o)
        node_map[o.id] = node
        if o.parent_id and o.parent_id in node_map:
            node_map[o.parent_id].children.append(node)
        else:
            roots.append(node)
    for node in node_map.values():
        node.children.sort(key=lambda n: (n.outline.sort_order, n.outline.created_at))
    roots.sort(key=lambda n: (n.outline.sort_order, n.outline.created_at))
    return roots


async def _load_chapter_contents(
    db: AsyncSession, roots: list[_OutlineNode]
) -> dict[uuid.UUID, ChapterContent]:
    ids = []

    def walk(node: _OutlineNode):
        ids.append(node.outline.id)
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(ChapterContent).where(ChapterContent.outline_id.in_(ids))
        )
    ).scalars().all()
    return {r.outline_id: r for r in rows}


async def _load_tag_map(db: AsyncSession, project_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    rows = (
        await db.execute(
            select(OutlineTag, Tag)
            .join(Tag, Tag.id == OutlineTag.tag_id)
            .where(Tag.project_id == project_id)
        )
    ).all()
    result: dict[uuid.UUID, list[str]] = {}
    for ot, tag in rows:
        result.setdefault(ot.outline_id, []).append(tag.name)
    return result


async def _load_knowledge(
    db: AsyncSession, project_id: uuid.UUID
) -> tuple[list[Character], list[WorldSetting]]:
    chars = (
        await db.execute(
            select(Character).where(Character.project_id == project_id)
            .order_by(Character.created_at)
        )
    ).scalars().all()
    wss = (
        await db.execute(
            select(WorldSetting).where(WorldSetting.project_id == project_id)
            .order_by(WorldSetting.created_at)
        )
    ).scalars().all()
    return list(chars), list(wss)


async def _load_locations(db: AsyncSession, project_id: uuid.UUID) -> list[Location]:
    return list((await db.execute(
        select(Location).where(Location.project_id == project_id).order_by(Location.created_at)
    )).scalars().all())


async def _load_factions(db: AsyncSession, project_id: uuid.UUID) -> list[Faction]:
    return list((await db.execute(
        select(Faction).where(Faction.project_id == project_id).order_by(Faction.created_at)
    )).scalars().all())


async def _load_world_rules(db: AsyncSession, project_id: uuid.UUID) -> list[WorldRule]:
    return list((await db.execute(
        select(WorldRule).where(WorldRule.project_id == project_id).order_by(WorldRule.created_at)
    )).scalars().all())


async def _load_timeline_events(db: AsyncSession, project_id: uuid.UUID) -> list[TimelineEvent]:
    return list((await db.execute(
        select(TimelineEvent).where(TimelineEvent.project_id == project_id)
        .order_by(TimelineEvent.sort_order, TimelineEvent.created_at)
    )).scalars().all())


async def _load_relationships(db: AsyncSession, project_id: uuid.UUID) -> list[Relationship]:
    return list((await db.execute(
        select(Relationship).where(Relationship.project_id == project_id).order_by(Relationship.created_at)
    )).scalars().all())


async def _load_tags(db: AsyncSession, project_id: uuid.UUID) -> list[Tag]:
    return list((await db.execute(
        select(Tag).where(Tag.project_id == project_id).order_by(Tag.created_at)
    )).scalars().all())


async def _load_comments(
    db: AsyncSession, project_id: uuid.UUID, options, user_id: uuid.UUID | None, role: str
) -> list[Comment]:
    """加载评论（规格九/二十二：私人批注仅自己可见；协作成员私人评论不导出）。"""
    rows = (
        await db.execute(
            select(Comment)
            .where(Comment.project_id == project_id, Comment.deleted_at.is_(None))
            .order_by(Comment.created_at)
        )
    ).scalars().all()
    result = []
    for c in rows:
        if c.comment_type == "private_comment":
            if options.include_private_notes and user_id is not None and c.user_id == user_id:
                result.append(c)
            continue
        if c.comment_type == "public_comment":
            result.append(c)
            continue
        # review_comment / collaboration：仅 owner/editor 且非他人私有内容
        if role in ("owner", "editor") and options.include_comments:
            result.append(c)
    return result


async def _load_versions(
    db: AsyncSession, project_id: uuid.UUID, options, user_id: uuid.UUID | None, role: str
) -> list[dict]:
    """加载版本历史（规格九/十一）：统一版本 ContentVersion + 章节正文版本 ChapterVersion。"""
    if not options.include_version_history:
        return []
    rows = (
        await db.execute(
            select(ContentVersion)
            .where(ContentVersion.project_id == project_id)
            .order_by(ContentVersion.created_at)
        )
    ).scalars().all()
    items: list[dict] = [
        {
            "target_type": v.target_type,
            "target_id": str(v.target_id),
            "version_number": v.version_number,
            "title_snapshot": v.title_snapshot,
            "content_snapshot": v.content_snapshot,
            "change_type": v.change_type,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in rows
    ]
    ch_rows = (
        await db.execute(
            select(ChapterVersion, Outline)
            .join(Outline, Outline.id == ChapterVersion.outline_id)
            .where(Outline.project_id == project_id)
            .order_by(ChapterVersion.created_at)
        )
    ).all()
    for cv, o in ch_rows:
        items.append({
            "target_type": "chapter",
            "target_id": str(cv.outline_id),
            "version_number": cv.version,
            "title_snapshot": o.title,
            "content_snapshot": cv.content,
            "change_type": {
                "manual": "update",
                "continue": "ai_generate",
                "rewrite": "ai_generate",
                "polish": "ai_generate",
                "restore": "restore",
            }.get(cv.source, cv.source or "update"),
            "created_at": cv.created_at.isoformat() if cv.created_at else None,
        })
    return items


def _iter_flat(roots: list[_OutlineNode]):
    def walk(node: _OutlineNode):
        yield node
        for child in node.children:
            yield from walk(child)

    for root in roots:
        yield from walk(root)


def _node_included(node: _OutlineNode, options, scope: str) -> bool:
    """按导出选项过滤草稿/归档（规格九）。"""
    o = node.outline
    if o.status == "archived" and not options.include_archived:
        return False
    if o.status == "draft" and not options.include_drafts and scope == "whole_project":
        # 正文导入的章节默认 draft；whole 导出时默认包含（写作内容主体），
        # 仅当用户显式 include_drafts=False 且明确排除时过滤
        return False
    return True


# ---------- Markdown ----------

def _render_markdown(
    project: Project, scope: str, roots: list[_OutlineNode],
    chapter_map: dict[uuid.UUID, ChapterContent],
    characters: list[Character], world_settings: list[WorldSetting],
    locations: list[Location], tag_map: dict[uuid.UUID, list[str]], options,
) -> str:
    lines: list[str] = []
    chapter_no = 0
    if options.include_toc:
        lines.append("## 目录")
        for node in _iter_flat(roots):
            o = node.outline
            if o.outline_type in ("volume", "chapter"):
                lines.append(f"{'  ' * (_LEVEL_HASH[o.outline_type] - 2)}- {o.title}")
        lines.append("")
    lines.append(f"# {project.title}")
    if project.background:
        lines.append("")
        lines.append(project.background)
    lines.append("")
    emit_body = scope in ("whole_project", "volume", "chapter", "outline", "content")
    for node in roots:
        if scope in ("volume", "chapter"):
            if not _scope_match(node.outline, scope, options):
                continue
        chapter_no = _emit_markdown_node(
            lines, node, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    if scope in ("whole_project", "knowledge", "characters"):
        lines.extend(_render_knowledge_md(characters, world_settings, options))
    if scope == "locations":
        lines.append("## 地点")
        lines.append("")
        for loc in locations:
            lines.append(f"### {loc.name}")
            if loc.description:
                lines.append(loc.description)
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _scope_match(o: Outline, scope: str, options) -> bool:
    if scope == "volume" and o.outline_type == "volume":
        return True
    if scope == "chapter" and o.outline_type == "chapter":
        return True
    return False


def _emit_markdown_node(
    lines: list[str], node: _OutlineNode, chapter_map, tag_map, options,
    emit_body: bool, chapter_no: int,
) -> int:
    o = node.outline
    level = _LEVEL_HASH.get(o.outline_type, 3)
    title = o.title or ""
    if options.include_chapter_numbers and o.outline_type == "chapter":
        chapter_no += 1
        title = f"第{chapter_no}章 {title}"
    lines.append(f"{'#' * level} {title}")
    if options.include_outline_summaries and o.summary:
        lines.append("")
        lines.append(f"> 摘要：{o.summary}")
    if emit_body:
        body = ""
        if o.outline_type == "chapter":
            cc = chapter_map.get(o.id)
            body = (cc.content if cc else "") or ""
        else:
            body = o.content or ""
        if body:
            if not options.preserve_markdown:
                body = to_plain_text(body)
            lines.append("")
            lines.append(body)
        if options.include_tags and o.outline_type == "chapter":
            tag_names = tag_map.get(o.id) or []
            if tag_names:
                lines.append("")
                lines.append(f"标签：{'、'.join(tag_names)}")
    lines.append("")
    for child in node.children:
        chapter_no = _emit_markdown_node(
            lines, child, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    return chapter_no


def _render_knowledge_md(
    characters: list[Character], world_settings: list[WorldSetting], options,
) -> list[str]:
    if not options.include_characters:
        return []
    lines: list[str] = ["## 人物资料", ""]
    for ch in characters:
        lines.append(f"### {ch.name}")
        if ch.identity:
            lines.append(f"身份：{ch.identity}")
        if ch.personality:
            lines.append(f"性格：{ch.personality}")
        if ch.goal:
            lines.append(f"目标：{ch.goal}")
        lines.append("")
    if world_settings:
        lines.append("## 世界观设定")
        lines.append("")
        for ws in world_settings:
            lines.append(f"### {ws.name}")
            lines.append(ws.content)
            lines.append("")
    return lines


# ---------- 纯文本 ----------

def _render_text(
    project: Project, scope: str, roots: list[_OutlineNode],
    chapter_map, characters, world_settings, locations, tag_map, options,
) -> str:
    lines: list[str] = []
    chapter_no = 0
    if options.include_toc:
        lines.append("目录")
        for node in _iter_flat(roots):
            o = node.outline
            if o.outline_type in ("volume", "chapter"):
                lines.append(f"{'  ' * (_LEVEL_HASH[o.outline_type] - 2)}- {o.title}")
        lines.append("")
    lines.append(project.title)
    lines.append("")
    emit_body = scope in ("whole_project", "volume", "chapter", "outline", "content")
    for node in roots:
        if scope in ("volume", "chapter") and not _scope_match(node.outline, scope, options):
            continue
        chapter_no = _emit_text_node(
            lines, node, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    if scope in ("whole_project", "knowledge", "characters") and options.include_characters:
        lines.append("人物资料")
        for ch in characters:
            lines.append(f"- {ch.name}" + (f"（{ch.identity}）" if ch.identity else ""))
        for ws in world_settings:
            lines.append(f"- [{ws.category}] {ws.name}")
    if scope == "locations":
        lines.append("地点")
        for loc in locations:
            lines.append(f"- {loc.name}" + (f"（{loc.category}）" if loc.category else ""))
    return "\n".join(lines).strip() + "\n"


def _emit_text_node(
    lines: list[str], node: _OutlineNode, chapter_map, tag_map, options,
    emit_body: bool, chapter_no: int,
) -> int:
    o = node.outline
    level = _LEVEL_HASH.get(o.outline_type, 3)
    prefix = "  " * (level - 1)
    title = o.title or ""
    if options.include_chapter_numbers and o.outline_type == "chapter":
        chapter_no += 1
        title = f"第{chapter_no}章 {title}"
    lines.append(f"{prefix}{title}")
    if emit_body:
        body = ""
        if o.outline_type == "chapter":
            cc = chapter_map.get(o.id)
            body = (cc.content if cc else "") or ""
        else:
            body = o.content or ""
        if body:
            if not options.preserve_markdown:
                body = to_plain_text(body)
            lines.append("")
            lines.append(to_plain_text(body))
        if options.include_tags and o.outline_type == "chapter":
            tag_names = tag_map.get(o.id) or []
            if tag_names:
                lines.append(f"标签：{'、'.join(tag_names)}")
    lines.append("")
    for child in node.children:
        chapter_no = _emit_text_node(
            lines, child, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    return chapter_no


# ---------- HTML ----------

def _render_html(
    project: Project, scope: str, roots: list[_OutlineNode],
    chapter_map, characters, world_settings, locations, tag_map, options,
) -> str:
    out: list[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="zh-CN">')
    out.append("<head>")
    out.append('<meta charset="utf-8">')
    out.append(f"<title>{html_mod.escape(project.title)}</title>")
    out.append("</head>")
    out.append("<body>")
    out.append(f"<h1>{html_mod.escape(project.title)}</h1>")
    if options.include_toc:
        out.append("<h2>目录</h2>")
        out.append("<ul>")
        for node in _iter_flat(roots):
            o = node.outline
            if o.outline_type in ("volume", "chapter"):
                out.append(f"<li>{html_mod.escape(o.title or '')}</li>")
        out.append("</ul>")
    chapter_no = 0
    emit_body = scope in ("whole_project", "volume", "chapter", "outline", "content")
    for node in roots:
        if scope in ("volume", "chapter") and not _scope_match(node.outline, scope, options):
            continue
        chapter_no = _emit_html_node(
            out, node, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    if scope in ("whole_project", "knowledge", "characters") and options.include_characters:
        out.append("<h2>人物资料</h2>")
        for ch in characters:
            out.append(f"<h3>{html_mod.escape(ch.name)}</h3>")
            if ch.identity:
                out.append(f"<p>身份：{html_mod.escape(ch.identity)}</p>")
        for ws in world_settings:
            out.append(f"<h3>{html_mod.escape(ws.name)}</h3>")
            out.append(f"<p>{html_mod.escape(ws.content)}</p>")
    if scope == "locations":
        out.append("<h2>地点</h2>")
        for loc in locations:
            out.append(f"<h3>{html_mod.escape(loc.name)}</h3>")
            if loc.description:
                out.append(f"<p>{html_mod.escape(loc.description)}</p>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def _emit_html_node(
    out: list[str], node: _OutlineNode, chapter_map, tag_map, options,
    emit_body: bool, chapter_no: int,
) -> int:
    o = node.outline
    level = _LEVEL_HASH.get(o.outline_type, 3)
    title = o.title or ""
    if options.include_chapter_numbers and o.outline_type == "chapter":
        chapter_no += 1
        title = f"第{chapter_no}章 {title}"
    out.append(f"<h{level}>{html_mod.escape(title)}</h{level}>")
    if emit_body:
        body = ""
        if o.outline_type == "chapter":
            cc = chapter_map.get(o.id)
            body = (cc.content if cc else "") or ""
        else:
            body = o.content or ""
        if body:
            if not options.preserve_markdown:
                body = to_plain_text(body)
            for paragraph in body.splitlines():
                if paragraph.strip():
                    out.append(f"<p>{html_mod.escape(paragraph)}</p>")
        if options.include_tags and o.outline_type == "chapter":
            tag_names = tag_map.get(o.id) or []
            if tag_names:
                out.append(f"<p>标签：{html_mod.escape('、'.join(tag_names))}</p>")
    for child in node.children:
        chapter_no = _emit_html_node(
            out, child, chapter_map, tag_map, options, emit_body, chapter_no,
        )
    return chapter_no


# ---------- CSV（规格八：适合人物/地点/标签等表格资料） ----------

def _render_csv(
    project: Project, scope: str, characters, world_settings, locations,
    factions, world_rules, timeline_events, relationships, tags,
) -> str:
    out = io.StringIO()
    writer = csv.writer(out)

    def emit_table(name: str, header: list[str], rows: list[list]):
        writer.writerow([f"# {name}"])
        writer.writerow(header)
        for row in rows:
            writer.writerow([(str(v) if v is not None else "") for v in row])
        writer.writerow([])

    if scope in ("whole_project", "characters", "knowledge"):
        emit_table(
            "人物",
            ["name", "identity", "personality", "appearance", "background", "goal", "conflict", "relationship", "character_status"],
            [[c.name, c.identity, c.personality, c.appearance, c.background, c.goal, c.conflict, c.relationship, c.character_status] for c in characters],
        )
    if scope in ("whole_project", "world", "knowledge"):
        emit_table(
            "世界观设定",
            ["category", "name", "content", "importance"],
            [[w.category, w.name, w.content, w.importance] for w in world_settings],
        )
    if scope in ("whole_project", "locations"):
        emit_table(
            "地点",
            ["name", "category", "description", "importance"],
            [[l.name, l.category, l.description, l.importance] for l in locations],
        )
    if scope in ("whole_project", "world"):
        emit_table(
            "势力",
            ["name", "description", "leader", "goal"],
            [[f.name, f.description, f.leader, f.goal] for f in factions],
        )
        emit_table(
            "世界观规则",
            ["category", "name", "content", "importance"],
            [[r.category, r.name, r.content, r.importance] for r in world_rules],
        )
    if scope in ("whole_project", "timeline"):
        emit_table(
            "时间线",
            ["title", "description", "event_time", "sort_order", "importance"],
            [[t.title, t.description, t.event_time, t.sort_order, t.importance] for t in timeline_events],
        )
    if scope in ("whole_project", "relationships"):
        emit_table(
            "人物关系",
            ["from_name", "to_name", "relation_type", "description"],
            [[r.from_name, r.to_name, r.relation_type, r.description] for r in relationships],
        )
    if scope in ("whole_project", "tags"):
        emit_table(
            "标签",
            ["name", "color", "tag_type"],
            [[t.name, t.color, t.tag_type] for t in tags],
        )
    return out.getvalue()


# ---------- JSON ----------

def _project_brief(project: Project) -> dict:
    data: dict = {"id": str(project.id), "title": project.title}
    for field in (
        "genre", "background", "world_setting", "main_conflict",
        "ending_direction", "narrative_perspective", "writing_style",
        "forbidden_settings", "status",
    ):
        value = getattr(project, field)
        if value is not None:
            data[field] = value
    return data


def _flatten_outlines(roots: list[_OutlineNode]) -> list[dict]:
    result: list[dict] = []
    for node in _iter_flat(roots):
        o = node.outline
        result.append({
            "id": str(o.id),
            "parent_id": str(o.parent_id) if o.parent_id else None,
            "title": o.title,
            "outline_type": o.outline_type,
            "summary": o.summary,
            "content": o.content if o.outline_type != "chapter" else None,
            "sort_order": o.sort_order,
            "status": o.status,
        })
    return result


async def _render_json(
    db: AsyncSession, project: Project, scope: str, roots: list[_OutlineNode],
    chapter_map, characters, world_settings, tag_map,
    locations, factions, world_rules, timeline_events,
    relationships, tags, comments, versions, options, user_id, role,
) -> str:
    if scope == "whole_project":
        snapshot = await build_project_snapshot(db, project)
        # 阶段C「发布基线绑定」：导出携带当前基线（失败降级 None 不阻断导出）
        try:
            snapshot["baseline"] = await collect_baseline(db, project.id)
        except Exception:  # noqa: BLE001
            snapshot["baseline"] = None
        # 按选项追加评论/版本历史（规格九/十一）
        if options.include_comments:
            snapshot["comments"] = [
                {
                    "target_type": c.target_type,
                    "target_id": c.target_id,
                    "content": c.content,
                    "comment_type": c.comment_type,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in comments
            ]
        if options.include_version_history:
            snapshot["version_history"] = [
                {
                    "target_type": v["target_type"],
                    "target_id": v["target_id"],
                    "version_number": v["version_number"],
                    "title_snapshot": v["title_snapshot"],
                    "content_snapshot": v["content_snapshot"],
                    "change_type": v["change_type"],
                    "created_at": v["created_at"],
                }
                for v in versions
            ]
        return json.dumps(snapshot, ensure_ascii=False, indent=2)

    base: dict = {
        "schema_version": "1.0",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "project": _project_brief(project),
    }
    if scope in ("volume", "chapter", "outline", "content"):
        base["outlines"] = _flatten_outlines(roots)
        base["chapter_contents"] = [
            {
                "outline_id": str(node.outline.id),
                "title": node.outline.title,
                "content": (chapter_map.get(node.outline.id).content
                            if chapter_map.get(node.outline.id) else ""),
                "word_count": (chapter_map.get(node.outline.id).word_count
                               if chapter_map.get(node.outline.id) else 0),
                "version": (chapter_map.get(node.outline.id).version
                            if chapter_map.get(node.outline.id) else 1),
            }
            for node in _iter_flat(roots)
            if node.outline.outline_type == "chapter"
        ]
    if scope in ("characters", "knowledge"):
        base["characters"] = []
        for ch in characters:
            item: dict = {"name": ch.name}
            for field in (
                "identity", "personality", "appearance", "background",
                "goal", "conflict", "relationship", "character_status",
            ):
                value = getattr(ch, field)
                if value is not None:
                    item[field] = value
            base["characters"].append(item)
    if scope == "locations":
        base["locations"] = [
            {"name": l.name, "category": l.category, "description": l.description, "importance": l.importance}
            for l in locations
        ]
    if scope == "world":
        base["factions"] = [
            {"name": f.name, "description": f.description, "leader": f.leader, "goal": f.goal}
            for f in factions
        ]
        base["world_rules"] = [
            {"category": r.category, "name": r.name, "content": r.content, "importance": r.importance}
            for r in world_rules
        ]
        base["world_settings"] = [
            {"category": w.category, "name": w.name, "content": w.content, "importance": w.importance}
            for w in world_settings
        ]
    if scope == "timeline":
        base["timeline_events"] = [
            {"title": t.title, "description": t.description, "event_time": t.event_time,
             "sort_order": t.sort_order, "importance": t.importance}
            for t in timeline_events
        ]
    if scope == "relationships":
        base["relationships"] = [
            {"from_name": r.from_name, "to_name": r.to_name, "relation_type": r.relation_type,
             "description": r.description}
            for r in relationships
        ]
    if scope == "tags":
        base["tags"] = [
            {"name": t.name, "color": t.color, "tag_type": t.tag_type}
            for t in tags
        ]
    if scope == "comments":
        base["comments"] = [
            {"target_type": c.target_type, "target_id": c.target_id, "content": c.content,
             "comment_type": c.comment_type,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in comments
        ]
    if scope == "version_history":
        base["version_history"] = [
            {
                "target_type": v["target_type"],
                "target_id": v["target_id"],
                "version_number": v["version_number"],
                "title_snapshot": v["title_snapshot"],
                "content_snapshot": v["content_snapshot"],
                "change_type": v["change_type"],
                "created_at": v["created_at"],
            }
            for v in versions
        ]
    # 阶段C「发布基线绑定」：导出携带当前基线（失败降级 None 不阻断导出）
    try:
        base["baseline"] = await collect_baseline(db, project.id)
    except Exception:  # noqa: BLE001
        base["baseline"] = None
    return json.dumps(base, ensure_ascii=False, indent=2)
