# 作品业务逻辑：作品 CRUD，所有操作限定当前登录用户本人数据
import hashlib
import json
import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.schemas.project import ProjectCreate, ProjectUpdate


async def _get_owned_project(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Project:
    """按 id 查询作品并校验归属；非本人数据统一返回 404，避免泄露存在性。"""
    project = await db.get(Project, project_id)
    if (
        project is None
        or project.user_id != user_id
        or project.deleted_at is not None  # 回收站中的作品对常规接口不可见
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在",
        )
    return project


async def create_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: ProjectCreate,
) -> Project:
    """创建作品，归属当前用户。"""
    project = Project(user_id=user_id, **data.model_dump())
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def list_projects(
    db: AsyncSession,
    user_id: uuid.UUID,
    include_archived: bool = False,
) -> list[Project]:
    """列出当前用户作品；默认排除已归档（archived），永远排除回收站（软删除）。"""
    stmt = select(Project).where(
        Project.user_id == user_id,
        Project.deleted_at.is_(None),
    )
    if not include_archived:
        stmt = stmt.where(Project.status != "archived")
    stmt = stmt.order_by(Project.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Project:
    """获取作品详情（校验归属）。"""
    return await _get_owned_project(db, project_id, user_id)


async def update_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    data: ProjectUpdate,
) -> Project:
    """修改作品信息与设定集字段（仅更新传入字段）。"""
    project = await _get_owned_project(db, project_id, user_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    await db.commit()
    await db.refresh(project)
    return project


async def delete_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """软删除作品：写入回收站（deleted_at/deleted_by），子表数据原样保留。

    30 天后由后台清理任务彻底删除；期间可通过 restore_project 恢复。
    彻底删除请走 purge_project（会先做 before_delete 快照）。
    """
    project = await _get_owned_project(db, project_id, user_id)
    project.deleted_at = datetime.utcnow()
    project.deleted_by = user_id
    await db.commit()


async def list_trash_projects(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[Project]:
    """回收站列表：软删除的作品，按删除时间倒序。"""
    stmt = (
        select(Project)
        .where(
            Project.user_id == user_id,
            Project.deleted_at.is_not(None),
        )
        .order_by(Project.deleted_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def restore_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Project:
    """从回收站恢复作品：清除软删除标记。"""
    project = await db.get(Project, project_id)
    if (
        project is None
        or project.user_id != user_id
        or project.deleted_at is None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="回收站中不存在该作品",
        )
    project.deleted_at = None
    project.deleted_by = None
    await db.commit()
    await db.refresh(project)
    return project


async def purge_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """彻底删除回收站中的作品（真删除）。

    删除前先落一份 before_delete 快照（project_backups），保留 30 天兜底。
    """
    from pathlib import Path

    from app.services.snapshot_service import build_project_snapshot

    project = await db.get(Project, project_id)
    if (
        project is None
        or project.user_id != user_id
        or project.deleted_at is None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="回收站中不存在该作品",
        )
    try:
        snapshot = await build_project_snapshot(db, project)
    except Exception:  # noqa: BLE001 —— 快照失败时兜底最小快照，不阻断删除
        snapshot = {"error": "snapshot_build_failed", "project_id": str(project_id)}
    snapshot_text = json.dumps(snapshot, ensure_ascii=False, default=str)
    # before_delete 快照落独立文件（backend/trash_snapshots/）：
    # project_backups 表的 project_id 有 ON DELETE CASCADE 外键，挂库会在真删除时被级联删掉，
    # 起不到兜底作用。文件快照与项目生命周期解耦，由 30 天清理任务负责过期删除。
    snap_dir = Path(__file__).resolve().parents[2] / "trash_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"before_delete_{project.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    snap_file.write_text(snapshot_text, encoding="utf-8")
    await db.delete(project)
    await db.commit()


async def archive_project(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> Project:
    """归档作品：status 置为 archived，不删除数据。"""
    project = await _get_owned_project(db, project_id, user_id)
    project.status = "archived"
    await db.commit()
    await db.refresh(project)
    return project
