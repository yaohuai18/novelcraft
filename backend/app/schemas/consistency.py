# 一致性检查接口 Schema（第四期「写错能发现」）
import uuid

from pydantic import BaseModel, Field


class ConsistencyIssue(BaseModel):
    severity: str = "warning"  # warning | error
    type: str = "character_state_conflict"
    location: str = ""
    message: str = ""
    suggestion: str = ""
    data: dict = {}


class ConsistencyUpdate(BaseModel):
    category: str = "character_state"  # character_state | relationship | timeline | foreshadow
    title: str = ""
    description: str = ""
    payload: dict = {}


class ConsistencyReminder(BaseModel):
    type: str = "foreshadow_open"  # foreshadow_due | foreshadow_open
    content: str = ""
    planned_payoff: str = ""
    status: str = "planted"
    message: str = ""
    suggestion: str = ""
    id: str | None = None


class OutlineCompletion(BaseModel):
    completed: list[str] = []
    uncovered: list[str] = []
    score: int = 0


class RepetitionReport(BaseModel):
    internal: list[ConsistencyIssue] = []
    against_previous_summary: list[ConsistencyIssue] = []


class StyleReport(BaseModel):
    score: int = 0
    evaluation: str = ""


class CheckConsistencyRequest(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    content: str = Field(..., min_length=1)
    chapter_title: str = ""
    outline_text: str | None = None  # 可选：当前章细纲文本（未传时读 outlines 表）


class CheckConsistencyResponse(BaseModel):
    mode: str = "mock"
    score: int = 0
    issues: list[ConsistencyIssue] = []
    confirmed_updates: list[ConsistencyUpdate] = []
    proposed_updates: list[ConsistencyUpdate] = []
    reminders: list[ConsistencyReminder] = []
    outline: OutlineCompletion = OutlineCompletion()
    repetition: RepetitionReport = RepetitionReport()
    style: StyleReport = StyleReport()
    checked_at: str = ""


class ConsistencyCheckRecord(BaseModel):
    id: str
    chapter_id: str
    chapter_title: str = ""
    action_type: str = "check"  # check=AI 正文检查 | consistency=静态数据层检查
    status: str = "succeeded"
    result: dict = {}
    created_at: str = ""
    dimension: str = ""  # 静态检查维度（consistency 记录专用）
    summary: dict = {}   # 静态检查汇总（consistency 记录专用）


# ---------- 文26 P4 静态一致性检查（数据层） ----------
# 维度：all | dangling_refs | character_state | relationships | foreshadows | appearance | memory
DATA_CONSISTENCY_DIMENSIONS = [
    "all",
    "dangling_refs",      # 外键悬空：引用不存在的章节 / 生成记录
    "character_state",    # 角色状态快照与当前资料冲突
    "relationships",      # 关系状态与章节区间矛盾
    "foreshadows",        # 伏笔 planted 长期未 resolve / 无回收计划
    "appearance",         # 角色出场章节矛盾 / 最后出现章节滞后 / 重复别名
    "memory",             # 章节记忆与生成记录引用悬空
]


class DataConsistencyIssue(BaseModel):
    id: str = ""
    dimension: str = "dangling_refs"
    type: str = "dangling_chapter_ref"
    severity: str = "warning"  # error | warning | info
    location: str = ""         # 实体标识，如 角色:陈九 / 伏笔:xxx / 关系:陈九-向晚
    message: str = ""
    suggestion: str = ""
    related_ids: list[str] = []  # 与问题相关的实体 id（角色/伏笔/章节等）


class DataConsistencySummary(BaseModel):
    total: int = 0
    errors: int = 0
    warnings: int = 0
    info: int = 0
    by_type: dict = {}


class DataConsistencyRunResponse(BaseModel):
    check_id: str = ""
    project_id: str = ""
    dimension: str = "all"
    status: str = "succeeded"
    mode: str = "static"
    summary: DataConsistencySummary = DataConsistencySummary()
    issues: list[DataConsistencyIssue] = []
    created_at: str = ""


class DataConsistencyCheckRequest(BaseModel):
    dimension: str = "all"
