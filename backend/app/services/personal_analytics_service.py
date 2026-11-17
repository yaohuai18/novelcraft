# 个人创作分析与作品分析服务（第二十二批「数据分析、推荐系统和智能运营」规格八/九）
# 个人：每日/每周/每月字数、创作时长、完成章节、连续创作天数、目标完成率、常用创作时段、
#       趋势对比（本周vs上周、距目标差、未完成日期、平均每次字数）。
# 作品：总字数/章节数/更新频率/平均章节长度/阅读人数/完成率/收藏评论趋势/读者来源/发布后增长。
# 对外公开作品数据必须经过权限判断（由路由层配合权限校验处理）。
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.chapter_content import ChapterContent
from app.models.comment import Comment
from app.models.favorite import Favorite
from app.models.outline import Outline
from app.models.project import Project
from app.models.writing_goal import WritingGoal


def _now() -> datetime:
    return datetime.utcnow()


def _props(e: AnalyticsEvent) -> dict:
    try:
        return json.loads(e.properties or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


async def personal_creation_analysis(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """个人创作统计：周期字数/时长/章节/连续天数/目标完成率/时段/趋势对比。"""
    today = _now().date()
    events = (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.user_id == user_id,
            AnalyticsEvent.event_name.in_([
                "chapter.saved", "chapter.published", "chapter.edited",
            ]),
        )
    )).scalars().all()

    # 按日聚合
    daily_words: dict = defaultdict(int)
    daily_minutes: dict = defaultdict(int)
    daily_chapters: dict = defaultdict(int)
    hourly: dict = defaultdict(int)
    save_count = 0
    for e in events:
        p = _props(e)
        w = int(p.get("word_count") or 0)
        m = int(p.get("duration_minutes") or 0)
        d = e.occurred_at.date()
        daily_words[d] += w
        daily_minutes[d] += m
        hourly[e.occurred_at.hour] += 1
        save_count += 1
        if e.event_name == "chapter.saved":
            daily_chapters[d] += 1

    # 周/月聚合
    week_words = sum(v for d, v in daily_words.items() if _iso_week(d) == _iso_week(today))
    last_week_words = sum(
        v for d, v in daily_words.items() if _iso_week(d) == _iso_week(today - timedelta(days=7))
    )
    month_words = sum(v for d, v in daily_words.items() if d.year == today.year and d.month == today.month)

    # 连续创作天数
    active_days = set(daily_words.keys())
    streak = 0
    d = today
    if d not in active_days:
        d = d - timedelta(days=1)
    while d in active_days:
        streak += 1
        d = d - timedelta(days=1)

    # 目标完成率（取活跃目标中最近一个）
    goal = (await db.execute(
        select(WritingGoal).where(
            WritingGoal.user_id == user_id,
            WritingGoal.status == "active",
        ).order_by(WritingGoal.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    goal_progress = 0.0
    if goal:
        goal_progress = round(goal.current_value / max(goal.target_value or 1, 1), 4)

    # 未完成日期：本周内 active_days 缺失的日期
    missing_days = []
    wd = today - timedelta(days=today.weekday())
    for i in range(7):
        day = wd + timedelta(days=i)
        if day > today:
            break
        if day not in active_days:
            missing_days.append(day.isoformat())

    return {
        "daily_words": {d.isoformat(): v for d, v in sorted(daily_words.items())},
        "week_words": week_words,
        "last_week_words": last_week_words,
        "week_over_last_week_delta": week_words - last_week_words,
        "month_words": month_words,
        "active_minutes_total": sum(daily_minutes.values()),
        "avg_minutes_per_session": round(sum(daily_minutes.values()) / max(save_count, 1), 2),
        "completed_chapters": sum(daily_chapters.values()),
        "consecutive_creation_days": streak,
        "goal": {
            "goal_type": goal.goal_type if goal else None,
            "target_value": goal.target_value if goal else None,
            "current_value": goal.current_value if goal else None,
            "completion_rate": goal_progress,
            "remaining": (goal.target_value - goal.current_value) if goal else None,
        },
        "common_creation_hours": sorted(
            [{"hour": h, "count": c} for h, c in hourly.items()],
            key=lambda x: -x["count"],
        )[:5],
        "missing_dates_this_week": missing_days,
        "avg_words_per_session": round(sum(daily_words.values()) / max(save_count, 1), 2),
    }


def _iso_week(d) -> tuple:
    iso = d.isocalendar()
    return (iso[0], iso[1])


async def project_analysis(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """作品分析：总字数/章节数/更新频率/平均章节长度/阅读/完成率/收藏评论趋势/读者来源/发布后增长。"""
    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if not project:
        return {"error": "project_not_found"}

    # 章节与字数：Outline(chapter) + ChapterContent.word_count（权威正文来源）
    chapter_rows = (await db.execute(
        select(Outline.id, ChapterContent.word_count)
        .join(ChapterContent, ChapterContent.outline_id == Outline.id)
        .where(
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
            Outline.status == "published",
        )
    )).all()
    total_words = sum((row[1] or 0) for row in chapter_rows)
    chapter_count = len(chapter_rows)
    avg_chapter_words = round(total_words / max(chapter_count, 1), 2)

    # 更新频率：近 30 天发布章节数
    today = _now()
    recent_publish = (await db.execute(
        select(func.count(AnalyticsEvent.id)).where(
            AnalyticsEvent.project_id == project_id,
            AnalyticsEvent.event_name == "chapter.published",
            AnalyticsEvent.occurred_at >= today - timedelta(days=30),
        )
    )).scalar_one() or 0

    # 阅读人数与完成率：events 聚合
    events = (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.project_id == project_id,
            AnalyticsEvent.event_name.in_([
                "reading.page_view", "reading.chapter_start", "reading.chapter_complete",
                "favorite.added", "comment.created", "share.created",
            ]),
        )
    )).scalars().all()
    readers: set = set()
    starts = completes = favorites = shares = 0
    comment_ids: set = set()
    for e in events:
        if e.user_id:
            readers.add(e.user_id)
        elif e.anonymous_id:
            readers.add(e.anonymous_id)
        if e.event_name == "reading.chapter_start":
            starts += 1
        elif e.event_name == "reading.chapter_complete":
            completes += 1
        elif e.event_name == "favorite.added":
            favorites += 1
        elif e.event_name == "comment.created":
            comment_ids.add(e.object_id or e.id)
        elif e.event_name == "share.created":
            shares += 1

    comments = len(comment_ids)
    completion_rate = round(completes / max(starts, 1), 4)

    # 收藏/评论趋势（近 7 天）
    trend = {"favorites": favorites, "comments": comments, "shares": shares}

    # 读者来源：session 数近似访问次数
    reader_sessions: set = set()
    for e in (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.project_id == project_id,
            AnalyticsEvent.event_name == "reading.page_view",
        )
    )).scalars().all():
        if e.session_id:
            reader_sessions.add(e.session_id)

    # 发布后增长：发布后 7 天阅读事件数
    published_at = project.published_at
    growth = 0
    if published_at:
        growth = (await db.execute(
            select(func.count(AnalyticsEvent.id)).where(
                AnalyticsEvent.project_id == project_id,
                AnalyticsEvent.occurred_at >= published_at,
                AnalyticsEvent.occurred_at < published_at + timedelta(days=7),
            )
        )).scalar_one() or 0

    return {
        "project_id": str(project_id),
        "title": project.title,
        "total_words": total_words,
        "chapter_count": chapter_count,
        "avg_chapter_words": avg_chapter_words,
        "update_frequency_30d": recent_publish,
        "readers": len(readers),
        "reading_completion_rate": completion_rate,
        "favorites": favorites,
        "comments": comments,
        "shares": shares,
        "reader_visits": len(reader_sessions),
        "trend_7d": trend,
        "growth_after_publish_7d": growth,
        "published": bool(published_at),
    }
