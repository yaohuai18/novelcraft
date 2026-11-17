# 事实数据库与规则校验接口模型（第一批「事实数据库与规则校验」第五节）
# 两个只读接口：
#   POST /api/projects/{pid}/chapters/{oid}/validate → FactValidationResponse
#   GET  /api/projects/{pid}/facts                 → FactViewResponse
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FactIssueModel(BaseModel):
    """单条校验问题（确定性规则输出）。"""
    dimension: str = Field(description="timeline|state|possession|secret|chapter_meta")
    rule: str = Field(description="规则编码，如 timeline.age / state.death_reappear")
    severity: str = Field(description="blocking|warning|info")
    chapter_id: uuid.UUID | None = None
    character_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    message: str
    suggestion: str | None = None
    related_ids: list[uuid.UUID] = Field(default_factory=list)
    # 阶段B「人工处置与误报闭环」
    issue_key: str = Field(
        default="", description="问题指纹（同项目下同规则同实体同消息共用）"
    )
    disposition: str | None = Field(
        default=None,
        description="confirmed|false_positive|accepted_exception|fixed|deferred|wont_fix",
    )
    exempted: bool = Field(
        default=False, description="accepted_exception 豁免命中（不计入 blocking）"
    )


class FactValidationSummary(BaseModel):
    """校验摘要：valid 仅由 blocking 决定；不阻断生成/保存，只做记录与提示。"""
    valid: bool
    counts: dict[str, int] = Field(
        description="{'blocking': N, 'warning': N, 'info': N}"
    )
    dimensions: dict[str, dict[str, int]] = Field(
        description="按维度分组的严重级别计数"
    )
    checked_at: datetime


