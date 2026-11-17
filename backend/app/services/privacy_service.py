# 隐私合规服务（第十七批「统计分析与运营后台」规格二十）
# 数据最小化 / 脱敏展示（utils.text 提供 mask_* 函数）/ 保留期限管理 / 用户删除权。
# 删除账号处理：清除个人统计数据、匿名化阅读事件（user_id 置 NULL 保留匿名事件）、
#               清除 AI 使用记录；公开作品署名/评论内容/协作活动按业务规则标记。
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage_daily_stat import AIFailureStat, AIUsageDailyStat
from app.models.comment import Comment
from app.models.content_daily_stat import ContentDailyStat
from app.models.reading_event import ReadingEvent
from app.models.writing_daily_stat import WritingDailyStat
from app.models.writing_goal import WritingGoal

# 阅读事件原始数据保留期限（天）
READING_EVENT_RETENTION_DAYS = 180


async def purge_expired_reading_events(
    db: AsyncSession,
    *,
    retention_days: int = READING_EVENT_RETENTION_DAYS,
    before: datetime | None = None,
) -> int:
    """清理超过保留期限的阅读事件明细（保留长期聚合统计）。"""
    cutoff = before or (datetime.utcnow() - timedelta(days=retention_days))
    result = await db.execute(
        delete(ReadingEvent).where(ReadingEvent.occurred_at < cutoff)
    )
    await db.flush()
    return result.rowcount or 0


async def anonymize_user_data(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    keep_comments: bool = False,
) -> dict:
    """用户删除权：按规格二十处理个人数据。

    处理清单：
    - 写作统计/写作目标/AI 使用统计/AI 失败统计：删除（个人数据）
    - 阅读事件：user_id 置 NULL，保留 anonymous_id 匿名化事件（不删除，用于匿名聚合）
    - 评论内容：默认软删除（deleted_at 置位）；keep_comments=True 时保留
    - ContentDailyStat 为聚合数据：保留（不含个人身份）
    """
    stats = {"deleted_writing_stats": 0, "deleted_goals": 0, "deleted_ai_stats": 0,
             "anonymized_reading_events": 0, "deleted_comments": 0}

    r1 = await db.execute(
        delete(WritingDailyStat).where(WritingDailyStat.user_id == user_id)
    )
    stats["deleted_writing_stats"] = r1.rowcount or 0

    r2 = await db.execute(delete(WritingGoal).where(WritingGoal.user_id == user_id))
    stats["deleted_goals"] = r2.rowcount or 0

    r3 = await db.execute(
        delete(AIUsageDailyStat).where(AIUsageDailyStat.user_id == user_id)
    )
    stats["deleted_ai_stats"] = r3.rowcount or 0

    # AIFailureStat 无 user_id 字段，按项目维度归属；此处不做级联删除（保留系统级失败监控）

    events = (
        await db.execute(
            select(ReadingEvent).where(ReadingEvent.user_id == user_id)
        )
    ).scalars().all()
    for ev in events:
        ev.user_id = None
    stats["anonymized_reading_events"] = len(events)

    if not keep_comments:
        comments = (
            await db.execute(select(Comment).where(Comment.user_id == user_id))
        ).scalars().all()
        for c in comments:
            c.deleted_at = datetime.utcnow()
        stats["deleted_comments"] = len(comments)

    await db.flush()
    return stats


async def get_retention_summary(db: AsyncSession, *, user_id: uuid.UUID) -> dict:
    """返回用户数据保留概况（数据最小化/保留期限）。"""
    reading_events = (
        await db.execute(
            select(ReadingEvent).where(ReadingEvent.user_id == user_id)
        )
    ).scalars().all()
    oldest = min((e.occurred_at for e in reading_events), default=None)
    return {
        "reading_events": len(reading_events),
        "oldest_event_at": oldest.isoformat() + "Z" if oldest else None,
        "retention_days": READING_EVENT_RETENTION_DAYS,
        "aggregated_stats_kept": True,  # ContentDailyStat 聚合数据保留
        "note": "原始阅读事件超过保留期限后由 purge_expired_reading_events 清理；聚合统计不含个人身份。",
    }
