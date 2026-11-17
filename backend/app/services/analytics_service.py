# 统一事件采集服务（第二十二批「数据分析、推荐系统和智能运营」规格三/四/二十二）
# 职责：事件校验（名称白名单/必填字段/时间）、敏感字段清洗（禁止正文/密码/凭证/令牌）、
#       event_id 幂等去重、received_at 落库、离线补传、数据质量采样（重复/缺失/延迟）、死信。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import (
    ALLOWED_TEXT_FEATURE_KEYS,
    EVENT_NAMES,
    SENSITIVE_KEYS,
    AnalyticsEvent,
)
from app.models.data_quality import AnalyticsDeadLetter

MAX_BACKFILL_DAYS = 7      # 离线事件允许的最大回补窗口
MAX_ACCEPTABLE_DELAY_SEC = 300  # 正常延迟阈值（超过记入 event_time_delay 质量指标）
ALLOWED_EVENT_NAME_PREFIXES = ("recommend.", "goal.", "reading.", "ai.", "chapter.", "project.", "subscription.", "favorite.", "comment.", "share.", "search.", "user.", "payment.", "auth.", "audit.", "risk.", "template.", "activity.")


def _now() -> datetime:
    return datetime.utcnow()


def sanitize_properties(payload: dict) -> dict:
    """清洗 properties：剔除敏感键与敏感值；文本分析仅保留匿名统计特征。"""
    cleaned: dict = {}
    for k, v in payload.items():
        lk = k.lower()
        if any(s in lk for s in SENSITIVE_KEYS):
            continue
        if isinstance(v, str):
            low = v.lower()
            if any(s in low for s in SENSITIVE_KEYS):
                continue
            # 文本内容白名单：仅允许匿名统计特征键（词数/语言/标签等）
            if lk in ("text", "content", "body", "prompt", "output") and lk not in ALLOWED_TEXT_FEATURE_KEYS:
                continue
        # 标量/数组/对象值保留（JSON 序列化）
        cleaned[k] = v
    # 只保留允许的文本特征键（如 word_count/language/tags），其余自由键保留
    return cleaned


