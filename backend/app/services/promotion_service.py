# 优惠与促销服务（第二十批「商业化、订阅与计费系统」规格十二）
# 防滥用：重复使用、过期优惠、优惠超过订单金额、每用户限次、全局限次。
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_promotion import Promotion, PromotionRedemption


async def get_promotion(
    db: AsyncSession,
    code: str | None = None,
    promotion_id: uuid.UUID | None = None,
) -> Promotion | None:
    if code:
        result = await db.execute(
            select(Promotion).where(Promotion.code == code)
        )
        return result.scalar_one_or_none()
    if promotion_id:
        result = await db.execute(
            select(Promotion).where(Promotion.id == promotion_id)
        )
        return result.scalar_one_or_none()
    return None


def _is_active(promo: Promotion, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if promo.status != "active":
        return False
    if promo.starts_at and now < promo.starts_at:
        return False
    if promo.ends_at and now > promo.ends_at:
        return False
    return True


async def validate_promotion(
    db: AsyncSession,
    code: str,
    user_id: uuid.UUID,
    *,
    amount: float = 0,
    now: datetime | None = None,
) -> dict:
    """校验优惠码可用性：
    - 不存在/失效/未开始/已过期 → 失败
    - 全局使用次数超限 → 失败
    - 该用户使用次数超限 → 失败
    返回折扣信息（discount / quota_grant / promotion_id / applied_amount）。
    """
    now = now or datetime.utcnow()
    promo = await get_promotion(db, code=code)
    if not promo:
        return {"valid": False, "reason": "优惠码不存在", "promotion": None}
    if not _is_active(promo, now):
        return {"valid": False, "reason": "优惠码不可用或已过期", "promotion": promo}

    total = await _redemption_count(db, promo.id)
    if promo.usage_limit is not None and total >= promo.usage_limit:
        return {"valid": False, "reason": "优惠码使用次数已达上限", "promotion": promo}

    user_used = await _user_redemption_count(db, promo.id, user_id)
    if user_used >= promo.per_user_limit:
        return {"valid": False, "reason": "该优惠码已达个人使用上限", "promotion": promo}

    discount = 0.0
    quota_grant = 0
    if promo.promotion_type == "percent":
        ratio = float(promo.discount_value) / 100.0
        discount = round(float(amount) * ratio, 2)
    elif promo.promotion_type == "fixed":
        discount = float(promo.discount_value)
    # 优惠金额不能超过订单金额
    if discount > amount:
        discount = amount
    if promo.quota_grant:
        quota_grant = promo.quota_grant

    return {
        "valid": True,
        "reason": "ok",
        "promotion": promo,
        "discount": discount,
        "quota_grant": quota_grant,
    }


async def _redemption_count(db: AsyncSession, promotion_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count(PromotionRedemption.id)).where(
            PromotionRedemption.promotion_id == promotion_id
        )
    )
    return int(result.scalar() or 0)


async def _user_redemption_count(
    db: AsyncSession,
    promotion_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    result = await db.execute(
        select(func.count(PromotionRedemption.id)).where(
            PromotionRedemption.promotion_id == promotion_id,
            PromotionRedemption.user_id == user_id,
        )
    )
    return int(result.scalar() or 0)


async def create_promotion(
    db: AsyncSession,
    *,
    name: str,
    promotion_type: str,
    discount_value: float = 0,
    quota_grant: int | None = None,
    code: str | None = None,
    usage_limit: int | None = None,
    per_user_limit: int = 1,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    operator_id: uuid.UUID | None = None,
) -> Promotion:
    """管理员创建优惠活动（promotion_admin/super_admin）。"""
    if promotion_type not in (
        "percent", "fixed", "grant_quota", "upgrade_diff", "trial"
    ):
        raise ValueError("无效的优惠类型")
    promo = Promotion(
        name=name,
        code=code,
        promotion_type=promotion_type,
        discount_value=discount_value,
        quota_grant=quota_grant,
        usage_limit=usage_limit,
        per_user_limit=per_user_limit,
        starts_at=starts_at,
        ends_at=ends_at,
        status="active",
    )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return promo


async def mark_redemption(
    db: AsyncSession,
    promotion_id: uuid.UUID,
    user_id: uuid.UUID,
    order_id: uuid.UUID | None = None,
) -> PromotionRedemption:
    """记录优惠使用（订单创建时调用，防重复套取）。"""
    redemption = PromotionRedemption(
        promotion_id=promotion_id,
        user_id=user_id,
        order_id=order_id,
    )
    db.add(redemption)
    await db.commit()
    await db.refresh(redemption)
    return redemption


async def list_promotions(db: AsyncSession) -> list[Promotion]:
    result = await db.execute(
        select(Promotion).order_by(Promotion.created_at.desc())
    )
    return list(result.scalars().all())
