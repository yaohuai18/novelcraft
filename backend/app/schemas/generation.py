# AI 生成记录 schema（第一期「生成不丢失」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class GenerationRecordResponse(BaseModel):
    """单条生成记录：完整记录一次 AI 调用。"""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    project_id: uuid.UUID | None
    volume_id: uuid.UUID | None
    chapter_id: uuid.UUID | None
    scene_id: uuid.UUID | None
    outline_plan_version_id: uuid.UUID | None = None
    chapter_plan_id: str | None = None
    chapter_plan_snapshot: str | None = None
    title_snapshot: str | None = None
    action_type: str  # outline / draft / continue / rewrite / polish / summarize / extract_state / check
    model_provider: str | None
    model_name: str | None
    temperature: float | None
    max_tokens: int | None
    system_prompt: str
    user_prompt: str
    context_snapshot: str | None
    response_content: str
    input_tokens: int | None
    output_tokens: int | None
    status: str  # pending / succeeded / failed / cancelled
    error_message: str | None
    created_at: datetime


class GenerationRecordDetailResponse(GenerationRecordResponse):
    """生成记录详情：附 context_snapshot / chapter_plan_snapshot 解析后的 JSON。"""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    context: dict | None = None
    chapter_plan: dict | None = None


class GenerationRecordListResponse(BaseModel):
    """生成记录分页列表。"""

    items: list[GenerationRecordResponse]
    total: int
    page: int
    page_size: int
