# 人物路由：CRUD + 状态快照（第三期「人物和时间不漂」）
# 接口路径与行为严格按第三批规格实现；全部需 JWT 鉴权 + 作品归属校验
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.character import Character
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.project import Project
from app.models.user import User
from app.schemas.character import CharacterCreate, CharacterResponse, CharacterUpdate
from app.schemas.character_state_snapshot import (
    CharacterStateSnapshotCreate,
    CharacterStateSnapshotResponse,
)
from app.services import index_service

# 角色定位 / 生存状态 允许值（与 schema 保持一致）
ROLE_VALUES = ["主角", "配角", "反派", "路人"]
STATUS_VALUES = ["在世", "死亡", "失踪", "未知"]
SNAPSHOT_SOURCES = ["ai_extracted", "user_confirmed", "manual"]
SNAPSHOT_STATUSES = ["draft", "confirmed", "rejected"]


def _character_index_text(character) -> str:
    """人物索引文本：拼接全部资料字段（含第三期字段）。"""
    return " ".join(str(getattr(character, f, "") or "") for f in (
        "identity", "personality", "appearance", "background",
        "goal", "conflict", "relationship", "character_status",
        "name", "aliases", "role", "status", "profile", "goals",
        "secrets", "abilities", "current_location",
        "current_timeline_status",
    ))


def _dump_aliases(aliases) -> str | None:
    """aliases 兼容：list 或 JSON 字符串统一转 JSON 字符串存储。"""
    if aliases is None:
        return None
    if isinstance(aliases, str):
        try:
            json.loads(aliases)
            return aliases
        except Exception:  # noqa: BLE001
            return json.dumps([aliases], ensure_ascii=False)
    return json.dumps(list(aliases), ensure_ascii=False)


def _parse_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:  # noqa: BLE001
        pass
    return []


def _validate_role_status(role: str | None, status_: str | None) -> None:
    if role is not None and role not in ROLE_VALUES:
        raise HTTPException(status_code=422, detail=f"role 取值必须为 {ROLE_VALUES}")
    if status_ is not None and status_ not in STATUS_VALUES:
        raise HTTPException(status_code=422, detail=f"status 取值必须为 {STATUS_VALUES}")


router = APIRouter(
    prefix="/api/projects/{project_id}/characters",
    tags=["人物"]
)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
    """校验作品存在且归属当前用户，非本人作品统一返回 404。"""
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )
    return project


async def get_project_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Character:
    """定位项目内人物，作品无权访问或人物不存在均返回 404。"""
    await get_owned_project(project_id, current_user, db)
    result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.project_id == project_id
        )
    )
    character = result.scalar_one_or_none()
    if not character:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="人物不存在"
        )
    return character


async def _sync_index(
    db: AsyncSession, project_id: uuid.UUID, character: Character
) -> None:
    """同步人物搜索索引（失败不影响保存）。"""
    await index_service._safe_sync(
        db, "sync_character_index", project_id, "character", character.id,
        index_service.sync_character_index(
            db, project_id, character.id, character.name,
            _character_index_text(character),
        ),
    )


@router.post("", response_model=CharacterResponse, status_code=status.HTTP_201_CREATED)
async def create_character(
    project_id: uuid.UUID,
    data: CharacterCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建人物（含第三期人物库字段）。"""
    await get_owned_project(project_id, current_user, db)
    _validate_role_status(data.role, data.status)

    payload = data.model_dump(exclude_unset=False)
    payload.pop("aliases", None)
    character = Character(
        project_id=project_id,
        aliases=_dump_aliases(data.aliases),
        **payload,
    )
    db.add(character)
    await db.commit()
    await db.refresh(character)
    await _sync_index(db, project_id, character)
    await db.commit()
    character.aliases = _parse_aliases(character.aliases)
    return character


@router.get("", response_model=list[CharacterResponse])
async def list_characters(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """作品人物列表（按创建时间升序）。"""
    await get_owned_project(project_id, current_user, db)
    result = await db.execute(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at.asc())
    )
    chars = list(result.scalars().all())
    for c in chars:
        c.aliases = _parse_aliases(c.aliases)
    return chars


@router.get("/{character_id}", response_model=CharacterResponse)
async def get_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """人物详情。"""
    character = await get_project_character(project_id, character_id, current_user, db)
    character.aliases = _parse_aliases(character.aliases)
    return character


@router.put("/{character_id}", response_model=CharacterResponse)
async def update_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    data: CharacterUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改人物（仅更新传入字段）。"""
    character = await get_project_character(project_id, character_id, current_user, db)
    _validate_role_status(data.role, data.status)

    payload = data.model_dump(exclude_unset=True)
    aliases = payload.pop("aliases", None)
    for field, value in payload.items():
        setattr(character, field, value)
    if aliases is not None:
        character.aliases = _dump_aliases(aliases)
    await db.commit()
    await db.refresh(character)
    await _sync_index(db, project_id, character)
    await db.commit()
    character.aliases = _parse_aliases(character.aliases)
    return character


@router.delete("/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除人物。"""
    character = await get_project_character(project_id, character_id, current_user, db)
    await db.delete(character)
    await db.commit()


# ---------------------------------------------------------------- 状态快照 ----------

@router.get("/{character_id}/snapshots", response_model=list[CharacterStateSnapshotResponse])
async def list_character_snapshots(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """按人物查询状态快照历史（按章节/创建时间升序）。"""
    await get_project_character(project_id, character_id, current_user, db)
    result = await db.execute(
        select(CharacterStateSnapshot)
        .where(CharacterStateSnapshot.character_id == character_id)
        .order_by(CharacterStateSnapshot.created_at.asc())
    )
    snapshots = list(result.scalars().all())
    for s in snapshots:
        s.state_json = json.loads(s.state_json or "{}")
    return snapshots


@router.post(
    "/{character_id}/snapshots",
    response_model=CharacterStateSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_character_snapshot(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    data: CharacterStateSnapshotCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """手工创建人物状态快照（source=manual，status 默认 draft）。"""
    await get_project_character(project_id, character_id, current_user, db)
    if data.status not in SNAPSHOT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status 必须为 {SNAPSHOT_STATUSES}",
        )
    snapshot = CharacterStateSnapshot(
        character_id=character_id,
        chapter_id=data.chapter_id,
        state_json=json.dumps(data.state_json, ensure_ascii=False),
        source=data.source if data.source in SNAPSHOT_SOURCES else "manual",
        status=data.status,
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    snapshot.state_json = json.loads(snapshot.state_json or "{}")
    return snapshot


@router.post(
    "/{character_id}/snapshots/{snapshot_id}/confirm",
    response_model=CharacterStateSnapshotResponse,
)
async def confirm_character_snapshot(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """确认快照（ai_extracted → user_confirmed，status → confirmed，记录确认时间）。"""
    await get_project_character(project_id, character_id, current_user, db)
    snapshot = (
        await db.execute(
            select(CharacterStateSnapshot).where(
                CharacterStateSnapshot.id == snapshot_id,
                CharacterStateSnapshot.character_id == character_id,
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="快照不存在"
        )
    if snapshot.source == "ai_extracted":
        snapshot.source = "user_confirmed"
    snapshot.status = "confirmed"
    snapshot.confirmed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(snapshot)
    snapshot.state_json = json.loads(snapshot.state_json or "{}")
    return snapshot
