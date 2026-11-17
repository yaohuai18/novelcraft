# 权益服务（第二十批「商业化、订阅与计费系统」规格八）
# EntitlementService：
#   - get_effective_plan 综合基础套餐/付费订阅/团队套餐
#   - check_feature      能力开关
#   - check_limit        限制值（结合赠送额度）
#   - consume_quota / refund_quota 额度使用与回补
# 最终有效权益 = 基础套餐 + 临时赠送 + 活动额度 - 已使用额度。
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_plan import Plan, PlanFeature
from app.models.billing_subscription import Subscription
from app.models.billing_org import Organization
from app.services import plan_service, quota_service

# 订阅有效状态
ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active", "past_due", "paused"}


async def get_effective_plan(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
) -> Plan:
    """计算用户/组织的有效套餐：
    - 组织订阅优先（团队套餐覆盖成员个人）
    - 用户当前有效订阅（trialing/active/past_due/paused）
    - 无订阅 → 免费套餐
    """
    if organization_id is not None:
        result = await db.execute(
            select(Subscription).where(
                Subscription.organization_id == organization_id,
                Subscription.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
            ).order_by(Subscription.created_at.desc())
        )
        sub = result.scalars().first()
        if sub:
            plan = await plan_service.get_plan(db, sub.plan_id)
            if plan:
                return plan
        org_result = await db.execute(
            select(Organization).where(Organization.id == organization_id)
        )
        org = org_result.scalar_one_or_none()
        if org and org.plan_id:
            plan = await plan_service.get_plan(db, org.plan_id)
            if plan:
                return plan

    if user_id is not None:
        result = await db.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
            ).order_by(Subscription.created_at.desc())
        )
        sub = result.scalars().first()
        if sub:
            plan = await plan_service.get_plan(db, sub.plan_id)
            if plan:
                return plan

    free = await plan_service.get_free_plan(db)
    if free:
        return free
    # 兜底：直接建一个免费套餐
    plans = await plan_service.list_plans(db, only_active=True)
    if plans:
        return plans[0]
    raise ValueError("系统未初始化套餐数据")


async def get_feature(
    db: AsyncSession,
    plan: Plan,
    feature_key: str,
) -> PlanFeature | None:
    result = await db.execute(
        select(PlanFeature).where(
            PlanFeature.plan_id == plan.id,
            PlanFeature.feature_key == feature_key,
        )
    )
    return result.scalar_one_or_none()


def _parse_limit(value: str) -> int | None:
    """限制值解析：unlimited → None（无限制）。"""
    if value is None:
        return None
    if str(value).strip().lower() in ("unlimited", "inf", "-1", "none"):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


async def check_feature(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    feature_key: str,
) -> bool:
    """能力开关校验：feature 存在且 enabled。"""
    plan = await get_effective_plan(db, user_id=user_id, organization_id=organization_id)
    feature = await get_feature(db, plan, feature_key)
    if not feature:
        return False
    return feature.enabled


async def check_limit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    feature_key: str,
    current_value: int = 0,
) -> tuple[bool, int | None]:
    """限制校验：返回 (是否允许, 限制值)。"""
    plan = await get_effective_plan(db, user_id=user_id, organization_id=organization_id)
    feature = await get_feature(db, plan, feature_key)
    limit = _parse_limit(feature.limit_value) if feature else 0
    if limit is None:
        return True, None
    return current_value < limit, limit


async def consume_quota(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    quota_type: str,
    amount: int,
    reference_id: str,
) -> dict:
    """权益侧额度扣减：优先组织共享额度，其次用户额度。"""
    if organization_id is not None:
        try:
            await quota_service.reserve_quota(
                db, "org", organization_id, quota_type, amount,
                reference_id=reference_id,
                note="entitlement.consume",
            )
            await quota_service.consume_quota(
                db, "org", organization_id, quota_type, amount,
                reference_id=reference_id, reserved=amount,
                note="entitlement.consume",
            )
            return {"source": "organization", "quota_type": quota_type, "amount": amount}
        except ValueError:
            pass
    if user_id is None:
        raise ValueError("无可用额度账户")
    await quota_service.reserve_quota(
        db, "user", user_id, quota_type, amount,
        reference_id=reference_id,
        note="entitlement.consume",
    )
    await quota_service.consume_quota(
        db, "user", user_id, quota_type, amount,
        reference_id=reference_id, reserved=amount,
        note="entitlement.consume",
    )
    return {"source": "user", "quota_type": quota_type, "amount": amount}


async def refund_quota(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    quota_type: str,
    amount: int,
    reference_id: str,
) -> dict:
    """权益侧额度回补（优先回补来源账户）。"""
    if organization_id is not None:
        await quota_service.refund_quota(
            db, "org", organization_id, quota_type, amount,
            reference_id=reference_id,
            note="entitlement.refund",
        )
        return {"source": "organization", "quota_type": quota_type, "amount": amount}
    if user_id is None:
        raise ValueError("无可用额度账户")
    await quota_service.refund_quota(
        db, "user", user_id, quota_type, amount,
        reference_id=reference_id,
        note="entitlement.refund",
    )
    return {"source": "user", "quota_type": quota_type, "amount": amount}


async def usage_state(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
) -> dict:
    """汇总有效套餐与额度状态（GET /api/billing/quota、/usage 用）。"""
    plan = await get_effective_plan(db, user_id=user_id, organization_id=organization_id)
    features = await plan_service.get_features(db, plan.id)
    feature_map = {}
    for feat in features:
        feature_map[feat.feature_key] = {
            "limit_value": feat.limit_value,
            "limit_type": feat.limit_type,
            "enabled": feat.enabled,
        }
    accounts = await quota_service.get_usage_summary(
        db, "user", user_id,
    ) if user_id else []
    return {
        "plan": {
            "id": str(plan.id),
            "name": plan.name,
            "plan_type": plan.plan_type,
            "price": float(plan.price),
            "currency": plan.currency,
        },
        "features": feature_map,
        "quota": accounts,
    }
