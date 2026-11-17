# 备份 / 恢复服务（第十六批「数据导入、导出与备份恢复」规格实现）
# 备份类型（规格十二/十三）：manual（手动，永久保留）/ automatic（自动）/ pre_publish（发布前）/
#   release_snapshot（发布快照）/ before_import（导入前保护）/ before_restore（恢复前保护）。
# 恢复模式（规格十五/十六）：new_project（默认，恢复为新项目）/ overwrite_current（覆盖当前，需二次确认
#   + 输入作品名 + 自动创建恢复前备份）/ missing_only（仅恢复缺失）/ new_version（恢复到新版本，追加章节）。
# 校验（规格十八）：JSON 完整性 / checksum / 引用对象 / 重复 ID / 不允许数据，返回 valid/errors/warnings。
# 迁移（规格十七）：旧 schema_version（0.9 / 1）自动转换为当前 1.0。
# 保留策略（规格二十）：最近 7 个每日自动备份 + 最近 4 个发布前备份 + 最近 12 个发布快照 + 手动永久保留。
# 安全（规格十九）：storage_key 为逻辑键不暴露真实路径；恢复前预览、覆盖保护、操作日志。
import hashlib
import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collaboration import ActivityLog
from app.models.chapter_content import ChapterContent
from app.models.chapter_version import ChapterVersion
from app.models.character import Character
from app.models.faction import Faction
from app.models.location import Location
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_backup import (
    BACKUP_SCHEMA_VERSION,
    BACKUP_TYPES,
    LEGACY_SCHEMA_VERSIONS,
    ProjectBackup,
)
from app.models.relationship import Relationship
from app.models.search_document import SearchDocument
from app.models.tag import OutlineTag, Tag, TagRelation
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.world_setting import WorldSetting
from app.services.index_service import (
    sync_chapter_content_index,
    sync_outline_index,
)
from app.services.snapshot_service import build_project_snapshot

# ============ 常量（规格十三/十五/二十） ============

BACKUP_TYPES_SET = set(BACKUP_TYPES)  # manual/automatic/pre_publish/release_snapshot/before_import/before_restore
RESTORE_MODES = {"new_project", "overwrite_current", "missing_only", "new_version"}
RESTORE_SCOPES = {"whole", "outline", "content", "knowledge"}
RETENTION_LIMITS = {
    "automatic": 7,          # 最近 7 个每日备份
    "pre_publish": 4,        # 最近 4 个发布前备份
    "release_snapshot": 12,  # 最近 12 个发布快照
}
MAX_BACKUPS_PER_PROJECT = 30   # 单作品备份总数上限
BACKUP_TTL_DAYS = 90           # 自动类备份默认有效期（规格十九：避免长期占用存储）

# 不允许出现在备份快照中的数据（规格十四：登录凭证/密码/API 密钥/服务器内部路径等）
_FORBIDDEN_KEYS = {
    "password", "passwd", "pwd", "api_key", "apikey", "api_token",
    "access_token", "refresh_token", "secret", "private_key",
    "authorization", "cookie", "session_id", "storage_key",
    "server_path", "internal_path", "local_path", "absolute_path",
}
_FORBIDDEN_KEYWORDS = ("password=", "api_key=", "secret=", "token=", "BEGIN RSA", "BEGIN PRIVATE")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default_expires(backup_type: str):
    """自动类备份设置有效期；手动与保护性备份长期保留。"""
    if backup_type in ("automatic", "pre_publish", "release_snapshot"):
        return datetime.utcnow() + timedelta(days=BACKUP_TTL_DAYS)
    return None


# ============ 创建备份 ============

async def create_backup(
    db: AsyncSession,
    project: Project,
    backup_type: str = "manual",
    user_id: uuid.UUID | None = None,
    note: str | None = None,
) -> ProjectBackup:
    """创建备份快照（规格十二/十三）。不提交事务，由调用方 commit。"""
    if backup_type not in BACKUP_TYPES_SET:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"backup_type 仅支持 {'/'.join(sorted(BACKUP_TYPES_SET))}",
        )
    total = (
        await db.execute(
            select(ProjectBackup.id).where(ProjectBackup.project_id == project.id)
        )
    ).scalars().all()
    if len(total) >= MAX_BACKUPS_PER_PROJECT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"备份存储空间已达上限（{MAX_BACKUPS_PER_PROJECT} 个）",
        )

    snapshot = await build_project_snapshot(db, project)
    if note:
        snapshot["_note"] = note
    snapshot_text = json.dumps(snapshot, ensure_ascii=False)
    backup = ProjectBackup(
        project_id=project.id,
        user_id=user_id or project.user_id,
        backup_type=backup_type,
        schema_version=BACKUP_SCHEMA_VERSION,
        snapshot=snapshot_text,
        checksum=_sha256(snapshot_text),
        size=len(snapshot_text.encode("utf-8")),
        storage_key=f"backup://{project.id}/{uuid.uuid4().hex[:12]}",
        expires_at=_default_expires(backup_type),
    )
    db.add(backup)
    await db.flush()

    # 保留策略（规格二十）：自动类备份按类型裁剪最旧条目
    if backup_type in RETENTION_LIMITS:
        await _trim_by_type(db, project.id, backup_type, RETENTION_LIMITS[backup_type])
    return backup


