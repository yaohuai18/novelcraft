# 财务对账服务（第二十批「商业化、订阅与计费系统」规格十六）
# 每日/每月对账：平台订单总额 vs 渠道到账 vs 退款金额 vs 手续费 vs 实际收入 vs 赠送额度 vs 异常订单。
# 状态：pending / matched / unmatched / manual_review / resolved。
# 差异记录：订单号、渠道交易号、差异金额、差异类型、处理人员、处理结果、处理时间。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_finance import Reconciliation
from app.models.billing_order import Order
from app.models.user import User


async def run_reconciliation(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    operator_id: uuid.UUID | None = None,
    override_provider_total: float | None = None,
) -> Reconciliation:
    """执行对账：
    - platform_total：平台订单总额（paid/partially_refunded/refunded 订单金额合计）
    - provider_total：渠道到账（stub 渠道按订单总额到账，零手续费；测试可注入差异）
    - diff = platform_total - provider_total；无差异 → matched，有差异 → manual_review
    - 退款金额/异常订单数/手续费等统计写入 result（JSON 摘要，服务端记录）
    """
    result = await db.execute(
        select(
            func.coalesce(func.sum(Order.amount), 0),
            func.count(Order.id),
        ).where(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status.in_(["paid", "partially_refunded", "refunded"]),
        )
    )
    platform_total, order_count = result.one()
    platform_total = float(platform_total or 0)

    result = await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0)).where(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status == "refunded",
        )
    )
    refund_total = float(result.scalar() or 0)

    result = await db.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status == "failed",
        )
    )
    abnormal_orders = int(result.scalar() or 0)

    provider_total = (
        float(override_provider_total)
        if override_provider_total is not None
        else platform_total
    )
    diff = round(platform_total - provider_total, 2)
    if abs(diff) < 0.005:
        status = "matched"
        diff_type = None
    else:
        status = "manual_review"
        diff_type = "amount_diff"

    operator_name = None
    if operator_id is not None:
        user_result = await db.execute(
            select(User.username).where(User.id == operator_id)
        )
        operator_name = user_result.scalar_one_or_none()

    rec = Reconciliation(
        recon_date=start.date().isoformat(),
        status=status,
        platform_total=platform_total,
        provider_total=provider_total,
        diff_amount=diff,
        diff_type=diff_type,
        operator_id=operator_id,
        operator_name=operator_name,
        result=json.dumps(
            {
                "refund_total": refund_total,
                "abnormal_order_count": abnormal_orders,
                "order_count": order_count,
                "fee_total": 0,
                "actual_income": round(provider_total - refund_total, 2),
                "note": "stub 渠道按 100% 到账、零手续费",
            },
            ensure_ascii=False,
        ),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def list_reconciliations(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[Reconciliation]:
    stmt = select(Reconciliation).order_by(Reconciliation.created_at.desc())
    if status is not None:
        stmt = stmt.where(Reconciliation.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def get_reconciliation(
    db: AsyncSession,
    recon_id: uuid.UUID,
) -> Reconciliation | None:
    result = await db.execute(
        select(Reconciliation).where(Reconciliation.id == recon_id)
    )
    return result.scalar_one_or_none()


async def resolve_reconciliation(
    db: AsyncSession,
    rec: Reconciliation,
    *,
    result_text: str,
    operator_id: uuid.UUID,
    operator_name: str | None = None,
) -> Reconciliation:
    """人工处理对账差异：记录处理人/结果/时间 → resolved。"""
    if operator_name is None:
        user_result = await db.execute(
            select(User.username).where(User.id == operator_id)
        )
        operator_name = user_result.scalar_one_or_none()
    rec.status = "resolved"
    rec.result = result_text
    rec.operator_id = operator_id
    rec.operator_name = operator_name
    rec.processed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(rec)
    return rec