async def ingest_event(
    db: AsyncSession,
    *,
    event_name: str,
    event_id: str,
    user_id: uuid.UUID | None = None,
    anonymous_id: str | None = None,
    session_id: str | None = None,
    project_id: uuid.UUID | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    properties: dict | None = None,
    device_type: str | None = None,
    app_version: str | None = None,
    locale: str | None = None,
    region: str | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    """采集一条行为事件。

    返回 {"status": "recorded"|"duplicate"|"rejected", "reason": ...}。
    - recorded：正常入库
    - duplicate：event_id 幂等去重（规格二十六）
    - rejected：校验失败/敏感内容，写入死信队列
    """
    if not event_name or event_name not in EVENT_NAMES:
        if not any(event_name.startswith(p) for p in ALLOWED_EVENT_NAME_PREFIXES):
            await _dead_letter(db, event_id, event_name, properties, "event_name 不在白名单")
            return {"status": "rejected", "reason": "event_name_not_allowed"}
    if not event_id:
        await _dead_letter(db, event_id, event_name, properties, "event_id 缺失")
        return {"status": "rejected", "reason": "event_id_missing"}
    if len(event_id) > 64:
        return {"status": "rejected", "reason": "event_id_too_long"}

    occurred = occurred_at or _now()
    if occurred > _now() + timedelta(minutes=5):
        await _dead_letter(db, event_id, event_name, properties, "occurred_at 在未来时间")
        return {"status": "rejected", "reason": "occurred_at_in_future"}

    # 幂等去重
    exists = (await db.execute(
        select(AnalyticsEvent.id).where(AnalyticsEvent.event_id == event_id)
    )).scalar_one_or_none()
    if exists is not None:
        return {"status": "duplicate", "reason": "event_id_already_exists"}

    cleaned_props = sanitize_properties(properties or {})
    ev = AnalyticsEvent(
        event_id=event_id,
        event_name=event_name,
        user_id=user_id,
        anonymous_id=anonymous_id,
        session_id=session_id,
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
        properties=json.dumps(cleaned_props, ensure_ascii=False),
        device_type=device_type,
        app_version=app_version,
        locale=locale,
        region=region,
        occurred_at=occurred,
        received_at=_now(),
    )
    db.add(ev)
    await db.flush()
    return {"status": "recorded", "event_id": str(ev.id)}


async def _dead_letter(
    db: AsyncSession,
    event_id: str,
    event_name: str,
    properties: dict | None,
    error: str,
) -> None:
    dl = AnalyticsDeadLetter(
        event_id=event_id or "",
        event_name=event_name or "unknown",
        payload=json.dumps({"properties": properties}, ensure_ascii=False)[:8000],
        error=error,
        retry_count=0,
        status="dead",
    )
    db.add(dl)
    await db.flush()


async def replay_dead_letters(db: AsyncSession, *, limit: int = 50) -> dict:
    """死信重试：校验失败事件在补全必填项后可回放重入（规格二十二：失败重试/数据回放）。"""
    rows = (
        await db.execute(
            select(AnalyticsDeadLetter)
            .where(AnalyticsDeadLetter.status.in_(["dead", "retried"]))
            .order_by(AnalyticsDeadLetter.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    replayed = 0
    for dl in rows:
        try:
            payload = json.loads(dl.payload or "{}") or {}
            props = payload.get("properties") or {}
            result = await ingest_event(
                db,
                event_name=dl.event_name,
                event_id=dl.event_id,
                properties=props,
            )
            if result["status"] == "recorded":
                dl.status = "replayed"
                replayed += 1
            elif result["status"] == "duplicate":
                dl.status = "dropped"
            else:
                dl.retry_count += 1
                if dl.retry_count >= 5:
                    dl.status = "dropped"
                else:
                    dl.status = "retried"
        except Exception as exc:  # noqa: BLE001
            dl.retry_count += 1
            dl.error = str(exc)[:500]
            if dl.retry_count >= 5:
                dl.status = "dropped"
    await db.flush()
    return {"replayed": replayed, "processed": len(rows)}


async def sample_quality(db: AsyncSession) -> dict:
    """数据质量采样（规格二十二）：重复率/字段缺失率/时间延迟/类型错误。"""
    total = int((await db.execute(select(func.count(AnalyticsEvent.id)))).scalar_one() or 0)
    dup = int((await db.execute(
        select(func.count()).select_from(
            select(AnalyticsEvent.event_id).group_by(AnalyticsEvent.event_id)
            .having(func.count() > 1).subquery()
        )
    )).scalar_one() or 0)
    # 缺失率：properties 为空或 user_id/anonymous_id 均缺失
    missing_props = int((await db.execute(
        select(func.count(AnalyticsEvent.id)).where(
            (AnalyticsEvent.properties.is_(None)) | (AnalyticsEvent.properties == "{}")
        )
    )).scalar_one() or 0)
    missing_id = int((await db.execute(
        select(func.count(AnalyticsEvent.id)).where(
            AnalyticsEvent.user_id.is_(None),
            AnalyticsEvent.anonymous_id.is_(None),
        )
    )).scalar_one() or 0)
    # 延迟：occurred_at 与 received_at 差超过阈值
    delayed = int((await db.execute(
        select(func.count(AnalyticsEvent.id)).where(
            AnalyticsEvent.received_at - AnalyticsEvent.occurred_at > timedelta(seconds=MAX_ACCEPTABLE_DELAY_SEC)
        )
    )).scalar_one() or 0)
    return {
        "total_events": total,
        "duplicate_rows": dup,
        "duplicate_rate": round(dup / total, 6) if total else 0.0,
        "field_missing_rate": round((missing_props + missing_id) / (total * 2), 6) if total else 0.0,
        "delayed_events": delayed,
        "time_delay_rate": round(delayed / total, 6) if total else 0.0,
    }