async def _trim_by_type(
    db: AsyncSession,
    project_id: uuid.UUID,
    backup_type: str,
    keep: int,
) -> int:
    """按类型保留最近 keep 个备份，删除更旧的；返回删除数量。"""
    ids = (
        await db.execute(
            select(ProjectBackup.id)
            .where(
                ProjectBackup.project_id == project_id,
                ProjectBackup.backup_type == backup_type,
            )
            .order_by(ProjectBackup.created_at.asc(), ProjectBackup.id.asc())
        )
    ).scalars().all()
    excess = len(ids) - keep
    if excess > 0:
        for old_id in ids[:excess]:
            await db.execute(delete(ProjectBackup).where(ProjectBackup.id == old_id))
    return max(excess, 0)


async def apply_retention_policy(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """应用保留策略（规格二十）；手动备份与保护性备份永不清理。"""
    deleted = 0
    for backup_type, keep in RETENTION_LIMITS.items():
        deleted += await _trim_by_type(db, project_id, backup_type, keep)
    return {"deleted": deleted, "limits": dict(RETENTION_LIMITS)}


# ============ 校验（规格十八） ============

def _collect_ids(snapshot: dict) -> list[tuple[str, str]]:
    """收集各数组条目的 (类型, id)。

    兼容字段 outlines 按 outline_type 归一到 volumes/chapters 镜像组（mirror: 前缀），
    避免新结构 volumes/chapters 与其兼容镜像 outlines 中的同一实体被误判为重复 ID。
    """
    result: list[tuple[str, str]] = []
    for key in ("volumes", "chapters", "characters", "locations", "factions",
                "world_rules", "timeline_events", "relationships", "tags"):
        for item in snapshot.get(key) or []:
            if isinstance(item, dict) and item.get("id"):
                result.append((key, str(item["id"])))
    for item in snapshot.get("outlines") or []:
        if isinstance(item, dict) and item.get("id"):
            ot = item.get("outline_type")
            kind = "volumes" if ot == "volume" else ("chapters" if ot == "chapter" else "outlines")
            result.append((f"mirror:{kind}", str(item["id"])))
    return result


def _scan_forbidden(data, errors: list[str], path: str = "") -> None:
    """递归扫描不允许的数据（规格十四/十八）。"""
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in _FORBIDDEN_KEYS:
                errors.append(f"{path}{k} 属于不允许备份的数据（登录凭证/密钥/内部路径等）")
            _scan_forbidden(v, errors, f"{path}{k}.")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _scan_forbidden(v, errors, f"{path}[{i}].")
    elif isinstance(data, str):
        low = data.lower()
        if any(kw in low for kw in _FORBIDDEN_KEYWORDS):
            errors.append(f"{path} 内容疑似包含敏感数据（密钥/Token 等）")


async def validate_backup(backup: ProjectBackup) -> dict:
    """校验备份（规格十八）：JSON 完整/checksum/引用对象/重复 ID/不允许数据。"""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        snapshot = json.loads(backup.snapshot)
    except json.JSONDecodeError:
        return {
            "valid": False,
            "errors": ["备份快照 JSON 解析失败（数据损坏）"],
            "warnings": [],
            "schema_version": backup.schema_version,
            "checksum_ok": False,
        }

    checksum_ok = _sha256(backup.snapshot) == backup.checksum
    if not checksum_ok:
        errors.append("checksum 校验失败：备份内容可能已被篡改或损坏")

    schema_version = str(snapshot.get("schema_version") or backup.schema_version or "")
    if schema_version not in (BACKUP_SCHEMA_VERSION, *tuple(LEGACY_SCHEMA_VERSIONS)):
        errors.append(
            f"schema_version={schema_version} 无法识别（当前支持 {BACKUP_SCHEMA_VERSION}，可迁移旧版 0.9/1）"
        )
    elif schema_version in LEGACY_SCHEMA_VERSIONS:
        warnings.append(
            f"检测到旧版本备份（schema_version={schema_version}），恢复时将自动迁移到 {BACKUP_SCHEMA_VERSION}"
        )

    # 必需字段：新结构数组，或旧结构兼容字段
    new_fields = ["project", "volumes", "chapters", "characters", "locations",
                  "factions", "world_rules", "timeline_events", "relationships", "tags"]
    missing_new = [f for f in new_fields if f not in snapshot]
    legacy_fields = ["outlines", "chapter_contents", "knowledge_items"]
    if missing_new and not any(f in snapshot for f in legacy_fields):
        errors.append(f"快照缺少必需字段：{', '.join(missing_new)}")

    # 重复 ID（volumes/chapters 与其兼容镜像 outlines 中同一实体视为同一，不判重）
    seen: dict[str, tuple[str, bool]] = {}
    for raw_kind, oid in _collect_ids(snapshot):
        is_mirror = raw_kind.startswith("mirror:")
        base = raw_kind[7:] if is_mirror else raw_kind
        if oid in seen:
            prev_base, prev_mirror = seen[oid]
            if prev_base == base:
                if not (prev_mirror or is_mirror):
                    errors.append(f"检测到重复 ID：{oid} 在 {base} 中重复出现")
            else:
                errors.append(f"检测到重复 ID：{oid} 同时出现在 {prev_base} 与 {base}")
        else:
            seen[oid] = (base, is_mirror)

    # 引用对象有效性
    volumes = {str(v.get("id")) for v in snapshot.get("volumes") or [] if v.get("id")}
    outline_ids = {str(o.get("id")) for o in snapshot.get("outlines") or [] if o.get("id")}
    for ch in snapshot.get("chapters") or []:
        vid = ch.get("volume_id")
        if vid and vid not in volumes and vid not in outline_ids:
            errors.append(f"章节「{ch.get('title')}」的 volume_id 指向不存在的内容")
    for o in snapshot.get("outlines") or []:
        parent = o.get("parent_id")
        if parent and parent not in outline_ids:
            errors.append(f"大纲「{o.get('title')}」的 parent_id 指向不存在的大纲")
    char_ids = {str(c.get("id")) for c in snapshot.get("characters") or [] if c.get("id")}
    for rel in snapshot.get("relationships") or []:
        fid, tid = rel.get("from_character_id"), rel.get("to_character_id")
        if fid and fid not in char_ids:
            errors.append(f"人物关系 {rel.get('id')} 指向不存在的人物 {fid}")
        if tid and tid not in char_ids:
            errors.append(f"人物关系 {rel.get('id')} 指向不存在的人物 {tid}")

    # 不允许的数据
    _scan_forbidden(snapshot, errors)

    # 常见缺失提示
    for ch in snapshot.get("chapters") or []:
        if not ch.get("content") and not ch.get("word_count"):
            warnings.append(f"章节「{ch.get('title')}」缺少正文内容")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "schema_version": schema_version,
        "checksum_ok": checksum_ok,
    }


