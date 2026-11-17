# 运维/性能/可靠性与安全加固 路由（第十八批「性能优化、可靠性与安全加固」）
# 覆盖：缓存读写与主动失效 / Job 队列与死信 / 限流 / AI 额度 / 幂等 / Outbox / 故障演练 / 性能指标
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import require_admin_roles
from app.models.user import User
from app.schemas.ops_schemas import (
    AIQuotaCheckRequest,
    AIQuotaCheckResponse,
    AIQuotaConsumeRequest,
    AIQuotaConsumeResponse,
    AIQuotaResponse,
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CacheReadResponse,
    CacheWriteRequest,
    CacheWriteResponse,
    DrillFinishRequest,
    DrillFinishResponse,
    DrillListResponse,
    DrillRecordResponse,
    DrillStartRequest,
    DrillStartResponse,
    IdempotencyListResponse,
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    JobRetryResponse,
    MetricsListResponse,
    OutboxDispatchResponse,
    OutboxListResponse,
    OutboxEventResponse,
    RateLimitCheckRequest,
    RateLimitCheckResponse,
    RateLimitListResponse,
    RateLimitRecordResponse,
)
from app.services import (
    ai_quota_service,
    cache_service,
    drill_service,
    idempotency_service,
    job_service,
    outbox_service,
    perf_service,
    rate_limit_service,
)

router = APIRouter(prefix="/api/ops", tags=["运维与可靠性"])

ADMIN_ROLES = ("super_admin", "operator", "analyst")


# ---------- 缓存（规格三/四） ----------

