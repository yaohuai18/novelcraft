# 跨区域同步与冲突处理服务（第二十一批「国际化、多语言和多区域部署」规格十七/十八）
# RegionEvent：event_id / source_region / occurred_at / entity_id / entity_version。
# 幂等处理重复事件（unique event_id → duplicate）；乱序事件（版本落后 → stale）。
# 乐观锁：客户端提交 base_version → 服务端检查当前版本 → 不一致返回 version_conflict。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.region_event import REGION_EVENT_STATUSES, RegionEvent

CONFLICT_ERROR_CODE = "version_conflict"


def version_conflict_error(
    *,
    current_version: int,
    submitted_version: int,
    entity_type: str = "chapter",
) -> dict:
    """构造乐观锁冲突返回（规格十八返回示例）。"""
    return {
        "error": {
            "code": CONFLICT_ERROR_CODE,
            "message": "章节已被其他设备修改",
            "current_version": current_version,
            "submitted_version": submitted_version,
        },
        "entity_type": entity_type,
    }


async def create_region_event(
    db: AsyncSession,
    *,
    event_id: str,
    source_region: str,
    entity_type: str,
    entity_id: str,
    entity_version: int,
    occurred_at: datetime | None = None,
    payload: dict | None = None,
) -> RegionEvent:
    existing = (
        await db.execute(
            select(RegionEvent).where(RegionEvent.event_id == event_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # 重复事件：幂等返回 duplicate，不重复入库
        if existing.status != "duplicate":
            existing.status = "duplicate"
            await db.flush()
        return existing
    evt = RegionEvent(
        event_id=event_id,
        source_region=source_region,
        occurred_at=occurred_at or datetime.utcnow(),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
        status="pending",
    )
    db.add(evt)
    await db.flush()
    return evt


async def deliver_event(db: AsyncSession, event: RegionEvent, *, delivered: bool = True) -> RegionEvent:
    """投递事件；重复投递标记为 duplicate（幂等）。"""
    if event.status == "duplicate":
        return event
    event.status = "delivered" if delivered else "failed"
    await db.flush()
    return event


async def apply_entity_event(
    db: AsyncSession,
    *,
    event_id: str,
    source_region: str,
    entity_type: str,
    entity_id: str,
    entity_version: int,
    occurred_at: datetime | None = None,
    payload: dict | None = None,
    latest_entity_version: int | None = None,
) -> dict:
    """应用跨区域事件到本地实体，处理重复/乱序：
    - 事件已存在（重复）→ duplicate
    - 事件版本 < 本地当前版本（乱序/旧事件）→ stale
    - 正常 → delivered
    """
    evt = await create_region_event(
        db,
        event_id=event_id,
        source_region=source_region,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        occurred_at=occurred_at,
        payload=payload,
    )
    if evt.status == "duplicate":
        return {"status": "duplicate", "event": evt}

    if latest_entity_version is not None and entity_version < latest_entity_version:
        evt.status = "stale"
        await db.flush()
        return {"status": "stale", "event": evt}

    evt.status = "applied"
    await db.flush()
    return {"status": "applied", "event": evt}


async def get_entity_events(
    db: AsyncSession,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    source_region: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[RegionEvent]:
    stmt = select(RegionEvent).order_by(RegionEvent.occurred_at.desc())
    if entity_type is not None:
        stmt = stmt.where(RegionEvent.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(RegionEvent.entity_id == entity_id)
    if source_region is not None:
        stmt = stmt.where(RegionEvent.source_region == source_region)
    if status is not None:
        stmt = stmt.where(RegionEvent.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


# ==================== 章节乐观锁 ====================

async def get_chapter_content(
    db: AsyncSession, chapter_id: uuid.UUID
) -> ChapterContent | None:
    return (
        await db.execute(
            select(ChapterContent).where(ChapterContent.outline_id == chapter_id)
        )
    ).scalar_one_or_none()


async def update_chapter_with_version(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    content: str,
    base_version: int,
    actor_id: uuid.UUID | None = None,
) -> dict:
    """章节更新（乐观锁）：客户端提交 base_version，服务端检查当前版本。
    版本一致 → 保存并 version+1；不一致 → 返回 version_conflict 错误结构。
    """
    chapter = await get_chapter_content(db, chapter_id)
    if chapter is None:
        raise ValueError("章节不存在")
    current_version = chapter.version
    if base_version != current_version:
        return version_conflict_error(
            current_version=current_version,
            submitted_version=base_version,
            entity_type="chapter",
        )
    chapter.content = content
    chapter.version += 1
    chapter.updated_at = datetime.utcnow()
    await db.flush()
    return {
        "ok": True,
        "chapter_id": str(chapter_id),
        "version": chapter.version,
        "word_count": len(content),
    }


async def update_chapter_with_version_returning_conflict(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    content: str,
    base_version: int,
) -> tuple[ChapterContent | None, dict | None]:
    """同 update_chapter_with_version，但返回 (章节对象, 冲突结构) 便于路由直接处理。"""
    chapter = await get_chapter_content(db, chapter_id)
    if chapter is None:
        return None, None
    if base_version != chapter.version:
        return chapter, version_conflict_error(
            current_version=chapter.version,
            submitted_version=base_version,
            entity_type="chapter",
        )
    chapter.content = content
    chapter.version += 1
    chapter.updated_at = datetime.utcnow()
    await db.flush()
    return chapter, None
