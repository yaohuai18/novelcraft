# AI 任务请求/响应模型（第八批规格实现 + P0 任务系统补齐）
import uuid
from datetime import datetime

from pydantic import BaseModel


class AITaskCreateResponse(BaseModel):
    task_id: uuid.UUID
    status: str
    duplicated: bool = False


class AITaskResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    outline_id: uuid.UUID | None
    task_type: str
    status: str
    prompt: str | None = None
    result: str | None
    error_message: str | None
    retry_count: int
    # 阶段D「异步任务增强」字段
    max_retries: int = 0
    cost_budget: int | None = None
    resume_token: str | None = None
    # P0 补齐字段
    model_name: str | None = None
    prompt_version: str | None = None
    idempotency_key: str | None = None
    request_snapshot: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    timeout_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {
        "from_attributes": True
    }


class AITaskListResponse(BaseModel):
    items: list[AITaskResponse]
    total: int
