# 角色关系路由（第三期「人物和时间不漂」）
# CRUD：列表 / 新建 / 详情 / 修改 / 删除；全部需 JWT 鉴权 + 作品归属校验
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.character import Character
from app.models.character_relationship import CharacterRelationship
from app.models.project import Project
from app.models.user import User
from app.schemas.character_relationship import (
    RELATION_TYPES,
    RELATION_STATUS,
    CharacterRelationshipCreate,
    CharacterRelationshipResponse,
    CharacterRelationshipUpdate,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/relationships",
    tags=["角色关系"],
)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
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


async def _check_characters(
    db: AsyncSession, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> None:
    rows = (
        await db.execute(
            select(Character.id).where(
                Character.project_id == project_id,
                Character.id.in_(ids),
            )
        )
    ).scalars().all()
    if len(rows) != len(set(ids)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="关联人物不存在或不属于该作品"
        )


async def _get_relationship(
    db: AsyncSession, project_id: uuid.UUID, relation_id: uuid.UUID
) -> CharacterRelationship:
    row = (
        await db.execute(
            select(CharacterRelationship).where(
                CharacterRelationship.id == relation_id,
                CharacterRelationship.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="关系不存在"
        )
    return row


def _validate(
    relation_type: str | None, status_: str | None
) -> None:
    if relation_type is not None and relation_type not in RELATION_TYPES:
        raise HTTPException(status_code=422, detail=f"relation_type 必须为 {RELATION_TYPES}")
    if status_ is not None and status_ not in RELATION_STATUS:
        raise HTTPException(status_code=422, detail=f"status 必须为 {RELATION_STATUS}")


@router.get("", response_model=list[CharacterRelationshipResponse])
async def list_relationships(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """作品关系列表，可按 status 过滤（proposed|confirmed|archived）。"""
    await get_owned_project(project_id, current_user, db)
    stmt = (
        select(CharacterRelationship)
        .where(CharacterRelationship.project_id == project_id)
        .order_by(CharacterRelationship.created_at.asc())
    )
    if status_filter:
        stmt = stmt.where(CharacterRelationship.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "",
    response_model=CharacterRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship(
    project_id: uuid.UUID,
    data: CharacterRelationshipCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """新建关系（关联两个人物 + 关系类型 + 描述 + 起止章节）。"""
    await get_owned_project(project_id, current_user, db)
    _validate(data.relation_type, data.status)
    if data.source_character_id == data.target_character_id:
        raise HTTPException(status_code=422, detail="关系双方不能是同一人物")
    await _check_characters(
        db, project_id, [data.source_character_id, data.target_character_id]
    )

    row = CharacterRelationship(
        project_id=project_id,
        source_character_id=data.source_character_id,
        target_character_id=data.target_character_id,
        relation_type=data.relation_type,
        description=data.description,
        start_chapter_id=data.start_chapter_id,
        end_chapter_id=data.end_chapter_id,
        status=data.status if data.status in RELATION_STATUS else "confirmed",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{relation_id}", response_model=CharacterRelationshipResponse)
async def get_relationship(
    project_id: uuid.UUID,
    relation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """关系详情。"""
    await get_owned_project(project_id, current_user, db)
    return await _get_relationship(db, project_id, relation_id)


@router.put("/{relation_id}", response_model=CharacterRelationshipResponse)
async def update_relationship(
    project_id: uuid.UUID,
    relation_id: uuid.UUID,
    data: CharacterRelationshipUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改关系（仅更新传入字段）。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_relationship(db, project_id, relation_id)
    _validate(data.relation_type, data.status)
    payload = data.model_dump(exclude_unset=True)
    for field, value in payload.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relationship(
    project_id: uuid.UUID,
    relation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除关系。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_relationship(db, project_id, relation_id)
    await db.delete(row)
    await db.commit()
