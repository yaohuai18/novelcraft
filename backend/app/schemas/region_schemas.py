# 第二十一批「国际化、多语言和多区域部署」Schema（Pydantic）
# 数据区域 / 区域访问 / 跨区域事件同步 / CDN 静态资源 / 区域监控与容灾
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ---------- 数据区域 ----------
class DataRegionCreate(BaseModel):
    region_code: str
    storage_endpoint: str | None = None
    database_cluster: str | None = None
    ai_provider: str | None = None
    status: str = "active"
    priority: int = 100


class DataRegionResponse(BaseModel):
    id: uuid.UUID
    region_code: str
    storage_endpoint: str | None
    database_cluster: str | None
    ai_provider: str | None
    status: str
    priority: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class DataRegionPolicy(BaseModel):
    data_type: str
    source_region: str
    target_region: str
    allowed: bool
    reason: str


class RegionAccessLogResponse(BaseModel):
    id: uuid.UUID
    request_id: str
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    region_code: str
    path: str
    method: str
    status_code: int | None
    data_type: str | None
    response_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 跨区域事件同步 ----------
class RegionEventCreate(BaseModel):
    event_id: str | None = None
    source_region: str
    entity_id: str
    entity_type: str
    event_type: str
    payload: dict | None = None
    entity_version: int = 1


class RegionEventResponse(BaseModel):
    id: uuid.UUID
    event_id: str
    source_region: str
    occurred_at: datetime
    entity_id: str
    entity_type: str
    event_type: str
    entity_version: int
    payload: dict | None
    status: str
    conflict_reason: str | None

    model_config = {"from_attributes": True}


class ChapterVersionCheckRequest(BaseModel):
    chapter_id: uuid.UUID
    base_version: int


class ChapterVersionCheckResponse(BaseModel):
    chapter_id: uuid.UUID
    current_version: int
    submitted_version: int
    ok: bool
    error: str | None = None


# ---------- CDN 静态资源 ----------
class CdnAssetCreate(BaseModel):
    project_id: uuid.UUID | None = None
    object_key: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    visibility: str = "public"
    region: str | None = None
    tags: list[str] | None = None


class CdnAssetResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    object_key: str
    content_type: str
    size_bytes: int
    visibility: str
    region: str | None
    fingerprint: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SignedUrlResponse(BaseModel):
    asset_id: uuid.UUID
    url: str
    expires_at: datetime
    ttl_seconds: int


class DownloadLogResponse(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    user_id: uuid.UUID | None
    region: str | None
    ip_hash: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 区域监控与容灾 ----------
class RegionMetricCreate(BaseModel):
    region_code: str
    request_count: int = 0
    error_count: int = 0
    total_response_ms: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    ai_available: bool = True


class RegionMetricResponse(BaseModel):
    id: uuid.UUID
    region_code: str
    metric_at: datetime
    request_count: int
    error_count: int
    total_response_ms: int
    error_rate: float
    cache_hit_count: int
    cache_miss_count: int
    ai_available: bool

    model_config = {"from_attributes": True}


class RegionHealthResponse(BaseModel):
    region_code: str
    status: str
    reason: str | None
    failover_to: str | None
    data_resident_blocked: bool
    detected_at: datetime | None
    updated_at: datetime | None


class FailoverRequest(BaseModel):
    region_code: str
    target_region: str
    sensitive_data_types: list[str] | None = None


class FailoverResponse(BaseModel):
    switched: bool
    reason: str
    blocked_types: list[str] = []
    failover_to: str | None = None
