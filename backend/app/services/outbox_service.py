# Outbox 模式服务（第十八批「性能优化、可靠性与安全加固」规格十四/十五）
# 事务性发件箱：业务数据 + 事件在同一事务保存 -> 后台任务读取未发布事件 ->
# 投递到消费者 -> 标记完成。投递失败可重试，超过次数进入失败态待人工排查。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent

MAX_ATTEMPTS = 5


async def add_event(
    db: AsyncSession,
    *,
    event_type: str,
    payload: dict,
) -> OutboxEvent:
    """在业务事务内登记事件（与业务数据同事务提交，保证不丢）。"""
    event = OutboxEvent(
        event_type=event_type,
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        available_at=datetime.utcnow(),
    )
    db.add(event)
    await db.flush()
    return event


async def claim_pending(
    db: AsyncSession,
    *,
    batch_size: int = 10,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[OutboxEvent]:
    """后台任务领取待发布事件（pending/retrying 且 available_at <= now）。"""
    rows = (
        await db.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.status.in_(["pending", "retrying"]),
                OutboxEvent.available_at <= datetime.utcnow(),
            )
            .order_by(OutboxEvent.created_at.asc())
            .limit(batch_size)
        )
    ).scalars().all()
    return list(rows)


async def mark_delivered(db: AsyncSession, event: OutboxEvent) -> OutboxEvent:
    """投递成功：标记完成。"""
    event.status = "delivered"
    event.processed_at = datetime.utcnow()
    event.error_message = None
    await db.flush()
    return event


async def mark_failed(
    db: AsyncSession,
    event: OutboxEvent,
    *,
    error_message: str,
    retry_delay_seconds: int = 30,
) -> OutboxEvent:
    """投递失败：尝试次数未超则进入 retrying（指数退避），超限进入 failed。"""
    event.attempts += 1
    event.error_message = error_message
    if event.attempts >= MAX_ATTEMPTS:
        event.status = "failed"
    else:
        event.status = "retrying"
        event.available_at = datetime.utcnow() + timedelta(seconds=retry_delay_seconds * event.attempts)
    await db.flush()
    return event


async def list_events(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    event_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[OutboxEvent], int]:
    """Outbox 事件查询（管理端）。"""
    filters = []
    if status_filter:
        filters.append(OutboxEvent.status == status_filter)
    if event_type:
        filters.append(OutboxEvent.event_type == event_type)
    total = (
        await db.execute(select(func.count(OutboxEvent.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(OutboxEvent)
            .where(*filters)
            .order_by(OutboxEvent.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
