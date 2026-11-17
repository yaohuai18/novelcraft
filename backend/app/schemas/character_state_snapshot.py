# 人物状态快照 Schema（第三期「人物和时间不漂」/ 文26 P2）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

SNAPSHOT_SOURCES = ["manual", "ai_extracted", "user_confirmed"]
SNAPSHOT_STATUSES = ["draft", "confirmed", "rejected"]


class CharacterStateSnapshotCreate(BaseModel):
    """手工创建快照请求体。"""

    chapter_id: uuid.UUID
    state_json: dict
    source: str = "manual"
    status: str = "draft"


class CharacterStateSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    character_id: uuid.UUID
    chapter_id: uuid.UUID
    state_json: dict
    source: str
    status: str = "draft"
    confirmed_at: datetime | None = None
    created_at: datetime | None = None
