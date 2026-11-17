# 订单服务（第二十批「商业化、订阅与计费系统」规格六/七）
# 订单必须具备：唯一订单号、支付状态、金额和币种、支付渠道、关联用户、关联商品/套餐、退款状态。
# 金额计算全部在服务端完成（套餐价格 × 周期系数 - 优惠折扣 + 升级补差 + 超额席位），不信任客户端。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_order import Order
from app.services import (
    plan_service,
    promotion_service,
    organization_service,
)
from app.services.payment_provider import get_payment_provider

ORDER_NO_PREFIX = "NC"


async def generate_order_no(db: AsyncSession) -> str:
    """生成唯一订单号：NC + 时间戳 + 随机段；入库前查重。"""
    while True:
        order_no = (
            f"{ORDER_NO_PREFIX}{datetime.utcnow():%Y%m%d%H%M%S}"
            f"{uuid.uuid4().hex[:10].upper()}"
        )
        result = await db.execute(
            select(Order.id).where(Order.order_no == order_no)
        )
        if not result.scalar_one_or_none():
            return order_no


async def calc_amount(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID | None = None,
    order_type: str = "subscription",
    promotion_code: str | None = None,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    interval: str = "monthly",
    seats: int = 0,
) -> dict:
    """服务端金额计算（不信任客户端）：
    base = plan.price × interval_factor
    团队订单追加超额席位费：seats × OVERAGE_SEAT_PRICE
    减去优惠折扣（限优惠金额不超过订单金额）
    返回金额、折扣、赠送额度信息。
    """
    plan = await plan_service.get_plan(db, plan_id) if plan_id else None
    base = 0.0
    plan_interval = interval
    if plan:
        plan_interval = plan.billing_interval
        base = float(plan.price) * plan_service.interval_factor(plan_interval)
    seat_fee = 0.0
    if order_type == "team_seat" or organization_id is not None:
        if seats > 0:
            seat_fee = float(seats) * organization_service.OVERAGE_SEAT_PRICE

    gross = round(base + seat_fee, 2)
    discount = 0.0
    quota_grant = 0
    promotion_id = None
    if promotion_code and user_id is not None:
        check = await promotion_service.validate_promotion(
            db, promotion_code, user_id, amount=gross
        )
        if check["valid"]:
            discount = check["discount"]
            quota_grant = check["quota_grant"]
            promotion_id = check["promotion"].id if check["promotion"] else None
        else:
            raise ValueError(check["reason"])
    final = round(gross - discount, 2)
    if final < 0:
        final = 0.0
    return {
        "plan_id": str(plan.id) if plan else None,
        "plan_interval": plan_interval,
        "base": base,
        "seat_fee": seat_fee,
        "discount": discount,
        "quota_grant": quota_grant,
        "promotion_id": str(promotion_id) if promotion_id else None,
        "amount": final,
        "currency": "CNY",
    }


async def create_order(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    order_type: str = "subscription",
    plan_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    promotion_code: str | None = None,
    interval: str = "monthly",
    seats: int = 0,
    description: str | None = None,
) -> Order:
    """创建订单：金额由服务端计算，重复创建由唯一订单号 + 未支付订单查重防护。"""
    # 未支付同类订单去重：避免用户重复点击产生多个订单（幂等）
    dup = await find_pending_order(
        db, user_id=user_id, organization_id=organization_id,
        plan_id=plan_id, order_type=order_type,
    )
    if dup:
        return dup

    calc = await calc_amount(
        db,
        plan_id=plan_id,
        order_type=order_type,
        promotion_code=promotion_code,
        user_id=user_id,
        organization_id=organization_id,
        interval=interval,
        seats=seats,
    )
    order_no = await generate_order_no(db)
    provider = get_payment_provider()
    checkout = await provider.create_checkout(
        order_no,
        calc["amount"],
        calc["currency"],
    )
    order = Order(
        order_no=order_no,
        user_id=user_id,
        organization_id=organization_id,
        plan_id=plan_id,
        order_type=order_type,
        amount=calc["amount"],
        currency=calc["currency"],
        status="pending",
        payment_provider=provider.name,
        provider_order_id=checkout["provider_order_id"],
        promotion_applied=promotion_code,
        description=description,
    )
    db.add(order)
    await db.flush()
    if calc["promotion_id"]:
        await promotion_service.mark_redemption(
            db, uuid.UUID(calc["promotion_id"]), user_id, order.id
        )
        # 注意：mark_redemption 内部 commit，重新刷新订单
        await db.refresh(order)
    else:
        await db.commit()
        await db.refresh(order)
    return order


async def find_pending_order(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
    plan_id: uuid.UUID | None,
    order_type: str,
) -> Order | None:
    result = await db.execute(
        select(Order).where(
            Order.user_id == user_id,
            Order.organization_id == organization_id,
            Order.plan_id == plan_id,
            Order.order_type == order_type,
            Order.status == "pending",
        ).order_by(Order.created_at.desc())
    )
    return result.scalars().first()


async def get_order(
    db: AsyncSession,
    order_id: uuid.UUID | None = None,
    order_no: str | None = None,
) -> Order | None:
    if order_id:
        result = await db.execute(select(Order).where(Order.id == order_id))
        return result.scalar_one_or_none()
    if order_no:
        result = await db.execute(select(Order).where(Order.order_no == order_no))
        return result.scalar_one_or_none()
    return None


async def get_order_by_provider(
    db: AsyncSession,
    provider_order_id: str,
) -> Order | None:
    result = await db.execute(
        select(Order).where(Order.provider_order_id == provider_order_id)
    )
    return result.scalar_one_or_none()


async def list_orders(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[Order]:
    stmt = select(Order).order_by(Order.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    if organization_id is not None:
        stmt = stmt.where(Order.organization_id == organization_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def update_order_status(
    db: AsyncSession,
    order: Order,
    status: str,
    *,
    provider_event_id: str | None = None,
    paid_at: datetime | None = None,
    cancelled_at: datetime | None = None,
) -> Order:
    """订单状态迁移（支付/取消/退款）。"""
    order.status = status
    if provider_event_id is not None:
        order.provider_event_id = provider_event_id
    if paid_at is not None:
        order.paid_at = paid_at
    if cancelled_at is not None:
        order.cancelled_at = cancelled_at
    order.updated_at = datetime.utcnow()
    await db.flush()
    return order
