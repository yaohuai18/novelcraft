# 用户阶段与转化分析服务（第二十二批「数据分析、推荐系统和智能运营」规格七）
# 阶段：注册→首次创建项目→首次完成章节→首次发布→稳定/高频创作→付费→团队→沉默→流失。
# 计算各阶段人数、转化率（本阶段/上一阶段）与流失率。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.billing_subscription import Subscription
from app.models.billing_org import OrganizationMember
from app.models.project import Project
from app.models.user import User

# 阶段顺序（规格七）
FUNNEL_STAGES = [
    "registered", "created_project", "completed_first_chapter",
    "published_first_chapter", "stable_creation", "frequent_creation",
    "paid", "team", "silent", "churned",
]


def _now() -> datetime:
    return datetime.utcnow()


async def user_funnel(db: AsyncSession, *, days: int = 90) -> dict:
    """全站用户阶段漏斗（注册→首次创建→首次完成→首次发布→稳定/高频→付费→团队→沉默/流失）。"""
    today = _now()
    start = today - timedelta(days=days)

    users = (await db.execute(
        select(User).where(User.created_at >= start)
    )).scalars().all()
    user_ids = {u.id for u in users}

    # 首次创建项目
    first_created: dict[uuid.UUID, datetime] = {}
    # 首次完成章节（chapter.complete 事件）
    first_completed: dict[uuid.UUID, datetime] = {}
    # 首次发布
    first_published: dict[uuid.UUID, datetime] = {}
    for e in (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.occurred_at >= start,
            AnalyticsEvent.event_name.in_([
                "project.created", "reading.chapter_complete",
                "chapter.published",
            ]),
        )
    )).scalars().all():
        if not e.user_id:
            continue
        if e.event_name == "project.created" and e.user_id not in first_created:
            first_created[e.user_id] = e.occurred_at
        elif e.event_name == "reading.chapter_complete" and e.user_id not in first_completed:
            first_completed[e.user_id] = e.occurred_at
        elif e.event_name == "chapter.published" and e.user_id not in first_published:
            first_published[e.user_id] = e.occurred_at

    # 稳定创作：>=3 个创作日；高频创作：>=7 个创作日
    creation_days: dict[uuid.UUID, set] = {}
    for e in (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.occurred_at >= start,
            AnalyticsEvent.event_name.in_(["chapter.saved", "chapter.published"]),
        )
    )).scalars().all():
        if e.user_id:
            creation_days.setdefault(e.user_id, set()).add(e.occurred_at.date())

    # 付费用户
    paid_user_ids = set((await db.execute(
        select(Subscription.user_id).where(Subscription.status.in_(["active", "trialing"]))
    )).scalars().all())

    # 团队用户
    team_user_ids = set((await db.execute(
        select(OrganizationMember.user_id)
    )).scalars().all())

    # 沉默：30 天无活动；流失：60 天无登录
    last_activity: dict[uuid.UUID, datetime] = {}
    for e in (await db.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.occurred_at >= start)
    )).scalars().all():
        if e.user_id and (e.user_id not in last_activity or e.occurred_at > last_activity[e.user_id]):
            last_activity[e.user_id] = e.occurred_at

    counts = {"registered": len(user_ids)}
    counts["created_project"] = len(first_created)
    counts["completed_first_chapter"] = len(first_completed)
    counts["published_first_chapter"] = len(first_published)
    counts["stable_creation"] = sum(1 for d in creation_days.values() if len(d) >= 3)
    counts["frequent_creation"] = sum(1 for d in creation_days.values() if len(d) >= 7)
    counts["paid"] = len(user_ids & paid_user_ids)
    counts["team"] = len(user_ids & team_user_ids)
    counts["silent"] = sum(
        1 for uid, last in last_activity.items()
        if uid in user_ids and last < today - timedelta(days=30)
    )
    counts["churned"] = sum(
        1 for uid, last in last_activity.items()
        if uid in user_ids and last < today - timedelta(days=60)
    )

    # 转化率 = 本阶段人数/上一阶段人数；流失率 = 1 - 转化率
    stages = []
    prev = counts[FUNNEL_STAGES[0]]
    for i, stage in enumerate(FUNNEL_STAGES):
        cur = counts[stage]
        if i == 0:
            rate, loss = 1.0, 0.0
        else:
            rate = round(cur / max(prev, 1), 4)
            loss = round(1 - rate, 4)
        stages.append({
            "stage": stage,
            "count": cur,
            "conversion_rate": rate,
            "loss_rate": loss,
        })
        prev = cur
    return {"stages": stages, "counts": counts, "window_days": days}


async def user_stage(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """单个用户当前阶段定位。"""
    ev = (await db.execute(
        select(AnalyticsEvent.event_name).where(
            AnalyticsEvent.user_id == user_id,
            AnalyticsEvent.event_name.in_([
                "project.created", "chapter.saved", "chapter.published",
                "subscription.upgraded",
            ]),
        )
    )).scalars().all()
    has_project = any("project.created" == n for n in ev)
    has_edit = any("chapter.saved" == n for n in ev)
    has_publish = any("chapter.published" == n for n in ev)
    has_paid = any("subscription.upgraded" == n for n in ev)

    member = (await db.execute(
        select(OrganizationMember.id).where(OrganizationMember.user_id == user_id)
    )).scalar_one_or_none()

    if has_paid:
        stage = "paid"
    elif member is not None:
        stage = "team"
    elif has_publish:
        stage = "published_first_chapter"
    elif has_edit:
        stage = "completed_first_chapter"
    elif has_project:
        stage = "created_project"
    else:
        stage = "registered"
    return {"user_id": str(user_id), "stage": stage}
