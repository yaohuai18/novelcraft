# A/B 测试服务（第二十二批「数据分析、推荐系统和智能运营」规格二十）
# Experiment(name/target/variants/allocation/start_at/end_at/status)；
# 记录用户分组/实验版本/曝光/核心结果；hash 分桶保证分组稳定不漂移；
# 活动重复触发保护：同一用户同一实验只分配一次。
import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import (
    EXPERIMENT_STATUSES,
    Experiment,
    ExperimentAssignment,
    ExperimentEvent,
)

EXPERIMENT_TARGETS = [
    "homepage_layout", "recommendation_rank", "onboarding", "notification_copy",
    "plan_display", "pricing_page", "ai_entry",
]


def _now() -> datetime:
    return datetime.utcnow()


async def create_experiment(
    db: AsyncSession,
    *,
    name: str,
    target: str,
    variants: list[str],
    allocation: dict | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    status: str = "draft",
    created_by: uuid.UUID | None = None,
) -> dict:
    if target not in EXPERIMENT_TARGETS:
        return {"error": "invalid_target"}
    if len(variants) < 2:
        return {"error": "need_at_least_two_variants"}
    if status not in EXPERIMENT_STATUSES:
        return {"error": "invalid_status"}
    alloc = allocation or {v: 1.0 / len(variants) for v in variants}
    if abs(sum(alloc.values()) - 1.0) > 1e-6:
        return {"error": "allocation_sum_must_be_1"}

    exp = Experiment(
        name=name, target=target,
        variants=json.dumps(variants, ensure_ascii=False),
        allocation=json.dumps(alloc, ensure_ascii=False),
        start_at=start_at, end_at=end_at, status=status, created_by=created_by,
    )
    db.add(exp)
    await db.flush()
    return {"status": "created", "experiment_id": str(exp.id)}


async def start_experiment(db: AsyncSession, *, experiment_id: uuid.UUID) -> dict:
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if not exp:
        return {"error": "experiment_not_found"}
    exp.status = "running"
    exp.start_at = exp.start_at or _now()
    await db.flush()
    return {"status": "running", "experiment_id": str(experiment_id)}


async def assign_user(
    db: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict:
    """hash 分桶分配组：同一用户同一实验固定分组（稳定不漂移），重复调用返回原分组。"""
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if not exp:
        return {"error": "experiment_not_found"}
    if exp.status != "running":
        return {"error": "experiment_not_running"}

    existing = (await db.execute(
        select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.user_id == user_id,
        )
    )).scalar_one_or_none()
    if existing:
        return {"variant": existing.variant, "bucket": existing.bucket, "stable": True, "reassigned": False}

    variants = json.loads(exp.variants or "[]")
    alloc = json.loads(exp.allocation or "{}") or {}
    digest = hashlib.sha256(f"{experiment_id}:{user_id}".encode()).hexdigest()
    bucket = int(digest[:8], 16) % 10000

    # 按 allocation 分配区间
    acc = 0.0
    chosen = variants[0]
    for v in variants:
        acc += float(alloc.get(v, 0.0))
        if bucket / 10000.0 <= acc:
            chosen = v
            break

    db.add(ExperimentAssignment(
        experiment_id=experiment_id, user_id=user_id,
        variant=chosen, bucket=bucket,
    ))
    await db.flush()
    return {"variant": chosen, "bucket": bucket, "stable": True, "reassigned": True}


async def record_event(
    db: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    user_id: uuid.UUID,
    event_type: str,
    value: float | None = None,
) -> dict:
    if event_type not in ("exposure", "conversion"):
        return {"error": "invalid_event_type"}
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if not exp:
        return {"error": "experiment_not_found"}
    assignment = (await db.execute(
        select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not assignment:
        return {"error": "user_not_assigned"}

    # 曝光去重：同一用户同一实验同一类型只记一次
    dup = (await db.execute(
        select(ExperimentEvent.id).where(
            ExperimentEvent.experiment_id == experiment_id,
            ExperimentEvent.user_id == user_id,
            ExperimentEvent.event_type == event_type,
        )
    )).scalar_one_or_none()
    if dup:
        return {"status": "duplicate", "variant": assignment.variant}

    db.add(ExperimentEvent(
        experiment_id=experiment_id, user_id=user_id,
        variant=assignment.variant, event_type=event_type, value=value,
    ))
    await db.flush()
    return {"status": "recorded", "variant": assignment.variant}


async def experiment_report(db: AsyncSession, *, experiment_id: uuid.UUID) -> dict:
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if not exp:
        return {"error": "experiment_not_found"}
    variants = json.loads(exp.variants or "[]")

    report = {}
    for v in variants:
        assigned = int((await db.execute(
            select(func.count(ExperimentAssignment.id)).where(
                ExperimentAssignment.experiment_id == experiment_id,
                ExperimentAssignment.variant == v,
            )
        )).scalar_one() or 0)
        exposures = int((await db.execute(
            select(func.count(ExperimentEvent.id)).where(
                ExperimentEvent.experiment_id == experiment_id,
                ExperimentEvent.variant == v,
                ExperimentEvent.event_type == "exposure",
            )
        )).scalar_one() or 0)
        conversions = int((await db.execute(
            select(func.count(ExperimentEvent.id)).where(
                ExperimentEvent.experiment_id == experiment_id,
                ExperimentEvent.variant == v,
                ExperimentEvent.event_type == "conversion",
            )
        )).scalar_one() or 0)
        report[v] = {
            "assigned": assigned,
            "exposures": exposures,
            "conversions": conversions,
            "conversion_rate": round(conversions / max(exposures, 1), 4),
        }
    return {"experiment_id": str(experiment_id), "name": exp.name, "status": exp.status, "report": report}
