# 通知路由（第十五批「通知、评论与协作反馈」规格实现）
# GET /api/notifications（type/is_read/project_id/from/to/page/page_size 过滤）、
# unread-count、/{id}/read、read-all、/{id}/archive；偏好读写（安全类不允许关站内）。
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import (
    NotificationListResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    UnreadCountResponse,
)
from app.services import notification_service

router = APIRouter(
    prefix="/api/notifications",
    tags=["通知"]
)


def _serialize(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=n.id,
        user_id=n.user_id,
        project_id=n.project_id,
        actor_id=n.actor_id,
        type=n.type,
        title=n.title,
        content=n.content,
        target_type=n.target_type,
        target_id=n.target_id,
        payload=notification_service._loads(n.payload),
        status=n.status,
        is_read=n.is_read,
        read_at=n.read_at,
        created_at=n.created_at,
    )


async def get_owned_notification(
    notification_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> Notification:
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知不存在")
    return n


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    type: str | None = Query(default=None, description="通知类型"),
    is_read: bool | None = Query(default=None, description="已读状态过滤"),
    project_id: uuid.UUID | None = Query(default=None, description="所属作品过滤"),
    from_time: datetime | None = Query(default=None, alias="from", description="起始时间"),
    to_time: datetime | None = Query(default=None, alias="to", description="结束时间"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="游标分页游标（base64）"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """通知列表（created_at 倒序，支持 cursor/limit 游标分页），含未读总数。"""
    items, total, unread_count, next_cursor = await notification_service.list_notifications(
        db,
        current_user.id,
        type=type,
        is_read=is_read,
        project_id=project_id,
        from_time=from_time,
        to_time=to_time,
        page=page,
        page_size=page_size,
        cursor=cursor,
    )
    return NotificationListResponse(
        items=[_serialize(n) for n in items],
        total=total,
        page=page,
        page_size=page_size,
        unread_count=unread_count,
        next_cursor=next_cursor,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """未读通知数量（客户端轮询：每 30-60 秒查询一次）。"""
    count = await notification_service.get_unread_count(db, current_user.id)
    return UnreadCountResponse(unread_count=count)


@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """标记单条通知已读。"""
    n = await get_owned_notification(notification_id, current_user, db)
    await notification_service.mark_read(db, n)
    await db.commit()
    await db.refresh(n)
    return _serialize(n)


@router.post("/read-all", response_model=UnreadCountResponse)
async def read_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """全部标记已读。"""
    await notification_service.mark_read_all(db, current_user.id)
    await db.commit()
    count = await notification_service.get_unread_count(db, current_user.id)
    return UnreadCountResponse(unread_count=count)


@router.post("/{notification_id}/archive", response_model=NotificationResponse)
async def archive(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """归档通知（保留审计，不直接删除未读）。"""
    n = await get_owned_notification(notification_id, current_user, db)
    await notification_service.archive(db, n)
    await db.commit()
    await db.refresh(n)
    return _serialize(n)


@router.get("/preferences", response_model=list[NotificationPreferenceResponse])
async def list_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """通知偏好列表。"""
    prefs = await notification_service.list_preferences(db, current_user.id)
    return [NotificationPreferenceResponse.model_validate(p) for p in prefs]


@router.put("/preferences/{notification_type}", response_model=NotificationPreferenceResponse)
async def update_preference(
    notification_type: str,
    data: NotificationPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新通知偏好；安全类通知不允许关闭站内通知（in_app 强制开启）。"""
    if notification_type not in notification_service.NOTIFICATION_TYPES and \
       notification_type not in notification_service.SECURE_NOTIFICATION_TYPES:
        raise HTTPException(status_code=400, detail="未知通知类型")
    pref = await notification_service.update_preference(
        db,
        current_user.id,
        notification_type,
        in_app_enabled=data.in_app_enabled,
        email_enabled=data.email_enabled,
        push_enabled=data.push_enabled,
    )
    await db.commit()
    await db.refresh(pref)
    return NotificationPreferenceResponse.model_validate(pref)
