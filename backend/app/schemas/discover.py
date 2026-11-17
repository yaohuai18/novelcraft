# 发现系统 Schema（第十四批「搜索、标签与发现系统」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PublicProjectItem(BaseModel):
    """公开作品列表条目（最新发布/最近更新/热门）。"""
    id: uuid.UUID
    title: str
    subtitle: str | None = None
    description: str | None = None
    cover_text: str | None = None
    project_slug: str | None = None
    genre: str | None = None
    publication_status: str
    published_at: datetime | None = None
    updated_at: datetime | None = None
    last_published_at: datetime | None = None
    # 服务端统计字段
    view_count: int = 0
    unique_reader_count: int = 0
    favorite_count: int = 0
    chapter_count: int = 0
    word_count: int = 0
    completion_count: int = 0
    # 热度（十四节公式 hot_score = score / pow(age_hours + 2, 0.5)）
    hot_score: float = 0.0
    tags: list[str] = []


class PublicProjectListResponse(BaseModel):
    items: list[PublicProjectItem]
    total: int
    page: int
    page_size: int


class HotTagItem(BaseModel):
    id: uuid.UUID
    name: str
    slug: str | None = None
    color: str
    category: str
    usage_count: int


class SimilarProjectItem(BaseModel):
    project: PublicProjectItem
    common_tag_count: int
    similarity: float


class DiscoverResponse(BaseModel):
    latest: PublicProjectListResponse | None = None
    updated: PublicProjectListResponse | None = None
    hot: PublicProjectListResponse | None = None
    hot_tags: list[HotTagItem] = []