@router.get("/cache/{cache_key:path}", response_model=CacheReadResponse)
async def cache_read(
    cache_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    value, hit = await cache_service.get_cached(db, cache_key)
    return CacheReadResponse(cache_key=cache_key, value=json.dumps(value, ensure_ascii=False) if value else None, hit=hit)


@router.put("/cache", response_model=CacheWriteResponse)
async def cache_write(
    req: CacheWriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    try:
        value = json.loads(req.value) if req.value else {"raw": req.value}
    except Exception:
        value = {"raw": req.value}
    row = await cache_service.set_cached(
        db, req.cache_key, value, ttl_seconds=req.ttl_seconds
    )
    return CacheWriteResponse(cache_key=row.cache_key, version=row.version, expires_at=row.expires_at)


@router.post("/cache/invalidate", response_model=CacheInvalidateResponse)
async def cache_invalidate(
    req: CacheInvalidateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    deleted = await cache_service.invalidate_prefix(db, req.key_prefix)
    return CacheInvalidateResponse(prefix=req.key_prefix, deleted=deleted)


# ---------- Job 队列与死信（规格九/十） ----------

@router.post("/jobs", response_model=JobResponse)
async def job_enqueue(
    req: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    try:
        payload = json.loads(req.payload) if req.payload else {}
    except Exception:
        payload = {"raw": req.payload}
    job = await job_service.enqueue(
        db,
        queue=req.queue,
        task_type=req.task_type,
        payload=payload,
        priority=req.priority,
        max_attempts=req.max_attempts,
    )
    return JobResponse.model_validate(job)


@router.get("/jobs", response_model=JobListResponse)
async def job_list(
    queue: str | None = Query(default=None),
    status: str | None = Query(default=None),
    task_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await job_service.list_jobs(
        db, queue=queue, status_filter=status, task_type=task_type, page=page, page_size=page_size
    )
    return JobListResponse(total=total, items=[JobResponse.model_validate(r) for r in rows])


@router.get("/jobs/dead", response_model=JobListResponse)
async def job_dead_list(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await job_service.list_jobs(db, status_filter="dead", page=1, page_size=100)
    return JobListResponse(total=total, items=[JobResponse.model_validate(r) for r in rows])


@router.post("/jobs/requeue/{job_id}", response_model=JobRetryResponse)
async def job_requeue(
    job_id: uuid.UUID = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    job = await job_service.get_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        await job_service.requeue_dead(db, job)
        return JobRetryResponse(retried=1, message="死信任务已重新入队")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------- 限流（规格十一） ----------

@router.post("/rate-limit/check", response_model=RateLimitCheckResponse)
async def rate_limit_check(
    req: RateLimitCheckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await rate_limit_service.check_and_bump(
        db,
        scope=req.scope,
        dimension=req.dimension,
        dimension_value=req.dimension_value,
        limit=req.limit,
    )
    return RateLimitCheckResponse(
        allowed=result["allowed"],
        count=result["count"],
        limit=result["limit"],
        retry_after=result["retry_after"],
        request_id=result["request_id"],
    )


@router.get("/rate-limit/records", response_model=RateLimitListResponse)
async def rate_limit_records(
    scope: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await rate_limit_service.list_limits(db, scope=scope, page=page, page_size=page_size)
    return RateLimitListResponse(total=total, items=[RateLimitRecordResponse.model_validate(r) for r in rows])


# ---------- AI 额度与成本保护（规格十二） ----------

@router.get("/ai-quota", response_model=AIQuotaResponse)
async def ai_quota_get(
    project_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    data = await ai_quota_service.get_quota(
        db, user_id=current_user.id, project_id=project_id, period_date=today
    )
    return AIQuotaResponse(
        id=uuid.uuid4(),
        user_id=uuid.UUID(data["user_id"]),
        project_id=uuid.UUID(data["project_id"]) if data["project_id"] else None,
        period_date=data["period_date"],
        daily_limit=data["daily_limit"],
        daily_used=data["daily_used"],
        project_budget=data["project_budget"],
        project_used=data["project_used"],
        user_budget=data["user_budget"],
        user_used=data["user_used"],
        concurrency_limit=data["concurrency_limit"],
        concurrency_active=data["concurrency_active"],
        updated_at=datetime.utcnow(),
    )


@router.post("/ai-quota/check", response_model=AIQuotaCheckResponse)
async def ai_quota_check(
    req: AIQuotaCheckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    result = await ai_quota_service.check_submission(
        db,
        user_id=current_user.id,
        project_id=req.project_id,
        content_length=req.estimated_input_chars,
        period_date=today,
        user_role=getattr(current_user, "role", "user") or "user",
    )
    q = result["quota"]
    return AIQuotaCheckResponse(
        allowed=result["allowed"],
        reason=result["reason"],
        daily_remaining=max(0, q.daily_limit - q.daily_used) if q else 0,
        project_remaining=max(0, q.project_budget - q.project_used) if q and q.project_budget is not None else None,
        user_remaining=max(0, q.user_budget - q.user_used) if q and q.user_budget is not None else None,
        concurrency_available=max(0, q.concurrency_limit - q.concurrency_active) if q else 0,
    )


@router.post("/ai-quota/consume", response_model=AIQuotaConsumeResponse)
async def ai_quota_consume(
    req: AIQuotaConsumeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    quota = await ai_quota_service.consume(
        db, user_id=current_user.id, project_id=req.project_id, period_date=today
    )
    return AIQuotaConsumeResponse(
        ok=True,
        daily_used=quota.daily_used,
        project_used=quota.project_used,
        user_used=quota.user_used,
        concurrency_active=quota.concurrency_active,
    )


# ---------- 幂等（规格十三） ----------

@router.get("/idempotency", response_model=IdempotencyListResponse)
async def idempotency_list(
    endpoint: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    from app.schemas.ops_schemas import IdempotencyRecordResponse

    rows, total = await idempotency_service.list_records(db, endpoint=endpoint, page=page, page_size=page_size)
    return IdempotencyListResponse(total=total, items=[IdempotencyRecordResponse.model_validate(r) for r in rows])


@router.post("/idempotency/demo", response_model=dict)
async def idempotency_demo(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """幂等演示接口：相同 key 重复调用返回首次结果。"""
    idem_key = "demo-create-backup"
    replay = await idempotency_service.find_replay(db, idempotency_key=idem_key, endpoint="/api/ops/idempotency/demo")
    if replay is not None:
        return {
            "replayed": True,
            "status": replay.response_status,
            "body": json.loads(replay.response_body),
        }
    result = {"backup_id": str(uuid.uuid4()), "created_at": datetime.utcnow().isoformat()}
    await idempotency_service.save_result(
        db,
        idempotency_key=idem_key,
        endpoint="/api/ops/idempotency/demo",
        user_id=current_user.id,
        response_status=201,
        response_body=result,
    )
    await db.commit()
    return {"replayed": False, "status": 201, "body": result}


# ---------- Outbox（规格十四/十五） ----------

@router.post("/outbox/events", response_model=OutboxEventResponse)
async def outbox_add(
    req: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    event_type = req.get("event_type", "domain.event")
    payload = req.get("payload", {})
    event = await outbox_service.add_event(db, event_type=event_type, payload=payload)
    await db.commit()
    await db.refresh(event)
    return OutboxEventResponse.model_validate(event)


@router.get("/outbox", response_model=OutboxListResponse)
async def outbox_list(
    status: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await outbox_service.list_events(
        db, status_filter=status, event_type=event_type, page=page, page_size=page_size
    )
    return OutboxListResponse(total=total, items=[OutboxEventResponse.model_validate(r) for r in rows])


@router.post("/outbox/dispatch", response_model=OutboxDispatchResponse)
async def outbox_dispatch(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    """后台任务派发：领取 pending 事件 -> 投递（模拟）-> 标记完成。"""
    events = await outbox_service.claim_pending(db, batch_size=50)
    dispatched = 0
    for event in events:
        try:
            await outbox_service.mark_delivered(db, event)
            dispatched += 1
        except Exception as exc:
            await outbox_service.mark_failed(db, event, error_message=str(exc))
    await db.commit()
    return OutboxDispatchResponse(dispatched=dispatched, failed=len(events) - dispatched, message="派发完成")


# ---------- 故障演练（规格十六） ----------

@router.get("/drills", response_model=DrillListResponse)
async def drill_list(
    drill_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await drill_service.list_drills(db, drill_type=drill_type, status=status, page=page, page_size=page_size)
    return DrillListResponse(
        total=total,
        items=[DrillRecordResponse.model_validate(r) for r in rows],
        dr_policy=drill_service.DR_POLICY,
    )


@router.post("/drills/start", response_model=DrillStartResponse)
async def drill_start(
    req: DrillStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    try:
        row = await drill_service.start_drill(
            db, drill_type=req.drill_type, created_by=current_user.id, notes=req.notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return DrillStartResponse(id=row.id, drill_type=row.drill_type, status=row.status, started_at=row.started_at)


@router.post("/drills/{drill_id}/finish", response_model=DrillFinishResponse)
async def drill_finish(
    drill_id: uuid.UUID = Path(...),
    req: DrillFinishRequest = DrillFinishRequest(passed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, _ = await drill_service.list_drills(db, page=1, page_size=100)
    record = next((r for r in rows if r.id == drill_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="演练记录不存在")
    try:
        row = await drill_service.finish_drill(db, record=record, passed=req.passed, notes=req.notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return DrillFinishResponse(
        id=row.id, drill_type=row.drill_type, status=row.status, completed_at=row.completed_at, result=row.result
    )


# ---------- 性能指标（规格二） ----------

@router.post("/metrics/persist", response_model=dict)
async def metrics_persist(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    row = await perf_service.persist_snapshot(db)
    return {"persisted": True, "metric_id": str(row.id), "snapshot": perf_service.live.snapshot()}


@router.get("/metrics", response_model=MetricsListResponse)
async def metrics_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    rows, total = await perf_service.list_metrics(db, page=page, page_size=page_size)
    return MetricsListResponse(
        total=total,
        snapshot=perf_service.live.snapshot(),
        items=[
            {
                "id": str(r.id),
                "endpoint": r.endpoint,
                "method": r.method,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "db_ms": r.db_ms,
                "ai_ms": r.ai_ms,
                "search_ms": r.search_ms,
                "queue_wait_ms": r.queue_wait_ms,
                "cache_hit": r.cache_hit,
                "is_error": r.is_error,
                "is_timeout": r.is_timeout,
                "slow_sql": r.slow_sql,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    )
