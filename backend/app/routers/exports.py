# 导出路由（第十六批「数据导入、导出与备份恢复」规格实现）
# POST /api/projects/{project_id}/export              创建导出任务（规格八/九/十/二十一）
# GET  /api/export-tasks/{task_id}                   导出任务详情
# GET  /api/export-tasks/{task_id}/result?token=xxx  通过一次性 result_token 获取结果（规格十/十九）
# POST /api/export-tasks/{task_id}/cancel            取消导出任务
# 兼容旧路径（第十批）：
#   POST /api/projects/{project_id}/exports               创建任务
#   GET  /api/projects/{project_id}/exports               任务列表
#   GET  /api/projects/{project_id}/exports/{export_id}   任务详情
# 权限（规格二十二）：owner 完整导出；editor/reviewer/viewer 按协作角色限制导出范围，
#   SCOPE_PERMISSIONS 在 export_service 内生效。
# 归属校验统一 404；未登录 401。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.collaboration import ProjectMember
from app.models.export_task import ExportTask
from app.models.project import Project
from app.models.user import User
from app.schemas.import_export import (
    ExportCreate,
    ExportResultResponse,
    ExportTaskResponse,
)
from app.services.export_service import (
    cancel_export,
    create_export_task,
    get_export_result,
)


router = APIRouter(tags=["导出"])


async def _get_project_with_role(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Project, str]:
    """加载项目并获取用户协作角色：owner 优先，其次 ProjectMember.role。"""
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id == user_id:
        return project, "owner"
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    return project, member.role


def _to_response(task: ExportTask) -> ExportTaskResponse:
    return ExportTaskResponse(
        id=task.id,
        user_id=task.user_id,
        project_id=task.project_id,
        export_type=task.export_type,
        scope=task.scope,
        export_format=task.export_format,
        options=task.options,
        status=task.status,
        progress=task.progress,
        content=task.content,
        content_size=len(task.content.encode("utf-8")) if task.content else 0,
        result_token=task.result_token,
        expires_at=task.expires_at,
        error_message=task.error_message,
        created_at=task.created_at,
        completed_at=task.completed_at,
    )


async def _load_owned_task(db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> ExportTask:
    task = (
        await db.execute(
            select(ExportTask).where(
                ExportTask.id == task_id,
                ExportTask.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导出任务不存在",
        )
    return task


# ============ 新接口（规格二十一） ============

@router.post("/api/projects/{project_id}/export", response_model=ExportTaskResponse)
async def create_project_export(
    project_id: uuid.UUID,
    body: ExportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project, role = await _get_project_with_role(db, project_id, current_user.id)
    task = await create_export_task(db, project, current_user.id, body, role=role)
    await db.commit()
    await db.refresh(task)
    return _to_response(task)


@router.get("/api/export-tasks/{task_id}", response_model=ExportTaskResponse)
async def get_export_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _load_owned_task(db, task_id, current_user.id)
    return _to_response(task)


@router.get("/api/export-tasks/{task_id}/result", response_model=ExportResultResponse)
async def get_export_task_result(
    task_id: uuid.UUID,
    token: str = Query(..., description="一次性 result_token"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """通过一次性 result_token 获取导出结果（短期有效、绑定用户、取用后作废）。"""
    task = await _load_owned_task(db, task_id, current_user.id)
    consumed = await get_export_result(db, task, current_user.id, token)
    await db.commit()
    return ExportResultResponse(
        task_id=consumed.id,
        project_id=consumed.project_id,
        format=consumed.export_format,
        scope=consumed.scope,
        content=consumed.content or "",
        content_size=len((consumed.content or "").encode("utf-8")),
        expires_at=consumed.expires_at,
    )


@router.post("/api/export-tasks/{task_id}/cancel", response_model=ExportTaskResponse)
async def cancel_export_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _load_owned_task(db, task_id, current_user.id)
    result = await cancel_export(db, task)
    await db.commit()
    await db.refresh(result)
    return _to_response(result)


# ============ 兼容旧路径（第十批） ============

_legacy = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["导出（旧路径兼容）"])


@_legacy.post("", response_model=ExportTaskResponse)
async def create_export_legacy(
    project_id: uuid.UUID,
    body: ExportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project, role = await _get_project_with_role(db, project_id, current_user.id)
    task = await create_export_task(db, project, current_user.id, body, role=role)
    await db.commit()
    await db.refresh(task)
    return _to_response(task)


@_legacy.get("", response_model=list[ExportTaskResponse])
async def list_exports(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_project_with_role(db, project_id, current_user.id)
    rows = (
        await db.execute(
            select(ExportTask)
            .where(ExportTask.project_id == project_id)
            .order_by(ExportTask.created_at.desc())
        )
    ).scalars().all()
    return [_to_response(t) for t in rows]


@_legacy.get("/{export_id}", response_model=ExportTaskResponse)
async def get_export_legacy(
    project_id: uuid.UUID,
    export_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_project_with_role(db, project_id, current_user.id)
    task = (
        await db.execute(
            select(ExportTask).where(
                ExportTask.id == export_id,
                ExportTask.project_id == project_id,
                ExportTask.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导出任务不存在",
        )
    return _to_response(task)


router.include_router(_legacy)
