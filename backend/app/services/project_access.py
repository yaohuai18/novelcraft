# 作品归属校验共享 helper（第十批导入/导出/备份复用）
# 统一语义：Project.id == project_id 且 Project.user_id == current_user.id，否则 404。
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project


async def check_project_access(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID
) -> Project:
    """校验作品归属；非本人作品统一返回 404（不泄露存在性）。"""
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id,
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    return project
