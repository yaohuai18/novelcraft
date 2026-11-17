# 收藏 Schema（第九批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# 允许收藏的目标类型（第九批 + 第十四批新增 project）
FAVORITE_TARGET_TYPES = [
    "outline",
    "chapter_content",
    "character",
    "world_setting",
    "tag",
    "project",
]


class FavoriteCreate(BaseModel):
    target_type: str = Field(max_length=30)
    target_id: uuid.UUID


class FavoriteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    created_at: datetime
