# 变更建议 Schema（第三期「变更确认流程」核心）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

SUGGESTION_CATEGORIES = ["character", "relationship", "timeline", "foreshadow"]
SUGGESTION_STATUS = ["pending", "confirmed", "modified", "ignored"]


class SuggestionListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    category: str
    character_id: uuid.UUID | None = None
    target_character_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    payload: dict
    status: str
    created_at: datetime | None = None
    confirmed_at: datetime | None = None


class SuggestionConfirmRequest(BaseModel):
    """确认变更建议请求体：action=confirm|ignore；修改确认时传 payload 覆盖。"""

    action: str = "confirm"
    payload: dict | None = None


class SuggestionCreateRequest(BaseModel):
    """新建变更建议请求体（第四期「采纳建议」入口：一致性检查 proposed_updates 转变更建议）。

    category 兼容一致性检查输出：character_state|relationship|timeline|foreshadow，
    存储时 character_state 归一化为 character。
    """

    chapter_id: uuid.UUID
    category: str = "character_state"
    title: str = "变更建议"
    description: str | None = None
    payload: dict = {}