# 兼容第十批调用（返回 (ok, errors)）
async def verify_backup(backup: ProjectBackup) -> tuple[bool, list[str]]:
    result = await validate_backup(backup)
    return result["valid"], result["errors"]


# ============ 迁移（规格十七） ============

def migrate_snapshot(snapshot: dict) -> dict:
    """将旧 schema_version 备份迁移为当前 1.0 结构（不直接删除旧字段，提供兼容转换）。"""
    version = str(snapshot.get("schema_version") or "")
    if version == BACKUP_SCHEMA_VERSION:
        return snapshot

    if version not in LEGACY_SCHEMA_VERSIONS:
        raise ValueError(f"不支持的备份 schema_version：{version}")

    # 旧版（0.9 / 1）：基于 outlines / chapter_contents / knowledge_items 兼容字段
    outlines = list(snapshot.get("outlines") or [])
    chapter_contents = list(snapshot.get("chapter_contents") or [])
    knowledge_items = list(snapshot.get("knowledge_items") or [])
    content_by_outline = {
        str(cc.get("outline_id")): cc for cc in chapter_contents if cc.get("outline_id")
    }

    volumes = [
        {
            "id": str(o.get("id")),
            "title": o.get("title") or "未命名分卷",
            "summary": o.get("summary"),
            "sort_order": o.get("sort_order") or 0,
            "status": o.get("status") or "active",
        }
        for o in outlines if o.get("outline_type") == "volume"
    ]
    chapters = []
    for o in outlines:
        if o.get("outline_type") != "chapter":
            continue
        oid = str(o.get("id"))
        cc = content_by_outline.get(oid) or {}
        chapters.append({
            "id": oid,
            "volume_id": str(o.get("parent_id")) if o.get("parent_id") else None,
            "title": o.get("title") or cc.get("title") or "未命名章节",
            "content": cc.get("content") or "",
            "word_count": cc.get("word_count") or 0,
            "version": cc.get("version") or 1,
            "sort_order": o.get("sort_order") or 0,
            "status": o.get("status") or "draft",
        })

    characters, world_rules = [], []
    for item in knowledge_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "character":
            characters.append({
                "id": str(item.get("id") or uuid.uuid4()),
                "name": item.get("name") or "未命名人物",
                **{k: v for k, v in item.items() if k in (
                    "identity", "personality", "appearance", "background",
                    "goal", "conflict", "relationship", "character_status") and v is not None},
            })
        else:
            world_rules.append({
                "id": str(item.get("id") or uuid.uuid4()),
                "name": item.get("name") or "未命名条目",
                "category": item.get("category") or "rule",
                "content": item.get("content") or "",
                "importance": item.get("importance") or "normal",
            })

    migrated = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": snapshot.get("exported_at"),
        "project": snapshot.get("project") or {"title": snapshot.get("title") or "未命名作品"},
        "volumes": volumes,
        "chapters": chapters,
        "characters": characters,
        "locations": snapshot.get("locations") or [],
        "factions": snapshot.get("factions") or [],
        "world_rules": world_rules,
        "timeline_events": snapshot.get("timeline_events") or [],
        "relationships": snapshot.get("relationships") or [],
        "tags": snapshot.get("tags") or [],
        # 保留旧字段以便兼容（规格十七：不建议直接删除旧字段）
        "outlines": outlines,
        "chapter_contents": chapter_contents,
        "knowledge_items": knowledge_items,
    }
    if snapshot.get("_note"):
        migrated["_note"] = snapshot["_note"]
    return migrated