class FactValidationResponse(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    issues: list[FactIssueModel] = Field(default_factory=list)
    summary: FactValidationSummary


class FactItemView(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    first_chapter_id: uuid.UUID | None = None
    status: str = "active"
    # 阶段A「事实权威与锁定」
    authority_level: str = "normal"
    confidence: float | None = None
    locked: bool = False
    source_record_id: str | None = None


class FactPossessionView(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    owner_character_id: uuid.UUID
    since_chapter_id: uuid.UUID | None = None
    until_chapter_id: uuid.UUID | None = None
    status: str = "active"
    # 阶段A「事实权威与锁定」
    authority_level: str = "normal"
    confidence: float | None = None
    locked: bool = False
    source_record_id: str | None = None


class FactSecretKnowerView(BaseModel):
    id: uuid.UUID
    secret_id: uuid.UUID
    character_id: uuid.UUID
    learned_chapter_id: uuid.UUID | None = None
    source: str = "ai_extracted"
    # 阶段A「事实权威与锁定」
    authority_level: str = "normal"
    confidence: float | None = None
    locked: bool = False
    source_record_id: str | None = None


class FactSnapshotView(BaseModel):
    id: uuid.UUID
    character_id: uuid.UUID
    chapter_id: uuid.UUID
    age: int | None = None
    birth_year: int | None = None
    is_alive: bool | None = None
    location_id: uuid.UUID | None = None
    possessions: list[str] = Field(default_factory=list)
    known_secrets: list[str] = Field(default_factory=list)
    injuries: list[str] = Field(default_factory=list)
    status: str = "draft"
    # 阶段A「事实权威与锁定」
    authority_level: str = "normal"
    confidence: float | None = None
    locked: bool = False
    source_record_id: str | None = None


class FactTimelineEventView(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    event_date_label: str | None = None
    sequence_order: int | None = None
    absolute_time: str | None = None
    relative_time: str | None = None
    location_id: uuid.UUID | None = None
    participants: list[str] = Field(default_factory=list)
    status: str = "draft"
    # 阶段A「事实权威与锁定」
    authority_level: str = "normal"
    confidence: float | None = None
    locked: bool = False
    source_record_id: str | None = None


class FactViewResponse(BaseModel):
    """项目级事实视图（只读，便于人工核查与前端展示）。"""
    project_id: uuid.UUID
    items: list[FactItemView] = Field(default_factory=list)
    possessions: list[FactPossessionView] = Field(default_factory=list)
    secret_knowers: list[FactSecretKnowerView] = Field(default_factory=list)
    snapshots: list[FactSnapshotView] = Field(default_factory=list)
    timeline_events: list[FactTimelineEventView] = Field(default_factory=list)

    model_config = {
        "extra": "forbid",
    }


def to_fact_issue_model(issue: dict[str, Any]) -> FactIssueModel:
    """把 fact_validator.FactIssue.to_dict() 转为接口模型（uuid 解析失败置 None）。"""
    def _uuid(value: Any) -> uuid.UUID | None:
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except Exception:  # noqa: BLE001
            return None

    return FactIssueModel(
        dimension=issue["dimension"],
        rule=issue["rule"],
        severity=issue["severity"],
        chapter_id=_uuid(issue.get("chapter_id")),
        character_id=_uuid(issue.get("character_id")),
        item_id=_uuid(issue.get("item_id")),
        location_id=_uuid(issue.get("location_id")),
        message=issue["message"],
        suggestion=issue.get("suggestion"),
        related_ids=[u for v in issue.get("related_ids", []) if (u := _uuid(v))],
        issue_key=issue.get("issue_key", ""),
        disposition=issue.get("disposition"),
        exempted=bool(issue.get("exempted", False)),
    )


# ---------- 阶段A「事实权威与锁定」：事实锁定写接口模型 ----------
_FACT_TYPES = ("snapshot", "timeline_event", "item", "possession", "secret_knower")
_AUTHORITY_LEVELS = ("low", "normal", "high")


class FactLockRequest(BaseModel):
    """事实锁定写接口请求。

    fact_type: snapshot|timeline_event|item|possession|secret_knower
    locked: True 锁定（重算跳过、不被自动提取覆盖）/ False 解锁
    authority_level / confidence / source_record_id 可选传入，传则同时更新。
    """
    fact_type: str = Field(
        description="snapshot|timeline_event|item|possession|secret_knower"
    )
    fact_id: uuid.UUID
    locked: bool
    authority_level: str | None = Field(
        default=None, description="low|normal|high，传则更新"
    )
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="0-1，传则更新"
    )
    source_record_id: str | None = Field(
        default=None, description="来源记录 ID，传则更新"
    )


class FactLockResponse(BaseModel):
    """事实锁定写接口响应（回读落库后的权威信息）。"""
    fact_type: str
    fact_id: uuid.UUID
    locked: bool
    authority_level: str
    confidence: float | None = None
    source_record_id: str | None = None


# ---------- 阶段B「人工处置与误报闭环」（补充5）----------
_DISPOSITION_TYPES = (
    "confirmed",
    "false_positive",
    "accepted_exception",
    "fixed",
    "deferred",
    "wont_fix",
)


class FactDispositionRequest(BaseModel):
    """人工审稿问题处置写接口请求。

    issue_key：校验问题指纹（validate 返回的 issue.issue_key，同项目下
    同规则同实体同消息共用），幂等更新该项目下该问题的处置状态；
    disposition：confirmed / false_positive / accepted_exception /
      fixed / deferred / wont_fix；
    accepted_exception 需携带触发条件（entity_type+entity_id / rule / 范围
      至少一项），写入豁免白名单，后续回归校验命中则标记 exempted 不报错。
    """
    issue_key: str = Field(description="校验问题指纹（validate 返回的 issue_key）")
    disposition: str = Field(
        description="confirmed|false_positive|accepted_exception|fixed|deferred|wont_fix"
    )
    note: str | None = Field(default=None, description="人工备注")
    entity_type: str | None = Field(
        default=None, description="豁免触发条件：实体类型（可选）"
    )
    entity_id: uuid.UUID | None = Field(
        default=None, description="豁免触发条件：实体 ID（可选）"
    )
    rule: str | None = Field(
        default=None, description="豁免触发条件：规则编码（可选）"
    )


class FactDispositionResponse(BaseModel):
    """处置写接口响应（回读落库后的处置记录）。"""
    issue_key: str
    disposition: str
    note: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    rule: str | None = None
    set_by: str | None = None
    set_at: datetime | None = None
    exempted: bool = Field(
        default=False, description="accepted_exception 且豁免开关开启时 True"
    )
