# 伏笔管理接口 Schema（第四期「写错能发现」伏笔追踪 / 文26 P2）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

FORESHADOW_STATUSES = ["planned", "planted", "resolved", "abandoned"]
FORESHADOW_IMPORTANCE = ["high", "medium", "low"]


class ForeshadowBase(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(..., min_length=1, max_length=2000)
    importance: str = "medium"
    planted_chapter_id: uuid.UUID | None = None
    planned_payoff_chapter_id: uuid.UUID | None = None
    planned_payoff: str | None = None
    status: str = "planned"
    source: str = "manual"
    note: str | None = None
    resolved_chapter_id: uuid.UUID | None = None


class ForeshadowCreate(ForeshadowBase):
    pass


class ForeshadowUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    importance: str | None = None
    planted_chapter_id: uuid.UUID | None = None
    planned_payoff_chapter_id: uuid.UUID | None = None
    planned_payoff: str | None = None
    status: str | None = None
    note: str | None = None
    resolved_chapter_id: uuid.UUID | None = None
    paid_off_chapter_id: uuid.UUID | None = None
    paid_off_note: str | None = None


class ForeshadowResponse(ForeshadowBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    paid_off_chapter_id: uuid.UUID | None = None
    paid_off_note: str | None = None
    created_at: datetime
    updated_at: datetime


class ForeshadowImportRequest(BaseModel):
    """从章节记忆 / 规划大纲同步导入伏笔。"""

    chapter_ids: list[uuid.UUID] | None = None  # 为空则导入全部章节记忆中的伏笔
    mark_planted: bool = True  # 从章节记忆导入的伏笔视为已埋设（planted）
    replace_duplicates: bool = False  # 内容相同的伏笔是否覆盖更新（默认跳过）


class ForeshadowImportResult(BaseModel):
    imported: int = 0
    skipped_duplicates: int = 0
    items: list[ForeshadowResponse] = []
