# 伏笔管理路由（第四期「写错能发现」伏笔追踪）
# GET/POST /api/projects/{project_id}/foreshadows 列表/新建
# GET/PUT/DELETE /api/projects/{project_id}/foreshadows/{id}
# POST /api/projects/{project_id}/foreshadows/import 从章节记忆同步导入
# POST /api/projects/{project_id}/foreshadows/{id}/paid-off 标记回收
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.foreshadow import Foreshadow, FORESHADOW_STATUSES
from app.models.project import Project
from app.models.user import User
from app.schemas.foreshadow import (
    ForeshadowCreate,
    ForeshadowImportRequest,
    ForeshadowImportResult,
    ForeshadowResponse,
    ForeshadowUpdate,
)
from app.services import collaboration_service
from app.services.foreshadow_service import import_from_memory

router = APIRouter(
    prefix="/api/projects/{project_id}/foreshadows",
    tags=["伏笔管理"],
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


async def _get_foreshadow(
    db: AsyncSession, project_id: uuid.UUID, f_id: uuid.UUID
) -> Foreshadow:
    row = (
        await db.execute(
            select(Foreshadow).where(
                Foreshadow.id == f_id, Foreshadow.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="伏笔不存在",
        )
    return row


def _now_utc() -> datetime:
    return datetime.utcnow()


@router.get("", response_model=list[ForeshadowResponse])
async def list_foreshadows(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """伏笔列表，可按状态过滤。"""
    await _get_project(db, project_id, current_user)
    stmt = select(Foreshadow).where(Foreshadow.project_id == project_id)
    if status_filter:
        if status_filter not in FORESHADOW_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status 仅支持 {FORESHADOW_STATUSES}",
            )
        stmt = stmt.where(Foreshadow.status == status_filter)
    stmt = stmt.order_by(Foreshadow.created_at.asc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("", response_model=ForeshadowResponse, status_code=201)
async def create_foreshadow(
    project_id: uuid.UUID,
    data: ForeshadowCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新建伏笔。"""
    await _get_project(db, project_id, current_user)
    if data.status not in FORESHADOW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status 仅支持 {FORESHADOW_STATUSES}",
        )
    now = _now_utc()
    row = Foreshadow(
        project_id=project_id,
        title=data.title,
        content=data.content,
        importance=data.importance,
        planted_chapter_id=data.planted_chapter_id,
        planned_payoff_chapter_id=data.planned_payoff_chapter_id,
        planned_payoff=data.planned_payoff,
        resolved_chapter_id=data.resolved_chapter_id,
        status=data.status,
        source=data.source or "manual",
        note=data.note,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{foreshadow_id}", response_model=ForeshadowResponse)
async def get_foreshadow(
    project_id: uuid.UUID,
    foreshadow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_project(db, project_id, current_user)
    return await _get_foreshadow(db, project_id, foreshadow_id)


@router.put("/{foreshadow_id}", response_model=ForeshadowResponse)
async def update_foreshadow(
    project_id: uuid.UUID,
    foreshadow_id: uuid.UUID,
    data: ForeshadowUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_project(db, project_id, current_user)
    row = await _get_foreshadow(db, project_id, foreshadow_id)
    if data.title is not None:
        row.title = data.title
    if data.content is not None:
        row.content = data.content
    if data.importance is not None:
        row.importance = data.importance
    if data.planted_chapter_id is not None:
        row.planted_chapter_id = data.planted_chapter_id
    if data.planned_payoff_chapter_id is not None:
        row.planned_payoff_chapter_id = data.planned_payoff_chapter_id
    if data.planned_payoff is not None:
        row.planned_payoff = data.planned_payoff
    if data.resolved_chapter_id is not None:
        row.resolved_chapter_id = data.resolved_chapter_id
    if data.status is not None:
        if data.status not in FORESHADOW_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status 仅支持 {FORESHADOW_STATUSES}",
            )
        row.status = data.status
    if data.note is not None:
        row.note = data.note
    if data.paid_off_chapter_id is not None:
        row.paid_off_chapter_id = data.paid_off_chapter_id
    if data.paid_off_note is not None:
        row.paid_off_note = data.paid_off_note
    row.updated_at = _now_utc()
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{foreshadow_id}", status_code=204)
async def delete_foreshadow(
    project_id: uuid.UUID,
    foreshadow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_project(db, project_id, current_user)
    row = await _get_foreshadow(db, project_id, foreshadow_id)
    await db.delete(row)
    await db.commit()


@router.post("/import", response_model=ForeshadowImportResult)
async def import_foreshadows(
    project_id: uuid.UUID,
    data: ForeshadowImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """从章节记忆（foreshadows_added）同步导入伏笔清单。"""
    await _get_project(db, project_id, current_user)
    result = await import_from_memory(
        db,
        project_id,
        chapter_ids=data.chapter_ids,
        mark_planted=data.mark_planted,
        replace_duplicates=data.replace_duplicates,
    )
    return ForeshadowImportResult(
        imported=result["imported"],
        skipped_duplicates=result["skipped_duplicates"],
        items=result["items"],
    )


@router.post("/{foreshadow_id}/paid-off", response_model=ForeshadowResponse)
async def mark_paid_off(
    project_id: uuid.UUID,
    foreshadow_id: uuid.UUID,
    body: ForeshadowUpdate | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """标记伏笔已回收（兼容旧端点；写入统一枚举 resolved）。"""
    await _get_project(db, project_id, current_user)
    row = await _get_foreshadow(db, project_id, foreshadow_id)
    row.status = "resolved"
    if body is not None:
        if body.paid_off_chapter_id is not None:
            row.paid_off_chapter_id = body.paid_off_chapter_id
            row.resolved_chapter_id = body.paid_off_chapter_id
        if body.paid_off_note is not None:
            row.paid_off_note = body.paid_off_note
        if body.planned_payoff is not None:
            row.planned_payoff = body.planned_payoff
    row.updated_at = _now_utc()
    await db.commit()
    await db.refresh(row)
    return row
