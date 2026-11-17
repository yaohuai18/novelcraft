# 搜索历史与建议 Schema（第十四批「搜索、标签与发现系统」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SearchHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID | None = None
    query: str
    created_at: datetime


class SearchHistoryListResponse(BaseModel):
    items: list[SearchHistoryItem]
    total: int


class SearchSuggestionsResponse(BaseModel):
    query: str
    items: list[str]
