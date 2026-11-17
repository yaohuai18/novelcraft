# 订阅管理服务（第二十批「商业化、订阅与计费系统」规格四/十三/十四）
# 状态机：trialing → active → past_due → paused → cancelled / expired。
# 取消默认「周期结束取消 + 到期降级」；退款回收未使用权益。
# 升级/降级支持跨周期补差价。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_subscription import Subscription
from app.services import billing_event_service, plan_service, quota_service
from app.services.payment_provider import get_payment_provider

MONTHLY_DAYS = 30
YEARLY_DAYS = 365
TRIAL_DAYS = 7

# 降级后超额数据保留策略：不删除数据，禁止新增超限内容（由 check_limit 拦截）


def period_delta(interval: str, days: int = MONTHLY_DAYS) -> timedelta:
    if interval == "yearly":
        return timedelta(days=YEARLY_DAYS)
    if interval == "trial":
        return timedelta(days=TRIAL_DAYS)
    return timedelta(days=days)


async def create_subscription(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    plan_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    provider: str = "local_stub",
    provider_subscription_id: str | None = None,
    status: str = "active",
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
) -> Subscription:
    plan = await plan_service.get_plan(db, plan_id)
    if not plan:
        raise ValueError("套餐不存在")
    now = current_period_start or datetime.utcnow()
    end = current_period_end or (now + period_delta(plan.billing_interval))
    sub = Subscription(
        user_id=user_id,
        organization_id=organization_id,
        plan_id=plan_id,
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        status=status,
        current_period_start=now,
        current_period_end=end,
        cancel_at_period_end=False,
    )
    db.add(sub)
    await db.flush()
    await billing_event_service.emit_event(
        db,
        "subscription.created",
        user_id=user_id,
        organization_id=organization_id,
        payload={
            "subscription_id": str(sub.id),
            "plan_id": str(plan_id),
            "status": status,
        },
    )
    return sub


async def get_user_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None = None,
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    if organization_id is not None:
        stmt = (
            select(Subscription)
            .where(Subscription.organization_id == organization_id)
            .order_by(Subscription.created_at.desc())
        )
    result = await db.execute(stmt)
    return result.scalars().first()


async def cancel_subscription(
    db: AsyncSession,
    sub: Subscription,
    *,
    immediate: bool = False,
    user_id: uuid.UUID | None = None,
) -> Subscription:
    """取消订阅：
    - 默认周期结束取消（cancel_at_period_end=True，保留权益到周期末）
    - immediate=True 立即取消并降级
    """
    if sub.status not in ("active", "trialing", "past_due", "paused"):
        raise ValueError("当前订阅状态不可取消")
    now = datetime.utcnow()
    provider = get_payment_provider(sub.provider)
    await provider.cancel_subscription(sub.provider_subscription_id or "")
    sub.cancelled_at = now
    if immediate:
        sub.status = "cancelled"
        sub.cancel_at_period_end = True
    else:
        sub.cancel_at_period_end = True
        # 状态保留 active，周期末由到期处理降级
    sub.updated_at = now
    await db.flush()
    await billing_event_service.emit_event(
        db,
        "subscription.cancelled",
        user_id=user_id or sub.user_id,
        organization_id=sub.organization_id,
        payload={
            "subscription_id": str(sub.id),
            "immediate": immediate,
            "cancel_at_period_end": sub.cancel_at_period_end,
        },
    )
    plan = await plan_service.get_plan(db, sub.plan_id)
    if plan:
        await billing_event_service.notify_subscription_cancelled(
            db, sub.user_id, plan.name, sub.current_period_end,
        )
    await db.commit()
    await db.refresh(sub)
    return sub


async def resume_subscription(
    db: AsyncSession,
    sub: Subscription,
    *,
    user_id: uuid.UUID | None = None,
) -> Subscription:
    """恢复订阅（仅周期末取消未到期的订阅可恢复）。"""
    if sub.status not in ("active", "paused", "past_due"):
        raise ValueError("当前订阅状态不可恢复")
    if not sub.cancel_at_period_end and sub.status != "paused":
        raise ValueError("订阅未处于取消待生效状态")
    now = datetime.utcnow()
    if now > sub.current_period_end:
        raise ValueError("订阅已过期，无法恢复，请重新订阅")
    sub.cancel_at_period_end = False
    sub.cancelled_at = None
    sub.status = "active"
    sub.updated_at = now
    await db.flush()
    await billing_event_service.emit_event(
        db,
        "subscription.created",
        user_id=user_id or sub.user_id,
        organization_id=sub.organization_id,
        payload={"subscription_id": str(sub.id), "action": "resume"},
    )
    await db.commit()
    await db.refresh(sub)
    return sub