# ============ 恢复预览（规格十五） ============

async def get_restore_preview(db: AsyncSession, backup: ProjectBackup) -> dict:
    """恢复前预览：创建时间/作品名/章节数/资料数/版本号/大小/校验结果。"""
    validation = await validate_backup(backup)
    try:
        snapshot = json.loads(backup.snapshot)
        if validation["valid"] or backup.schema_version in LEGACY_SCHEMA_VERSIONS:
            snapshot = migrate_snapshot(snapshot)
    except (json.JSONDecodeError, ValueError):
        snapshot = {}

    project = snapshot.get("project") or {}
    chapters = snapshot.get("chapters") or []
    data_count = (
        len(snapshot.get("characters") or [])
        + len(snapshot.get("locations") or [])
        + len(snapshot.get("factions") or [])
        + len(snapshot.get("world_rules") or [])
        + len(snapshot.get("timeline_events") or [])
        + len(snapshot.get("relationships") or [])
        + len(snapshot.get("tags") or [])
    )
    return {
        "backup_id": backup.id,
        "created_at": backup.created_at,
        "project_title": project.get("title") or "未命名作品",
        "chapter_count": len(chapters),
        "knowledge_count": data_count,
        "version": validation["schema_version"],
        "backup_size": backup.size,
        "backup_type": backup.backup_type,
        "validation": validation,
    }


# ============ 恢复执行（规格十五/十六） ============

async def restore_backup(
    db: AsyncSession,
    backup: ProjectBackup,
    body,
    current_user_id: uuid.UUID,
) -> dict:
    """执行恢复：校验 -> 迁移 -> 覆盖保护 -> 重建数据 -> 写恢复日志。"""
    mode = body.restore_mode or "new_project"
    if mode not in RESTORE_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="restore_mode 仅支持 new_project / overwrite_current / missing_only / new_version",
        )
    if body.scope and body.scope not in RESTORE_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope 仅支持 whole / outline / content / knowledge",
        )

    validation = await validate_backup(backup)
    if not validation["valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="备份校验失败：" + "；".join(validation["errors"]),
        )

    try:
        snapshot = json.loads(backup.snapshot)
        snapshot = migrate_snapshot(snapshot)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"备份解析或迁移失败：{exc}",
        ) from exc

    warnings: list[str] = list(validation["warnings"])
    restored_project: Project | None = None
    created_backup_id: uuid.UUID | None = None
    scope = body.scope or "whole"

    if mode == "new_project":
        proj = snapshot.get("project") or {}
        restored_project = Project(
            user_id=backup.user_id,
            title=(proj.get("title") or "未命名作品") + "（恢复）",
            genre=proj.get("genre"),
            background=proj.get("background"),
            world_setting=proj.get("world_setting"),
            main_conflict=proj.get("main_conflict"),
            ending_direction=proj.get("ending_direction"),
            narrative_perspective=proj.get("narrative_perspective"),
            writing_style=proj.get("writing_style"),
            forbidden_settings=proj.get("forbidden_settings"),
            status="active",
        )
        db.add(restored_project)
        await db.flush()
        scope = "whole"
    else:
        target = (
            await db.execute(
                select(Project).where(Project.id == backup.project_id)
            )
        ).scalar_one_or_none()
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="备份关联的作品不存在",
            )
        if target.user_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权恢复到该作品",
            )
        restored_project = target

        if mode == "overwrite_current":
            # 覆盖保护（规格十六）：输入作品名二次确认 + 自动创建恢复前备份
            expected = target.title
            if not body.confirm_title or body.confirm_title != expected:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="覆盖当前项目需输入当前作品名称确认（confirm_title 必须与当前作品标题一致）",
                )
            before = await create_backup(db, target, "before_restore", user_id=current_user_id)
            created_backup_id = before.id
            # 差异提示
            current_snap = await build_project_snapshot(db, target)
            warnings.append(
                f"覆盖前自动创建保护备份（{before.id}）；当前章节 {len(current_snap.get('chapters') or [])} 个，"
                f"备份章节 {len(snapshot.get('chapters') or [])} 个"
            )
        elif mode == "new_version":
            # 恢复到新版本：不触碰现有数据，备份章节追加为独立分卷
            if scope not in ("whole", "outline"):
                warnings.append("new_version 模式仅恢复章节到新分卷，其余资料范围忽略")
            counts = await _rebuild_as_new_version(db, restored_project, snapshot, warnings)
            counts["created_backup_id"] = created_backup_id
            counts["warnings"] = warnings
            counts["status"] = "completed"
            counts["restore_mode"] = mode
            counts["restored_project_id"] = restored_project.id
            await _write_restore_log(db, restored_project, backup, current_user_id, mode, counts)
            await db.flush()
            return counts

    if mode == "missing_only":
        counts = await _rebuild_missing(db, restored_project, snapshot, warnings)
    else:
        counts = await _rebuild_project_data(db, restored_project, snapshot, scope, warnings)

    result = {
        "status": "completed",
        "restore_mode": mode,
        "restored_project_id": restored_project.id,
        "created_backup_id": created_backup_id,
        "restored_outlines": counts["outlines"],
        "restored_chapters": counts["chapters"],
        "restored_knowledge_items": counts["knowledge_items"],
        "restored_locations": counts["locations"],
        "restored_factions": counts["factions"],
        "restored_world_rules": counts["world_rules"],
        "restored_timeline_events": counts["timeline_events"],
        "restored_relationships": counts["relationships"],
        "restored_tags": counts["tags"],
        "warnings": warnings,
    }
    await _write_restore_log(db, restored_project, backup, current_user_id, mode, result)
    await db.flush()
    return result


