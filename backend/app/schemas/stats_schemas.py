# 统计模块请求体 Schema（第十七批「统计分析与运营后台」）
# 响应统一为 dict 结构，含 updated_at / is_realtime / is_estimated 标识（规格十九）。
import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class WritingGoalCreate(BaseModel):
    """创建写作目标（规格三）。"""
    goal_type: str = Field(..., description="daily_words/weekly_words/project_words/chapter_count/publish_count")
    target_value: int = Field(..., gt=0)
    start_date: date | None = None
    end_date: date | None = None


class WritingGoalUpdate(BaseModel):
    target_value: int | None = Field(default=None, gt=0)
    current_value: int | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None


class WritingDailyStatCreate(BaseModel):
    """每日写作快照写入（规格四，避免每次扫全文）。"""
    stat_date: date
    word_count: int | None = Field(default=None, ge=0)
    new_word_count: int | None = Field(default=None, ge=0)
    edited_word_count: int | None = Field(default=None, ge=0)
    deleted_word_count: int | None = Field(default=None, ge=0)
    chapter_count: int | None = Field(default=None, ge=0)
    active_minutes: int | None = Field(default=None, ge=0)


class ReadingEventCreate(BaseModel):
    """阅读事件上报（规格八）。"""
    event_type: str = Field(..., description="page_view/chapter_start/chapter_complete/favorite/unfavorite/comment/share")
    chapter_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    anonymous_id: str | None = None
    session_id: str | None = None
    occurred_at: datetime | None = None
    metadata: dict | None = None


class AIUsageRecordCreate(BaseModel):
    """AI 调用用量上报（规格十/十一）。

    阶段 1 token 口径最小修复（2026-10）：token 字段只接受供应商真实 usage。
    未取得真实 usage 时保持 None（表示"未知"），禁止用字符数折算填充；
    服务端对未知按"不折算成本"处理，不得静默当作真实 0 token 计费。
    """
    user_id: uuid.UUID
    provider: str = "default"
    model: str
    task_type: str
    success: bool = True
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = None
    failure_reason: str | None = None
    retry_count: int = Field(default=0, ge=0)
