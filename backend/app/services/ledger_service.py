# 账务服务（第二十批「商业化、订阅与计费系统」规格十五）
# LedgerEntry 只追加、不可修改、可追溯（来源订单/参考号），支持退款关联与月度汇总。
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_finance import LedgerEntry


async def record_entry(
    db: AsyncSession,
    *,
    entry_type: str,
    user_id: uuid.UUID | None,
    amount: float,
    currency: str = "CNY",
    direction: str = "in",
    organization_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    reference_id: str | None = None,
    operator_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> LedgerEntry:
    """写入一条账务记录（只追加，不提供修改/删除入口）。"""
    if direction not in ("in", "out"):
        raise ValueError("账务方向必须为 in/out")
    entry = LedgerEntry(
        entry_type=entry_type,
        user_id=user_id,
        organization_id=organization_id,
        order_id=order_id,
        amount=float(amount),
        currency=currency,
        direction=direction,
        reference_id=reference_id,
        operator_id=operator_id,
        reason=reason,
    )
    db.add(entry)
    await db.flush()
    return entry


async def list_entries(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[LedgerEntry]:
    stmt = select(LedgerEntry).order_by(LedgerEntry.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(LedgerEntry.user_id == user_id)
    if organization_id is not None:
        stmt = stmt.where(LedgerEntry.organization_id == organization_id)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def monthly_summary(
    db: AsyncSession,
    year: int,
    month: int,
) -> dict:
    """月度汇总：平台订单总额/退款金额/赠送额度/实际收入。"""
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)

    result = await db.execute(
        select(
            func.coalesce(func.sum(LedgerEntry.amount), 0),
        ).where(
            LedgerEntry.created_at >= start,
            LedgerEntry.created_at < end,
            LedgerEntry.direction == "in",
            LedgerEntry.entry_type == "payment",
        )
    )
    revenue = float(result.scalar() or 0)

    result = await db.execute(
        select(
            func.coalesce(func.sum(LedgerEntry.amount), 0),
        ).where(
            LedgerEntry.created_at >= start,
            LedgerEntry.created_at < end,
            LedgerEntry.direction == "out",
            LedgerEntry.entry_type == "refund",
        )
    )
    refunds = float(result.scalar() or 0)

    result = await db.execute(
        select(func.count(LedgerEntry.id)).where(
            LedgerEntry.created_at >= start,
            LedgerEntry.created_at < end,
            LedgerEntry.entry_type == "quota_grant",
        )
    )
    quota_grants = int(result.scalar() or 0)

    return {
        "year": year,
        "month": month,
        "revenue": revenue,
        "refunds": refunds,
        "quota_grant_entries": quota_grants,
        "net_income": round(revenue - refunds, 2),
    }
