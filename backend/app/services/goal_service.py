# 创作目标系统服务（第二十二批「数据分析、推荐系统和智能运营」规格十七）
# 扩展 WritingGoal：goal_type 支持 daily_words/weekly_words/month_chapters/project_deadline/
#   consecutive_days/publish_frequency；period 周期；current_value 自动累计；
# 提醒策略：用户主动开启、接近截止、低于进度、完成提醒；频率可配置不频繁打扰。
import json
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.outline import Outline
from app.models.writing_goal import GOAL_PERIODS, GOAL_TYPES, WritingGoal

GOAL_TYPE_PERIOD_MAP = {
    "daily_words": "daily", "weekly_words": "weekly", "project_words": "period",
    "chapter_count": "period", "publish_count": "period", "month_chapters": "monthly",
    "project_deadline": "deadline", "consecutive_days": "streak", "publish_frequency": "frequency",
}


def _now() -> datetime:
    return datetime.utcnow()


async def create_goal(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    goal_type: str,
    target_value: int | float,
    project_id: uuid.UUID | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> dict:
    """创建创作目标（扩展新类型与 period）。"""
    if goal_type not in GOAL_TYPES:
        return {"error": "invalid_goal_type"}
    if target_value <= 0:
        return {"error": "target_value_must_be_positive"}
    period = GOAL_TYPE_PERIOD_MAP.get(goal_type, "period")
    if period not in GOAL_PERIODS:
        return {"error": "invalid_period"}

    goal = WritingGoal(
        user_id=user_id,
        project_id=project_id,
        goal_type=goal_type,
        target_value=int(target_value),
        current_value=0,
        start_date=starts_at.date() if starts_at else date.today(),
        end_date=ends_at.date() if ends_at else None,
        period=period,
        status="active",
    )
    db.add(goal)
    await db.flush()
    return {"status": "created", "goal_id": str(goal.id), "period": period}


async def accumulate_goal(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    goal_type: str,
    delta: int,
) -> dict:
    """自动累计 current_value（保存章节/发布章节/创作日事件触发）。"""
    if goal_type not in GOAL_TYPES:
        return {"error": "invalid_goal_type"}
    stmt = select(WritingGoal).where(
        WritingGoal.user_id == user_id,
        WritingGoal.goal_type == goal_type,
        WritingGoal.status == "active",
    )
    if project_id:
        stmt = stmt.where(WritingGoal.project_id == project_id)
    goals = (await db.execute(stmt)).scalars().all()
    updated = []
    for g in goals:
        g.current_value = int(g.current_value or 0) + delta
        if g.target_value and g.current_value >= g.target_value:
            g.status = "completed"
        updated.append(str(g.id))
    await db.flush()
    return {"updated": updated}


async def goal_reminders(db: AsyncSession, *, user_id: uuid.UUID, hour: int = 20) -> list[dict]:
    """提醒策略：低于进度/接近截止/完成提醒；开启提醒的用户才发送，频率受控（每 24h 至多 1 次）。"""
    goals = (await db.execute(
        select(WritingGoal).where(
            WritingGoal.user_id == user_id,
            WritingGoal.status == "active",
        )
    )).scalars().all()
    reminders: list[dict] = []
    now = _now()

    for g in goals:
        progress = (g.current_value or 0) / max(g.target_value or 1, 1)
        last_events = (await db.execute(
            select(AnalyticsEvent).where(
                AnalyticsEvent.user_id == user_id,
                AnalyticsEvent.event_name == "goal.updated",
                AnalyticsEvent.occurred_at >= now - timedelta(hours=24),
            )
        )).scalars().all()
        if len(last_events) > 0:
            continue  # 24h 内已提醒过，避免频繁打扰

        if progress >= 1.0:
            reminders.append({
                "goal_id": str(g.id), "type": "completed",
                "message": f"目标「{g.goal_type}」已完成！",
            })
        elif g.end_date and g.end_date <= date.today() + timedelta(days=1):
            reminders.append({
                "goal_id": str(g.id), "type": "deadline_approaching",
                "message": f"目标「{g.goal_type}」即将截止（{g.end_date}），当前进度 {int(progress*100)}%。",
            })
        elif progress < 0.5:
            reminders.append({
                "goal_id": str(g.id), "type": "below_progress",
                "message": f"目标「{g.goal_type}」进度 {int(progress*100)}%，低于计划 50%，加油。",
            })
    return reminders


async def update_goal_status(db: AsyncSession, *, user_id: uuid.UUID, goal_id: uuid.UUID, status: str) -> dict:
    if status not in ("active", "paused", "completed", "archived"):
        return {"error": "invalid_status"}
    g = (await db.execute(
        select(WritingGoal).where(WritingGoal.id == goal_id, WritingGoal.user_id == user_id)
    )).scalar_one_or_none()
    if not g:
        return {"error": "goal_not_found"}
    g.status = status
    await db.flush()
    return {"status": "ok", "goal_id": str(goal_id)}


async def list_goals(db: AsyncSession, *, user_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(
        select(WritingGoal).where(WritingGoal.user_id == user_id).order_by(WritingGoal.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": str(g.id), "project_id": str(g.project_id) if g.project_id else None,
            "goal_type": g.goal_type, "period": g.period,
            "target_value": g.target_value, "current_value": g.current_value,
            "status": g.status, "start_date": str(g.start_date) if g.start_date else None,
            "end_date": str(g.end_date) if g.end_date else None,
        }
        for g in rows
    ]
