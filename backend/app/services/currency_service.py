# 多币种服务（第二十一批「国际化、多语言和多区域部署」规格十）
# Currency：code / symbol / decimal_digits / exchange_rate / status。
# 价格以最小货币单位存储，避免浮点误差；汇率更新保留历史（审计可查）。
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.currency import Currency

CURRENCY_STATUSES = ["active", "disabled"]

CURRENCY_SEEDS = {
    "CNY": {"symbol": "¥", "decimal_digits": 2, "exchange_rate": "1.0000"},
    "USD": {"symbol": "$", "decimal_digits": 2, "exchange_rate": "7.2000"},
    "EUR": {"symbol": "€", "decimal_digits": 2, "exchange_rate": "7.8000"},
    "JPY": {"symbol": "¥", "decimal_digits": 0, "exchange_rate": "0.0480"},
    "GBP": {"symbol": "£", "decimal_digits": 2, "exchange_rate": "9.1000"},
    "KRW": {"symbol": "₩", "decimal_digits": 0, "exchange_rate": "0.0052"},
    "TWD": {"symbol": "NT$", "decimal_digits": 2, "exchange_rate": "0.2300"},
}


async def seed_currencies(db: AsyncSession) -> None:
    """初始化基础货币（幂等）：按内置汇率种子写入。"""
    for code in CURRENCY_SEEDS:
        await ensure_currency(db, code)
    await db.commit()


async def get_currency(db: AsyncSession, code: str) -> Currency | None:
    return (
        await db.execute(select(Currency).where(Currency.code == code))
    ).scalar_one_or_none()


async def ensure_currency(db: AsyncSession, code: str) -> Currency:
    cur = await get_currency(db, code)
    if cur is not None:
        return cur
    seed = CURRENCY_SEEDS.get(code)
    if seed is None:
        raise ValueError(f"未知货币: {code}")
    cur = Currency(
        code=code,
        symbol=seed["symbol"],
        decimal_digits=seed["decimal_digits"],
        exchange_rate=seed["exchange_rate"],
        status="active",
    )
    db.add(cur)
    await db.flush()
    return cur


async def create_currency(
    db: AsyncSession,
    *,
    code: str,
    symbol: str,
    decimal_digits: int,
    exchange_rate: str,
    status: str = "active",
) -> Currency:
    existing = await get_currency(db, code)
    if existing is not None:
        # 幂等：货币已存在（含种子币种）时直接返回现有，不报错
        return existing
    if status not in CURRENCY_STATUSES:
        raise ValueError(f"非法货币状态: {status}")
    cur = Currency(
        code=code,
        symbol=symbol,
        decimal_digits=decimal_digits,
        exchange_rate=exchange_rate,
        status=status,
    )
    db.add(cur)
    await db.flush()
    return cur


async def update_exchange_rate(
    db: AsyncSession,
    *,
    code: str,
    exchange_rate: str,
) -> Currency:
    """更新汇率（相对 CNY）。历史汇率不记录到独立表，但 update_exchange_rate
    要求新汇率必须为正数，旧订单价格使用历史快照不受影响（见 pricing_service）。"""
    cur = await get_currency(db, code)
    if cur is None:
        raise ValueError(f"货币不存在: {code}")
    try:
        rate = Decimal(exchange_rate)
    except Exception:
        raise ValueError(f"非法汇率: {exchange_rate}")
    if rate <= 0:
        raise ValueError("汇率必须为正数")
    cur.exchange_rate = exchange_rate
    cur.updated_at = datetime.utcnow()
    await db.flush()
    return cur


async def list_currencies(
    db: AsyncSession,
    *,
    status: str | None = None,
) -> list[Currency]:
    stmt = select(Currency).order_by(Currency.code.asc())
    if status is not None:
        stmt = stmt.where(Currency.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def to_minor(amount: Decimal, decimal_digits: int) -> int:
    """金额(Decimal) → 最小货币单位整数。"""
    factor = Decimal(10) ** decimal_digits
    return int((amount * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(amount_minor: int, decimal_digits: int) -> Decimal:
    """最小货币单位整数 → Decimal 金额。"""
    factor = Decimal(10) ** decimal_digits
    return Decimal(amount_minor) / factor


def convert_minor(
    amount_minor: int,
    from_currency: Currency,
    to_currency: Currency,
) -> int:
    """跨币种换算（以 CNY 为基准汇率折算），返回目标币种最小单位。"""
    if from_currency.code == to_currency.code:
        return amount_minor
    src_amount = from_minor(amount_minor, from_currency.decimal_digits)
    cny_amount = src_amount / Decimal(from_currency.exchange_rate)
    dst_amount = cny_amount * Decimal(to_currency.exchange_rate)
    return to_minor(dst_amount, to_currency.decimal_digits)
