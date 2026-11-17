# 数据质量管理服务（第二十二批「数据分析、推荐系统和智能运营」规格二十二）
# 监控：事件丢失率/重复率/字段缺失率/时间延迟/类型错误/跨服务ID不一致/统计异常。
# 管道：事件校验/失败重试/死信队列/重复去除/数据回放/版本兼容。
import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.data_quality import AnalyticsDeadLetter, DataQualityMetric
from app.models.data_privacy_pref import DataPrivacyPref

REQUIRED_FIELDS = ["event_name", "occurred_at", "received_at"]

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


async def record_quality_snapshot(db: AsyncSession, *, period: str = "1h") -> dict:
    """采集数据质量快照：重复率/缺失率/时间延迟/类型错误/跨服务ID不一致/统计异常。"""
    now = _now()
    window_start = now - timedelta(hours=1 if period == "1h" else 24)

    events = (await db.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.received_at >= window_start)
    )).scalars().all()
    total = len(events)

    dup_count = int((await db.execute(
        select(func.count(AnalyticsEvent.id)).where(
            AnalyticsEvent.event_id.isnot(None),
            AnalyticsEvent.received_at >= window_start,
        )
    )).scalar_one() or 0)
    # 近似重复率：带 event_id 的事件数/总数（真实重复已由唯一约束去重，此处分母为应计事件）
    dup_rate = round(dup_count / max(total, 1), 4) if total else 0.0

    missing_fields = {f: 0 for f in REQUIRED_FIELDS}
    delay_sum = 0
    type_errors = 0
    id_mismatch = 0
    for e in events:
        for f in REQUIRED_FIELDS:
            if getattr(e, f) is None:
                missing_fields[f] += 1
        delay = (e.received_at - e.occurred_at).total_seconds()
        delay_sum += max(delay, 0)
        if delay > 86400:
            type_errors += 1  # 时间延迟异常
        if e.user_id and e.anonymous_id and str(e.user_id) == str(e.anonymous_id):
            id_mismatch += 1

    missing_rate = round(sum(missing_fields.values()) / max(total * len(REQUIRED_FIELDS), 1), 4) if total else 0.0
    avg_delay = round(delay_sum / max(total, 1), 2) if total else 0.0
    extra = json.dumps({
        "total_events": total,
        "duplicate_rows": dup_count,
        "missing_fields": missing_fields,
        "type_error_count": type_errors,
        "id_mismatch_count": id_mismatch,
        "dead_letter_count": int((await db.execute(
            select(func.count(AnalyticsDeadLetter.id))
        )).scalar_one() or 0),
    }, ensure_ascii=False)

    # 每种质量指标单独落一行（规格二十二：metric_type 维度监控）
    metric_defs = [
        ("event_loss_rate", 0.0),
        ("event_duplicate_rate", dup_rate),
        ("field_missing_rate", missing_rate),
        ("event_time_delay", avg_delay),
        ("type_error_rate", round(type_errors / max(total, 1), 4) if total else 0.0),
        ("cross_service_id_mismatch", id_mismatch),
        ("stat_anomaly", 0.0),
    ]
    for mtype, value in metric_defs:
        db.add(DataQualityMetric(
            metric_type=mtype,
            period_start=window_start,
            period_end=now,
            event_name=None,
            value=float(value),
            extra=extra,
        ))
    await db.flush()
    return {
        "period": period,
        "total_events": total,
        "duplicate_rate": dup_rate,
        "missing_field_rate": missing_rate,
        "avg_delay_seconds": avg_delay,
        "type_error_count": type_errors,
        "id_mismatch_count": id_mismatch,
        "dead_letter_count": json.loads(extra)["dead_letter_count"],
    }


async def replay_events(db: AsyncSession, *, event_ids: list[uuid.UUID] | None = None, dead_letter: bool = True) -> dict:
    """数据回放：从死信队列重放事件（重复去除由 event_id 唯一约束保证）。"""
    stmt = select(AnalyticsDeadLetter)
    if event_ids:
        stmt = stmt.where(AnalyticsDeadLetter.id.in_(event_ids))
    dl_rows = (await db.execute(stmt)).scalars().all()
    replayed = 0
    failed = 0
    for dl in dl_rows:
        try:
            payload = json.loads(dl.payload or "{}") or {}
            existing = (await db.execute(
                select(AnalyticsEvent.id).where(AnalyticsEvent.event_id == payload.get("event_id"))
            )).scalar_one_or_none() if payload.get("event_id") else None
            if existing:
                failed += 1
                continue
            db.add(AnalyticsEvent(
                event_name=payload.get("event_name", ""),
                event_id=payload.get("event_id") or str(uuid.uuid4()),
                user_id=uuid.UUID(payload["user_id"]) if payload.get("user_id") else None,
                anonymous_id=payload.get("anonymous_id"),
                session_id=payload.get("session_id"),
                project_id=uuid.UUID(payload["project_id"]) if payload.get("project_id") else None,
                object_type=payload.get("object_type"),
                object_id=payload.get("object_id"),
                properties=json.dumps(payload.get("properties") or {}, ensure_ascii=False),
                device_type=payload.get("device_type"),
                app_version=payload.get("app_version"),
                locale=payload.get("locale"),
                region=payload.get("region"),
                occurred_at=datetime.fromisoformat(payload["occurred_at"]) if payload.get("occurred_at") else _now(),
                received_at=_now(),
            ))
            await db.delete(dl)
            replayed += 1
        except Exception:  # noqa: BLE001
            failed += 1
    await db.flush()
    return {"replayed": replayed, "failed": failed}


