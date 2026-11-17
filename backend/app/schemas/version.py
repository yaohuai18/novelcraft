# 版本管理与差异对比 Pydantic 请求/响应模型（第十二批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ============ 版本创建 / 列表 / 详情 ============

class VersionCreate(BaseModel):
    """创建版本请求（规格四：change_summary / source）。

    is_draft=True 表示自动保存草稿（status=draft，不出现在主要版本列表）；
    is_draft=False 表示正式版本（status=stable）。
    created_by 由服务端根据当前登录用户生成，不接受客户端传入。
    """

    change_summary: str | None = Field(
        default=None,
        max_length=500
    )
    source: str = Field(
        default="manual",
        description="manual / ai / import / restore"
    )
    is_draft: bool = False


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    version_number: int
    title_snapshot: str | None
    content_snapshot: str
    metadata_snapshot: str | None
    change_type: str
    change_summary: str | None
    source: str
    status: str
    created_by: uuid.UUID
    generation_id: uuid.UUID | None
    parent_version_id: uuid.UUID | None
    candidate_content: str | None
    accepted_at: datetime | None
    rejected_at: datetime | None
    locked: bool
    created_at: datetime


class VersionListResponse(BaseModel):
    items: list[VersionResponse]
    total: int
    page: int
    page_size: int


# ============ 差异对比 ============

class DiffRequest(BaseModel):
    """对比两个版本的差异。"""

    base_version_id: uuid.UUID
    compare_version_id: uuid.UUID


class ParagraphDiffBlock(BaseModel):
    """正文段落级差异块（规格六）。"""

    type: str  # added / removed / modified / unchanged
    text: str
    new_text: str | None = None  # modified 时旧文本的新版本


class ParagraphDiffResponse(BaseModel):
    """正文段落级差异（规格六：source_version / target_version / blocks）。"""

    diff_type: str = "paragraph"
    source_version: int
    target_version: int
    blocks: list[ParagraphDiffBlock]


class OutlineFieldChange(BaseModel):
    """单个字段变化（大纲差异）。"""

    field: str
    old: str | None = None
    new: str | None = None


class OutlineStructureChange(OutlineFieldChange):
    """结构变化：父级调整 / 顺序调整 / 类型修改。"""

    change: str  # parent_changed / order_changed / type_changed


class OutlineContentChange(OutlineFieldChange):
    """内容变化（正文 / 摘要）。"""

    pass


class OutlineDiffResponse(BaseModel):
    """大纲结构差异（规格七：structure_changes / field_changes / content_changes）。"""

    diff_type: str = "outline"
    source_version: int
    target_version: int
    structure_changes: list[OutlineStructureChange] = Field(default_factory=list)
    field_changes: list[OutlineFieldChange] = Field(default_factory=list)
    content_changes: list[OutlineContentChange] = Field(default_factory=list)


class KnowledgeDiffResponse(BaseModel):
    """知识库条目差异（字段级对比）。"""

    diff_type: str = "knowledge"
    source_version: int
    target_version: int
    field_changes: list[OutlineFieldChange] = Field(default_factory=list)


# ============ 版本恢复 ============

class RestoreResponse(BaseModel):
    """恢复结果（规格九）。"""

    restored: bool
    new_version_id: uuid.UUID
    restored_from: uuid.UUID
    backup_version_id: uuid.UUID


# ============ AI 候选版本 ============

class AICandidateCreate(BaseModel):
    """创建 AI 候选版本（AI 生成结果不直接覆盖正文，先保留为候选版本）。"""

    generation_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    candidate_content: str = Field(min_length=1, description="AI 生成结果全文")
    change_summary: str | None = Field(
        default=None,
        max_length=500
    )


class AICandidateActionResponse(BaseModel):
    """AI 候选操作结果。"""

    status: str
    version_id: uuid.UUID
    accepted_at: datetime | None = None
    rejected_at: datetime | None = None
    new_version_id: uuid.UUID | None = None
    current_version: int | None = None
    message: str | None = None


class AIInsertRequest(BaseModel):
    """插入到当前位置：把候选内容插入到正文第 anchor_index 段（0-based）前/后。"""

    anchor_index: int = Field(ge=0)
    position: str = Field(
        default="after",
        description="before / after"
    )


class AIApplyBlockDecision(BaseModel):
    """逐段采纳：针对目标正文第 paragraph_index 段（0-based）的选择。"""

    paragraph_index: int = Field(ge=0)
    decision: str = Field(
        description="ai=采用 AI 内容 / keep=保留原内容 / edit=手动编辑"
    )
    text: str | None = None  # decision=edit 时的手动编辑内容


class AIApplyBlocksRequest(BaseModel):
    """逐段采纳请求（规格十）。"""

    decisions: list[AIApplyBlockDecision] = Field(
        min_length=1,
        max_length=500
    )


# ============ 版本锁定 ============

class LockRequest(BaseModel):
    locked: bool = True


class LockResponse(BaseModel):
    target_type: str
    target_id: uuid.UUID
    locked: bool
    version_id: uuid.UUID


# ============ 版本清理策略 ============

class VersionCleanupRequest(BaseModel):
    """清理规则（规格十五）：保留所有正式版本；草稿与 AI 候选各保留最近 N 条。"""

    keep_drafts: int = Field(
        default=30,
        ge=0,
        le=500
    )
    keep_ai_candidates: int = Field(
        default=10,
        ge=0,
        le=500
    )


class VersionCleanupResponse(BaseModel):
    """清理结果。"""

    deleted_drafts: int = 0
    deleted_ai_candidates: int = 0
    kept_stable: int = 0
    kept_drafts: int = 0
    kept_ai_candidates: int = 0
