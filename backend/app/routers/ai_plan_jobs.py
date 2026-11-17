# 「整本规划」后台作业路由：提交 → 轮询 → 取结果（prefix=/api/ai）
#
# 为什么单独做一套而不是复用 /plan-novel：
#   /plan-novel 是同步接口，一次规划 240~322 秒、两万多 token，浏览器 240 秒先断开，
#   结果被丢弃但 token 已经花掉。这里把同一次模型调用放进后台，接口本身毫秒级返回。
#
# 边界：
#   - 旧的 POST /plan-novel 原样保留，前端可随时回退（app.routers.ai_novel_generation）。
#   - 不写 ai_tasks、不接任务中心状态机；只写新增的 novel_plan_jobs 表。
#   - 提交时就把 real/mock 解析定：未配置密钥要在提交那一刻 503，而不是让人等五分钟。
import uuid

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.routers.ai_novel_generation import (
    _ai_error_response,
    _build_ai_override,
    _enforce_rate_limit,
    _resolve_real,
)
from app.schemas.ai_novel import PlanNovelRequest
from app.core.observability import current_request_id
from app.services import audit_service
from app.services import novel_plan_job_service as jobs
from app.services import novel_plan_job_state as st

router = APIRouter(prefix="/api/ai", tags=["AI 整本规划任务"])


@router.post("/plan-novel/jobs", status_code=202)
async def submit_plan_job(
    data: PlanNovelRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交一次整本规划，立刻返回 job_id（不在这里等模型）。

    真正的执行在 `novel_plan_job_service.run_job` 里，通过 asyncio 后台任务跑。
    前端拿到 job_id 后每 3 秒查一次 `/plan-novel/jobs/{job_id}`。
    """
    rate_error = await _enforce_rate_limit(current_user)
    if rate_error:
        return rate_error

    try:
        real = _resolve_real(data.requested_mode)
    except RuntimeError as error:
        raw = str(error)
        code, _, message = raw.partition(": ")
        return _ai_error_response(code, message or raw, status=503)

    payload = data.model_dump(mode="json")
    # 只保留真正会影响生成的字段，避免把默认 None 一起存进快照
    payload["_override"] = _build_ai_override(data)

    key = (idempotency_key or "").strip() or None
    if key is not None and len(key) > 128:
        return _ai_error_response(
            "invalid_idempotency_key", "Idempotency-Key 过长（上限 128 字符）", status=400
        )

    job, created = await jobs.create_job(
        db,
        user_id=current_user.id,
        request_payload=payload,
        mode="real" if real else "mock",
        idempotency_key=key,
    )
    if created:
        jobs.dispatch(job.id)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "poll_url": f"/api/ai/plan-novel/jobs/{job.id}",
        "reused": not created,
        "idempotency_key": job.idempotency_key,
    }


@router.get("/plan-novel/jobs/{job_id}")
async def get_plan_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查一个规划任务的状态；成功时一并带回完整 plan。

    别人的任务一律 404（不泄露存在性）。
    """
    job = await jobs.get_job(db, job_id=job_id, user_id=current_user.id)
    if job is None:
        return _ai_error_response("plan_job_not_found", "规划任务不存在或无权访问", status=404)
    return jobs.serialize_job(job)


@router.post("/plan-novel/jobs/{job_id}/cancel")
async def cancel_plan_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """请求取消一个规划任务。

    queued → cancelled；running → cancel_requested（worker 在安全点停）。
    已终态直接返回当前状态，不报错（重复点击幂等）。
    """
    status_value, changed = await jobs.request_cancel(
        db, job_id=job_id, user_id=current_user.id
    )
    if status_value is None:
        return _ai_error_response("plan_job_not_found", "规划任务不存在或无权访问", status=404)
    job = await jobs.get_job(db, job_id=job_id, user_id=current_user.id)
    return {
        "job_id": str(job_id),
        "status": status_value,
        "changed": changed,
        "job": jobs.serialize_job(job, include_plan=False) if job else None,
    }


class PlanResultReviewIn(BaseModel):
    """人工处置一份「晚到但已成功生成」的规划结果。"""

    decision: Literal["accepted", "discarded"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/plan-novel/jobs/{job_id}/result-review")
async def review_plan_result(
    job_id: uuid.UUID,
    body: PlanResultReviewIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把 `result_review_status=pending_review` 的晚到结果判为 accepted / discarded。

    第三批 g：原实现只定义了这两个值、没有任何写入路径 —— 公开接口暴露了一个
    永远不会发生的状态。这里补上唯一入口，并保证：

    - 只有任务所属用户可处置（越权 404，不泄露存在性）；
    - 只接受 pending_review → accepted|discarded 的一次性迁移（第二次处置 409）；
    - 不改任务终态（status / error_code 保持执行事实）；
    - 审计留痕：action=plan_result_reviewed，metadata 含 job_id、decision、request_id。
    """
    job = await jobs.get_job(db, job_id=job_id, user_id=current_user.id)
    if job is None:
        return _ai_error_response("plan_job_not_found", "规划任务不存在或无权访问", status=404)
    if job.result_review_status != st.RESULT_REVIEW_PENDING:
        return _ai_error_response(
            "plan_result_not_pending_review",
            "该任务没有待处置的晚到结果",
            status=409,
        )

    ok = await jobs.decide_late_result(
        db,
        job_id=job_id,
        user_id=current_user.id,
        decision=body.decision,
        note=body.note,
    )
    if not ok:
        await db.rollback()
        return _ai_error_response(
            "plan_result_review_conflict",
            "该结果已被处置或状态已变化，请刷新后重试",
            status=409,
        )

    await audit_service.record(
        db,
        actor_id=current_user.id,
        action="plan_result_reviewed",
        target_type="novel_plan_job",
        target_id=str(job_id),
        metadata={
            "decision": body.decision,
            "note": body.note,
            "request_id": current_request_id(),
            "late_result_reason": job.late_result_reason,
        },
    )
    await db.commit()
    fresh = await jobs.get_job(db, job_id=job_id, user_id=current_user.id)
    return jobs.serialize_job(fresh or job)


@router.get("/plan-novel/jobs")
async def list_plan_jobs(
    limit: int = Query(jobs.DEFAULT_LIST_LIMIT, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户最近的规划任务（只带状态，不带 plan）。

    用途：刷新页面后找回"刚才那个还在跑的任务"，不用从头再来。
    """
    rows = await jobs.list_jobs(db, user_id=current_user.id, limit=limit)
    return {
        "items": [jobs.serialize_job(job, include_plan=False) for job in rows],
        "total": len(rows),
    }
