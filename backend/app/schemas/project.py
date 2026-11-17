# 作品相关 Pydantic 请求/响应模型
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """创建作品请求体。"""

    title: str = Field(min_length=1, max_length=255, description="作品标题")
    genre: str | None = None
    background: str | None = None
    world_setting: str | None = None
    main_conflict: str | None = None
    ending_direction: str | None = None
    narrative_perspective: str | None = None
    writing_style: str | None = None
    forbidden_settings: str | None = None


class ProjectUpdate(BaseModel):
    """修改作品请求体（字段均可选，仅更新传入字段）。"""

    title: str | None = Field(default=None, min_length=1, max_length=255, description="作品标题")
    genre: str | None = None
    background: str | None = None
    world_setting: str | None = None
    main_conflict: str | None = None
    ending_direction: str | None = None
    narrative_perspective: str | None = None
    writing_style: str | None = None
    forbidden_settings: str | None = None
    status: str | None = None  # 规格要求：支持更新 status（归档能力）


class ProjectResponse(BaseModel):
    """作品响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    genre: str | None
    background: str | None
    world_setting: str | None
    main_conflict: str | None
    ending_direction: str | None
    narrative_perspective: str | None
    writing_style: str | None
    forbidden_settings: str | None
    status: str
    # 第十三批「发布与阅读模式」：公开信息与发布状态
    subtitle: str | None
    description: str | None
    cover_text: str | None
    project_slug: str | None
    publication_status: str
    published_at: datetime | None
    created_at: datetime  # 保留现有额外字段（规格未含，现有实现已有）
    updated_at: datetime  # 保留现有额外字段（规格未含，现有实现已有）
