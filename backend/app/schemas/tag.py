# 标签 Schema（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default="#4F6EF7", max_length=30)
    tag_type: str | None = Field(default=None, max_length=30)
    # 第十四批新增字段
    category: str | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=500)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=30)
    tag_type: str | None = Field(default=None, max_length=30)
    category: str | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=500)


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID | None = None
    name: str
    slug: str | None = None
    color: str
    description: str | None = None
    category: str
    tag_type: str | None
    usage_count: int
    created_at: datetime
    updated_at: datetime


class OutlineTagsUpdate(BaseModel):
    """给大纲条目整体替换标签集合（第九批，保留）。"""
    tag_ids: list[uuid.UUID] = Field(max_length=200)


class TagRelationCreate(BaseModel):
    """给单个标签打上某个目标。"""
    target_type: str = Field(max_length=30)
    target_id: uuid.UUID


class TagBatchAddRequest(BaseModel):
    """批量添加标签：target_type + target_ids + tag_ids。"""
    target_type: str = Field(max_length=30)
    target_ids: list[uuid.UUID] = Field(max_length=200)
    tag_ids: list[uuid.UUID] = Field(max_length=200)


class TagBatchAddResponse(BaseModel):
    created: int
    skipped: int
    relation_ids: list[uuid.UUID]
