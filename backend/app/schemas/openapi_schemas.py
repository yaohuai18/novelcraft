# 开放 API 请求模型（第十九批「移动端适配与开放 API」）
# 仅定义入参校验模型；响应统一由 core/api_response.ok() 构造 dict。
import uuid
from typing import Any

from pydantic import BaseModel, Field


# ---------- 令牌 ----------
class TokenCreateRequest(BaseModel):
    name: str = Field("", max_length=100)
    token_type: str = Field("personal", pattern="^(personal|app|api_key)$")
    scopes: list[str] = Field(default_factory=list, description="默认最小权限")
    expires_in_days: int | None = Field(None, ge=1, le=3650)


class TokenUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=100)


# ---------- 项目 ----------
class ProjectCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    genre: str | None = Field(None, max_length=100)


class ProjectUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    genre: str | None = Field(None, max_length=100)


# ---------- 章节 ----------
class ChapterCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    content: str = ""
    visibility: str = Field("private", pattern="^(private|project_member|link_only|public)$")


class ChapterUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = None
    visibility: str | None = Field(
        None, pattern="^(private|project_member|link_only|public)$"
    )


class PublishRequest(BaseModel):
    visibility: str = Field("public", pattern="^(private|project_member|link_only|public)$")
    title: str | None = None
    description: str | None = None


# ---------- AI 任务 ----------
class AITaskCreateRequest(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    task_type: str
    model: str = "default"
    input: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


# ---------- Webhook ----------
class WebhookCreateRequest(BaseModel):
    app_id: uuid.UUID | None = None
    endpoint: str = Field(min_length=1, max_length=500)
    events: list[str] = Field(min_length=1)


class WebhookUpdateRequest(BaseModel):
    endpoint: str | None = Field(None, max_length=500)
    events: list[str] | None = None
    status: str | None = Field(None, pattern="^(active|paused|disabled)$")


# ---------- 开发者平台 ----------
class AppCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    callback_url: str | None = Field(None, max_length=500)
    requested_scopes: list[str] = Field(default_factory=list)


class AppUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    callback_url: str | None = Field(None, max_length=500)
    requested_scopes: list[str] | None = None


# ---------- 移动端草稿 ----------
class MobileDraftSaveRequest(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    content: str = ""
    base_version: str | None = None
    device_info: str | None = Field(None, max_length=200)


class MobileDraftResolveRequest(BaseModel):
    strategy: str = Field(pattern="^(keep_server|keep_local|save_as_new)$")
