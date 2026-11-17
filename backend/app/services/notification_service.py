# 通知服务（第十五批「通知、评论与协作反馈」规格实现）
# 站内通知创建（按偏好过滤 in_app）、查询（type/is_read/project_id/from/to 过滤）、
# 已读/全部已读/归档；通知偏好读写；安全类通知不允许关闭站内。
# 第十八批规格六：通知列表支持游标分页（cursor/limit，keyset 分页）。
import base64
import json
import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NOTIFICATION_STATUSES,
    NOTIFICATION_TYPES,
    SECURE_NOTIFICATION_TYPES,
    Notification,
    NotificationPreference,
)
from app.models.user import User


def _dumps(payload: dict | None) -> str | None:
    return json.dumps(payload, ensure_ascii=False) if payload is not None else None


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def create_notification(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    content: str | None = None,
    project_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict | None = None,
) -> Notification | None:
    """创建站内通知；按用户偏好过滤 in_app（安全类通知不允许关闭，强制创建）。

    返回创建的 Notification；被偏好过滤时返回 None。
    """
    pref = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.notification_type == type,
            )
        )
    ).scalar_one_or_none()

    if pref is not None and not pref.in_app_enabled:
        if type not in SECURE_NOTIFICATION_TYPES:
            return None

    notification = Notification(
        user_id=user_id,
        project_id=project_id,
        actor_id=actor_id,
        type=type,
        title=title,
        content=content,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        payload=_dumps(payload),
        status="unread",
        is_read=False,
    )
    db.add(notification)
    await db.flush()
    return notification


async def list_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    type: str | None = None,
    is_read: bool | None = None,
    project_id: uuid.UUID | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
) -> tuple[list[Notification], int, int, str | None]:
    """查询通知列表（按 created_at 倒序），返回 (items, total, unread_count, next_cursor)。

    游标分页：cursor 为 base64("created_at_iso|notification_id")，keyset 过滤。
    """
    filters = [
        Notification.user_id == user_id,
        # 归档通知从通知中心隐藏（规格三：标记已归档便于审计，不直接删除未读）
        Notification.status != "archived",
    ]
    if type:
        filters.append(Notification.type == type)
    if is_read is not None:
        filters.append(Notification.is_read == is_read)
    if project_id is not None:
        filters.append(Notification.project_id == project_id)
    if from_time is not None:
        filters.append(Notification.created_at >= from_time)
    if to_time is not None:
        filters.append(Notification.created_at <= to_time)

    cursor_dt, cursor_id = _decode_cursor(cursor) if cursor else (None, None)
    if cursor_dt is not None and cursor_id is not None:
        filters.append(
            or_(
                Notification.created_at < cursor_dt,
                and_(
                    Notification.created_at == cursor_dt,
                    Notification.id < uuid.UUID(cursor_id),
                ),
            )
        )

    total = (
        await db.execute(select(func.count(Notification.id)).where(*filters))
    ).scalar_one()

    unread_count = (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
                Notification.status != "archived",
            )
        )
    ).scalar_one()

    result = await db.execute(
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(page_size)
    )
    items = list(result.scalars().all())
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].id)
        if len(items) == page_size
        else None
    )
    return items, total, unread_count, next_cursor


def _encode_cursor(created_at: datetime, notification_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{notification_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime | None, str | None]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        dt_str, nid = raw.rsplit("|", 1)
        return datetime.fromisoformat(dt_str), nid
    except Exception:
        return None, None


async def get_unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
                Notification.status != "archived",
            )
        )
    ).scalar_one()


async def mark_read(db: AsyncSession, notification: Notification) -> None:
    """标记单条已读（归档/已读均幂等）。"""
    if notification.is_read:
        return
    notification.is_read = True
    notification.read_at = datetime.utcnow()
    if notification.status == "unread":
        notification.status = "read"
    await db.flush()


async def mark_read_all(db: AsyncSession, user_id: uuid.UUID) -> int:
    """全部标记已读，返回受影响条数。"""
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
    )
    items = list(result.scalars().all())
    now = datetime.utcnow()
    for n in items:
        n.is_read = True
        n.read_at = now
        if n.status == "unread":
            n.status = "read"
    await db.flush()
    return len(items)


async def archive(db: AsyncSession, notification: Notification) -> None:
    """归档：从通知中心隐藏但保留审计。"""
    notification.status = "archived"
    await db.flush()


# ==================== 通知偏好 ====================

async def get_or_create_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
) -> NotificationPreference:
    pref = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.notification_type == notification_type,
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        pref = NotificationPreference(
            user_id=user_id,
            notification_type=notification_type,
        )
        db.add(pref)
        await db.flush()
    return pref


async def update_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
    *,
    in_app_enabled: bool | None = None,
    email_enabled: bool | None = None,
    push_enabled: bool | None = None,
) -> NotificationPreference:
    """更新通知偏好；安全类通知不允许关闭站内通知（in_app 强制 True）。"""
    pref = await get_or_create_preference(db, user_id, notification_type)
    if in_app_enabled is not None:
        pref.in_app_enabled = in_app_enabled
    if email_enabled is not None:
        pref.email_enabled = email_enabled
    if push_enabled is not None:
        pref.push_enabled = push_enabled

    if notification_type in SECURE_NOTIFICATION_TYPES:
        pref.in_app_enabled = True
    await db.flush()
    return pref


async def list_preferences(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[NotificationPreference]:
    result = await db.execute(
        select(NotificationPreference)
        .where(NotificationPreference.user_id == user_id)
        .order_by(NotificationPreference.notification_type.asc())
    )
    return list(result.scalars().all())
