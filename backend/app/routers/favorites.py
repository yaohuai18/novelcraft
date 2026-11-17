# 收藏路由（第九批规格实现）
# GET/POST /api/projects/{project_id}/favorites、DELETE /api/projects/{project_id}/favorites/{target_id}
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.character import Character
from app.models.chapter_content import ChapterContent
from app.models.favorite import Favorite
from app.models.outline import Outline
from app.models.project import Project
from app.models.tag import Tag
from app.models.user import User
from app.models.world_setting import WorldSetting
from app.schemas.favorite import FAVORITE_TARGET_TYPES, FavoriteCreate, FavoriteResponse
from app.services import discover_service

router = APIRouter(
    prefix="/api/projects/{project_id}/favorites",
    tags=["收藏"]
)


async def check_project_access(
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


async def validate_target(
    project: Project,
    target_type: str,
    target_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """校验收藏目标存在且属于当前作品。"""
    if target_type not in FAVORITE_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不允许的收藏目标类型"
        )
    if target_type == "outline":
        stmt = select(Outline).where(
            Outline.id == target_id,
            Outline.project_id == project.id
        )
        exists = (await db.execute(stmt)).scalar_one_or_none() is not None
    elif target_type == "chapter_content":
        stmt = (
            select(ChapterContent)
            .join(Outline, Outline.id == ChapterContent.outline_id)
            .where(
                ChapterContent.id == target_id,
                Outline.project_id == project.id
            )
        )
        exists = (await db.execute(stmt)).scalar_one_or_none() is not None
    elif target_type == "character":
        stmt = select(Character).where(
            Character.id == target_id,
            Character.project_id == project.id
        )
        exists = (await db.execute(stmt)).scalar_one_or_none() is not None
    elif target_type == "world_setting":
        stmt = select(WorldSetting).where(
            WorldSetting.id == target_id,
            WorldSetting.project_id == project.id
        )
        exists = (await db.execute(stmt)).scalar_one_or_none() is not None
    elif target_type == "project":
        # 收藏作品本身：target_id 必须等于当前作品 id
        exists = target_id == project.id
    else:  # tag
        stmt = select(Tag).where(
            Tag.id == target_id,
            Tag.project_id == project.id
        )
        exists = (await db.execute(stmt)).scalar_one_or_none() is not None
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="收藏目标不存在或不属于当前作品"
        )


@router.get("", response_model=list[FavoriteResponse])
async def list_favorites(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户在该作品下的收藏列表（按创建时间倒序）。"""
    await check_project_access(project_id, current_user, db)
    result = await db.execute(
        select(Favorite)
        .where(
            Favorite.user_id == current_user.id,
            Favorite.project_id == project_id,
        )
        .order_by(Favorite.created_at.desc())
    )
    return list(result.scalars().all())


@router.post(
    "",
    response_model=FavoriteResponse,
    status_code=status.HTTP_201_CREATED
)
async def create_favorite(
    project_id: uuid.UUID,
    data: FavoriteCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """收藏章节/大纲/人物/设定/标签；重复收藏幂等返回已有记录。"""
    project = await check_project_access(project_id, current_user, db)
    await validate_target(project, data.target_type, data.target_id, db)

    result = await db.execute(
        select(Favorite).where(
            Favorite.user_id == current_user.id,
            Favorite.project_id == project.id,
            Favorite.target_type == data.target_type,
            Favorite.target_id == data.target_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    favorite = Favorite(
        user_id=current_user.id,
        project_id=project.id,
        target_type=data.target_type,
        target_id=data.target_id,
    )
    db.add(favorite)
    await db.commit()
    await db.refresh(favorite)
    # 服务端统计：收藏数变化后重算作品公开统计（规格十三）
    await discover_service.refresh_project_stats(db, project.id)
    await db.commit()
    return favorite


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    project_id: uuid.UUID,
    target_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消收藏（按 target_id 删除当前用户在作品下的对应收藏）。"""
    await check_project_access(project_id, current_user, db)
    result = await db.execute(
        select(Favorite).where(
            Favorite.user_id == current_user.id,
            Favorite.project_id == project_id,
            Favorite.target_id == target_id,
        )
    )
    favorites = list(result.scalars().all())
    for favorite in favorites:
        await db.delete(favorite)
    await db.commit()
    # 服务端统计：收藏数变化后重算作品公开统计（规格十三）
    await discover_service.refresh_project_stats(db, project_id)
    await db.commit()
