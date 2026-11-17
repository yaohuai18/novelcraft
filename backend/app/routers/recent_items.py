# 最近访问路由（第九批规格实现）
# GET /api/projects/{project_id}/recent-items
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.recent_item import RecentItemResponse
from app.services import recent_item_service

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["最近访问"]
)


@router.get("/recent-items", response_model=list[RecentItemResponse])
async def list_recent_items(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户在该作品下的最近访问记录（按访问时间倒序，最多 20 条）。"""
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
    return await recent_item_service.list_recent_items(
        db, current_user.id, project.id
    )
