# 导入路由（第十六批「数据导入、导出与备份恢复」规格实现）
# POST /api/projects/import/preview            导入预览（规格五）
# POST /api/projects/import                    创建导入任务（规格四/二十一）
# GET  /api/import-tasks/{task_id}             导入任务详情
# POST /api/import-tasks/{task_id}/cancel      取消导入任务
# 兼容旧路径（第十批）：
#   POST /api/projects/{project_id}/imports               创建任务
#   GET  /api/projects/{project_id}/imports               任务列表
#   GET  /api/projects/{project_id}/imports/{import_id}   任务详情
#   POST /api/projects/{project_id}/imports/{import_id}/confirm 确认执行
#   POST /api/projects/{project_id}/imports/{import_id}/cancel  取消
# 归属校验统一 404；未登录 401。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.import_task import ImportTask
from app.models.user import User
from app.schemas.import_export import (
    ImportExecuteRequest,
    ImportPreviewRequest,
    ImportPreviewResponse,
    ImportReportResponse,
    ImportTaskCreateRequest,
    ImportTaskResponse,
)
from app.services.import_service import (
    cancel_import,
    confirm_import,
    create_import_task,
    preview_import,
)
from app.services.project_access import check_project_access


router = APIRouter(tags=["导入"])


async def _load_owned_task(db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> ImportTask:
    task = (
        await db.execute(
            select(ImportTask).where(
                ImportTask.id == task_id,
                ImportTask.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导入任务不存在",
        )
    return task


# ============ 新接口（规格二十一） ============

@router.post("/api/projects/import/preview", response_model=ImportPreviewResponse)
async def preview_project_import(
    body: ImportPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导入预览：解析内容返回作品名/分卷数/章节数/预计字数/无法识别/重复/格式警告。"""
    return await preview_import(db, current_user.id, body)


@router.post("/api/projects/import", response_model=ImportTaskResponse)
async def create_project_import(
    body: ImportTaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建导入任务（大文件异步导入，规格四）。"""
    project_id = None
    if body.project_id is not None:
        project = await check_project_access(db, body.project_id, current_user.id)
        project_id = project.id
    task = await create_import_task(
        db, current_user.id, project_id, body,
        body.file_name, body.file_type, body.content,
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/api/import-tasks/{task_id}", response_model=ImportTaskResponse)
async def get_import_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _load_owned_task(db, task_id, current_user.id)
    return task


@router.post("/api/import-tasks/{task_id}/cancel", response_model=ImportTaskResponse)
async def cancel_import_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _load_owned_task(db, task_id, current_user.id)
    result = await cancel_import(db, task)
    await db.commit()
    await db.refresh(result)
    return result


# ============ 兼容旧路径（第十批） ============

_legacy = APIRouter(prefix="/api/projects/{project_id}/imports", tags=["导入（旧路径兼容）"])


@_legacy.post("", response_model=ImportTaskResponse)
async def create_import_legacy(
    project_id: uuid.UUID,
    body: ImportTaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await check_project_access(db, project_id, current_user.id)
    task = await create_import_task(
        db, current_user.id, project.id, body,
        body.file_name, body.file_type, body.content,
    )
    await db.commit()
    await db.refresh(task)
    return task


@_legacy.get("", response_model=list[ImportTaskResponse])
async def list_imports(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_project_access(db, project_id, current_user.id)
    rows = (
        await db.execute(
            select(ImportTask)
            .where(ImportTask.project_id == project_id)
            .order_by(ImportTask.created_at.desc())
        )
    ).scalars().all()
    return rows


@_legacy.get("/{import_id}", response_model=ImportTaskResponse)
async def get_import(
    project_id: uuid.UUID,
    import_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_project_access(db, project_id, current_user.id)
    task = (
        await db.execute(
            select(ImportTask).where(
                ImportTask.id == import_id,
                ImportTask.project_id == project_id,
                ImportTask.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导入任务不存在",
        )
    return task


@_legacy.post("/{import_id}/confirm", response_model=ImportReportResponse)
async def confirm_import_task(
    project_id: uuid.UUID,
    import_id: uuid.UUID,
    body: ImportExecuteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await check_project_access(db, project_id, current_user.id)
    task = (
        await db.execute(
            select(ImportTask).where(
                ImportTask.id == import_id,
                ImportTask.project_id == project_id,
                ImportTask.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导入任务不存在",
        )
    report = await confirm_import(db, task, body)
    await db.commit()
    return report


@_legacy.post("/{import_id}/cancel", response_model=ImportTaskResponse)
async def cancel_import_legacy(
    project_id: uuid.UUID,
    import_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_project_access(db, project_id, current_user.id)
    task = (
        await db.execute(
            select(ImportTask).where(
                ImportTask.id == import_id,
                ImportTask.project_id == project_id,
                ImportTask.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="导入任务不存在",
        )
    result = await cancel_import(db, task)
    await db.commit()
    await db.refresh(result)
    return result


router.include_router(_legacy)