async def _write_restore_log(
    db: AsyncSession,
    project: Project,
    backup: ProjectBackup,
    user_id: uuid.UUID,
    mode: str,
    summary: dict,
) -> None:
    """写入恢复日志（规格十六/十九：操作日志）。"""
    log = ActivityLog(
        project_id=project.id,
        user_id=user_id,
        action="backup.restore",
        target_type="project_backup",
        target_id=str(backup.id),
        metadata_json=json.dumps({
            "restore_mode": mode,
            "source_backup": str(backup.id),
            "restored_chapters": summary.get("restored_chapters", 0),
            "restored_project_id": str(project.id),
        }, ensure_ascii=False),
    )
    db.add(log)


async def _clear_project_data(
    db: AsyncSession,
    project_id: uuid.UUID,
    scope: str,
) -> None:
    """按 scope 清理当前作品数据（SQLite 未启用级联外键，显式逐表删除）。"""
    outline_ids = (
        await db.execute(
            select(Outline.id).where(Outline.project_id == project_id)
        )
    ).scalars().all()
    if scope in ("whole", "outline") and outline_ids:
        await db.execute(
            delete(OutlineTag).where(OutlineTag.outline_id.in_(outline_ids))
        )
        await db.execute(
            delete(ChapterVersion).where(ChapterVersion.outline_id.in_(outline_ids))
        )
        await db.execute(
            delete(ChapterContent).where(ChapterContent.outline_id.in_(outline_ids))
        )
        await db.execute(delete(Outline).where(Outline.project_id == project_id))
    if scope in ("whole", "knowledge"):
        await db.execute(delete(Character).where(Character.project_id == project_id))
        await db.execute(delete(WorldSetting).where(WorldSetting.project_id == project_id))
        await db.execute(delete(Location).where(Location.project_id == project_id))
        await db.execute(delete(Faction).where(Faction.project_id == project_id))
        await db.execute(delete(WorldRule).where(WorldRule.project_id == project_id))
        await db.execute(delete(TimelineEvent).where(TimelineEvent.project_id == project_id))
        await db.execute(delete(Relationship).where(Relationship.project_id == project_id))
        tag_ids = (
            await db.execute(select(Tag.id).where(Tag.project_id == project_id))
        ).scalars().all()
        if tag_ids:
            await db.execute(
                delete(TagRelation).where(TagRelation.tag_id.in_(tag_ids))
            )
        await db.execute(delete(Tag).where(Tag.project_id == project_id))
    if scope in ("whole", "outline"):
        await db.execute(delete(SearchDocument).where(SearchDocument.project_id == project_id))
    elif scope == "content" and outline_ids:
        await db.execute(
            delete(SearchDocument).where(
                SearchDocument.project_id == project_id,
                SearchDocument.target_type == "chapter_content",
                SearchDocument.target_id.in_(outline_ids),
            )
        )


