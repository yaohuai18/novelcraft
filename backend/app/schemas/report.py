# 举报相关 Pydantic 模型（第十五批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ReportCreate(BaseModel):
    target_type: str = Field(max_length=40)
    target_id: str = Field(max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class ReportUpdate(BaseModel):
    status: str = Field(max_length=20)


class ReportResponse(BaseModel):
    id: uuid.UUID
    reporter_id: uuid.UUID
    target_type: str
    target_id: str
    reason: str
    status: str
    handled_by: uuid.UUID | None
    handled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReportListResponse(BaseModel):
    items: list[ReportResponse]
    total: int
    page: int
    page_size: int
