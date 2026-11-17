# 数据分层聚合服务（第二十二批「数据分析、推荐系统和智能运营」规格五/六/七）
# 从 AnalyticsEvent 统一事件聚合出：用户指标 / 创作指标 / 内容指标 / 商业化指标。
# 复用既有模型（Project/User/WritingDailyStat/ReadingEvent/AIUsageLog/Subscription/Order 等）。
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.billing_order import Order
from app.models.billing_subscription import Subscription
from app.models.project import Project
from app.models.user import User
from app.models.writing_daily_stat import WritingDailyStat


def _now() -> datetime:
    return datetime.utcnow()


def _day(dt: datetime) -> date:
    return dt.date()


async def _events_between(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    event_names: list[str] | None = None,
) -> list[AnalyticsEvent]:
    stmt = select(AnalyticsEvent).where(
        AnalyticsEvent.occurred_at >= start,
        AnalyticsEvent.occurred_at < end,
    )
    if event_names:
        stmt = stmt.where(AnalyticsEvent.event_name.in_(event_names))
    return list((await db.execute(stmt)).scalars().all())


async def user_metrics(db: AsyncSession, *, days: int = 30, region: str | None = None) -> dict:
    """用户指标：DAU/MAU/新用户/留存/回访/登录频率/设备分布/地区分布（规格六）。"""
    today = _now()
    d30 = today - timedelta(days=days)
    d7 = today - timedelta(days=7)

    dau_events = await _events_between(db, today - timedelta(days=1), today, ["user.login"])
    dau_users = {e.user_id for e in dau_events if e.user_id}
    mau_events = await _events_between(db, d30, today, ["user.login"])
    mau_users = {e.user_id for e in mau_events if e.user_id}
    # 登录频率：每个用户登录次数均值
    login_counts: dict[uuid.UUID, int] = {}
    for e in mau_events:
        if e.user_id:
            login_counts[e.user_id] = login_counts.get(e.user_id, 0) + 1

    # 新用户数
    new_users = int((await db.execute(
        select(func.count(User.id)).where(User.created_at >= d30)
    )).scalar_one() or 0)

    # 留存：d7 内活跃用户中，之前 7-14 天活跃过的比例（简单回访/留存）
    d14_28 = await _events_between(db, today - timedelta(days=28), d7, ["user.login"])
    old_active = {e.user_id for e in d14_28 if e.user_id}
    recent_events = await _events_between(db, d7, today, ["user.login"])
    recent_users = {e.user_id for e in recent_events if e.user_id}
    retained = len(old_active & recent_users)
    retention_rate = round(retained / max(len(old_active), 1), 4)
    revisit_users = {e.user_id for e in d14_28 if e.user_id and e.user_id in recent_users}
    revisit_rate = round(len(revisit_users) / max(len(old_active), 1), 4)

    device_dist: dict[str, int] = {}
    region_dist: dict[str, int] = {}
    for e in dau_events:
        device_dist[e.device_type or "unknown"] = device_dist.get(e.device_type or "unknown", 0) + 1
        if e.region:
            region_dist[e.region] = region_dist.get(e.region, 0) + 1

    return {
        "dau": len(dau_users),
        "mau": len(mau_users),
        "new_users": new_users,
        "retention_rate_7d": retention_rate,
        "revisit_rate": revisit_rate,
        "avg_login_frequency": round(sum(login_counts.values()) / max(len(login_counts), 1), 2),
        "device_distribution": device_dist,
        "region_distribution": region_dist,
        "window_days": days,
    }