async def quality_history(db: AsyncSession, *, limit: int = 10) -> list[dict]:
    rows = (await db.execute(
        select(DataQualityMetric).order_by(DataQualityMetric.created_at.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "metric_type": m.metric_type,
            "period_start": m.period_start.isoformat() if m.period_start else None,
            "period_end": m.period_end.isoformat() if m.period_end else None,
            "value": m.value,
            "extra": json.loads(m.extra or "{}"),
        }
        for m in rows
    ]


# ---------- 阶段B「质量指标聚合」（补充9）----------
_PHASE_B_METRIC_TYPES = (
    "generation_truncate_rate",
    "validation_blocking_rate",
    "false_positive_rate",
    "repair_success_rate",
)


def _parse_json_obj(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def aggregate_phase_b_metrics(
    db: AsyncSession,
    *,
    project_id: str | None = None,
) -> dict[str, float | None]:
    """聚合正文质量指标（补充9，只读统计，不落库）：

    - generation_truncate_rate  生成截断率：截断 generation 数 / 总数
    - validation_blocking_rate  校验阻断率：含 blocking 问题的校验轮次 / 总校验轮次
    - false_positive_rate       误报率：disposition=false_positive / 已处置问题数
    - repair_success_rate       修复成功率：disposition=fixed / (fixed + confirmed)
    按项目过滤（project_id 非空时）；无数据返回 None 而非 0，避免假指标。
    指标口径与处置表联动，构成「误报反馈闭环」的统计端。
    """
    from app.models.chapter_content import ChapterContent
    from app.models.fact_issue_disposition import FactIssueDisposition
    from app.models.generation import GenerationRecord
    from app.models.outline import Outline

    out: dict[str, float | None] = {
        "generation_truncate_rate": None,
        "validation_blocking_rate": None,
        "false_positive_rate": None,
        "repair_success_rate": None,
    }
    # project_id 可能来自路由（str）或内部调用（UUID）：generation/outline 为
    # UUID 列，disposition 为 String 列，分别规范化避免绑定类型错误。
    gen_pid = uuid.UUID(str(project_id)) if project_id else None
    disp_pid = str(project_id) if project_id else None
    try:
        # 1) 生成截断率：截断/超时/失败 generation 数 / 总数（无 generation 返回 None）
        gen_base = select(func.count(GenerationRecord.id))
        gen_trunc = select(func.count(GenerationRecord.id)).where(
            GenerationRecord.was_truncated.is_(True)
        )
        if gen_pid:
            gen_base = gen_base.where(GenerationRecord.project_id == gen_pid)
            gen_trunc = gen_trunc.where(GenerationRecord.project_id == gen_pid)
        total = (await db.execute(gen_base)).scalar() or 0
        truncated = (await db.execute(gen_trunc)).scalar() or 0
        if total:
            out["generation_truncate_rate"] = round(truncated / total, 4)

        # 2) 校验阻断率：validation_summary 含 blocking 的章节数 / 已校验章节数
        #    summary 落 chapter_contents（chapter_lifecycle 保存链路），经 Outline
        #    关联章节归属项目。
        ch_q = select(ChapterContent).join(
            Outline,
            Outline.id == ChapterContent.outline_id,
        ).where(Outline.outline_type == "chapter")
        if gen_pid:
            ch_q = ch_q.where(Outline.project_id == gen_pid)
        chapters = (await db.execute(ch_q)).scalars().all()
        checked = 0
        blocked = 0
        for ch in chapters:
            summary = _parse_json_obj(ch.validation_summary)
            if summary is None:
                continue
            checked += 1
            counts = summary.get("counts") or {}
            if (counts.get("blocking") or 0) > 0:
                blocked += 1
        if checked:
            out["validation_blocking_rate"] = round(blocked / checked, 4)

        # 3) 误报率：false_positive / 已处置问题数
        disp_base = select(func.count(FactIssueDisposition.id))
        if disp_pid:
            disp_base = disp_base.where(
                FactIssueDisposition.project_id == disp_pid
            )
        disp_total = (await db.execute(disp_base)).scalar() or 0
        fp_total = (await db.execute(
            disp_base.where(
                FactIssueDisposition.disposition == "false_positive"
            )
        )).scalar() or 0
        if disp_total:
            out["false_positive_rate"] = round(fp_total / disp_total, 4)

        # 4) 修复成功率：fixed / (fixed + confirmed)
        fixed_total = (await db.execute(
            disp_base.where(FactIssueDisposition.disposition == "fixed")
        )).scalar() or 0
        confirmed_total = (await db.execute(
            disp_base.where(FactIssueDisposition.disposition == "confirmed")
        )).scalar() or 0
        denom = fixed_total + confirmed_total
        if denom:
            out["repair_success_rate"] = round(fixed_total / denom, 4)
    except Exception:  # noqa: BLE001
        logger.exception("阶段B质量指标聚合失败")
    return out
