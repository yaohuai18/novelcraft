# 项目设置路由（第二期「章节能被记住」规格实现）
# GET/PUT /api/projects/{project_id}/settings
# auto_summarize（自动保存章节摘要开关）本期生效；
# character_confirm / foreshadow_confirm / timeline_confirm 随第三期「人物库/时间线」功能生效。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.project_setting import ProjectSetting, CONFIRM_POLICIES
from app.models.user import User
from app.schemas.project_setting import (
    ProjectSettingResponse,
    ProjectSettingUpdate,
)
from app.services import collaboration_service
from app.services.chapter_memory_service import get_or_create_project_setting

router = APIRouter(
    prefix="/api/projects/{project_id}/settings",
    tags=["项目设置"],
)


async def _get_project(
    db: AsyncSession, project_id: uuid.UUID, current_user: User
) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="作品不存在或无权访问",
            )
    return project


@router.get("", response_model=ProjectSettingResponse)
async def get_project_settings(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """读取项目设置；从未创建时按默认值自动创建。"""
    await _get_project(db, project_id, current_user)
    setting = await get_or_create_project_setting(db, project_id)
    return setting


@router.put("", response_model=ProjectSettingResponse)
async def update_project_settings(
    project_id: uuid.UUID,
    data: ProjectSettingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新项目设置（部分字段）。"""
    await _get_project(db, project_id, current_user)
    setting = await get_or_create_project_setting(db, project_id)

    if data.auto_summarize is not None:
        setting.auto_summarize = data.auto_summarize
    if data.auto_check is not None:
        setting.auto_check = data.auto_check
    for field, value in (
        ("character_confirm", data.character_confirm),
        ("foreshadow_confirm", data.foreshadow_confirm),
        ("timeline_confirm", data.timeline_confirm),
    ):
        if value is not None:
            if value not in CONFIRM_POLICIES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{field} 取值仅支持 {'/'.join(CONFIRM_POLICIES)}",
                )
            setattr(setting, field, value)

    await db.commit()
    await db.refresh(setting)
    return setting
