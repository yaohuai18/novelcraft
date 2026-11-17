# 套餐服务（第二十批「商业化、订阅与计费系统」规格二/三）
# 提供内置套餐种子（免费/个人/专业/团队/企业）、套餐与能力项查询、管理端套餐修改。
# 金额与限制全部以服务端配置为准，不信任前端。
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_plan import Plan, PlanFeature
from app.models.billing_finance import LedgerEntry

# 内置套餐种子：价格、周期、能力项
PLAN_SEEDS = [
    {
        "name": "免费版",
        "plan_type": "free",
        "billing_interval": "monthly",
        "price": 0,
        "description": "基础功能，项目与 AI 额度有限",
        "features": {
            "project.max_count": ("3", "count"),
            "storage.max_bytes": ("104857600", "bytes"),        # 100MB
            "ai.monthly_tokens": ("100000", "tokens"),
            "ai.monthly_tasks": ("30", "tasks"),
            "team.max_members": ("1", "members"),
            "export.monthly_count": ("5", "count"),
            "stats.history_days": ("7", "days"),
            "api.requests_per_minute": ("10", "requests"),
        },
    },
    {
        "name": "个人会员",
        "plan_type": "personal",
        "billing_interval": "monthly",
        "price": 19,
        "description": "更多项目、更高 AI 额度、高级统计与更大存储",
        "features": {
            "project.max_count": ("20", "count"),
            "storage.max_bytes": ("1073741824", "bytes"),       # 1GB
            "ai.monthly_tokens": ("2000000", "tokens"),
            "ai.monthly_tasks": ("500", "tasks"),
            "team.max_members": ("1", "members"),
            "export.monthly_count": ("100", "count"),
            "stats.history_days": ("365", "days"),
            "api.requests_per_minute": ("60", "requests"),
        },
    },
    {
        "name": "专业版",
        "plan_type": "professional",
        "billing_interval": "monthly",
        "price": 59,
        "description": "面向个人创作者的高阶能力与优先队列",
        "features": {
            "project.max_count": ("100", "count"),
            "storage.max_bytes": ("10737418240", "bytes"),      # 10GB
            "ai.monthly_tokens": ("10000000", "tokens"),
            "ai.monthly_tasks": ("2000", "tasks"),
            "team.max_members": ("1", "members"),
            "export.monthly_count": ("500", "count"),
            "stats.history_days": ("1095", "days"),
            "api.requests_per_minute": ("120", "requests"),
        },
    },
    {
        "name": "团队版",
        "plan_type": "team",
        "billing_interval": "monthly",
        "price": 199,
        "description": "多成员协作、团队空间、共享 AI 额度",
        "features": {
            "project.max_count": ("500", "count"),
            "storage.max_bytes": ("53687091200", "bytes"),      # 50GB
            "ai.monthly_tokens": ("50000000", "tokens"),
            "ai.monthly_tasks": ("10000", "tasks"),
            "team.max_members": ("10", "members"),
            "export.monthly_count": ("2000", "count"),
            "stats.history_days": ("1095", "days"),
            "api.requests_per_minute": ("300", "requests"),
        },
    },
    {
        "name": "企业版",
        "plan_type": "enterprise",
        "billing_interval": "monthly",
        "price": 999,
        "description": "专属模型、企业 API 服务、高级导入导出与专属支持",
        "features": {
            "project.max_count": ("unlimited", "none"),
            "storage.max_bytes": ("unlimited", "none"),
            "ai.monthly_tokens": ("unlimited", "none"),
            "ai.monthly_tasks": ("unlimited", "none"),
            "team.max_members": ("unlimited", "none"),
            "export.monthly_count": ("unlimited", "none"),
            "stats.history_days": ("unlimited", "none"),
            "api.requests_per_minute": ("1000", "requests"),
        },
    },
]

DEFAULT_INTERVAL_FACTOR = {"monthly": 1, "yearly": 10, "one_time": 1, "trial": 0}


async def seed_plans(db: AsyncSession) -> None:
    """启动时若套餐表为空则写入内置套餐种子（幂等）。"""
    result = await db.execute(select(Plan.id).limit(1))
    if result.first():
        return
    for seed in PLAN_SEEDS:
        plan = Plan(
            name=seed["name"],
            plan_type=seed["plan_type"],
            billing_interval=seed["billing_interval"],
            price=seed["price"],
            currency="CNY",
            description=seed["description"],
            status="active",
        )
        db.add(plan)
        await db.flush()
        for key, (value, limit_type) in seed["features"].items():
            db.add(
                PlanFeature(
                    plan_id=plan.id,
                    feature_key=key,
                    limit_value=str(value),
                    limit_type=limit_type,
                    enabled=True,
                )
            )
    await db.commit()


async def list_plans(
    db: AsyncSession,
    only_active: bool = True,
) -> list[Plan]:
    """列出套餐（含能力项）。"""
    stmt = select(Plan).order_by(Plan.price.asc())
    if only_active:
        stmt = stmt.where(Plan.status == "active")
    result = await db.execute(stmt)
    plans = result.scalars().all()
    return plans


async def get_plan(
    db: AsyncSession,
    plan_id: uuid.UUID,
    only_active: bool = False,
) -> Plan | None:
    stmt = select(Plan).where(Plan.id == plan_id)
    if only_active:
        stmt = stmt.where(Plan.status == "active")
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_plan_by_type(
    db: AsyncSession,
    plan_type: str,
    interval: str = "monthly",
) -> Plan | None:
    result = await db.execute(
        select(Plan).where(
            Plan.plan_type == plan_type,
            Plan.billing_interval == interval,
            Plan.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def get_free_plan(db: AsyncSession) -> Plan | None:
    return await get_plan_by_type(db, "free", "monthly")


async def get_features(
    db: AsyncSession,
    plan_id: uuid.UUID,
) -> list[PlanFeature]:
    result = await db.execute(
        select(PlanFeature).where(PlanFeature.plan_id == plan_id)
    )
    return list(result.scalars().all())


async def update_plan(
    db: AsyncSession,
    plan_id: uuid.UUID,
    operator_id: uuid.UUID,
    *,
    name: str | None = None,
    price: float | None = None,
    status: str | None = None,
    description: str | None = None,
    reason: str = "管理员调整套餐",
) -> Plan | None:
    """管理端修改套餐（价格/名称/状态/描述）。版本自增并写账务审计。"""
    plan = await get_plan(db, plan_id)
    if not plan:
        return None
    if name is not None:
        plan.name = name
    if price is not None:
        plan.price = float(price)
    if status is not None:
        if status not in ("active", "inactive", "archived"):
            raise ValueError("无效的套餐状态")
        plan.status = status
    if description is not None:
        plan.description = description
    plan.version += 1
    plan.updated_at = datetime.utcnow()
    db.add(
        LedgerEntry(
            entry_type="adjustment",
            user_id=operator_id,
            amount=0,
            currency="CNY",
            direction="out",
            reason=f"{reason}: plan={plan.name}({plan.id}) v{plan.version}",
        )
    )
    await db.commit()
    await db.refresh(plan)
    return plan


def interval_factor(interval: str) -> int:
    """计费周期换算系数：年付按 10 个月计（年付折扣）。"""
    return DEFAULT_INTERVAL_FACTOR.get(interval, 1)
