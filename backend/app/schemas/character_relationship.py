# 角色关系 Schema（第三期「人物和时间不漂」）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

RELATION_TYPES = ["师徒", "敌对", "同盟", "亲属", "怀疑", "爱慕", "主仆", "同僚", "其他"]
RELATION_STATUS = ["draft", "confirmed", "archived"]


class CharacterRelationshipCreate(BaseModel):
    """创建关系请求体。"""

    source_character_id: uuid.UUID
    target_character_id: uuid.UUID
    relation_type: str
    description: str | None = None
    start_chapter_id: uuid.UUID | None = None
    end_chapter_id: uuid.UUID | None = None
    status: str = "confirmed"


class CharacterRelationshipUpdate(BaseModel):
    """修改关系请求体（字段均可选）。"""

    relation_type: str | None = None
    description: str | None = None
    start_chapter_id: uuid.UUID | None = None
    end_chapter_id: uuid.UUID | None = None
    status: str | None = None


class CharacterRelationshipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    source_character_id: uuid.UUID
    target_character_id: uuid.UUID
    relation_type: str
    description: str | None = None
    start_chapter_id: uuid.UUID | None = None
    end_chapter_id: uuid.UUID | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
