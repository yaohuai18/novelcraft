# 章节记忆接口 Schema（第二期「章节能被记住」规格实现）
import json
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class ChapterMemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    chapter_id: uuid.UUID
    project_id: uuid.UUID
    summary: str
    memory: dict
    source: str
    status: str
    generation_record_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("memory", mode="before")
    @classmethod
    def _parse_memory_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v) if v else {}
            except Exception:
                return {}
        return v or {}


class ChapterMemoryUpdate(BaseModel):
    summary: str = ""
    memory: dict = {}


class ChapterMemoryConfirmResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    chapter_id: uuid.UUID
    status: str
