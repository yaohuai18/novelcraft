# 作品快照服务（第十六批「数据导入、导出与备份恢复」规格实现）
# 供 JSON 导出与备份共用：组装完整作品快照。
# 结构按规格十一：schema_version / exported_at / project / volumes / chapters /
# characters / locations / factions / world_rules / timeline_events / relationships / tags。
# 同时保留 outlines / chapter_contents / knowledge_items 兼容字段（供旧恢复逻辑与第十批测试使用）。
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.character import Character
from app.models.faction import Faction
from app.models.location import Location
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_backup import BACKUP_SCHEMA_VERSION
from app.models.relationship import Relationship
from app.models.tag import OutlineTag, Tag
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.world_setting import WorldSetting

# 快照允许的 project 字段（避免把系统内部字段暴露给用户）
_PROJECT_FIELDS = [
    "id",
    "title",
    "genre",
    "background",
    "world_setting",
    "main_conflict",
    "ending_direction",
    "narrative_perspective",
    "writing_style",
    "forbidden_settings",
    "status",
]


async def build_project_snapshot(db: AsyncSession, project: Project) -> dict:
    """组装完整作品快照（JSON 导出 / 备份共用），返回规格十一结构。"""
    project_data = {
        field: getattr(project, field) for field in _PROJECT_FIELDS
        if getattr(project, field) is not None
    }
    project_data["id"] = str(project.id)

    outline_rows = (
        await db.execute(
            select(Outline)
            .where(Outline.project_id == project.id)
            .order_by(Outline.sort_order, Outline.created_at)
        )
    ).scalars().all()
    outlines = [
        {
            "id": str(o.id),
            "parent_id": str(o.parent_id) if o.parent_id else None,
            "title": o.title,
            "outline_type": o.outline_type,
            "summary": o.summary,
            "content": o.content,
            "sort_order": o.sort_order,
            "status": o.status,
        }
        for o in outline_rows
    ]
    # 规格十一：volumes / chapters 为扁平数组
    volumes = [
        {
            "id": o["id"],
            "title": o["title"],
            "summary": o["summary"],
            "sort_order": o["sort_order"],
            "status": o["status"],
        }
        for o in outlines if o["outline_type"] == "volume"
    ]
    chapter_outlines = [o for o in outlines if o["outline_type"] == "chapter"]

    outline_ids = [o.id for o in outline_rows]
    chapter_contents: list[dict] = []
    content_map: dict[uuid.UUID, ChapterContent] = {}
    title_map: dict[uuid.UUID, str] = {o.id: o.title for o in outline_rows}
    if outline_ids:
        cc_rows = (
            await db.execute(
                select(ChapterContent).where(
                    ChapterContent.outline_id.in_(outline_ids)
                )
            )
        ).scalars().all()
        for cc in cc_rows:
            content_map[cc.outline_id] = cc
            chapter_contents.append({
                "outline_id": str(cc.outline_id),
                "title": title_map.get(cc.outline_id, ""),
                "content": cc.content,
                "word_count": cc.word_count,
                "version": cc.version,
            })

    chapters = []
    for o in chapter_outlines:
        oid = uuid.UUID(o["id"])
        cc = content_map.get(oid)
        chapters.append({
            "id": o["id"],
            "volume_id": o["parent_id"],
            "title": o["title"],
            "content": cc.content if cc else "",
            "word_count": cc.word_count if cc else 0,
            "version": cc.version if cc else 1,
            "sort_order": o["sort_order"],
            "status": o["status"],
        })

    # 人物
    char_rows = (
        await db.execute(
            select(Character).where(Character.project_id == project.id)
            .order_by(Character.created_at)
        )
    ).scalars().all()
    characters = []
    for ch in char_rows:
        item = {"id": str(ch.id), "name": ch.name}
        for field in (
            "identity", "personality", "appearance", "background",
            "goal", "conflict", "relationship", "character_status",
        ):
            value = getattr(ch, field)
            if value is not None:
                item[field] = value
        characters.append(item)

    # 知识条目（兼容第十批字段）
    knowledge_items: list[dict] = []
    for ch in char_rows:
        item = {"type": "character", "name": ch.name}
        for field in (
            "identity", "personality", "appearance", "background",
            "goal", "conflict", "relationship", "character_status",
        ):
            value = getattr(ch, field)
            if value is not None:
                item[field] = value
        knowledge_items.append(item)

    # 地点
    location_rows = (
        await db.execute(
            select(Location).where(Location.project_id == project.id)
            .order_by(Location.created_at)
        )
    ).scalars().all()
    locations = [
        {
            "id": str(l.id),
            "name": l.name,
            "category": l.category,
            "description": l.description,
            "importance": l.importance,
        }
        for l in location_rows
    ]

    # 势力
    faction_rows = (
        await db.execute(
            select(Faction).where(Faction.project_id == project.id)
            .order_by(Faction.created_at)
        )
    ).scalars().all()
    factions = [
        {
            "id": str(f.id),
            "name": f.name,
            "description": f.description,
            "leader": f.leader,
            "goal": f.goal,
        }
        for f in faction_rows
    ]

    # 世界观规则（WorldRule 结构化 + WorldSetting 知识条目兼容）
    rule_rows = (
        await db.execute(
            select(WorldRule).where(WorldRule.project_id == project.id)
            .order_by(WorldRule.created_at)
        )
    ).scalars().all()
    world_rules = [
        {
            "id": str(r.id),
            "name": r.name,
            "category": r.category,
            "content": r.content,
            "importance": r.importance,
        }
        for r in rule_rows
    ]

    ws_rows = (
        await db.execute(
            select(WorldSetting).where(WorldSetting.project_id == project.id)
            .order_by(WorldSetting.created_at)
        )
    ).scalars().all()
    for ws in ws_rows:
        knowledge_items.append({
            "type": "world_setting",
            "category": ws.category,
            "name": ws.name,
            "content": ws.content,
            "importance": ws.importance,
        })
        world_rules.append({
            "id": str(ws.id),
            "name": ws.name,
            "category": ws.category,
            "content": ws.content,
            "importance": ws.importance,
        })

    # 时间线事件
    te_rows = (
        await db.execute(
            select(TimelineEvent).where(TimelineEvent.project_id == project.id)
            .order_by(TimelineEvent.sort_order, TimelineEvent.created_at)
        )
    ).scalars().all()
    timeline_events = [
        {
            "id": str(te.id),
            "title": te.title,
            "description": te.description,
            "event_time": te.event_time,
            "sort_order": te.sort_order,
            "importance": te.importance,
        }
        for te in te_rows
    ]

    # 人物关系
    rel_rows = (
        await db.execute(
            select(Relationship).where(Relationship.project_id == project.id)
            .order_by(Relationship.created_at)
        )
    ).scalars().all()
    relationships = [
        {
            "id": str(r.id),
            "from_character_id": str(r.from_character_id) if r.from_character_id else None,
            "to_character_id": str(r.to_character_id) if r.to_character_id else None,
            "from_name": r.from_name,
            "to_name": r.to_name,
            "relation_type": r.relation_type,
            "description": r.description,
        }
        for r in rel_rows
    ]

    # 标签
    tag_rows = (
        await db.execute(
            select(Tag).where(Tag.project_id == project.id)
            .order_by(Tag.created_at)
        )
    ).scalars().all()
    tags = [
        {
            "id": str(t.id),
            "name": t.name,
            "color": t.color,
            "tag_type": t.tag_type,
        }
        for t in tag_rows
    ]

    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": project_data,
        "volumes": volumes,
        "chapters": chapters,
        "characters": characters,
        "locations": locations,
        "factions": factions,
        "world_rules": world_rules,
        "timeline_events": timeline_events,
        "relationships": relationships,
        "tags": tags,
        # 兼容字段（旧恢复逻辑与第十批导出）
        "outlines": outlines,
        "chapter_contents": chapter_contents,
        "knowledge_items": knowledge_items,
    }


def _outline_title(outline_rows: list[Outline], outline_id: uuid.UUID) -> str:
    for o in outline_rows:
        if o.id == outline_id:
            return o.title
    return ""