async def creation_metrics(db: AsyncSession, *, user_id: uuid.UUID | None = None) -> dict:
    """创作指标：新建项目/新增章节/编辑字数/发布章节/连续创作/平均时长/完成率（规格六）。"""
    stats = {}
    base = select(AnalyticsEvent)
    if user_id is not None:
        base = base.where(AnalyticsEvent.user_id == user_id)

    stats["projects_created"] = int((await db.execute(
        base.where(AnalyticsEvent.event_name == "project.created")
    )).scalars().all().__len__())
    stats["chapters_created"] = int((await db.execute(
        base.where(AnalyticsEvent.event_name == "chapter.created")
    )).scalars().all().__len__())
    stats["chapters_published"] = int((await db.execute(
        base.where(AnalyticsEvent.event_name == "chapter.published")
    )).scalars().all().__len__())

    edited_events = (await db.execute(
        base.where(AnalyticsEvent.event_name == "chapter.saved")
    )).scalars().all()
    words = 0
    active_minutes = 0
    edit_days: set[date] = set()
    for e in edited_events:
        props = _props(e)
        words += int(props.get("word_count") or 0)
        active_minutes += int(props.get("duration_minutes") or 0)
        edit_days.add(_day(e.occurred_at))
    stats["edited_words"] = words
    stats["active_minutes"] = active_minutes
    stats["edit_days"] = len(edit_days)
    stats["avg_edit_duration_minutes"] = round(active_minutes / max(len(edited_events), 1), 2)

    # 连续创作天数（按 edit_days 递增计算当前连续天数）
    streak = 0
    if edit_days:
        d = _now().date()
        if d not in edit_days:
            d = d - timedelta(days=1)
        while d in edit_days:
            streak += 1
            d = d - timedelta(days=1)
    stats["consecutive_creation_days"] = streak

    # 章节完成率：已保存过的章节中发布的比例（以事件近似）
    saved_ids = {e.object_id for e in edited_events if e.object_id}
    published_ids = {
        e.object_id for e in (await db.execute(
            base.where(AnalyticsEvent.event_name == "chapter.published")
        )).scalars().all() if e.object_id
    }
    stats["chapter_completion_rate"] = round(
        len(saved_ids & published_ids) / max(len(saved_ids), 1), 4
    )

    # 项目完成率：有已发布章节的项目占比
    proj_created = {
        e.project_id for e in (await db.execute(
            base.where(AnalyticsEvent.event_name == "project.created")
        )).scalars().all() if e.project_id
    }
    proj_published = {
        e.project_id for e in (await db.execute(
            base.where(AnalyticsEvent.event_name == "chapter.published")
        )).scalars().all() if e.project_id
    }
    stats["project_completion_rate"] = round(
        len(proj_created & proj_published) / max(len(proj_created), 1), 4
    )
    return stats


async def content_metrics(db: AsyncSession, *, project_id: uuid.UUID | None = None, days: int = 30) -> dict:
    """内容指标：阅读量/完成率/收藏/评论/分享/订阅/跳出率/读者留存（规格六）。"""
    today = _now()
    start = today - timedelta(days=days)
    base = select(AnalyticsEvent).where(AnalyticsEvent.occurred_at >= start)
    if project_id is not None:
        base = base.where(AnalyticsEvent.project_id == project_id)

    counts = {
        "page_views": 0, "chapter_starts": 0, "chapter_completes": 0,
        "favorites": 0, "comments": 0, "shares": 0, "subscribes": 0,
    }
    for e in (await db.execute(base)).scalars().all():
        if e.event_name == "reading.page_view":
            counts["page_views"] += 1
        elif e.event_name == "reading.chapter_start":
            counts["chapter_starts"] += 1
        elif e.event_name == "reading.chapter_complete":
            counts["chapter_completes"] += 1
        elif e.event_name == "favorite.added":
            counts["favorites"] += 1
        elif e.event_name == "comment.created":
            counts["comments"] += 1
        elif e.event_name == "share.created":
            counts["shares"] += 1
        elif e.event_name == "subscription.upgraded":
            counts["subscribes"] += 1

    # 跳出率：章节启动但未完成的近似（1 - 完成/启动）
    bounce_rate = round(
        1 - counts["chapter_completes"] / max(counts["chapter_starts"], 1), 4
    )
    # 读者留存：有 page_view 的读者中再次访问（session 数 > 1）比例
    reader_sessions: dict[uuid.UUID, set] = {}
    for e in (await db.execute(base.where(AnalyticsEvent.event_name == "reading.page_view"))).scalars().all():
        key = e.user_id or e.anonymous_id
        if key:
            reader_sessions.setdefault(key, set()).add(e.session_id or "")
    repeat_readers = sum(1 for s in reader_sessions.values() if len(s) > 1)
    return {
        **counts,
        "reading_completion_rate": round(counts["chapter_completes"] / max(counts["chapter_starts"], 1), 4),
        "bounce_rate": bounce_rate,
        "reader_retention_rate": round(repeat_readers / max(len(reader_sessions), 1), 4),
        "unique_readers": len(reader_sessions),
    }


