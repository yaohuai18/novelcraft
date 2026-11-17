# 地区价格与税费服务（第二十一批「国际化、多语言和多区域部署」规格十一/十二）
# PlanPrice：套餐 × 地区 × 货币 × 计费周期的价格（最小货币单位，tax_included 标记）。
# 客户端提交订单不能直接提交价格：服务端按 套餐/用户地区/货币/优惠/税费 计算。
# 税费只追加（TaxRecord），税率变化不影响历史订单（旧记录持有当时税率快照）。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_plan import Plan
from app.models.billing_order import Order
from app.models.plan_price import PlanPrice
from app.models.tax_record import TAX_TYPES, TaxRecord
from app.services import plan_service, promotion_service
from app.services.currency_service import (
    convert_minor,
    ensure_currency,
    from_minor,
    get_currency,
    to_minor,
)
from app.services.locale_service import format_currency

# 地区默认税率：region -> (rate, tax_type)，0.13 表示 13%
REGION_TAX_RULES = {
    "CN": (0.13, "vat"),
    "HK": (0.0, "none"),
    "TW": (0.05, "vat"),
    "US": (0.0, "sales_tax"),
    "EU": (0.20, "vat"),
    "JP": (0.10, "vat"),
    "GB": (0.20, "vat"),
    "KR": (0.10, "vat"),
}

# 区域价格回退顺序（找不到精确 region 时按顺序尝试）
REGION_FALLBACK_CHAIN = ["CN", "US", "EU", "JP", "GB", "KR", "HK", "TW"]


async def get_plan_price(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    region: str,
    currency: str,
    billing_interval: str = "monthly",
    status: str = "active",
) -> PlanPrice | None:
    return (
        await db.execute(
            select(PlanPrice).where(
                PlanPrice.plan_id == plan_id,
                PlanPrice.region == region,
                PlanPrice.currency == currency,
                PlanPrice.billing_interval == billing_interval,
                PlanPrice.status == status,
            )
        )
    ).scalar_one_or_none()


async def find_plan_price(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    region: str,
    currency: str,
    billing_interval: str = "monthly",
) -> PlanPrice | None:
    """按 精确 region → 回退链 查找可用价格；再按货币换算。"""
    # 先精确匹配 region+currency
    price = await get_plan_price(
        db,
        plan_id=plan_id,
        region=region,
        currency=currency,
        billing_interval=billing_interval,
    )
    if price is not None:
        return price
    # region 回退链中找同货币
    for fallback in REGION_FALLBACK_CHAIN:
        if fallback == region:
            continue
        price = await get_plan_price(
            db,
            plan_id=plan_id,
            region=fallback,
            currency=currency,
            billing_interval=billing_interval,
        )
        if price is not None:
            return price
    # 同 region 其它货币 → 用基准货币（CNY）换算
    base = await get_plan_price(
        db,
        plan_id=plan_id,
        region=region,
        currency="CNY",
        billing_interval=billing_interval,
    )
    if base is not None:
        return base
    return None


async def upsert_plan_price(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    region: str,
    currency: str,
    amount_minor: int,
    billing_interval: str = "monthly",
    tax_included: bool = False,
    status: str = "active",
) -> PlanPrice:
    """新增/更新地区价格（管理端）。版本自增，旧价格可通过版本历史审计。"""
    plan = await plan_service.get_plan(db, plan_id)
    if plan is None:
        raise ValueError("套餐不存在")
    await ensure_currency(db, currency)
    price = await get_plan_price(
        db,
        plan_id=plan_id,
        region=region,
        currency=currency,
        billing_interval=billing_interval,
    )
    if price is None:
        price = PlanPrice(
            plan_id=plan_id,
            region=region,
            currency=currency,
            amount_minor=amount_minor,
            billing_interval=billing_interval,
            tax_included=tax_included,
            status=status,
        )
        db.add(price)
        await db.flush()
        return price
    price.amount_minor = amount_minor
    price.tax_included = tax_included
    if status is not None:
        if status not in ("active", "inactive"):
            raise ValueError("非法价格状态")
        price.status = status
    price.version += 1
    price.updated_at = datetime.utcnow()
    await db.flush()
    return price


def resolve_tax_rule(region: str) -> tuple[float, str]:
    """返回地区默认税率与税种；未配置地区默认 0。"""
    return REGION_TAX_RULES.get(region, (0.0, "none"))


