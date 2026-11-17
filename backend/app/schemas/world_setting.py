# 世界观条目 Pydantic 请求/响应模型（第三批规格实现）
import uuid

from pydantic import BaseModel, ConfigDict, Field

# 建议分类取值（与规格一致）：
# faction 势力 / location 地点 / power 力量体系 / history 历史事件
# race 种族 / item 物品 / rule 世界规则 / taboo 禁忌
WORLD_SETTING_CATEGORIES = [
    "faction",
    "location",
    "power",
    "history",
    "race",
    "item",
    "rule",
    "taboo",
]


class WorldSettingCreate(BaseModel):
    """创建世界观条目请求体。"""

    category: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    importance: str = Field(default="normal", max_length=20)


class WorldSettingUpdate(BaseModel):
    """修改世界观条目请求体（字段均可选，仅更新传入字段）。"""

    category: str | None = Field(default=None, min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    importance: str | None = Field(default=None, max_length=20)


class WorldSettingResponse(BaseModel):
    """世界观条目响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    category: str
    name: str
    content: str
    importance: str
