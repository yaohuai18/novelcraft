# 人物相关 Pydantic 请求/响应模型（第三批规格实现，第三期人物库扩展）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# 角色定位 / 生存状态 可选值
ROLE_VALUES = ["主角", "配角", "反派", "路人"]
STATUS_VALUES = ["在世", "死亡", "失踪", "未知"]


class CharacterCreate(BaseModel):
    """创建人物请求体（含第三期人物库字段）。"""

    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] | None = None
    role: str = "配角"
    status: str = "在世"
    profile: str | None = None
    goals: str | None = None
    secrets: str | None = None
    abilities: str | None = None
    current_location: str | None = None
    current_timeline_status: str | None = None
    first_chapter_id: uuid.UUID | None = None
    last_chapter_id: uuid.UUID | None = None
    # 一期兼容字段
    identity: str | None = None
    personality: str | None = None
    appearance: str | None = None
    background: str | None = None
    goal: str | None = None
    conflict: str | None = None
    relationship: str | None = None
    character_status: str | None = None


class CharacterUpdate(BaseModel):
    """修改人物请求体（字段均可选，仅更新传入字段）。"""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100
    )
    aliases: list[str] | None = None
    role: str | None = None
    status: str | None = None
    profile: str | None = None
    goals: str | None = None
    secrets: str | None = None
    abilities: str | None = None
    current_location: str | None = None
    current_timeline_status: str | None = None
    first_chapter_id: uuid.UUID | None = None
    last_chapter_id: uuid.UUID | None = None
    # 一期兼容字段
    identity: str | None = None
    personality: str | None = None
    appearance: str | None = None
    background: str | None = None
    goal: str | None = None
    conflict: str | None = None
    relationship: str | None = None
    character_status: str | None = None


class CharacterResponse(BaseModel):
    """人物响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    aliases: list[str] | None = None
    role: str
    status: str
    profile: str | None = None
    goals: str | None = None
    secrets: str | None = None
    abilities: str | None = None
    current_location: str | None = None
    current_timeline_status: str | None = None
    first_chapter_id: uuid.UUID | None = None
    last_chapter_id: uuid.UUID | None = None
    # 一期兼容字段
    identity: str | None = None
    personality: str | None = None
    appearance: str | None = None
    background: str | None = None
    goal: str | None = None
    conflict: str | None = None
    relationship: str | None = None
    character_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
