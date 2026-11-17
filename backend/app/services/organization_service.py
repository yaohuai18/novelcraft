# 团队与组织计费服务（第二十批「商业化、订阅与计费系统」规格十一）
# Organization：基础团队费用 + 超出席位费用（初期推荐模式）。
# 团队共享 AI 额度：组织维度 QuotaAccount，成员消耗优先走组织共享账户。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_org import Organization, OrganizationMember
from app.services import plan_service, quota_service
from app.services.entitlement_service import get_feature

# 超出席位单价（元/席位/月）
OVERAGE_SEAT_PRICE = 30


async def create_organization(
    db: AsyncSession,
    *,
    name: str,
    owner_id: uuid.UUID,
    billing_email: str | None = None,
    plan_type: str = "team",
) -> Organization:
    """创建组织并默认绑定团队套餐 + 初始共享额度。"""
    plan = await plan_service.get_plan_by_type(db, plan_type, "monthly")
    if not plan:
        plan = await plan_service.get_free_plan(db)
    org = Organization(
        name=name,
        owner_id=owner_id,
        plan_id=plan.id if plan else None,
        billing_email=billing_email,
        status="active",
    )
    db.add(org)
    await db.flush()
    # 创始成员
    db.add(
        OrganizationMember(
            organization_id=org.id,
            user_id=owner_id,
            role="owner",
            status="active",
            joined_at=datetime.utcnow(),
        )
    )
    # 团队共享 AI 额度（按月发放由订阅权益触发，这里先建账户）
    await quota_service.grant_quota(
        db, "org", org.id, "ai_tokens", 0,
        reference_type="organization",
        note="团队共享额度账户初始化",
    )
    await db.commit()
    await db.refresh(org)
    return org


async def get_organization(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> Organization | None:
    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    return result.scalar_one_or_none()


async def get_member(
    db: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMember | None:
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def add_member(
    db: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    *,
    role: str = "member",
    operator_id: uuid.UUID,
) -> OrganizationMember:
    """添加成员：校验团队套餐成员上限（超限 → 提示需购买额外席位）。"""
    allowed, limit = await _member_limit(db, org)
    member_count = await count_active_members(db, org.id)
    if limit is not None and member_count >= limit:
        raise ValueError(
            f"团队成员数已达上限 {limit}，请升级套餐或购买额外席位"
        )
    existing = await get_member(db, org.id, user_id)
    if existing:
        existing.status = "active"
        existing.role = role
        member = existing
    else:
        member = OrganizationMember(
            organization_id=org.id,
            user_id=user_id,
            role=role,
            status="active",
            joined_at=datetime.utcnow(),
        )
        db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def _member_limit(db: AsyncSession, org: Organization) -> tuple[bool, int | None]:
    plan = await plan_service.get_plan(db, org.plan_id) if org.plan_id else None
    if not plan:
        return False, 0
    feature = await get_feature(db, plan, "team.max_members")
    if not feature or not feature.enabled:
        return False, 0
    value = feature.limit_value.strip().lower()
    if value in ("unlimited", "none"):
        return True, None
    try:
        return True, int(value)
    except ValueError:
        return False, 0


async def count_active_members(db: AsyncSession, org_id: uuid.UUID) -> int:
    result = await db.execute(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.status == "active",
        )
    )
    return len(result.all())


async def remove_member(
    db: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    *,
    operator_id: uuid.UUID,
) -> bool:
    """移除成员：成员离开后数据归属组织（不删除数据，仅移除成员关系）。"""
    member = await get_member(db, org.id, user_id)
    if not member:
        return False
    if member.role == "owner":
        raise ValueError("不能移除组织所有者")
    member.status = "left"
    await db.commit()
    return True


async def seat_overage(db: AsyncSession, org: Organization) -> int:
    """超出席位数量（基础团队费用 + 超出席位费用）。"""
    allowed, limit = await _member_limit(db, org)
    if limit is None:
        return 0
    count = await count_active_members(db, org.id)
    return max(0, count - limit)


async def list_members(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[OrganizationMember]:
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
        ).order_by(OrganizationMember.joined_at.asc())
    )
    return list(result.scalars().all())


async def list_organizations(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[Organization]:
    stmt = select(Organization).order_by(Organization.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Organization.owner_id == user_id)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())
