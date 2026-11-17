# 运维/性能/可靠性与安全加固 响应模型（第十八批「性能优化、可靠性与安全加固」）
# 覆盖：缓存 / Job 队列与死信 / 限流 / AI 额度 / 幂等 / Outbox / 故障演练 / 性能指标
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 缓存（规格三/四） ----------

class CacheReadResponse(BaseModel):
    cache_key: str
    value: str | None = None
    version: int | None = None
    expires_at: datetime | None = None
    hit: bool
    stale: bool = False


class CacheWriteRequest(BaseModel):
    cache_key: str = Field(..., min_length=1, max_length=255)
    value: str
    ttl_seconds: int | None = Field(default=None, ge=1)


class CacheWriteResponse(BaseModel):
    cache_key: str
    version: int
    expires_at: datetime | None


class CacheInvalidateRequest(BaseModel):
    key_prefix: str = Field(..., min_length=1, max_length=255)


class CacheInvalidateResponse(BaseModel):
    prefix: str
    deleted: int


# ---------- Job 队列（规格九/十） ----------

class JobCreateRequest(BaseModel):
    queue: str = Field(..., min_length=1, max_length=50)
    task_type: str = Field(..., min_length=1, max_length=80)
    payload: str = "{}"
    priority: int = 0
    max_attempts: int = Field(default=3, ge=1, le=10)
    available_in_seconds: int = Field(default=0, ge=0)


class JobResponse(ORMModel):
    id: uuid.UUID
    queue: str
    task_type: str
    payload: str
    priority: int
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    created_at: datetime


class JobListResponse(BaseModel):
    total: int
    items: list[JobResponse]


class JobRetryResponse(BaseModel):
    retried: int
    message: str


# ---------- 限流（规格十一） ----------

class RateLimitCheckRequest(BaseModel):
    scope: str = Field(..., min_length=1, max_length=50)
    dimension: str = Field(..., min_length=1, max_length=30)
    dimension_value: str = Field(..., min_length=1, max_length=128)
    limit: int = Field(..., ge=1)


class RateLimitCheckResponse(BaseModel):
    allowed: bool
    count: int
    limit: int
    retry_after: int = 0
    request_id: str | None = None


class RateLimitRecordResponse(ORMModel):
    id: uuid.UUID
    dimension: str
    dimension_value: str
    scope: str
    limit: int
    count: int
    period_start: datetime
    period_end: datetime


class RateLimitListResponse(BaseModel):
    total: int
    items: list[RateLimitRecordResponse]


# ---------- AI 额度（规格十二） ----------

class AIQuotaResponse(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    period_date: str
    daily_limit: int
    daily_used: int
    project_budget: int | None
    project_used: int
    user_budget: int | None
    user_used: int
    concurrency_limit: int
    concurrency_active: int
    updated_at: datetime


class AIQuotaCheckRequest(BaseModel):
    project_id: uuid.UUID | None = None
    estimated_input_chars: int = Field(default=0, ge=0)


class AIQuotaCheckResponse(BaseModel):
    allowed: bool
    reason: str
    daily_remaining: int
    project_remaining: int | None
    user_remaining: int | None
    concurrency_available: int


class AIQuotaConsumeRequest(BaseModel):
    project_id: uuid.UUID | None = None
    amount: int = Field(default=1, ge=0)
    input_chars: int = Field(default=0, ge=0)


class AIQuotaConsumeResponse(BaseModel):
    ok: bool
    daily_used: int
    project_used: int
    user_used: int
    concurrency_active: int


# ---------- 幂等（规格十三） ----------

class IdempotencyRecordResponse(ORMModel):
    id: uuid.UUID
    idempotency_key: str
    endpoint: str
    user_id: uuid.UUID | None
    response_status: int
    response_body: str
    created_at: datetime
    expires_at: datetime | None


class IdempotencyListResponse(BaseModel):
    total: int
    items: list[IdempotencyRecordResponse]


# ---------- Outbox（规格十四/十五） ----------

class OutboxEventResponse(ORMModel):
    id: uuid.UUID
    event_type: str
    payload: str
    status: str
    attempts: int
    available_at: datetime
    processed_at: datetime | None
    error_message: str | None
    created_at: datetime


class OutboxListResponse(BaseModel):
    total: int
    items: list[OutboxEventResponse]


class OutboxDispatchResponse(BaseModel):
    dispatched: int
    failed: int
    message: str


# ---------- 故障演练（规格十六） ----------

class DrillStartRequest(BaseModel):
    drill_type: str
    notes: str | None = None


class DrillStartResponse(BaseModel):
    id: uuid.UUID
    drill_type: str
    status: str
    started_at: datetime


class DrillFinishRequest(BaseModel):
    passed: bool
    notes: str | None = None


class DrillFinishResponse(BaseModel):
    id: uuid.UUID
    drill_type: str
    status: str
    completed_at: datetime
    result: str | None


class DrillRecordResponse(ORMModel):
    id: uuid.UUID
    drill_type: str
    status: str
    result: str | None
    notes: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime


class DrillListResponse(BaseModel):
    total: int
    items: list[DrillRecordResponse]
    dr_policy: dict


# ---------- 性能指标（规格二） ----------

class MetricsListResponse(BaseModel):
    total: int
    snapshot: dict
    items: list[dict]
