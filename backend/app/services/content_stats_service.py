# 内容统计与阅读事件服务（第十七批「统计分析与运营后台」规格五/六/七/八）
# 阅读事件：page_view / chapter_start / chapter_complete / favorite / unfavorite / comment / share。
# 防虚增：同一 session 对同一目标同一类型事件去重（dedup_key 唯一约束兜底）。
# 防污染：机器人事件（metadata.bot=true）单独记录但排除出公开统计聚合。
# 未公开章节：仅项目已发布且章节公开时计入公开统计。
# 长期聚合：ContentDailyStat 按 项目+目标+日期 保存汇总；原始事件按保留期限管理。
import json
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import Comment
from app.models.content_daily_stat import ContentDailyStat
from app.models.favorite import Favorite
from app.models.outline import Outline
from app.models.project import Project
from app.models.reading_event import READING_EVENT_TYPES, ReadingEvent
from app.utils.text import count_words


class ReadingEventError(ValueError):
    pass


# ---------- 事件写入 ----------

def _parse_metadata(metadata: dict | None) -> dict:
    return dict(metadata or {})


def _is_bot(metadata: dict) -> bool:
    return bool(metadata.get("bot"))


async def record_event(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type: str,
    chapter_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    anonymous_id: str | None = None,
    session_id: str | None = None,
    occurred_at: datetime | None = None,
    metadata: dict | None = None,
) -> ReadingEvent | None:
    """记录阅读事件；重复/未公开/非法事件返回 None 或抛错。"""
    if event_type not in READING_EVENT_TYPES:
        raise ReadingEventError(f"非法事件类型: {event_type}")

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise ReadingEventError("作品不存在")

    meta = _parse_metadata(metadata)
    occurred = occurred_at or datetime.utcnow()
    stat_date = occurred.date()

    # 章节校验：chapter_id 给定且未公开 → 事件不入公开统计（但允许记录为内部事件？）
    # 规格：未公开章节不能被统计到公开数据中 → 直接拒绝记录公开统计类事件。
    chapter = None
    if chapter_id is not None:
        chapter = (
            await db.execute(select(Outline).where(Outline.id == chapter_id))
        ).scalar_one_or_none()
        if chapter is None or chapter.outline_type != "chapter":
            raise ReadingEventError("章节不存在")
        if event_type in ("chapter_start", "chapter_complete"):
            if chapter.visibility != "public" and not meta.get("internal"):
                raise ReadingEventError("未公开章节不计入公开统计")

    if project.publication_status != "published" and not meta.get("internal"):
        if event_type in ("chapter_start", "chapter_complete", "page_view"):
            raise ReadingEventError("未发布作品不计入公开统计")

    # 防重复刷新：同一 session 同一事件同一目标只计一次
    target_key = str(chapter_id or project_id)
    dedup_key = f"{session_id or ''}:{event_type}:{target_key}"
    if dedup_key:
        existing = (
            await db.execute(
                select(ReadingEvent).where(ReadingEvent.dedup_key == dedup_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None  # 重复事件：不虚增

    event = ReadingEvent(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        anonymous_id=anonymous_id,
        event_type=event_type,
        session_id=session_id,
        occurred_at=occurred,
        metadata_json=json.dumps(meta, ensure_ascii=False),
        dedup_key=dedup_key or None,
    )
    db.add(event)
    await db.flush()

    # 同步刷新当日聚合（仅公开事件，排除机器人）
    if not _is_bot(meta) and not meta.get("internal"):
        await _refresh_daily_aggregate(
            db, project_id, stat_date,
            target_type="chapter" if chapter_id else "project",
            target_id=str(chapter_id or project_id),
        )
    return event


# ---------- 每日聚合 ----------

async def _refresh_daily_aggregate(
    db: AsyncSession,
    project_id: uuid.UUID,
    stat_date: date,
    *,
    target_type: str,
    target_id: str,
) -> ContentDailyStat:
    """重算某日某目标的公开统计聚合并 upsert。"""
    filters = [
        ReadingEvent.project_id == project_id,
        ReadingEvent.occurred_at >= datetime.combine(stat_date, datetime.min.time()),
        ReadingEvent.occurred_at <= datetime.combine(stat_date, datetime.max.time()),
    ]
    if target_type == "chapter":
        filters.append(ReadingEvent.chapter_id == uuid.UUID(target_id))
        filters.append(ReadingEvent.event_type.in_(
            ["page_view", "chapter_start", "chapter_complete", "favorite", "unfavorite", "comment", "share"]
        ))
    else:
        filters.append(ReadingEvent.chapter_id.is_(None))
        filters.append(ReadingEvent.event_type.in_(
            ["page_view", "favorite", "unfavorite", "comment", "share"]
        ))

    # 排除机器人事件（metadata 不含 bot=true）
    events = (
        await db.execute(select(ReadingEvent).where(*filters))
    ).scalars().all()
    events = [e for e in events if not _is_bot(_parse_metadata(json.loads(e.metadata_json)))]

    view_count = sum(1 for e in events if e.event_type == "page_view")
    completion_count = sum(1 for e in events if e.event_type == "chapter_complete")
    favorite_count = sum(1 for e in events if e.event_type == "favorite")
    comment_count = sum(1 for e in events if e.event_type == "comment")

    readers: set[str] = set()
    read_seconds: list[float] = []
    for e in events:
        if e.user_id is not None:
            readers.add(f"u:{e.user_id}")
        if e.anonymous_id:
            readers.add(f"a:{e.anonymous_id}")
        m = _parse_metadata(json.loads(e.metadata_json))
        if e.event_type == "chapter_complete" and m.get("read_seconds"):
            read_seconds.append(float(m["read_seconds"]))

    avg_read_seconds = (
        round(sum(read_seconds) / len(read_seconds), 2) if read_seconds else 0.0
    )

    stat = (
        await db.execute(
            select(ContentDailyStat).where(
                ContentDailyStat.project_id == project_id,
                ContentDailyStat.target_type == target_type,
                ContentDailyStat.target_id == target_id,
                ContentDailyStat.stat_date == stat_date,
            )
        )
    ).scalar_one_or_none()

    if stat is None:
        stat = ContentDailyStat(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            stat_date=stat_date,
            view_count=view_count,
            unique_reader_count=len(readers),
            favorite_count=favorite_count,
            comment_count=comment_count,
            completion_count=completion_count,
            average_read_seconds=avg_read_seconds,
        )
        db.add(stat)
    else:
        stat.view_count = view_count
        stat.unique_reader_count = len(readers)
        stat.favorite_count = favorite_count
        stat.comment_count = comment_count
        stat.completion_count = completion_count
        stat.average_read_seconds = avg_read_seconds
    await db.flush()
    return stat


# ---------- 内容统计查询 ----------

async def get_chapter_stats(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    chapter_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """章节表现统计（规格五示例结构）。"""
    from_date = from_date or date.today() - timedelta(days=29)
    to_date = to_date or date.today()

    chapters = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            ).order_by(Outline.sort_order.asc())
        )
    ).scalars().all()
    if chapter_id is not None:
        chapters = [c for c in chapters if c.id == chapter_id]
        if not chapters:
            raise ReadingEventError("章节不存在")

    chapter_map = {str(c.id): c for c in chapters}

    stats = (
        await db.execute(
            select(ContentDailyStat).where(
                ContentDailyStat.project_id == project_id,
                ContentDailyStat.target_type == "chapter",
                ContentDailyStat.stat_date >= from_date,
                ContentDailyStat.stat_date <= to_date,
            )
        )
    ).scalars().all()

    agg: dict[str, dict] = defaultdict(lambda: {
        "view_count": 0,
        "unique_reader_count": 0,
        "favorite_count": 0,
        "comment_count": 0,
        "completion_count": 0,
        "read_seconds_sum": 0.0,
        "read_seconds_count": 0,
    })
    for s in stats:
        a = agg[s.target_id]
        a["view_count"] += s.view_count
        a["unique_reader_count"] = max(a["unique_reader_count"], s.unique_reader_count)
        a["favorite_count"] += s.favorite_count
        a["comment_count"] += s.comment_count
        a["completion_count"] += s.completion_count
        if s.average_read_seconds:
            a["read_seconds_sum"] += s.average_read_seconds * s.view_count
            a["read_seconds_count"] += s.view_count

    # 实时收藏数（全量）
    fav_counts = {
        str(f.target_id): c for f, c in (
            await db.execute(
                select(Favorite.target_id, func.count(Favorite.id)).where(
                    Favorite.project_id == project_id,
                    Favorite.target_type == "chapter",
                ).group_by(Favorite.target_id)
            )
        ).all()
    }
    # 实时评论数
    comment_counts = {
        str(cm.target_id): c for cm, c in (
            await db.execute(
                select(Comment.target_id, func.count(Comment.id)).where(
                    Comment.project_id == project_id,
                    Comment.deleted_at.is_(None),
                    Comment.comment_type == "public_comment",
                ).group_by(Comment.target_id)
            )
        ).all()
    }

    items = []
    for cid, ch in chapter_map.items():
        a = agg.get(cid, defaultdict(int))
        view_count = a.get("view_count", 0)
        avg_read = (
            round(a["read_seconds_sum"] / a["read_seconds_count"], 2)
            if a.get("read_seconds_count")
            else 0.0
        )
        items.append({
            "chapter_id": cid,
            "title": ch.title,
            "word_count": count_words(ch.content or ""),
            "view_count": view_count,
            "favorite_count": fav_counts.get(cid, 0),
            "comment_count": comment_counts.get(cid, 0),
            "completion_count": a.get("completion_count", 0),
            "completion_rate": round(
                a.get("completion_count", 0) / view_count, 4
            ) if view_count else 0.0,
            "average_read_seconds": avg_read,
            "updated_at": ch.updated_at.isoformat() + "Z",
            "published_at": ch.published_at.isoformat() + "Z" if ch.published_at else None,
            "visibility": ch.visibility,
            "status": ch.status,
        })

    return {
        "project_id": str(project_id),
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "chapter_stats": items,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }


async def get_project_content_stats(
    db: AsyncSession,
    project: Project,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """项目层面内容统计（阅读量/收藏/评论/完读/平均时长）。"""
    from_date = from_date or date.today() - timedelta(days=29)
    to_date = to_date or date.today()

    stats = (
        await db.execute(
            select(ContentDailyStat).where(
                ContentDailyStat.project_id == project.id,
                ContentDailyStat.target_type == "project",
                ContentDailyStat.stat_date >= from_date,
                ContentDailyStat.stat_date <= to_date,
            )
        )
    ).scalars().all()

    view_count = sum(s.view_count for s in stats)
    unique_reader_count = max((s.unique_reader_count for s in stats), default=0)
    favorite_count = sum(s.favorite_count for s in stats)
    comment_count = sum(s.comment_count for s in stats)
    completion_count = sum(s.completion_count for s in stats)

    daily = [
        {
            "date": s.stat_date.isoformat(),
            "view_count": s.view_count,
            "unique_reader_count": s.unique_reader_count,
            "favorite_count": s.favorite_count,
            "comment_count": s.comment_count,
            "completion_count": s.completion_count,
            "average_read_seconds": s.average_read_seconds,
        }
        for s in stats
    ]

    return {
        "project_id": str(project.id),
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "view_count": view_count,
        "unique_reader_count": unique_reader_count,
        "favorite_count": favorite_count,
        "comment_count": comment_count,
        "completion_count": completion_count,
        "completion_rate": round(completion_count / view_count, 4) if view_count else 0.0,
        "daily": daily,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }


# ---------- 阅读漏斗 ----------

async def get_reading_funnel(
    db: AsyncSession,
    project: Project,
    *,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> dict:
    """阅读漏斗：曝光→详情→首章→继续→完读→收藏→评论。

    事件语义约定（metadata.channel）：
      page_view + channel=exposure/list → 作品曝光
      page_view + channel=detail/project → 作品详情页访问
      chapter_start（第一章）→ 首章开始；chapter_start（后续章节）→ 继续阅读
      chapter_complete → 完读；favorite → 收藏；comment → 评论
    """
    from_dt = from_dt or (datetime.utcnow() - timedelta(days=29))
    to_dt = to_dt or datetime.utcnow()

    events = (
        await db.execute(
            select(ReadingEvent).where(
                ReadingEvent.project_id == project.id,
                ReadingEvent.occurred_at >= from_dt,
                ReadingEvent.occurred_at <= to_dt,
            )
        )
    ).scalars().all()
    events = [e for e in events if not _is_bot(_parse_metadata(json.loads(e.metadata_json)))]

    # 章节排序（首章 = sort_order 最小）
    chapters = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project.id,
                Outline.outline_type == "chapter",
            ).order_by(Outline.sort_order.asc())
        )
    ).scalars().all()
    first_chapter_id = str(chapters[0].id) if chapters else None
    chapter_order = {str(c.id): i for i, c in enumerate(chapters)}

    def channel_of(e: ReadingEvent) -> str:
        return _parse_metadata(json.loads(e.metadata_json)).get("channel", "")

    exposure_sessions: set[str] = set()
    detail_sessions: set[str] = set()
    first_start_sessions: set[str] = set()
    continue_sessions: set[str] = set()
    complete_count = 0
    favorite_count = 0
    comment_count = 0

    for e in events:
        sid = e.session_id or f"anon:{e.anonymous_id}"
        if e.event_type == "page_view":
            ch = channel_of(e)
            if ch in ("exposure", "list", "homepage"):
                exposure_sessions.add(sid)
            elif ch in ("detail", "project", "") and e.chapter_id is None:
                detail_sessions.add(sid)
        elif e.event_type == "chapter_start" and e.chapter_id is not None:
            cid = str(e.chapter_id)
            if cid == first_chapter_id:
                first_start_sessions.add(sid)
            elif cid in chapter_order:
                continue_sessions.add(sid)
        elif e.event_type == "chapter_complete":
            complete_count += 1
        elif e.event_type == "favorite":
            favorite_count += 1
        elif e.event_type == "comment":
            comment_count += 1

    exposure = len(exposure_sessions)
    detail = len(detail_sessions)
    first_start = len(first_start_sessions)
    continue_read = len(continue_sessions)
    complete = complete_count
    favorite = favorite_count
    comment = comment_count

    def rate(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    funnel = {
        "exposure": exposure,
        "detail": detail,
        "first_chapter_start": first_start,
        "continue_reading": continue_read,
        "complete": complete,
        "favorite": favorite,
        "comment": comment,
    }
    metrics = {
        "detail_click_rate": rate(detail, exposure),
        "first_chapter_start_rate": rate(first_start, detail),
        "continue_reading_rate": rate(continue_read, first_start),
        "completion_rate": rate(complete, first_start),
        "favorite_conversion_rate": rate(favorite, detail),
        "comment_conversion_rate": rate(comment, detail),
    }

    # 章节维度分析：流失最多 / 收藏率最高 / 评论最集中 / 平均阅读时长异常
    per_chapter: dict[str, dict] = {}
    for cid, i in chapter_order.items():
        per_chapter[cid] = {
            "title": chapters[i].title,
            "sort_order": i,
            "start_count": 0,
            "complete_count": 0,
            "favorite_count": 0,
            "comment_count": 0,
            "read_seconds": [],
        }
    for e in events:
        if e.chapter_id is None:
            continue
        cid = str(e.chapter_id)
        if cid not in per_chapter:
            continue
        m = _parse_metadata(json.loads(e.metadata_json))
        if e.event_type == "chapter_start":
            per_chapter[cid]["start_count"] += 1
        elif e.event_type == "chapter_complete":
            per_chapter[cid]["complete_count"] += 1
        elif e.event_type == "favorite":
            per_chapter[cid]["favorite_count"] += 1
        elif e.event_type == "comment":
            per_chapter[cid]["comment_count"] += 1
        if e.event_type == "chapter_complete" and m.get("read_seconds"):
            per_chapter[cid]["read_seconds"].append(float(m["read_seconds"]))

    for cid, info in per_chapter.items():
        rs = info.pop("read_seconds")
        info["average_read_seconds"] = round(sum(rs) / len(rs), 2) if rs else 0.0

    avg_all_seconds = sum(
        c["average_read_seconds"] for c in per_chapter.values()
        if c["average_read_seconds"]
    ) / max(len([c for c in per_chapter.values() if c["average_read_seconds"]]), 1)

    chapter_analysis = {
        "most_dropoff": max(
            per_chapter.values(), key=lambda c: c["start_count"] - c["complete_count"]
        ) if per_chapter else None,
        "highest_favorite_rate": max(
            per_chapter.values(),
            key=lambda c: rate(c["favorite_count"], c["start_count"]),
        ) if per_chapter else None,
        "most_commented": max(
            per_chapter.values(), key=lambda c: c["comment_count"]
        ) if per_chapter else None,
        "anomalous_read_seconds": [
            {**c, "title": c["title"]}
            for c in per_chapter.values()
            if c["average_read_seconds"] and abs(c["average_read_seconds"] - avg_all_seconds) > avg_all_seconds * 0.5
        ],
        "per_chapter": per_chapter,
    }

    return {
        "project_id": str(project.id),
        "from": from_dt.isoformat() + "Z",
        "to": to_dt.isoformat() + "Z",
        "funnel": funnel,
        "metrics": metrics,
        "chapter_analysis": chapter_analysis,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }
