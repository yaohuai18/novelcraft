# 章节计划（ChapterPlan）请求/响应模型
# 用途：把「当前章要写什么」从自由文本细纲升级为结构化计划，
# Context Builder 将其作为高优先级约束注入 Prompt，前端可编辑并预览。
import uuid
from typing import Literal

from pydantic import BaseModel, Field


class SceneTime(BaseModel):
    """章节场景时间（阶段F「场景合同」）。

    type 三选一：
      exact    -> 精确时间点（value 如「景历1024年秋」）
      range    -> 时间段（value 如「景历1024年秋至1025年春」）
      relative -> 相对时间（value 如「上章事件结束后的第三天」）
    """

    type: Literal["exact", "range", "relative"]
    value: str = Field(max_length=255)


class StateChangeItem(BaseModel):
    """单条预期状态变化（变化前 → 变化后）。"""

    target: str = Field(max_length=255)
    before: str | None = Field(default=None, max_length=2000)
    after: str = Field(max_length=2000)


class ExpectedStateChanges(BaseModel):
    """结构化预期状态变化：人物 / 地点 / 物品 / 秘密 四类。"""

    characters: list[StateChangeItem] = Field(default_factory=list, max_length=20)
    locations: list[StateChangeItem] = Field(default_factory=list, max_length=20)
    items: list[StateChangeItem] = Field(default_factory=list, max_length=20)
    secrets: list[StateChangeItem] = Field(default_factory=list, max_length=20)


class ChapterPlanData(BaseModel):
    """结构化章节计划（落库 JSON 结构，字段与 outlines.chapter_plan 一致）。

    title 为章节标题（章节标题单一事实来源是 outlines.title；
    此处仅保存用户编辑计划时自带的标题，用于比对与提示，不作为覆盖依据）。

    阶段F「场景合同」新增：
      sceneTime            -> 章节场景时间（exact/range/relative 三选一 + 值）；
      allowedReveals       -> 本章允许揭示的秘密/事件（forbiddenReveals 的豁免面）；
      expectedStateChanges -> 结构化预期状态变化（人物/地点/物品/秘密 变化前→变化后）。
    新字段均带默认值：旧客户端不传则保持默认（None/[]），不改变旧行为。
    """

    title: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=20000)
    sceneGoals: list[str] = Field(default_factory=list, max_length=30)
    requiredCharacters: list[str] = Field(default_factory=list, max_length=30)
    requiredLocations: list[str] = Field(default_factory=list, max_length=30)
    requiredEvents: list[str] = Field(default_factory=list, max_length=60)
    expectedOutcome: str | None = Field(default=None, max_length=20000)
    forbiddenReveals: list[str] = Field(default_factory=list, max_length=60)
    status: str = Field(default="draft", max_length=20)
    # 阶段F：场景合同扩展（新字段，旧数据缺失时回退默认）
    sceneTime: SceneTime | None = Field(default=None)
    allowedReveals: list[str] = Field(default_factory=list, max_length=60)
    expectedStateChanges: ExpectedStateChanges | None = Field(default=None)


class ChapterPlanResponse(BaseModel):
    """章节计划读取响应。

    plan_source:
      manual  -> 已保存的结构化计划（outlines.chapter_plan 非空）
      legacy  -> 旧数据回退（由 outlines.title/summary/content 构建的等价计划）
      none    -> 无计划（既无结构化计划，旧数据也无可回退内容）
    title_sync_required: 计划标题与章节表标题不一致时为 True，供前端提示并允许同步。
    """

    projectId: uuid.UUID
    chapterId: uuid.UUID
    chapterNumber: int
    title: str
    summary: str | None = None
    plan: ChapterPlanData | None = None
    planSource: str = "none"
    titleSyncRequired: bool = False


class ChapterPlanUpdateRequest(BaseModel):
    """保存章节计划请求体。

    sync_title: 为 true 时同时把计划标题同步到 outlines.title（显式用户确认后）；
                缺省/为 false 时仅保存计划，不改章节标题。
    """

    plan: ChapterPlanData
    syncTitle: bool = False


class PlanComplianceIssue(BaseModel):
    """计划符合度检查问题项（结构化输出）。"""

    severity: str  # error | warning | info
    type: str  # title_not_covered | event_missing | outcome_not_met | forbidden_reveal | advanced_plot | ok
    message: str
    evidence: str | None = None
    suggestion: str | None = None


class PlanComplianceResponse(BaseModel):
    """计划符合度检查响应（不修改正文，仅提示与按计划修订入口）。"""

    projectId: uuid.UUID
    chapterId: uuid.UUID
    chapterNumber: int
    title: str
    score: int
    issues: list[PlanComplianceIssue]
    planSummary: dict | None = None
    checkedAt: str
