# 通知相关 Pydantic 模型（第十五批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class NotificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    type: str
    title: str
    content: str | None
    target_type: str | None
    target_id: str | None
    payload: dict | None
    status: str
    is_read: bool
    read_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    total: int
    page: int
    page_size: int
    unread_count: int = 0
    next_cursor: str | None = None


class UnreadCountResponse(BaseModel):
    unread_count: int


class NotificationPreferenceUpdate(BaseModel):
    in_app_enabled: bool | None = Field(default=None)
    email_enabled: bool | None = Field(default=None)
    push_enabled: bool | None = Field(default=None)


class NotificationPreferenceResponse(BaseModel):
    user_id: uuid.UUID
    notification_type: str
    in_app_enabled: bool
    email_enabled: bool
    push_enabled: bool
    updated_at: datetime

    model_config = {"from_attributes": True}