async def _rebuild_project_data(
    db: AsyncSession,
    project: Project,
    snapshot: dict,
    scope: str,
    warnings: list[str],
) -> dict:
    counts = {"outlines": 0, "chapters": 0, "knowledge_items": 0, "locations": 0,
              "factions": 0, "world_rules": 0, "timeline_events": 0,
              "relationships": 0, "tags": 0}
    await _clear_project_data(db, project.id, scope)

    rebuild_outline = scope in ("whole", "outline")
    rebuild_knowledge = scope in ("whole", "knowledge")

    id_map: dict[str, uuid.UUID] = {}
    if rebuild_outline:
        pending = list(snapshot.get("outlines") or [])
        while pending:
            progressed = False
            for node in list(pending):
                old_id = str(node.get("id"))
                old_parent = node.get("parent_id")
                if old_parent and old_parent not in id_map:
                    continue
                kind = node.get("outline_type") or "chapter"
                parent_id = id_map.get(old_parent) if old_parent else None
                content = node.get("content") if kind != "chapter" else None
                outline = Outline(
                    project_id=project.id,
                    parent_id=parent_id,
                    title=node.get("title") or "未命名标题",
                    outline_type=kind,
                    summary=node.get("summary"),
                    content=content,
                    sort_order=node.get("sort_order") or 0,
                    status=node.get("status") or "draft",
                )
                db.add(outline)
                await db.flush()
                await sync_outline_index(db, outline)
                id_map[old_id] = outline.id
                counts["outlines"] += 1
                pending.remove(node)
                progressed = True
            if not progressed:
                warnings.append(
                    f"存在无法解析父子关系的 {len(pending)} 个大纲节点，已跳过"
                )
                break

        for cc in snapshot.get("chapter_contents") or []:
            old_outline_id = cc.get("outline_id")
            if old_outline_id not in id_map:
                warnings.append(
                    f"章节正文「{cc.get('title')}」引用的大纲不存在，已跳过"
                )
                continue
            content = cc.get("content") or ""
            db.add(ChapterContent(
                outline_id=id_map[old_outline_id],
                content=content,
                word_count=len(content),
                version=cc.get("version") or 1,
            ))
            await sync_chapter_content_index(
                db, project.id, id_map[old_outline_id],
                cc.get("title") or "", content,
            )
            counts["chapters"] += 1

    if rebuild_knowledge:
        # 人物（新数组优先，兼容 knowledge_items）
        characters = snapshot.get("characters") or []
        if not characters:
            characters = [
                item for item in (snapshot.get("knowledge_items") or [])
                if isinstance(item, dict) and item.get("type") == "character"
            ]
        char_id_map: dict[str, uuid.UUID] = {}
        for item in characters:
            if not isinstance(item, dict):
                continue
            ch = Character(
                project_id=project.id,
                name=item.get("name") or "未命名人物",
                identity=item.get("identity"),
                personality=item.get("personality"),
                appearance=item.get("appearance"),
                background=item.get("background"),
                goal=item.get("goal"),
                conflict=item.get("conflict"),
                relationship=item.get("relationship"),
                character_status=item.get("character_status"),
            )
            db.add(ch)
            await db.flush()
            if item.get("id"):
                char_id_map[str(item["id"])] = ch.id
            counts["knowledge_items"] += 1

        # 世界观设置（WorldSetting 兼容 + WorldRule）
        for item in (snapshot.get("knowledge_items") or []):
            if not isinstance(item, dict) or item.get("type") != "world_setting":
                continue
            db.add(WorldSetting(
                project_id=project.id,
                category=str(item.get("category") or "rule"),
                name=item.get("name") or "未命名条目",
                content=item.get("content") or "",
                importance=item.get("importance") or "normal",
            ))
            counts["knowledge_items"] += 1

        for item in snapshot.get("world_rules") or []:
            if not isinstance(item, dict):
                continue
            db.add(WorldRule(
                project_id=project.id,
                name=item.get("name") or "未命名规则",
                category=item.get("category") or "rule",
                content=item.get("content") or "",
                importance=item.get("importance") or "normal",
            ))
            counts["world_rules"] += 1

        # 地点
        for item in snapshot.get("locations") or []:
            if not isinstance(item, dict):
                continue
            db.add(Location(
                project_id=project.id,
                name=item.get("name") or "未命名地点",
                category=item.get("category") or "other",
                description=item.get("description"),
                importance=item.get("importance") or "normal",
            ))
            counts["locations"] += 1

        # 势力
        for item in snapshot.get("factions") or []:
            if not isinstance(item, dict):
                continue
            db.add(Faction(
                project_id=project.id,
                name=item.get("name") or "未命名势力",
                description=item.get("description"),
                leader=item.get("leader"),
                goal=item.get("goal"),
            ))
            counts["factions"] += 1

        # 时间线事件
        for item in snapshot.get("timeline_events") or []:
            if not isinstance(item, dict):
                continue
            db.add(TimelineEvent(
                project_id=project.id,
                title=item.get("title") or "未命名事件",
                description=item.get("description"),
                event_time=item.get("event_time"),
                sort_order=item.get("sort_order") or 0,
                importance=item.get("importance") or "normal",
            ))
            counts["timeline_events"] += 1

        # 人物关系（引用新人物 ID）
        for item in snapshot.get("relationships") or []:
            if not isinstance(item, dict):
                continue
            fid = item.get("from_character_id")
            tid = item.get("to_character_id")
            db.add(Relationship(
                project_id=project.id,
                from_character_id=char_id_map.get(str(fid)) if fid else None,
                to_character_id=char_id_map.get(str(tid)) if tid else None,
                from_name=item.get("from_name"),
                to_name=item.get("to_name"),
                relation_type=item.get("relation_type") or "unknown",
                description=item.get("description"),
            ))
            counts["relationships"] += 1

        # 标签
        for item in snapshot.get("tags") or []:
            if not isinstance(item, dict):
                continue
            db.add(Tag(
                project_id=project.id,
                name=item.get("name") or "未命名标签",
                color=item.get("color") or "#4F6EF7",
                tag_type=item.get("tag_type"),
            ))
            counts["tags"] += 1

    await db.flush()
    return counts