async def renew_subscription(
    db: AsyncSession,
    sub: Subscription,
    *,
    period_end: datetime | None = None,
    success: bool = True,
    user_id: uuid.UUID | None = None,
) -> Subscription:
    """自动续费：成功则滚动周期；失败标记 past_due（重试机会）。"""
    now = datetime.utcnow()
    if success:
        plan = await plan_service.get_plan(db, sub.plan_id)
        interval = plan.billing_interval if plan else "monthly"
        sub.current_period_start = now
        sub.current_period_end = period_end or (now + period_delta(interval))
        sub.status = "active"
        sub.cancel_at_period_end = False
        await billing_event_service.emit_event(
            db,
            "subscription.renewed",
            user_id=user_id or sub.user_id,
            organization_id=sub.organization_id,
            payload={"subscription_id": str(sub.id)},
        )
    else:
        sub.status = "past_due"
        await billing_event_service.emit_event(
            db,
            "payment.failed",
            user_id=user_id or sub.user_id,
            organization_id=sub.organization_id,
            payload={"subscription_id": str(sub.id)},
        )
    sub.updated_at = now
    await db.commit()
    await db.refresh(sub)
    return sub


async def change_plan(
    db: AsyncSession,
    sub: Subscription,
    new_plan_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> tuple[Subscription, float]:
    """套餐升级/降级：
    - 计算跨周期补差价（当前已付周期剩余价值 vs 新套餐按剩余天数折算）
    - 升级立即生效（upgraded）；降级立即生效但保留数据（downgraded）
    """
    new_plan = await plan_service.get_plan(db, new_plan_id)
    old_plan = await plan_service.get_plan(db, sub.plan_id)
    if not new_plan or not old_plan:
        raise ValueError("套餐不存在")

    now = datetime.utcnow()
    period_seconds = (
        sub.current_period_end - sub.current_period_start
    ).total_seconds() or 1
    remain_ratio = max(
        0.0,
        (sub.current_period_end - now).total_seconds() / period_seconds,
    )
    old_value = float(old_plan.price) * remain_ratio
    new_value = float(new_plan.price) * remain_ratio
    diff = round(new_value - old_value, 2)
    if diff < 0:
        diff = 0  # 降级不退款（策略：周期内不退款），避免复杂

    sub.plan_id = new_plan_id
    if new_plan.billing_interval != old_plan.billing_interval:
        sub.current_period_end = now + period_delta(new_plan.billing_interval)
    sub.updated_at = now
    await db.flush()
    event = "subscription.upgraded" if new_value > old_value else "subscription.downgraded"
    await billing_event_service.emit_event(
        db,
        event,
        user_id=user_id or sub.user_id,
        organization_id=sub.organization_id,
        payload={
            "subscription_id": str(sub.id),
            "old_plan_id": str(old_plan.id),
            "new_plan_id": str(new_plan.id),
            "diff_amount": diff,
        },
    )
    await db.commit()
    await db.refresh(sub)
    return sub, diff


async def expire_if_ended(
    db: AsyncSession,
    sub: Subscription,
    *,
    user_id: uuid.UUID | None = None,
) -> Subscription | None:
    """周期到期处理：若已过周期末且标记周期末取消 → 状态置 expired/cancelled 并降级。
    降级策略：保留已有数据，禁止新增超限（check_limit 负责）。"""
    now = datetime.utcnow()
    if sub.current_period_end > now:
        return sub
    if sub.status == "expired":
        return sub
    if sub.cancel_at_period_end or sub.status == "cancelled":
        sub.status = "expired" if sub.cancel_at_period_end else sub.status
        sub.cancelled_at = sub.cancelled_at or now
        sub.updated_at = now
        await billing_event_service.emit_event(
            db,
            "subscription.expired",
            user_id=user_id or sub.user_id,
            organization_id=sub.organization_id,
            payload={"subscription_id": str(sub.id)},
        )
        await db.commit()
        await db.refresh(sub)
    return sub


async def list_subscriptions(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[Subscription]:
    stmt = select(Subscription).order_by(Subscription.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Subscription.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Subscription.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())
