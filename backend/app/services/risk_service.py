# 风控监控服务（第二十批「商业化、订阅与计费系统」规格二十一）
# 监控：短时间大量注册/大量试用额度/异常优惠码使用/支付失败次数过多/
#       同一设备多账号/异常退款行为/AI 额度快速消耗。
# 措施：flag（标记）/ review（人工审核）/ delay（延迟发放）/ freeze（冻结账户）。
# 记录可申诉（appeal），避免误伤正常用户。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_finance import RiskEvent
from app.models.billing_order import Order
from app.models.billing_subscription import Subscription
from app.models.billing_quota import QuotaLedger

RISK_ACTIONS = ["flag", "review", "delay", "freeze", "verify"]


async def record_risk(
    db: AsyncSession,
    event_type: str,
    *,
    user_id: uuid.UUID | None = None,
    severity: str = "medium",
    action_taken: str = "flag",
    evidence: str | None = None,
) -> RiskEvent:
    """记录风控事件。"""
    if action_taken not in RISK_ACTIONS:
        action_taken = "flag"
    event = RiskEvent(
        user_id=user_id,
        event_type=event_type,
        severity=severity,
        action_taken=action_taken,
        status="open",
        evidence=evidence,
    )
    db.add(event)
    await db.flush()
    return event


async def check_payment_failures(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    threshold: int = 3,
    window_minutes: int = 30,
) -> RiskEvent | None:
    """支付失败次数过多 → 标记风险（verify/人工审核）。"""
    since = datetime.utcnow() - timedelta(minutes=window_minutes)
    result = await db.execute(
        select(func.count(Order.id)).where(
            Order.user_id == user_id,
            Order.status == "failed",
            Order.created_at >= since,
        )
    )
    failures = int(result.scalar() or 0)
    if failures >= threshold:
        return await record_risk(
            db,
            "payment_failure",
            user_id=user_id,
            severity="high",
            action_taken="verify",
            evidence=f"最近 {window_minutes} 分钟内支付失败 {failures} 次",
        )
    return None


async def check_trial_abuse(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    threshold: int = 2,
) -> RiskEvent | None:
    """试用订阅过多 → 标记试用滥用。"""
    result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == user_id,
            Subscription.status == "trialing",
        )
    )
    trials = int(result.scalar() or 0)
    if trials >= threshold:
        return await record_risk(
            db,
            "trial_abuse",
            user_id=user_id,
            severity="medium",
            action_taken="review",
            evidence=f"试用订阅数量 {trials} 超过阈值 {threshold}",
        )
    return None


async def check_quota_burn(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    threshold: int = 50000,
    window_minutes: int = 10,
) -> RiskEvent | None:
    """AI 额度快速消耗 → 标记风控（延迟发放/人工审核）。"""
    since = datetime.utcnow() - timedelta(minutes=window_minutes)
    result = await db.execute(
        select(func.coalesce(func.sum(QuotaLedger.amount), 0)).where(
            QuotaLedger.created_at >= since,
            QuotaLedger.action == "consume",
        )
    )
    total = int(result.scalar() or 0)
    if total >= threshold:
        return await record_risk(
            db,
            "quota_burn",
            user_id=user_id,
            severity="high",
            action_taken="delay",
            evidence=f"最近 {window_minutes} 分钟内消耗额度 {total}",
        )
    return None


async def list_risks(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[RiskEvent]:
    stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(RiskEvent.user_id == user_id)
    if status is not None:
        stmt = stmt.where(RiskEvent.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def appeal_risk(
    db: AsyncSession,
    risk_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
) -> RiskEvent | None:
    """用户申诉渠道：将 open/reviewed 的风险事件转为 appealed 状态。"""
    result = await db.execute(
        select(RiskEvent).where(RiskEvent.id == risk_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return None
    # 仅本人可申诉（或超级管理员）
    if event.user_id and event.user_id != user_id:
        return None
    event.status = "appealed"
    event.evidence = (event.evidence or "") + f"\n[appeal] {reason}"
    await db.commit()
    await db.refresh(event)
    return event


async def resolve_risk(
    db: AsyncSession,
    risk_id: uuid.UUID,
    *,
    result: str,
) -> RiskEvent | None:
    """风控处理：resolved + 处理结果。"""
    result_row = await db.execute(
        select(RiskEvent).where(RiskEvent.id == risk_id)
    )
    event = result_row.scalar_one_or_none()
    if not event:
        return None
    event.status = "resolved"
    event.evidence = (event.evidence or "") + f"\n[resolve] {result}"
    await db.commit()
    await db.refresh(event)
    return event