async def _rebuild_missing(
    db: AsyncSession,
    project: Project,
    snapshot: dict,
    warnings: list[str],
) -> dict:
    """仅恢复缺失内容（规格十五）：不清空现有数据，按标题/名称判缺失。"""
    counts = {"outlines": 0, "chapters": 0, "knowledge_items": 0, "locations": 0,
              "factions": 0, "world_rules": 0, "timeline_events": 0,
              "relationships": 0, "tags": 0}

    existing_outlines = (
        await db.execute(
            select(Outline).where(Outline.project_id == project.id)
        )
    ).scalars().all()
    outline_by_key: dict[tuple[str, str], Outline] = {}
    volume_by_title: dict[str, Outline] = {}
    for o in existing_outlines:
        if o.outline_type == "volume":
            volume_by_title[o.title] = o
        outline_by_key[(o.outline_type, o.title)] = o
    existing_chapter_titles = {o.title for o in existing_outlines if o.outline_type == "chapter"}

    # 大纲（volume -> chapter 层级）
    for node in snapshot.get("outlines") or []:
        kind = node.get("outline_type") or "chapter"
        title = node.get("title") or "未命名标题"
        if kind == "volume":
            if title in volume_by_title:
                continue
            vol = Outline(
                project_id=project.id, parent_id=None, title=title,
                outline_type="volume", summary=node.get("summary"),
                sort_order=node.get("sort_order") or 0,
                status=node.get("status") or "active",
            )
            db.add(vol)
            await db.flush()
            await sync_outline_index(db, vol)
            volume_by_title[title] = vol
            counts["outlines"] += 1
        elif kind == "chapter":
            if title in existing_chapter_titles:
                continue
            parent = volume_by_title.get(node.get("parent_title") or "") if node.get("parent_title") else None
            if not parent and node.get("volume_id"):
                parent = volume_by_title.get(
                    next((v.get("title") for v in (snapshot.get("volumes") or [])
                          if str(v.get("id")) == str(node.get("volume_id"))), ""), None
                )
            outline = Outline(
                project_id=project.id, parent_id=parent.id if parent else None,
                title=title, outline_type="chapter",
                summary=node.get("summary"),
                sort_order=node.get("sort_order") or 0,
                status=node.get("status") or "draft",
            )
            db.add(outline)
            await db.flush()
            await sync_outline_index(db, outline)
            existing_chapter_titles.add(title)
            counts["outlines"] += 1

    # 正文：新章节补齐；已有章节正文缺失才补
    outlines = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project.id,
                Outline.outline_type == "chapter",
            )
        )
    ).scalars().all()
    content_map = {o.title: o for o in outlines}
    for cc in snapshot.get("chapter_contents") or []:
        outline = content_map.get(cc.get("title") or "")
        if not outline:
            continue
        row = (
            await db.execute(
                select(ChapterContent).where(ChapterContent.outline_id == outline.id)
            )
        ).scalar_one_or_none()
        if row is not None and row.content:
            continue
        content = cc.get("content") or ""
        if row is None:
            row = ChapterContent(outline_id=outline.id, content=content,
                                 word_count=len(content), version=cc.get("version") or 1)
            db.add(row)
        else:
            row.content = content
            row.word_count = len(content)
        await sync_chapter_content_index(db, project.id, outline.id, outline.title, content)
        counts["chapters"] += 1

    # 人物（缺失才补）
    existing_names = set(
        (await db.execute(
            select(Character.name).where(Character.project_id == project.id)
        )).scalars().all()
    )
    char_id_map: dict[str, uuid.UUID] = {}
    for item in snapshot.get("characters") or []:
        name = item.get("name") or "未命名人物"
        if name in existing_names:
            continue
        ch = Character(
            project_id=project.id, name=name,
            identity=item.get("identity"), personality=item.get("personality"),
            appearance=item.get("appearance"), background=item.get("background"),
            goal=item.get("goal"), conflict=item.get("conflict"),
            relationship=item.get("relationship"),
            character_status=item.get("character_status"),
        )
        db.add(ch)
        await db.flush()
        if item.get("id"):
            char_id_map[str(item["id"])] = ch.id
        existing_names.add(name)
        counts["knowledge_items"] += 1

    # 地点/势力/世界观/时间线（缺失才补）
    _existing_names = set(
        (await db.execute(
            select(Location.name).where(Location.project_id == project.id)
        )).scalars().all()
    )
    for item in snapshot.get("locations") or []:
        name = item.get("name") or "未命名地点"
        if name in _existing_names:
            continue
        db.add(Location(project_id=project.id, name=name,
                        category=item.get("category") or "other",
                        description=item.get("description"),
                        importance=item.get("importance") or "normal"))
        _existing_names.add(name)
        counts["locations"] += 1

    _faction_names = set(
        (await db.execute(
            select(Faction.name).where(Faction.project_id == project.id)
        )).scalars().all()
    )
    for item in snapshot.get("factions") or []:
        name = item.get("name") or "未命名势力"
        if name in _faction_names:
            continue
        db.add(Faction(project_id=project.id, name=name,
                       description=item.get("description"),
                       leader=item.get("leader"), goal=item.get("goal")))
        _faction_names.add(name)
        counts["factions"] += 1

    _rule_names = set(
        (await db.execute(
            select(WorldRule.name).where(WorldRule.project_id == project.id)
        )).scalars().all()
    )
    for item in snapshot.get("world_rules") or []:
        name = item.get("name") or "未命名规则"
        if name in _rule_names:
            continue
        db.add(WorldRule(project_id=project.id, name=name,
                         category=item.get("category") or "rule",
                         content=item.get("content") or "",
                         importance=item.get("importance") or "normal"))
        _rule_names.add(name)
        counts["world_rules"] += 1

    _te_keys = set(
        (await db.execute(
            select(TimelineEvent.title).where(TimelineEvent.project_id == project.id)
        )).scalars().all()
    )
    for item in snapshot.get("timeline_events") or []:
        title = item.get("title") or "未命名事件"
        if title in _te_keys:
            continue
        db.add(TimelineEvent(project_id=project.id, title=title,
                             description=item.get("description"),
                             event_time=item.get("event_time"),
                             sort_order=item.get("sort_order") or 0,
                             importance=item.get("importance") or "normal"))
        _te_keys.add(title)
        counts["timeline_events"] += 1

    # 人物关系（两端都存在时才补）
    rel_keys = set(
        (await db.execute(
            select(Relationship.relation_type, Relationship.from_name, Relationship.to_name)
            .where(Relationship.project_id == project.id)
        )).all()
    )
    char_by_id: dict[str, Character] = {
        str(c.id): c for c in (
            await db.execute(
                select(Character).where(Character.project_id == project.id)
            )
        ).scalars().all()
    }
    for item in snapshot.get("relationships") or []:
        fid = item.get("from_character_id")
        tid = item.get("to_character_id")
        from_name = item.get("from_name") or (char_by_id.get(str(fid)).name if fid and str(fid) in char_by_id else None)
        to_name = item.get("to_name") or (char_by_id.get(str(tid)).name if tid and str(tid) in char_by_id else None)
        key = (item.get("relation_type") or "unknown", from_name, to_name)
        if key in rel_keys:
            continue
        db.add(Relationship(
            project_id=project.id,
            from_character_id=char_by_id.get(str(fid)).id if fid and str(fid) in char_by_id else None,
            to_character_id=char_by_id.get(str(tid)).id if tid and str(tid) in char_by_id else None,
            from_name=from_name, to_name=to_name,
            relation_type=item.get("relation_type") or "unknown",
            description=item.get("description"),
        ))
        rel_keys.add(key)
        counts["relationships"] += 1

    # 标签
    tag_names = set(
        (await db.execute(
            select(Tag.name).where(Tag.project_id == project.id)
        )).scalars().all()
    )
    for item in snapshot.get("tags") or []:
        name = item.get("name") or "未命名标签"
        if name in tag_names:
            continue
        db.add(Tag(project_id=project.id, name=name,
                   color=item.get("color") or "#4F6EF7",
                   tag_type=item.get("tag_type")))
        tag_names.add(name)
        counts["tags"] += 1

    await db.flush()
    return counts


