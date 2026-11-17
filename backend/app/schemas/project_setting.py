# 项目设置接口 Schema（第二期「章节能被记住」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProjectSettingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    project_id: uuid.UUID
    auto_summarize: bool
    auto_check: bool
    character_confirm: str
    foreshadow_confirm: str
    timeline_confirm: str
    updated_at: datetime


class ProjectSettingUpdate(BaseModel):
    auto_summarize: bool | None = None
    auto_check: bool | None = None
    character_confirm: str | None = None
    foreshadow_confirm: str | None = None
    timeline_confirm: str | None = None
