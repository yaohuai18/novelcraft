# 世界观条目路由：创建、列表、详情、修改、删除（全部需 JWT 鉴权 + 作品归属校验）
# 与人物模块同构实现，归属校验复用 characters.get_owned_project
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.models.world_setting import WorldSetting
from app.routers.characters import get_owned_project
from app.schemas.world_setting import (
    WorldSettingCreate,
    WorldSettingResponse,
    WorldSettingUpdate,
)
from app.services import index_service


router = APIRouter(
    prefix="/api/projects/{project_id}/world-settings",
    tags=["世界观"],
)


async def get_project_world_setting(
    project_id: uuid.UUID,
    setting_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> WorldSetting:
    """定位项目内世界观条目，作品无权访问或条目不存在均返回 404。"""
    await get_owned_project(project_id, current_user, db)

    result = await db.execute(
        select(WorldSetting).where(
            WorldSetting.id == setting_id,
            WorldSetting.project_id == project_id,
        )
    )

    setting = result.scalar_one_or_none()

    if not setting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="世界观条目不存在"
        )

    return setting


@router.post(
    "",
    response_model=WorldSettingResponse,
    status_code=status.HTTP_201_CREATED
)
async def create_world_setting(
    project_id: uuid.UUID,
    data: WorldSettingCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建世界观条目（归属作品必须属于当前用户）。"""
    await get_owned_project(project_id, current_user, db)

    setting = WorldSetting(
        project_id=project_id,
        **data.model_dump()
    )

    db.add(setting)
    await db.commit()
    await db.refresh(setting)

    # 索引同步：世界观条目创建后建立搜索索引（失败不影响保存）
    await index_service._safe_sync(
        db, "sync_world_setting_index", project_id, "world_setting", setting.id,
        index_service.sync_world_setting_index(
            db, project_id, setting.id, setting.name, setting.content,
        ),
    )
    await db.commit()

    return setting


@router.get(
    "",
    response_model=list[WorldSettingResponse]
)
async def list_world_settings(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """作品世界观条目列表（按创建时间升序）。"""
    await get_owned_project(project_id, current_user, db)

    result = await db.execute(
        select(WorldSetting)
        .where(WorldSetting.project_id == project_id)
        .order_by(WorldSetting.created_at.asc())
    )

    return list(result.scalars().all())


@router.get(
    "/{setting_id}",
    response_model=WorldSettingResponse
)
async def get_world_setting(
    project_id: uuid.UUID,
    setting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """世界观条目详情。"""
    return await get_project_world_setting(
        project_id,
        setting_id,
        current_user,
        db
    )


@router.put(
    "/{setting_id}",
    response_model=WorldSettingResponse
)
async def update_world_setting(
    project_id: uuid.UUID,
    setting_id: uuid.UUID,
    data: WorldSettingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改世界观条目（仅更新传入字段）。"""
    setting = await get_project_world_setting(
        project_id,
        setting_id,
        current_user,
        db
    )

    update_data = data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(setting, field, value)

    await db.commit()
    await db.refresh(setting)

    # 索引同步：世界观条目修改后重建搜索索引（失败不影响保存）
    await index_service._safe_sync(
        db, "sync_world_setting_index", project_id, "world_setting", setting.id,
        index_service.sync_world_setting_index(
            db, project_id, setting.id, setting.name, setting.content,
        ),
    )
    await db.commit()

    return setting


@router.delete(
    "/{setting_id}",
    status_code=status.HTTP_204_NO_CONTENT
)
async def delete_world_setting(
    project_id: uuid.UUID,
    setting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除世界观条目。"""
    setting = await get_project_world_setting(
        project_id,
        setting_id,
        current_user,
        db
    )

    await db.delete(setting)
    await db.commit()

    # 索引同步：世界观条目删除后软删搜索索引（deleted_at 排除，规格十七）
    await index_service._safe_sync(
        db, "delete_index", project_id, "world_setting", setting.id,
        index_service.delete_index(
            db, project_id, "world_setting", setting.id, soft=True,
        ),
    )
    await db.commit()