async def calc_price(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    region: str,
    currency: str,
    billing_interval: str = "monthly",
    promotion_code: str | None = None,
    user_id: uuid.UUID | None = None,
    tax_id_no: str | None = None,
    seats: int = 0,
    seat_price_minor: int = 0,
) -> dict:
    """服务端计算价格（多币种 + 税费 + 优惠），不信任客户端提交的金额。

    返回包含：基准价（最小单位）、优惠、含税/不含税金额、税额、税率、
    tax_included 语义、以及最终应付（最小单位）与展示金额。
    """
    plan = await plan_service.get_plan(db, plan_id)
    if plan is None:
        raise ValueError("套餐不存在")
    currency_model = await ensure_currency(db, currency)
    price = await find_plan_price(
        db,
        plan_id=plan_id,
        region=region,
        currency=currency,
        billing_interval=billing_interval,
    )
    if price is None:
        # 无地区价格：回退套餐内置 CNY 价格并换算
        base_cny_minor = to_minor(
            from_minor(int(round(float(plan.price) * 100)), 2),
            2,
        )
        cny = await ensure_currency(db, "CNY")
        base_minor = convert_minor(base_cny_minor, cny, currency_model)
        price_region = region
        tax_included = False
    else:
        base_minor = price.amount_minor
        price_region = price.region
        tax_included = price.tax_included

    interval_factor = plan_service.interval_factor(billing_interval)
    subtotal_minor = base_minor * interval_factor + seats * seat_price_minor

    # 优惠：promotion 折扣为 CNY 元，换算到目标货币最小单位
    discount_minor = 0
    promotion_id = None
    if promotion_code and user_id is not None:
        gross_cny = float(from_minor(subtotal_minor, 2))
        check = await promotion_service.validate_promotion(
            db, promotion_code, user_id, amount=gross_cny
        )
        if not check["valid"]:
            raise ValueError(check["reason"])
        discount_cny = check["discount"]
        discount_minor = to_minor(
            from_minor(int(round(discount_cny * 100)), 2),
            currency_model.decimal_digits,
        )
        promotion_id = check["promotion"].id if check["promotion"] else None
    if discount_minor > subtotal_minor:
        discount_minor = subtotal_minor
    net_minor = subtotal_minor - discount_minor

    # 税费
    rate, tax_type = resolve_tax_rule(price_region if price_region else region)
    tax_rate = rate if not tax_included else 0.0  # 含税价不再叠加
    tax_amount_minor = int(round(net_minor * tax_rate)) if tax_rate > 0 else 0
    if tax_included:
        # 含税价：税额从总价中拆出
        tax_amount_minor = int(round(net_minor - net_minor / (1 + tax_rate))) if tax_rate > 0 else 0
    amount_excl_minor = net_minor - tax_amount_minor if tax_included else net_minor
    amount_incl_minor = net_minor if tax_included else net_minor + tax_amount_minor

    tax_record = TaxRecord(
        order_id=None,
        region=region,
        currency=currency,
        rate=tax_rate,
        tax_type=tax_type,
        tax_amount_minor=tax_amount_minor,
        amount_excl_minor=amount_excl_minor,
        amount_incl_minor=amount_incl_minor,
        tax_id_no=tax_id_no,
    )
    db.add(tax_record)
    await db.flush()

    return {
        "plan_id": str(plan_id),
        "region": region,
        "price_region": price_region,
        "currency": currency,
        "billing_interval": billing_interval,
        "subtotal_minor": subtotal_minor,
        "discount_minor": discount_minor,
        "promotion_id": str(promotion_id) if promotion_id else None,
        "tax_rate": tax_rate,
        "tax_type": tax_type,
        "tax_amount_minor": tax_amount_minor,
        "amount_excl_minor": amount_excl_minor,
        "amount_incl_minor": amount_incl_minor,
        "amount_minor": amount_incl_minor,
        "tax_included": tax_included,
        "display": format_currency(
            amount_incl_minor,
            currency,
            symbol=currency_model.symbol,
            decimal_digits=currency_model.decimal_digits,
            locale="en-US",
        ),
        "tax_record_id": str(tax_record.id),
    }


async def bind_tax_record_to_order(
    db: AsyncSession,
    *,
    tax_record_id: uuid.UUID,
    order_id: uuid.UUID,
) -> TaxRecord:
    """订单确认后将税费记录绑定到订单（只追加、不回改）。"""
    rec = (
        await db.execute(
            select(TaxRecord).where(TaxRecord.id == tax_record_id)
        )
    ).scalar_one_or_none()
    if rec is None:
        raise ValueError("税费记录不存在")
    rec.order_id = order_id
    await db.flush()
    return rec


async def get_tax_records(
    db: AsyncSession,
    *,
    order_id: uuid.UUID | None = None,
    region: str | None = None,
) -> list[TaxRecord]:
    stmt = select(TaxRecord).order_by(TaxRecord.created_at.desc())
    if order_id is not None:
        stmt = stmt.where(TaxRecord.order_id == order_id)
    if region is not None:
        stmt = stmt.where(TaxRecord.region == region)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def seed_plan_prices(db: AsyncSession) -> None:
    """初始化地区价格（幂等）：按内置套餐 CNY 价格生成多地区价格种子。"""
    plans = await plan_service.list_plans(db)
    for plan in plans:
        if plan.price <= 0:
            continue
        base_cny_minor = int(round(float(plan.price) * 100))
        seeds = [
            ("CN", "CNY", base_cny_minor, 0.13),
            ("US", "USD", base_cny_minor * 720 // 100, 0.0),
            ("HK", "CNY", base_cny_minor, 0.0),
            ("TW", "CNY", base_cny_minor, 0.05),
            ("JP", "JPY", base_cny_minor * 48 // 100, 0.10),
            ("GB", "CNY", base_cny_minor, 0.20),
            ("EU", "CNY", base_cny_minor, 0.20),
            ("KR", "CNY", base_cny_minor, 0.10),
        ]
        for region, currency, amount_minor, _rate in seeds:
            existing = await get_plan_price(
                db,
                plan_id=plan.id,
                region=region,
                currency=currency,
                billing_interval=plan.billing_interval,
            )
            if existing is None:
                await upsert_plan_price(
                    db,
                    plan_id=plan.id,
                    region=region,
                    currency=currency,
                    amount_minor=amount_minor,
                    billing_interval=plan.billing_interval,
                    tax_included=False,
                )
    await db.commit()
