# 导入任务服务（第十六批「数据导入、导出与备份恢复」规格实现）
# 流程：上传内容 -> 预览(preview，可调整) -> 正式导入(execute) -> 导入报告
# 支持：CSV 表格导入（人物/地点/势力/世界观/时间线/关系/标签）、Markdown 规则解析与拆分选项、
#       预览统计（作品名/分卷数/章节数/预计字数/无法识别/重复/格式警告）、重复策略
#       （skip/create_new/overwrite/merge/mark_duplicate）、错误报告（success/warning/error 计数）。
# 导入前自动创建 before_import 备份；单条失败不中断。
import json
import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.character import Character
from app.models.comment import Comment
from app.models.content_version import ContentVersion
from app.models.faction import Faction
from app.models.import_task import ImportTask
from app.models.location import Location
from app.models.outline import Outline
from app.models.project import Project
from app.models.relationship import Relationship
from app.models.tag import Tag
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.world_setting import WorldSetting
from app.schemas.import_export import (
    ImportExecuteRequest,
    ImportPreviewRequest,
)
from app.services.backup_service import create_backup
from app.services.import_parser import (
    MAX_CHAPTER_LENGTH,
    parse_import,
    strip_script_tags,
)
from app.services.index_service import (
    sync_chapter_content_index,
    sync_outline_index,
)

DUPLICATE_STRATEGIES = {
    "skip", "create_new", "overwrite", "merge", "mark_duplicate",
}
ROOT_TYPES = {"outline", "project"}

# 层级父子映射（kind -> 允许的父 kind）
_PARENT_KIND = {
    "summary": None,
    "volume": "summary",
    "chapter": "volume",
    "scene": "chapter",
}


def _to_preview_dict(parsed: dict, split_mode: str) -> dict:
    """把 parser 输出转成预览响应结构（规格五）。"""
    return {
        "title": parsed.get("title") or "",
        "file_type": parsed.get("file_type") or "",
        "root_type": parsed.get("root_type") or "outline",
        "split_mode": split_mode,
        "volume_count": parsed.get("volume_count") or 0,
        "chapter_count": parsed.get("chapter_count") or 0,
        "estimated_word_count": parsed.get("estimated_word_count") or 0,
        "total_items": parsed.get("total_items") or 0,
        "characters_count": len(parsed.get("characters") or []),
        "locations_count": len(parsed.get("locations") or []),
        "factions_count": len(parsed.get("factions") or []),
        "world_rules_count": len(parsed.get("world_rules") or []),
        "timeline_events_count": len(parsed.get("timeline_events") or []),
        "relationships_count": len(parsed.get("relationships") or []),
        "tags_count": len(parsed.get("tags") or []),
        "comments_count": len(parsed.get("comments") or []),
        "versions_count": len(parsed.get("versions") or []),
        "unrecognized": parsed.get("unrecognized") or [],
        "duplicates": parsed.get("duplicates") or [],
        "warnings": parsed.get("warnings") or [],
        "schema_version": parsed.get("schema_version"),
    }


async def preview_import(
    db: AsyncSession,
    user_id: uuid.UUID,
    body: ImportPreviewRequest,
) -> dict:
    """导入预览：解析内容并返回预览统计与可调整项。不创建任何任务。"""
    try:
        parsed = parse_import(body.file_name, body.file_type, body.content, body.split_mode)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    for node in parsed["structure"]:
        node["content"] = strip_script_tags(node["content"])
    return _to_preview_dict(parsed, body.split_mode)


