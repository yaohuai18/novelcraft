# 安全与隐私 响应模型（第十八批「性能优化、可靠性与安全加固」）
# 覆盖：刷新令牌轮换 / 二次确认 / 安全事件审计 / 文件存储安全 / 健康检查
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 刷新令牌轮换（规格十八） ----------

class RefreshTokenRotateRequest(BaseModel):
    refresh_token: str
    device_info: str | None = Field(default=None, max_length=255)


class RefreshTokenRotateResponse(BaseModel):
    ok: bool
    new_token: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


class RefreshTokenRevokeAllResponse(BaseModel):
    ok: bool
    revoked: int


# ---------- 二次确认（规格十九） ----------

class ConfirmationCreateRequest(BaseModel):
    action: str


class ConfirmationCreateResponse(BaseModel):
    ok: bool
    action: str
    token: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


class ConfirmationConfirmRequest(BaseModel):
    action: str
    token: str


class ConfirmationConfirmResponse(BaseModel):
    ok: bool
    action: str
    reason: str | None = None


# ---------- 安全事件（规格二十四） ----------

class SecurityEventResponse(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    event_type: str
    ip_hash: str | None
    device_info: str | None
    details: str | None
    created_at: datetime


class SecurityEventListResponse(BaseModel):
    total: int
    items: list[SecurityEventResponse]


# ---------- 文件存储（规格二十二） ----------

class FileUploadValidateRequest(BaseModel):
    original_name: str = Field(..., min_length=1, max_length=255)
    declared_mime: str = ""
    content_length: int = Field(..., ge=0)
    is_private: bool = True


class FileUploadValidateResponse(BaseModel):
    ok: bool
    reason: str | None = None
    stored_name: str | None = None
    is_private: bool = True


class FileRegisterRequest(BaseModel):
    original_name: str = Field(..., min_length=1, max_length=255)
    stored_name: str = Field(..., min_length=1, max_length=255)
    mime_type: str
    file_size: int = Field(..., ge=0)
    is_private: bool = True
    max_downloads: int | None = Field(default=None, ge=1)
    expires_in_minutes: int | None = Field(default=None, ge=1)


class FileRegisterResponse(BaseModel):
    id: uuid.UUID
    stored_name: str
    original_name: str
    mime_type: str
    file_size: int
    is_private: bool
    download_count: int
    expires_at: datetime | None
    created_at: datetime


class FileSignResponse(BaseModel):
    stored_name: str
    signed_url: str


class StoredFileResponse(ORMModel):
    id: uuid.UUID
    original_name: str
    stored_name: str
    file_size: int
    mime_type: str
    is_private: bool
    download_count: int
    max_downloads: int | None
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime | None


class FileListResponse(BaseModel):
    total: int
    items: list[StoredFileResponse]


class FileDownloadResponse(BaseModel):
    ok: bool
    reason: str | None = None
    original_name: str | None = None
    mime_type: str | None = None
    download_count: int | None = None


# ---------- 隐私保护（规格二十三） ----------

class PrivacyExportResponse(BaseModel):
    ok: bool
    data: dict


class PrivacyDeleteAccountResponse(BaseModel):
    ok: bool
    reason: str | None = None


# ---------- 内容安全（规格二十一） ----------

class ContentCheckRequest(BaseModel):
    content: str = Field(..., max_length=20000)
    kind: str = Field(default="html", pattern="^(html|text)$")


class ContentCheckResponse(BaseModel):
    sanitized: str
    command_injection_risk: bool


# ---------- 健康检查（规格二十五） ----------

class HealthComponent(BaseModel):
    name: str
    status: str
    detail: str | None = None


class HealthDetailResponse(BaseModel):
    status: str
    components: list[HealthComponent]
    uptime_seconds: float | None = None
