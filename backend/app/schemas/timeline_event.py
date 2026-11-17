# 时间线事件 Schema（第三期「时间线事件结构化」）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

TIMELINE_SOURCES = ["manual", "ai_extracted"]
TIMELINE_STATUSES = ["draft", "confirmed", "archived"]


class TimelineEventCreate(BaseModel):
    """创建时间线事件请求体（第三期结构化字段 + 文26 P2）。"""

    title: str
    description: str | None = None
    chapter_id: uuid.UUID | None = None
    event_date_label: str | None = None
    time_label: str | None = None
    event_time: str | None = None
    sequence_order: int | None = None
    sort_order: int = 0
    location_id: uuid.UUID | None = None
    participants: list[str] | None = None
    importance: str = "normal"
    source: str = "manual"
    confirmed: bool = False
    status: str = "draft"


class TimelineEventUpdate(BaseModel):
    """修改时间线事件请求体（字段均可选）。"""

    title: str | None = None
    description: str | None = None
    chapter_id: uuid.UUID | None = None
    event_date_label: str | None = None
    time_label: str | None = None
    event_time: str | None = None
    sequence_order: int | None = None
    sort_order: int | None = None
    location_id: uuid.UUID | None = None
    participants: list[str] | None = None
    importance: str | None = None
    source: str | None = None
    confirmed: bool | None = None
    status: str | None = None


class TimelineEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None = None
    chapter_id: uuid.UUID | None = None
    event_date_label: str | None = None
    time_label: str | None = None
    event_time: str | None = None
    sequence_order: int | None = None
    sort_order: int = 0
    location_id: uuid.UUID | None = None
    participants: list[str] | None = None
    importance: str = "normal"
    source: str = "manual"
    confirmed: bool = False
    status: str = "draft"
    created_at: datetime | None = None