async def create_import_task(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    body: ImportExecuteRequest,
    file_name: str,
    file_type: str,
    content: str,
) -> ImportTask:
    """创建导入任务：解析并落 preview 状态，等待确认执行。"""
    task = ImportTask(
        user_id=user_id,
        project_id=project_id,
        file_name=file_name,
        file_type=file_type,
        source_type=file_type,
        status="parsing",
        options=json.dumps(body.model_dump(mode="json"), ensure_ascii=False),
        total_items=0,
        processed_items=0,
    )
    db.add(task)
    await db.flush()
    try:
        parsed = parse_import(file_name, file_type, content, body.split_mode)
        for node in parsed["structure"]:
            node["content"] = strip_script_tags(node["content"])
        task.status = "preview"
        task.total_items = parsed.get("total_items") or 0
        task.preview_data = json.dumps(parsed, ensure_ascii=False)
        task.started_at = datetime.utcnow()
        task.progress = 10
    except ValueError as error:
        task.status = "failed"
        task.error_message = str(error)
        task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return task


async def confirm_import(
    db: AsyncSession,
    import_task: ImportTask,
    body: ImportExecuteRequest,
) -> dict:
    """确认导入：备份 -> 执行 -> 报告（兼容任务流程入口）。"""
    if import_task.status not in ("preview", "importing"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有处于 preview 状态的导入任务可以确认",
        )
    if body.duplicate_strategy not in DUPLICATE_STRATEGIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"duplicate_strategy 仅支持 {'/'.join(sorted(DUPLICATE_STRATEGIES))}",
        )
    if not import_task.preview_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="导入任务缺少预览数据",
        )
    parsed = json.loads(import_task.preview_data)

    target_project: Project | None = None
    if import_task.project_id:
        target_project = (
            await db.execute(
                select(Project).where(Project.id == import_task.project_id)
            )
        ).scalar_one_or_none()
        if target_project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="目标项目不存在",
            )
        # 导入前备份（规格十：before_import；避免覆盖前丢失现有内容）
        if body.duplicate_strategy in ("overwrite", "merge"):
            await create_backup(db, target_project, "before_import")

    import_task.status = "importing"
    import_task.started_at = import_task.started_at or datetime.utcnow()
    import_task.progress = 20
    await db.flush()

    report = await _execute_import(
        db, target_project, parsed, body, import_task.user_id
    )

    import_task.status = "completed"
    import_task.processed_items = parsed.get("total_items", 0)
    import_task.progress = 100
    import_task.completed_at = datetime.utcnow()
    import_task.report_data = json.dumps(report, ensure_ascii=False)
    await db.commit()
    await db.refresh(import_task)
    return report


async def cancel_import(db: AsyncSession, import_task: ImportTask) -> ImportTask:
    if import_task.status not in {"pending", "parsing", "preview", "importing"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前任务无法取消",
        )
    import_task.status = "cancelled"
    import_task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(import_task)
    return import_task


