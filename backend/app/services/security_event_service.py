# 安全事件服务（第十八批「性能优化、可靠性与安全加固」规格十八/二十四）
# 覆盖登录失败/成功、改密、改权限、导出、恢复备份、查看私人内容、删内容、
# 生成 API 密钥、修改系统配置等安全事件；IP 与设备信息脱敏存储。
# 审计记录不可随意修改、不可由普通管理员删除（未提供公开删除接口）。
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security_utils import sha256_hex
from app.models.security_event import SecurityEvent

EVENT_TYPES = {
    "login_failed",
    "login_success",
    "password_changed",
    "permission_changed",
    "data_exported",
    "backup_restored",
    "private_content_viewed",
    "content_deleted",
    "api_key_generated",
    "config_changed",
    "new_device_login",
    "unusual_location_login",
    "account_locked",
    "data_export",
    "account_delete_requested",
}


async def record(
    db: AsyncSession,
    *,
    event_type: str,
    user_id: uuid.UUID | None,
    ip_address: str | None = None,
    device_info: str | None = None,
    details: str | None = None,
) -> SecurityEvent:
    """记录安全事件（IP 哈希脱敏，details 限制长度）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知安全事件类型: {event_type}")
    event = SecurityEvent(
        event_type=event_type,
        user_id=user_id,
        ip_hash=sha256_hex(ip_address) if ip_address else None,
        device_info=(device_info or "")[:255],
        details=(details or "")[:2000],
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def list_events(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    event_type: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[SecurityEvent], int]:
    """安全事件查询（管理端）。"""
    filters = []
    if user_id is not None:
        filters.append(SecurityEvent.user_id == user_id)
    if event_type:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知安全事件类型: {event_type}")
        filters.append(SecurityEvent.event_type == event_type)
    if from_dt:
        filters.append(SecurityEvent.created_at >= from_dt)
    if to_dt:
        filters.append(SecurityEvent.created_at <= to_dt)
    total = (
        await db.execute(select(func.count(SecurityEvent.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(SecurityEvent)
            .where(*filters)
            .order_by(SecurityEvent.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
