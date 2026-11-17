# 商业化事件与提醒服务（第二十批「商业化、订阅与计费系统」规格十九/二十）
# BillingEvent：订阅/支付/额度事件日志。
# 站内通知：订阅到期/自动续费/支付失败/额度即将用完/套餐降级/退款完成等提醒。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_subscription import BillingEvent
from app.models.notification import Notification


async def emit_event(
    db: AsyncSession,
    event_type: str,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> BillingEvent:
    """写入订阅/支付/额度事件日志。"""
    event = BillingEvent(
        user_id=user_id,
        organization_id=organization_id,
        event_type=event_type,
        payload=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.add(event)
    await db.flush()
    return event


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    content: str,
    *,
    ntype: str = "system",
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict | None = None,
) -> Notification:
    """创建站内通知（订阅/支付/额度提醒）。"""
    notification = Notification(
        user_id=user_id,
        type=ntype,
        title=title,
        content=content,
        target_type=target_type,
        target_id=target_id,
        payload=json.dumps(payload or {}, ensure_ascii=False),
        status="unread",
        is_read=False,
    )
    db.add(notification)
    await db.flush()
    return notification


async def notify_payment_success(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_no: str,
    amount: float,
) -> None:
    await notify(
        db, user_id,
        title="支付成功",
        content=f"订单 {order_no} 支付成功，金额 {amount} 元。",
        ntype="system",
        target_type="order",
        target_id=order_no,
        payload={"order_no": order_no, "amount": amount},
    )


async def notify_payment_failed(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_no: str,
) -> None:
    await notify(
        db, user_id,
        title="支付失败",
        content=f"订单 {order_no} 支付失败，请重新尝试或更换支付方式。",
        ntype="system",
        target_type="order",
        target_id=order_no,
        payload={"order_no": order_no},
    )


async def notify_subscription_cancelled(
    db: AsyncSession,
    user_id: uuid.UUID,
    plan_name: str,
    period_end: datetime,
) -> None:
    await notify(
        db, user_id,
        title="订阅已取消",
        content=f"您的 {plan_name} 订阅已取消，当前周期结束后（{period_end:%Y-%m-%d}）将降级。",
        ntype="system",
        target_type="subscription",
        payload={"plan_name": plan_name, "period_end": period_end.isoformat()},
    )


async def notify_quota_warning(
    db: AsyncSession,
    user_id: uuid.UUID,
    quota_type: str,
    remaining: int,
) -> None:
    await notify(
        db, user_id,
        title="额度即将用完",
        content=f"您的 {quota_type} 额度剩余 {remaining}，即将耗尽，请及时升级套餐。",
        ntype="system",
        target_type="quota",
        target_id=quota_type,
        payload={"quota_type": quota_type, "remaining": remaining},
    )


async def notify_refund_done(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_no: str,
    amount: float,
) -> None:
    await notify(
        db, user_id,
        title="退款处理完成",
        content=f"订单 {order_no} 已退款 {amount} 元。",
        ntype="system",
        target_type="order",
        target_id=order_no,
        payload={"order_no": order_no, "amount": amount},
    )


async def list_events(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[BillingEvent]:
    stmt = select(BillingEvent).order_by(BillingEvent.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(BillingEvent.user_id == user_id)
    if event_type is not None:
        stmt = stmt.where(BillingEvent.event_type == event_type)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())
