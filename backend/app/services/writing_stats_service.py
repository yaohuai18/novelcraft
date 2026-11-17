# 创作统计与写作目标服务（第十七批「统计分析与运营后台」规格二/三/四）
# 创作统计：总字数/章节数/分卷数/每日新增字数/连续创作天数/平均章节字数/
#          平均每日写作量/目标完成率/草稿数/已发布章节数/待处理评论数。
# 时间范围：今天 / 近 7 天 / 近 30 天 / 本年度 / 自定义。
# 写作目标：daily_words / weekly_words / project_words / chapter_count / publish_count，
#           自动计算提醒（今日未完成 / 本周 80% / 距截止 N 天）。
# 每日快照 WritingDailyStat 避免每次查询都扫描全部正文。
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import Comment
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.models.writing_daily_stat import WritingDailyStat
from app.models.writing_goal import GOAL_STATUSES, GOAL_TYPES, WritingGoal
from app.utils.text import count_words

GOAL_TYPE_DAILY = "daily_words"
GOAL_TYPE_WEEKLY = "weekly_words"
GOAL_TYPE_PROJECT = "project_words"
GOAL_TYPE_CHAPTER = "chapter_count"
GOAL_TYPE_PUBLISH = "publish_count"


class GoalError(ValueError):
    pass


# ---------- 时间范围解析 ----------

def resolve_period(
    period: str = "today",
    from_date: date | None = None,
    to_date: date | None = None,
    today: date | None = None,
) -> tuple[date, date]:
    """将 period（today/7d/30d/year/custom）解析为 [from, to] 日期区间。"""
    today = today or date.today()
    if period == "today":
        return today, today
    if period == "7d":
        return today - timedelta(days=6), today
    if period == "30d":
        return today - timedelta(days=29), today
    if period == "year":
        return date(today.year, 1, 1), today
    # custom：优先使用显式 from/to
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise GoalError("起始日期不能晚于结束日期")
        return from_date, to_date
    raise GoalError("自定义时间范围需要提供 from 与 to")


# ---------- 写作目标 ----------

