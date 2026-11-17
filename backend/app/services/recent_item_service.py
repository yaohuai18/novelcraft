# 最近访问服务（第九批规格实现）
# 记录最近访问并限制保留最近 20 条。
import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recent_item import RecentItem

RECENT_LIMIT = 20


async def record_recent_item(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
) -> RecentItem:
    """写入一条最近访问记录，并裁剪该用户+作品下超出上限的旧记录。"""
    item = RecentItem(
        user_id=user_id,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        accessed_at=datetime.utcnow(),
    )
    db.add(item)
    await db.flush()

    stmt = (
        select(RecentItem.id)
        .where(
            RecentItem.user_id == user_id,
            RecentItem.project_id == project_id,
        )
        .order_by(RecentItem.accessed_at.desc())
        .offset(RECENT_LIMIT)
    )
    extra_ids = (await db.execute(stmt)).scalars().all()
    if extra_ids:
        await db.execute(
            delete(RecentItem).where(RecentItem.id.in_(extra_ids))
        )
    return item


async def list_recent_items(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int = RECENT_LIMIT,
) -> list[RecentItem]:
    """按访问时间倒序返回最近访问记录。"""
    stmt = (
        select(RecentItem)
        .where(
            RecentItem.user_id == user_id,
            RecentItem.project_id == project_id,
        )
        .order_by(RecentItem.accessed_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())