async def _execute_import(
    db: AsyncSession,
    project: Project | None,
    parsed: dict,
    body: ImportExecuteRequest,
    user_id: uuid.UUID,
) -> dict:
    """正式导入：可导入到现有项目或创建新项目；返回规格七错误报告结构。"""
    report = {
        "status": "completed",
        "success_count": 0,
        "warning_count": 0,
        "error_count": 0,
        "errors": [],
        "warnings": list(parsed.get("warnings") or []),
        "created_outlines": 0,
        "created_chapters": 0,
        "created_knowledge_items": 0,
        "created_characters": 0,
        "created_locations": 0,
        "created_factions": 0,
        "created_world_rules": 0,
        "created_timeline_events": 0,
        "created_relationships": 0,
        "created_tags": 0,
        "created_comments": 0,
        "created_versions": 0,
        "skipped_items": 0,
        "overwritten_items": 0,
        "merged_items": 0,
        "marked_duplicates": 0,
        "created_project_id": None,
    }

    target_project = project
    created_project_id: uuid.UUID | None = None
    if target_project is None:
        new_title = body.title or (parsed.get("title") or "未命名作品")
        if not new_title.endswith("（导入）") and parsed.get("root_type") == "project":
            new_title = new_title + "（导入）"
        target_project = Project(
            user_id=user_id,
            title=new_title,
            status="active",
        )
        db.add(target_project)
        await db.flush()
        created_project_id = target_project.id

    strategy = body.duplicate_strategy
    if body.overwrite and strategy == "create_new":
        strategy = "overwrite"
    if strategy not in DUPLICATE_STRATEGIES:
        strategy = "create_new"

    structure = parsed.get("structure") or []
    characters = parsed.get("characters") or []
    locations = parsed.get("locations") or []
    factions = parsed.get("factions") or []
    world_rules = parsed.get("world_rules") or []
    timeline_events = parsed.get("timeline_events") or []
    relationships = parsed.get("relationships") or []
    tags = parsed.get("tags") or []
    comments = parsed.get("comments") or []
    versions = parsed.get("versions") or []
    knowledge_items = parsed.get("knowledge_items") or []

    # 已有条目查重集合
    existing_outline_keys: set[tuple[str, str]] = set()
    existing_character_names: set[str] = set()
    existing_ws_keys: set[tuple[str, str]] = set()
    existing_tag_keys: set[tuple[str, str | None]] = set()
    if strategy in ("skip", "overwrite", "merge"):
        existing_outline_keys = await _load_outline_keys(db, target_project.id)
        existing_character_names = await _load_character_names(db, target_project.id)
        existing_ws_keys = await _load_world_setting_keys(db, target_project.id)
        existing_tag_keys = await _load_tag_keys(db, target_project.id)

    # 大纲重建（层级栈）
    parent_stack: dict[int, Outline | None] = {}
    chapter_no = 0
    for idx, node in enumerate(structure):
        item = f"chapter_{idx + 1}"
        kind = node.get("kind") or "chapter"
        raw_title = (node.get("title") or "").strip()
        content = node.get("content") or ""
        summary = node.get("summary")
        level = node.get("level") or _level_of(kind)

        # 不导入标题时用序号命名（规格五）
        title = raw_title
        if not body.import_titles:
            if kind == "volume":
                title = f"第{chapter_no + 1}卷"
            elif kind == "chapter":
                title = f"第{chapter_no + 1}章"
            else:
                title = f"未命名{kind}"
        if not title.strip():
            title = "未命名标题"

        parent = None
        for candidate_level in range(level - 1, 0, -1):
            if parent_stack.get(candidate_level) is not None:
                parent = parent_stack[candidate_level]
                break
        if parent is None and level > 1:
            root = Outline(
                project_id=target_project.id,
                parent_id=None,
                title=body.title or (parsed.get("title") or "未命名作品"),
                outline_type="summary",
                sort_order=0,
                status="draft",
            )
            db.add(root)
            await db.flush()
            await sync_outline_index(db, root)
            parent_stack[1] = root
            parent = root
            report["created_outlines"] += 1
            report["success_count"] += 1

        # 保留格式（规格五）：preserve_format=False 时剥离引用/列表标记
        if not body.preserve_format:
            lines = []
            for line in content.splitlines():
                if line.startswith("> "):
                    lines.append(line[2:])
                elif line.startswith("- "):
                    lines.append(line[2:])
                else:
                    lines.append(line)
            content = "\n".join(lines)

        # 重复检测与策略执行（规格六）
        duplicate = (kind, title) in existing_outline_keys
        duplicate_target = None
        if duplicate and kind in ("chapter", "volume", "scene"):
            duplicate_target = await _load_outline_by_key(
                db, target_project.id, kind, title
            )

        if duplicate and strategy == "skip":
            report["skipped_items"] += 1
            report["warning_count"] += 1
            parent_stack[level] = duplicate_target
            continue
        if duplicate and strategy == "overwrite" and duplicate_target is not None:
            duplicate_target.title = title
            duplicate_target.summary = summary if summary is not None else duplicate_target.summary
            if kind == "chapter":
                cc = (
                    await db.execute(
                        select(ChapterContent).where(
                            ChapterContent.outline_id == duplicate_target.id
                        )
                    )
                ).scalar_one_or_none()
                if cc is None:
                    cc = ChapterContent(
                        outline_id=duplicate_target.id,
                        content=content[:MAX_CHAPTER_LENGTH],
                        word_count=len(content),
                        version=1,
                    )
                    db.add(cc)
                else:
                    cc.content = content[:MAX_CHAPTER_LENGTH]
                    cc.word_count = len(content)
                    cc.version = (cc.version or 1) + 1
                await db.flush()
                await sync_chapter_content_index(
                    db, target_project.id, duplicate_target.id, title, content
                )
                report["created_chapters"] += 1
            else:
                duplicate_target.content = content
            report["overwritten_items"] += 1
            report["success_count"] += 1
            parent_stack[level] = duplicate_target
            continue
        if duplicate and strategy == "merge" and duplicate_target is not None:
            if kind == "chapter":
                cc = (
                    await db.execute(
                        select(ChapterContent).where(
                            ChapterContent.outline_id == duplicate_target.id
                        )
                    )
                ).scalar_one_or_none()
                merged = content
                if cc is not None and cc.content:
                    merged = (cc.content.rstrip("\n") + "\n\n" + content.strip())[:MAX_CHAPTER_LENGTH]
                    cc.content = merged
                    cc.word_count = len(merged)
                    cc.version = (cc.version or 1) + 1
                else:
                    cc = ChapterContent(
                        outline_id=duplicate_target.id,
                        content=content[:MAX_CHAPTER_LENGTH],
                        word_count=len(content),
                        version=1,
                    )
                    db.add(cc)
                await db.flush()
                await sync_chapter_content_index(
                    db, target_project.id, duplicate_target.id, title, merged if "merged" in dir() else content
                )
                report["created_chapters"] += 1
            report["merged_items"] += 1
            report["success_count"] += 1
            parent_stack[level] = duplicate_target
            continue

        if duplicate and strategy == "mark_duplicate":
            report["marked_duplicates"] += 1
            report["warning_count"] += 1

        outline = Outline(
            project_id=target_project.id,
            parent_id=parent.id if parent else None,
            title=title,
            outline_type=kind,
            summary=summary,
            content=content if kind != "chapter" else None,
            sort_order=chapter_no if kind == "chapter" else 0,
            status="draft",
        )
        db.add(outline)
        await db.flush()
        await sync_outline_index(db, outline)
        report["created_outlines"] += 1
        report["success_count"] += 1
        parent_stack[level] = outline

        if kind == "chapter":
            if not content.strip() and not body.import_empty_chapters:
                report["skipped_items"] += 1
                parent_stack[level] = None
                continue
            if content and len(content) > MAX_CHAPTER_LENGTH:
                content = content[:MAX_CHAPTER_LENGTH]
            cc = ChapterContent(
                outline_id=outline.id,
                content=content,
                word_count=len(content),
                version=1,
            )
            db.add(cc)
            await db.flush()
            await sync_chapter_content_index(db, target_project.id, outline.id, title, content)
            report["created_chapters"] += 1
            chapter_no += 1

    # 人物
    if body.import_characters:
        for ch in characters:
            name = (ch.get("name") or "").strip() or "未命名人物"
            if name in existing_character_names and strategy in ("skip", "overwrite", "merge"):
                report["skipped_items"] += 1
                continue
            try:
                db.add(Character(
                    project_id=target_project.id,
                    name=name,
                    identity=ch.get("identity"),
                    personality=ch.get("personality"),
                    appearance=ch.get("appearance"),
                    background=ch.get("background"),
                    goal=ch.get("goal"),
                    conflict=ch.get("conflict"),
                    relationship=ch.get("relationship"),
                    character_status=ch.get("character_status"),
                ))
                report["created_knowledge_items"] += 1
                report["created_characters"] += 1
                report["success_count"] += 1
                existing_character_names.add(name)
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": name, "message": str(error)})

    # 知识条目（兼容字段）
    for ws in knowledge_items:
        ws_name = (ws.get("name") or "").strip() or "未命名条目"
        key = (str(ws.get("category") or "rule"), ws_name)
        if key in existing_ws_keys and strategy in ("skip", "overwrite", "merge"):
            report["skipped_items"] += 1
            continue
        try:
            db.add(WorldSetting(
                project_id=target_project.id,
                category=str(ws.get("category") or "rule"),
                name=ws_name,
                content=ws.get("content") or "",
                importance=ws.get("importance") or "normal",
            ))
            report["created_knowledge_items"] += 1
            report["success_count"] += 1
        except Exception as error:
            report["error_count"] += 1
            report["errors"].append({"item": ws_name, "message": str(error)})

    # 地点
    if body.import_locations:
        for loc in locations:
            loc_name = (loc.get("name") or "").strip() or "未命名地点"
            try:
                db.add(Location(
                    project_id=target_project.id,
                    name=loc_name,
                    category=loc.get("category"),
                    description=loc.get("description"),
                    importance=loc.get("importance") or "normal",
                ))
                report["created_locations"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": loc_name, "message": str(error)})

    # 势力
    if body.import_factions:
        for f in factions:
            f_name = (f.get("name") or "").strip() or "未命名势力"
            try:
                db.add(Faction(
                    project_id=target_project.id,
                    name=f_name,
                    description=f.get("description"),
                    leader=f.get("leader"),
                    goal=f.get("goal"),
                ))
                report["created_factions"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": f_name, "message": str(error)})

    # 世界观规则
    if body.import_world_rules:
        for rule in world_rules:
            r_name = (rule.get("name") or "").strip() or "未命名规则"
            try:
                db.add(WorldRule(
                    project_id=target_project.id,
                    name=r_name,
                    category=rule.get("category") or "rule",
                    content=rule.get("content") or "",
                    importance=rule.get("importance") or "normal",
                ))
                report["created_world_rules"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": r_name, "message": str(error)})

    # 时间线事件
    if body.import_timeline_events:
        for te in timeline_events:
            te_title = (te.get("title") or "").strip() or "未命名事件"
            try:
                db.add(TimelineEvent(
                    project_id=target_project.id,
                    title=te_title,
                    description=te.get("description"),
                    event_time=te.get("event_time"),
                    sort_order=int(te.get("sort_order") or 0),
                    importance=te.get("importance") or "normal",
                ))
                report["created_timeline_events"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": te_title, "message": str(error)})

    # 人物关系（按名称解析角色 ID）
    if body.import_relationships:
        character_id_by_name = await _load_character_id_map(db, target_project.id)
        for rel in relationships:
            from_name = (rel.get("from_name") or "").strip()
            to_name = (rel.get("to_name") or "").strip()
            if not from_name or not to_name:
                report["error_count"] += 1
                report["errors"].append({"item": f"{from_name}->{to_name}", "message": "关系缺少一端人物"})
                continue
            try:
                db.add(Relationship(
                    project_id=target_project.id,
                    from_character_id=character_id_by_name.get(from_name),
                    to_character_id=character_id_by_name.get(to_name),
                    from_name=from_name,
                    to_name=to_name,
                    relation_type=rel.get("relation_type") or "related",
                    description=rel.get("description"),
                ))
                report["created_relationships"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": f"{from_name}->{to_name}", "message": str(error)})

    # 标签
    if body.import_tags:
        for t in tags:
            tname = (t.get("name") or "").strip()
            if not tname:
                report["skipped_items"] += 1
                continue
            tkey = (tname, t.get("tag_type"))
            if tkey in existing_tag_keys and strategy in ("skip", "overwrite", "merge"):
                report["skipped_items"] += 1
                continue
            try:
                db.add(Tag(
                    project_id=target_project.id,
                    name=tname,
                    color=t.get("color") or "#4F6EF7",
                    tag_type=t.get("tag_type"),
                ))
                report["created_tags"] += 1
                report["success_count"] += 1
                existing_tag_keys.add(tkey)
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": tname, "message": str(error)})

    # 评论（仅公开评论；私人批注不导入，规格九隐私默认策略）
    if body.import_comments:
        for c in comments:
            ctype = c.get("type") or "public_comment"
            if ctype in ("private_comment", "private"):
                report["warning_count"] += 1
                continue
            target_title = (c.get("target_title") or "").strip()
            target_id = ""
            if target_title:
                found = (
                    await db.execute(
                        select(Outline).where(
                            Outline.project_id == target_project.id,
                            Outline.title == target_title,
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if found is not None:
                    target_id = str(found.id)
            if not target_id:
                target_id = str(target_project.id)
            try:
                db.add(Comment(
                    project_id=target_project.id,
                    user_id=user_id,
                    target_type=c.get("target_type") or "chapter",
                    target_id=target_id,
                    content=(c.get("content") or "")[:5000],
                    comment_type="public_comment",
                    status="open",
                ))
                report["created_comments"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": f"comment:{target_title}", "message": str(error)})

    # 版本历史（规格二：版本信息导入；按标题匹配目标，change_type=import）
    if body.import_versions:
        outline_by_title: dict[str, uuid.UUID] = {}
        rows = (
            await db.execute(
                select(Outline.id, Outline.title).where(
                    Outline.project_id == target_project.id
                )
            )
        ).all()
        for rid, rtitle in rows:
            outline_by_title.setdefault(rtitle or "", rid)
        for v in versions:
            v_title = (v.get("target_title") or "").strip()
            target_id = outline_by_title.get(v_title, target_project.id)
            target_type = "outline" if v_title and target_id != target_project.id else "chapter"
            try:
                db.add(ContentVersion(
                    project_id=target_project.id,
                    target_type=v.get("target_type") or target_type,
                    target_id=target_id,
                    version_number=int(v.get("version_number") or 1),
                    title_snapshot=v_title or None,
                    content_snapshot=(v.get("content") or "")[:100000],
                    change_type="import",
                    change_summary="导入的版本记录",
                    source="import",
                    status="stable",
                    created_by=user_id,
                ))
                report["created_versions"] += 1
                report["success_count"] += 1
            except Exception as error:
                report["error_count"] += 1
                report["errors"].append({"item": f"version:{v_title}", "message": str(error)})

    await db.flush()
    report["warning_count"] += len(report["warnings"])
    if created_project_id:
        report["created_project_id"] = str(created_project_id)
    return report


def _level_of(kind: str) -> int:
    return {"summary": 1, "volume": 2, "chapter": 3, "scene": 4}.get(kind, 3)


async def _load_outline_keys(db: AsyncSession, project_id: uuid.UUID) -> set[tuple[str, str]]:
    rows = (await db.execute(
        select(Outline).where(Outline.project_id == project_id)
    )).scalars().all()
    return {(o.outline_type, o.title) for o in rows}


async def _load_outline_by_key(
    db: AsyncSession, project_id: uuid.UUID, outline_type: str, title: str
) -> Outline | None:
    return (
        await db.execute(
            select(Outline)
            .where(
                Outline.project_id == project_id,
                Outline.outline_type == outline_type,
                Outline.title == title,
            )
            .order_by(Outline.sort_order, Outline.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_character_names(db: AsyncSession, project_id: uuid.UUID) -> set[str]:
    rows = (await db.execute(
        select(Character.name).where(Character.project_id == project_id)
    )).scalars().all()
    return set(rows)


async def _load_character_id_map(db: AsyncSession, project_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = (await db.execute(
        select(Character.id, Character.name).where(Character.project_id == project_id)
    )).all()
    return {name: rid for rid, name in rows}


async def _load_world_setting_keys(db: AsyncSession, project_id: uuid.UUID) -> set[tuple[str, str]]:
    rows = (await db.execute(
        select(WorldSetting).where(WorldSetting.project_id == project_id)
    )).scalars().all()
    return {(w.category, w.name) for w in rows}


async def _load_tag_keys(db: AsyncSession, project_id: uuid.UUID) -> set[tuple[str, str | None]]:
    rows = (await db.execute(
        select(Tag).where(Tag.project_id == project_id)
    )).scalars().all()
    return {(t.name, t.tag_type) for t in rows}