async def create_goal(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    goal_type: str,
    target_value: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> WritingGoal:
    """创建写作目标。"""
    if goal_type not in GOAL_TYPES:
        raise GoalError(f"不支持的目标类型: {goal_type}")
    if target_value <= 0:
        raise GoalError("目标值必须大于 0")
    if end_date is not None and start_date is not None and end_date < start_date:
        raise GoalError("截止日期不能早于开始日期")

    goal = WritingGoal(
        user_id=user.id,
        project_id=project_id,
        goal_type=goal_type,
        target_value=target_value,
        current_value=0,
        start_date=start_date,
        end_date=end_date,
        status="active",
    )
    db.add(goal)
    await db.flush()
    return goal


async def update_goal(
    db: AsyncSession,
    goal: WritingGoal,
    *,
    target_value: int | None = None,
    current_value: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    status: str | None = None,
) -> WritingGoal:
    """更新目标（允许局部更新）。"""
    if target_value is not None:
        if target_value <= 0:
            raise GoalError("目标值必须大于 0")
        goal.target_value = target_value
    if current_value is not None:
        if current_value < 0:
            raise GoalError("当前值不能为负数")
        goal.current_value = current_value
    if start_date is not None:
        goal.start_date = start_date
    if end_date is not None:
        goal.end_date = end_date
    if status is not None:
        if status not in GOAL_STATUSES:
            raise GoalError(f"非法目标状态: {status}")
        goal.status = status
    goal.updated_at = datetime.utcnow()
    await db.flush()
    return goal


async def list_goals(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    goal_type: str | None = None,
    status: str | None = None,
) -> list[WritingGoal]:
    """列出项目写作目标。"""
    filters = [WritingGoal.project_id == project_id]
    if goal_type:
        filters.append(WritingGoal.goal_type == goal_type)
    if status:
        filters.append(WritingGoal.status == status)
    result = await db.execute(
        select(WritingGoal)
        .where(*filters)
        .order_by(WritingGoal.created_at.asc())
    )
    return list(result.scalars().all())


def goal_reminders(goal: WritingGoal, today: date | None = None) -> list[str]:
    """根据目标当前进度计算提醒文案。"""
    today = today or date.today()
    reminders: list[str] = []
    if goal.status != "active":
        return reminders

    ratio = goal.current_value / goal.target_value if goal.target_value else 0.0

    if goal.goal_type in (GOAL_TYPE_DAILY, GOAL_TYPE_WEEKLY):
        if ratio < 1.0:
            reminders.append("今日尚未完成目标")
        elif ratio >= 1.0:
            reminders.append("今日目标已完成")
        if 0.8 <= ratio < 1.0:
            reminders.append("本周已完成 80%")

    if goal.end_date is not None:
        remain = (goal.end_date - today).days
        if remain > 0:
            reminders.append(f"距离截止日期还有 {remain} 天")
        elif remain == 0:
            reminders.append("今天就是截止日期")
        else:
            reminders.append(f"已超过截止日期 {-remain} 天")

    if ratio >= 1.0:
        reminders.append("目标已完成")
    return reminders


# ---------- 每日快照 ----------

async def upsert_daily_stat(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    stat_date: date,
    word_count: int | None = None,
    new_word_count: int | None = None,
    edited_word_count: int | None = None,
    deleted_word_count: int | None = None,
    chapter_count: int | None = None,
    active_minutes: int | None = None,
) -> WritingDailyStat:
    """保存/累加某日写作快照（唯一键 user+project+date）。"""
    stat = (
        await db.execute(
            select(WritingDailyStat).where(
                WritingDailyStat.user_id == user.id,
                WritingDailyStat.project_id == project_id,
                WritingDailyStat.stat_date == stat_date,
            )
        )
    ).scalar_one_or_none()

    if stat is None:
        stat = WritingDailyStat(
            user_id=user.id,
            project_id=project_id,
            stat_date=stat_date,
            word_count=word_count or 0,
            new_word_count=new_word_count or 0,
            edited_word_count=edited_word_count or 0,
            deleted_word_count=deleted_word_count or 0,
            chapter_count=chapter_count or 0,
            active_minutes=active_minutes or 0,
        )
        db.add(stat)
    else:
        if word_count is not None:
            stat.word_count = word_count
        if new_word_count is not None:
            stat.new_word_count += new_word_count
        if edited_word_count is not None:
            stat.edited_word_count += edited_word_count
        if deleted_word_count is not None:
            stat.deleted_word_count += deleted_word_count
        if chapter_count is not None:
            stat.chapter_count = chapter_count
        if active_minutes is not None:
            stat.active_minutes += active_minutes
    await db.flush()
    return stat


async def delete_daily_stats(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> int:
    """删除个人统计数据（用户删除权）。"""
    filters = [WritingDailyStat.user_id == user_id]
    if project_id is not None:
        filters.append(WritingDailyStat.project_id == project_id)
    result = await db.execute(delete(WritingDailyStat).where(*filters))
    return result.rowcount or 0


# ---------- 创作统计 ----------

async def get_project_snapshot(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    to_date: date | None = None,
) -> dict:
    """聚合项目当前创作概况：字数/章节/分卷/草稿/已发布/待处理评论。"""
    to_date = to_date or date.today()

    # 总字数：优先最新快照 word_count；否则实时聚合章节正文
    latest = (
        await db.execute(
            select(WritingDailyStat)
            .where(
                WritingDailyStat.project_id == project_id,
                WritingDailyStat.stat_date <= to_date,
            )
            .order_by(WritingDailyStat.stat_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    chapters = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            )
        )
    ).scalars().all()
    volumes = (
        await db.execute(
            select(func.count(Outline.id)).where(
                Outline.project_id == project_id,
                Outline.outline_type == "volume",
            )
        )
    ).scalar_one()

    chapter_count = len(chapters)
    draft_count = sum(1 for c in chapters if c.status == "draft")
    published_count = sum(
        1 for c in chapters
        if c.visibility == "public" or c.status == "published"
    )

    # 实时章节字数（不含 Markdown 标记与空格，标点计入）
    live_words = sum(count_words(c.content or "") for c in chapters)

    pending_comments = (
        await db.execute(
            select(func.count(Comment.id)).where(
                Comment.project_id == project_id,
                Comment.status == "open",
                Comment.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    return {
        "total_words": latest.word_count if latest else live_words,
        "live_words": live_words,
        "chapter_count": chapter_count,
        "volume_count": volumes,
        "draft_count": draft_count,
        "published_chapter_count": published_count,
        "pending_comment_count": pending_comments,
        "snapshot_date": latest.stat_date.isoformat() if latest else None,
    }


async def get_daily_series(
    db: AsyncSession,
    project_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> list[WritingDailyStat]:
    """按日返回写作快照序列。"""
    result = await db.execute(
        select(WritingDailyStat)
        .where(
            WritingDailyStat.project_id == project_id,
            WritingDailyStat.stat_date >= from_date,
            WritingDailyStat.stat_date <= to_date,
        )
        .order_by(WritingDailyStat.stat_date.asc())
    )
    return list(result.scalars().all())


async def get_writing_stats(
    db: AsyncSession,
    project: Project,
    *,
    period: str = "today",
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """项目创作统计（规格二示例结构）。"""
    frm, to = resolve_period(period, from_date, to_date)

    snapshot = await get_project_snapshot(db, project.id, to_date=to)
    series = await get_daily_series(db, project.id, frm, to)

    new_words = sum(s.new_word_count for s in series)
    edited_words = sum(s.edited_word_count for s in series)
    deleted_words = sum(s.deleted_word_count for s in series)

    active_days = sum(
        1 for s in series
        if s.active_minutes > 0 or s.new_word_count > 0 or s.edited_word_count > 0
    )

    # 连续创作天数：从 to 往前连续有活跃记录的天数
    consecutive_days = 0
    active_dates = {
        s.stat_date for s in series
        if s.active_minutes > 0 or s.new_word_count > 0 or s.edited_word_count > 0
    }
    cursor = to
    while cursor >= frm:
        if cursor in active_dates:
            consecutive_days += 1
            cursor -= timedelta(days=1)
        else:
            break

    span_days = max((to - frm).days + 1, 1)
    average_daily_words = round(new_words / active_days) if active_days else 0
    average_chapter_words = (
        round(snapshot["total_words"] / snapshot["chapter_count"])
        if snapshot["chapter_count"]
        else 0
    )

    # 目标完成率：取最近活跃目标（project_words 优先）
    goals = await list_goals(db, project.id, status="active")
    goal_completion_rate = None
    active_goal = None
    if goals:
        active_goal = max(goals, key=lambda g: g.updated_at)
        goal_completion_rate = round(
            active_goal.current_value / active_goal.target_value, 4
        ) if active_goal.target_value else 0.0

    return {
        "project_id": str(project.id),
        "period": f"{frm.isoformat()}/{to.isoformat()}",
        "from": frm.isoformat(),
        "to": to.isoformat(),
        "total_words": snapshot["total_words"],
        "new_words": new_words,
        "edited_words": edited_words,
        "deleted_words": deleted_words,
        "chapter_count": snapshot["chapter_count"],
        "volume_count": snapshot["volume_count"],
        "draft_count": snapshot["draft_count"],
        "published_chapter_count": snapshot["published_chapter_count"],
        "pending_comment_count": snapshot["pending_comment_count"],
        "active_days": active_days,
        "consecutive_days": consecutive_days,
        "average_daily_words": average_daily_words,
        "average_chapter_words": average_chapter_words,
        "goal_completion_rate": goal_completion_rate,
        "goal": {
            "id": str(active_goal.id),
            "goal_type": active_goal.goal_type,
            "target_value": active_goal.target_value,
            "current_value": active_goal.current_value,
            "completion_rate": goal_completion_rate,
            "reminders": goal_reminders(active_goal),
        } if active_goal else None,
        "daily": [
            {
                "date": s.stat_date.isoformat(),
                "word_count": s.word_count,
                "new_word_count": s.new_word_count,
                "edited_word_count": s.edited_word_count,
                "deleted_word_count": s.deleted_word_count,
                "chapter_count": s.chapter_count,
                "active_minutes": s.active_minutes,
            }
            for s in series
        ],
        "span_days": span_days,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }
