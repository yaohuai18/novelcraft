# 评论相关 Pydantic 模型（第十五批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PositionData(BaseModel):
    """段落评论定位信息：正文修改后优先用 block_id + selected_text 重新匹配。"""
    block_id: str | None = Field(default=None, max_length=128)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    selected_text: str | None = Field(default=None, max_length=500)


class CommentCreate(BaseModel):
    target_type: str = Field(max_length=40)
    target_id: str = Field(max_length=64)
    content: str = Field(min_length=1, max_length=5000)
    # 评论类型（公开评论/私人批注/审核意见/协作讨论）
    visibility: str = Field(default="public_comment", max_length=30)
    position_data: PositionData | None = None


class CommentUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=5000)
    position_data: PositionData | None = None


class CommentResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    target_type: str
    target_id: str
    parent_id: uuid.UUID | None
    content: str
    comment_type: str
    status: str
    position_data: dict | None
    orphaned: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    # 附加展示字段（由 service 填充）
    author_username: str | None = None
    mentioned_user_ids: list[uuid.UUID] = []
    reply_count: int = 0

    model_config = {"from_attributes": True}


class CommentListResponse(BaseModel):
    items: list[CommentResponse]
    total: int
    page: int
    page_size: int


class MentionResponse(BaseModel):
    id: uuid.UUID
    comment_id: uuid.UUID
    mentioned_user_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}
