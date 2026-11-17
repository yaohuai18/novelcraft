# 大纲相关 Pydantic 请求/响应模型（第四批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OutlineCreate(BaseModel):
    """创建大纲节点请求体。"""

    title: str = Field(min_length=1, max_length=255)
    outline_type: str
    parent_id: uuid.UUID | None = None
    summary: str | None = None
    content: str | None = None
    sort_order: int = 0
    status: str = "draft"


class OutlineUpdate(BaseModel):
    """修改大纲节点请求体（字段均可选，仅更新传入字段）。"""

    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=255
    )
    parent_id: uuid.UUID | None = None
    summary: str | None = None
    content: str | None = None
    sort_order: int | None = None
    status: str | None = None


class OutlineResponse(BaseModel):
    """大纲节点响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    parent_id: uuid.UUID | None
    title: str
    outline_type: str
    summary: str | None
    content: str | None
    sort_order: int
    status: str
    # 第十三批「发布与阅读模式」：章节可见性与公开链接
    visibility: str
    chapter_slug: str | None
    published_at: datetime | None


class OutlineBatchItem(BaseModel):
    """批量保存单条大纲项（第六批规格实现）。"""

    title: str = Field(min_length=1, max_length=255)
    outline_type: str
    parent_id: uuid.UUID | None = None
    summary: str | None = None
    content: str | None = None
    sort_order: int = 0
    status: str = "confirmed"


class OutlineBatchCreate(BaseModel):
    """批量保存请求体：1~100 条。"""

    items: list[OutlineBatchItem] = Field(
        min_length=1,
        max_length=100
    )
