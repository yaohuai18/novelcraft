# 时间线事件路由（第三期「时间线事件结构化」）
# CRUD：列表 / 新建 / 详情 / 修改 / 删除 + 确认标记；全部需 JWT 鉴权 + 作品归属校验
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.timeline_event import TimelineEvent
from app.models.user import User
from app.schemas.timeline_event import (
    TIMELINE_SOURCES,
    TIMELINE_STATUSES,
    TimelineEventCreate,
    TimelineEventResponse,
    TimelineEventUpdate,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/timeline-events",
    tags=["时间线"],
)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
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
    return project


async def _get_event(
    db: AsyncSession, project_id: uuid.UUID, event_id: uuid.UUID
) -> TimelineEvent:
    row = (
        await db.execute(
            select(TimelineEvent).where(
                TimelineEvent.id == event_id,
                TimelineEvent.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="时间线事件不存在"
        )
    return row


def _dump_participants(participants) -> str | None:
    if participants is None:
        return None
    if isinstance(participants, str):
        try:
            json.loads(participants)
            return participants
        except Exception:  # noqa: BLE001
            return json.dumps([participants], ensure_ascii=False)
    return json.dumps(list(participants), ensure_ascii=False)


def _parse_participants(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:  # noqa: BLE001
        pass
    return []


@router.get("", response_model=list[TimelineEventResponse])
async def list_timeline_events(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """作品时间线事件列表（按 event_date_label/time_label 排序优先，其次 sequence_order）。"""
    await get_owned_project(project_id, current_user, db)
    stmt = (
        select(TimelineEvent)
        .where(TimelineEvent.project_id == project_id)
        .order_by(
            TimelineEvent.event_date_label.asc().nullslast(),
            TimelineEvent.time_label.asc().nullslast(),
            TimelineEvent.sequence_order.asc().nullslast(),
            TimelineEvent.created_at.asc(),
        )
    )
    if chapter_id is not None:
        stmt = stmt.where(TimelineEvent.chapter_id == chapter_id)
    rows = (await db.execute(stmt)).scalars().all()
    for r in rows:
        r.participants = _parse_participants(r.participants)
    return list(rows)


@router.post(
    "",
    response_model=TimelineEventResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_timeline_event(
    project_id: uuid.UUID,
    data: TimelineEventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """新建时间线事件（结构化字段：章节/时间标签/排序/参与者/来源/状态）。"""
    await get_owned_project(project_id, current_user, db)
    source = data.source if data.source in TIMELINE_SOURCES else "manual"
    status_ = data.status if data.status in TIMELINE_STATUSES else "draft"
    payload = data.model_dump(exclude_unset=False)
    payload.pop("participants", None)
    payload.pop("source", None)
    payload.pop("status", None)
    row = TimelineEvent(
        project_id=project_id,
        participants=_dump_participants(data.participants),
        source=source,
        status=status_,
        **payload,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    row.participants = _parse_participants(row.participants)
    return row


@router.get("/{event_id}", response_model=TimelineEventResponse)
async def get_timeline_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """时间线事件详情。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_event(db, project_id, event_id)
    row.participants = _parse_participants(row.participants)
    return row


@router.put("/{event_id}", response_model=TimelineEventResponse)
async def update_timeline_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    data: TimelineEventUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改时间线事件（仅更新传入字段）。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_event(db, project_id, event_id)
    payload = data.model_dump(exclude_unset=True)
    participants = payload.pop("participants", None)
    status_ = payload.pop("status", None)
    if status_ is not None and status_ not in TIMELINE_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status 必须为 {TIMELINE_STATUSES}"
        )
    for field, value in payload.items():
        setattr(row, field, value)
    if participants is not None:
        row.participants = _dump_participants(participants)
    if status_ is not None:
        row.status = status_
        row.confirmed = status_ == "confirmed"
    await db.commit()
    await db.refresh(row)
    row.participants = _parse_participants(row.participants)
    return row


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_timeline_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除时间线事件。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_event(db, project_id, event_id)
    await db.delete(row)
    await db.commit()


@router.post("/{event_id}/confirm", response_model=TimelineEventResponse)
async def confirm_timeline_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """确认时间线事件（status → confirmed，confirmed=True）。"""
    await get_owned_project(project_id, current_user, db)
    row = await _get_event(db, project_id, event_id)
    row.confirmed = True
    row.status = "confirmed"
    await db.commit()
    await db.refresh(row)
    row.participants = _parse_participants(row.participants)
    return row