async def commercial_metrics(db: AsyncSession, *, days: int = 90) -> dict:
    """商业化指标：试用/付费转化/续费/退款/平均收入/套餐分布/AI额度消耗/LTV（规格六）。"""
    today = _now()
    start = today - timedelta(days=days)

    subs = (await db.execute(
        select(Subscription).where(Subscription.created_at >= start)
    )).scalars().all()
    trialing = [s for s in subs if s.status == "trialing"]
    active = [s for s in subs if s.status in ("active", "trialing")]
    paid_orders = (await db.execute(
        select(Order).where(Order.status == "paid", Order.created_at >= start)
    )).scalars().all()

    plan_dist: dict[str, int] = {}
    for s in subs:
        plan_dist[str(s.plan_id)] = plan_dist.get(str(s.plan_id), 0) + 1
    revenue = sum(o.amount or 0 for o in paid_orders)

    ai_events = (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.event_name == "ai.task_completed",
            AnalyticsEvent.occurred_at >= start,
        )
    )).scalars().all()
    ai_tokens = sum(int(_props(e).get("total_tokens") or 0) for e in ai_events)

    # LTV 近似：总收入 / 付费用户数
    paid_users = {o.user_id for o in paid_orders if o.user_id}
    return {
        "trial_count": len(trialing),
        "paid_count": len(active),
        "trial_conversion_rate": round(len(active) / max(len(trialing) + len(active), 1), 4),
        "paid_conversion_rate": round(len(paid_users) / max(len({s.user_id for s in subs}), 1), 4),
        "revenue": revenue,
        "average_revenue_per_user": round(revenue / max(len(paid_users), 1), 2),
        "plan_distribution": plan_dist,
        "ai_tokens_consumed": ai_tokens,
        "ltv": round(revenue / max(len(paid_users), 1), 2),
        "window_days": days,
    }


def _props(e: AnalyticsEvent) -> dict:
    import json
    try:
        return json.loads(e.properties or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


async def admin_dashboard(db: AsyncSession) -> dict:
    """管理员分析后台概览（规格二十四）：用户/创作/内容/阅读/AI/套餐转化/地区/性能/推荐/运营。"""
    users = await user_metrics(db, days=30)
    creation = await creation_metrics(db)
    content = await content_metrics(db, days=30)
    commercial = await commercial_metrics(db, days=90)
    recommend = await _recommend_overview(db)
    quality = await _quality_overview(db)
    return {
        "users": users,
        "creation": creation,
        "content": content,
        "commercial": commercial,
        "recommend": recommend,
        "quality": quality,
    }


async def _recommend_overview(db: AsyncSession) -> dict:
    from app.models.recommend_daily_stat import RecommendDailyStat
    rows = (await db.execute(
        select(RecommendDailyStat).order_by(RecommendDailyStat.stat_date.desc()).limit(14)
    )).scalars().all()
    return {
        "days": [
            {
                "date": str(r.stat_date),
                "exposure_count": r.exposure_count,
                "click_rate": r.click_rate,
                "completion_rate": r.completion_rate,
                "error_rate": r.error_rate,
            } for r in rows
        ],
    }


async def _quality_overview(db: AsyncSession) -> dict:
    from app.models.data_quality import DataQualityMetric
    rows = (await db.execute(
        select(DataQualityMetric).order_by(DataQualityMetric.created_at.desc()).limit(10)
    )).scalars().all()
    return {"recent_metrics": [{"type": r.metric_type, "value": r.value, "event_name": r.event_name} for r in rows]}
