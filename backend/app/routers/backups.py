# 备份 / 恢复路由（第十六批「数据导入、导出与备份恢复」规格实现）
# POST /api/projects/{project_id}/backups              创建备份（manual/automatic/pre_publish/release_snapshot）
# GET  /api/projects/{project_id}/backups              备份列表
# GET  /api/backups/{backup_id}                        备份详情（含快照）
# POST /api/backups/{backup_id}/validate               备份校验（规格十八）
# POST /api/backups/{backup_id}/restore                恢复（默认新项目；覆盖需二次确认+作品名+自动恢复前备份）
# DELETE /api/backups/{backup_id}                      删除备份
# 兼容旧路径（第十批）：
#   GET  /api/projects/{project_id}/backups/{backup_id}            详情
#   POST /api/projects/{project_id}/backups/{backup_id}/restore    恢复
# 归属校验统一 404；未登录 401。备份绑定创建用户（规格十九）。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project_backup import ProjectBackup
from app.models.user import User
from app.schemas.import_export import (
    BackupCreate,
    BackupDetailResponse,
    BackupResponse,
    BackupRestorePreview,
    BackupValidateResponse,
    RestoreCreate,
    RestoreResponse,
)
from app.services.backup_service import (
    BACKUP_TYPES,
    apply_retention_policy,
    create_backup,
    delete_backup,
    get_restore_preview,
    restore_backup,
    validate_backup,
)
from app.services.project_access import check_project_access


router = APIRouter(tags=["备份恢复"])

_SYSTEM_BACKUP_TYPES = {"before_import", "before_restore"}
_MANUAL_TYPES = {t for t in BACKUP_TYPES if t not in _SYSTEM_BACKUP_TYPES}


async def _load_backup(db: AsyncSession, backup_id: uuid.UUID, user_id: uuid.UUID) -> ProjectBackup:
    backup = (
        await db.execute(
            select(ProjectBackup).where(
                ProjectBackup.id == backup_id,
                ProjectBackup.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备份不存在",
        )
    return backup


# ============ 创建 / 列表（规格二十一） ============

@router.post("/api/projects/{project_id}/backups", response_model=BackupResponse)
async def create_project_backup(
    project_id: uuid.UUID,
    body: BackupCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await check_project_access(db, project_id, current_user.id)
    if body.backup_type not in _MANUAL_TYPES:
        if body.backup_type in _SYSTEM_BACKUP_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="导入前/恢复前备份由系统自动创建，不支持手工指定",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的备份类型",
        )
    backup = await create_backup(
        db, project, body.backup_type,
        user_id=current_user.id, note=body.note,
    )
    await apply_retention_policy(db, project.id)
    await db.commit()
    await db.refresh(backup)
    return backup


@router.get("/api/projects/{project_id}/backups", response_model=list[BackupResponse])
async def list_backups(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_project_access(db, project_id, current_user.id)
    rows = (
        await db.execute(
            select(ProjectBackup)
            .where(
                ProjectBackup.project_id == project_id,
                ProjectBackup.user_id == current_user.id,
            )
            .order_by(ProjectBackup.created_at.desc())
        )
    ).scalars().all()
    return rows


# ============ 新前缀接口（规格二十一：/api/backups/{backup_id}） ============

@router.get("/api/backups/{backup_id}", response_model=BackupDetailResponse)
async def get_backup(
    backup_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    backup = await _load_backup(db, backup_id, current_user.id)
    return backup


@router.post("/api/backups/{backup_id}/validate", response_model=BackupValidateResponse)
async def validate_backup_endpoint(
    backup_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    backup = await _load_backup(db, backup_id, current_user.id)
    return await validate_backup(backup)


@router.post("/api/backups/{backup_id}/restore", response_model=RestoreResponse)
async def restore_from_backup(
    backup_id: uuid.UUID,
    body: RestoreCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    backup = await _load_backup(db, backup_id, current_user.id)
    report = await restore_backup(db, backup, body, current_user.id)
    await db.commit()
    return report


@router.delete("/api/backups/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup_endpoint(
    backup_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    backup = await _load_backup(db, backup_id, current_user.id)
    await delete_backup(db, backup)
    await db.commit()
    return None


# ============ 兼容旧路径（第十批） ============

_legacy = APIRouter(prefix="/api/projects/{project_id}/backups", tags=["备份恢复（旧路径兼容）"])


@_legacy.get("/{backup_id}", response_model=BackupDetailResponse)
async def get_backup_legacy(
    project_id: uuid.UUID,
    backup_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_project_access(db, project_id, current_user.id)
    backup = (
        await db.execute(
            select(ProjectBackup).where(
                ProjectBackup.id == backup_id,
                ProjectBackup.project_id == project_id,
                ProjectBackup.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备份不存在",
        )
    return backup


@_legacy.post("/{backup_id}/restore", response_model=RestoreResponse)
async def restore_from_backup_legacy(
    project_id: uuid.UUID,
    backup_id: uuid.UUID,
    body: RestoreCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await check_project_access(db, project_id, current_user.id)
    backup = (
        await db.execute(
            select(ProjectBackup).where(
                ProjectBackup.id == backup_id,
                ProjectBackup.project_id == project_id,
                ProjectBackup.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备份不存在",
        )
    report = await restore_backup(db, backup, body, current_user.id)
    await db.commit()
    return report


router.include_router(_legacy)
