# 最近访问 Schema（第九批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RecentItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    accessed_at: datetime