async def _rebuild_as_new_version(
    db: AsyncSession,
    project: Project,
    snapshot: dict,
    warnings: list[str],
) -> dict:
    """恢复到新版本：将备份章节追加为独立分卷（不触碰现有数据）。"""
    counts = {"outlines": 0, "chapters": 0, "knowledge_items": 0, "locations": 0,
              "factions": 0, "world_rules": 0, "timeline_events": 0,
              "relationships": 0, "tags": 0}
    backup_title = (snapshot.get("project") or {}).get("title") or "恢复版本"
    vol = Outline(
        project_id=project.id, parent_id=None,
        title=f"{backup_title}（恢复版）",
        outline_type="volume", summary="从备份恢复到新版本",
        sort_order=999, status="active",
    )
    db.add(vol)
    await db.flush()
    await sync_outline_index(db, vol)
    counts["outlines"] += 1

    id_map: dict[str, uuid.UUID] = {}
    for node in snapshot.get("outlines") or []:
        if (node.get("outline_type") or "chapter") != "chapter":
            continue
        title = node.get("title") or "未命名章节"
        outline = Outline(
            project_id=project.id, parent_id=vol.id,
            title=title, outline_type="chapter",
            summary=node.get("summary"),
            sort_order=node.get("sort_order") or 0,
            status=node.get("status") or "draft",
        )
        db.add(outline)
        await db.flush()
        await sync_outline_index(db, outline)
        id_map[str(node.get("id"))] = outline.id
        counts["outlines"] += 1

    for cc in snapshot.get("chapter_contents") or []:
        oid = id_map.get(str(cc.get("outline_id")))
        if not oid:
            continue
        content = cc.get("content") or ""
        db.add(ChapterContent(outline_id=oid, content=content,
                              word_count=len(content), version=cc.get("version") or 1))
        await sync_chapter_content_index(db, project.id, oid,
                                         cc.get("title") or "", content)
        counts["chapters"] += 1

    await db.flush()
    return counts


# ============ 删除备份 ============

async def delete_backup(db: AsyncSession, backup: ProjectBackup) -> None:
    """删除备份（规格十九：删除策略）。"""
    await db.delete(backup)
