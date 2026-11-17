# 搜索 Schema（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# 第十四批结构：target_type/target_id/title/highlight/score/updated_at，标题优先+结果高亮。
# 保留第九批字段（type/id/snippet/outline_type/parent_id）以兼容既有调用。
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SearchItem(BaseModel):
    """单条搜索结果：返回匹配片段与高亮片段，不返回整篇正文。"""
    # 第十四批新结构字段
    target_type: str
    target_id: uuid.UUID
    title: str
    highlight: str = ""
    score: float
    updated_at: datetime | None = None
    # 补充展示字段（匹配位置/所属分卷/当前状态）
    status: str | None = None
    visibility: str | None = None
    volume_title: str | None = None
    match_position: str | None = None
    # 第九批兼容字段
    type: str = ""
    id: uuid.UUID | None = None
    snippet: str = ""
    outline_type: str | None = None
    parent_id: uuid.UUID | None = None


class SearchResponse(BaseModel):
    query: str = ""
    items: list[SearchItem]
    total: int
    page: int
    page_size: int
