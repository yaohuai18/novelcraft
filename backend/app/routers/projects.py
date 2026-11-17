# 作品路由：创建、列表、详情、修改、删除、归档（全部需 JWT 鉴权）
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services import index_service, project_service

router = APIRouter(prefix="/api/projects", tags=["作品"])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建作品（归属当前用户）。"""
    project = await project_service.create_project(db, current_user.id, data)
    # 索引同步：作品创建后建立搜索索引（失败不影响保存）
    await index_service._safe_sync(
        db, "sync_project_index", project.id, "project", project.id,
        index_service.sync_project_index(db, project),
    )
    await db.commit()
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户作品列表，默认排除已归档作品。"""
    return await project_service.list_projects(db, current_user.id, include_archived)


@router.get("/trash", response_model=list[ProjectResponse])
async def list_trash_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回收站列表（软删除的作品，按删除时间倒序）。"""
    return await project_service.list_trash_projects(db, current_user.id)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """作品详情（校验归属，非本人数据返回 404）。"""
    return await project_service.get_project(db, current_user.id, project_id)


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """修改作品信息与设定集字段。"""
    project = await project_service.update_project(db, current_user.id, project_id, data)
    # 索引同步：作品信息修改后重建搜索索引（失败不影响保存）
    await index_service._safe_sync(
        db, "sync_project_index", project.id, "project", project.id,
        index_service.sync_project_index(db, project),
    )
    await db.commit()
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """软删除作品：移入回收站（子表数据保留，可恢复；30 天后才真删）。"""
    await project_service.delete_project(db, current_user.id, project_id)
    # 索引同步：作品删除后软删搜索索引（deleted_at 排除，规格十七）
    await index_service._safe_sync(
        db, "delete_index", project_id, "project", project_id,
        index_service.delete_index(
            db, project_id, "project", project_id, soft=True,
        ),
    )
    await db.commit()


@router.post("/{project_id}/restore", response_model=ProjectResponse)
async def restore_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从回收站恢复作品（清除软删除标记）。"""
    project = await project_service.restore_project(db, current_user.id, project_id)
    await index_service._safe_sync(
        db, "sync_project_index", project.id, "project", project.id,
        index_service.sync_project_index(db, project),
    )
    await db.commit()
    return project


@router.delete("/{project_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """彻底删除回收站中的作品（删除前自动落 before_delete 快照）。"""
    await project_service.purge_project(db, current_user.id, project_id)
    await index_service._safe_sync(
        db, "delete_index", project_id, "project", project_id,
        index_service.delete_index(
            db, project_id, "project", project_id, soft=False,
        ),
    )
    await db.commit()


@router.post("/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """归档作品（status 置为 archived）。"""
    project = await project_service.archive_project(db, current_user.id, project_id)
    # 索引同步：归档后刷新作品索引状态（失败不影响保存）
    await index_service._safe_sync(
        db, "sync_project_index", project.id, "project", project.id,
        index_service.sync_project_index(db, project),
    )
    await db.commit()
    return project
