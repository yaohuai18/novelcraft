# 章节正文相关 Pydantic 请求/响应模型（第七批规格实现 + 第一期「生成不丢失」扩展）
import json
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _load_json_text(value):
    """ORM 侧 JSON 列存的是文本，转为响应 dict；空串/NULL 返回 None。"""
    if isinstance(value, str):
        if not value:
            return None
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return {"raw": value}
    return value


class ChapterContentUpdate(BaseModel):
    content: str = Field(
        default="",
        max_length=500000
    )
    change_summary: str | None = Field(
        default=None,
        max_length=500
    )
    # 第一期「生成不丢失」：接受 AI 预览 / 手工保存 / 恢复版本时标注版本来源。
    # manual（手工保存）/ ai_draft / ai_continue / ai_rewrite / ai_polish / restore。
    source: Literal[
        "manual",
        "ai_draft",
        "ai_continue",
        "ai_rewrite",
        "ai_polish",
        "restore",
    ] = "manual"
    # AI 来源版本必须携带对应 generation_record_id（AI 返回后先落记录，接受时才建版本）。
    generation_record_id: uuid.UUID | None = Field(
        default=None,
        description="AI 生成记录 ID（source 为 ai_* 时提供）"
    )
    # 版本链：本版本从哪个版本演变而来（默认指向当前 is_current 版本）。
    parent_version_id: uuid.UUID | None = Field(
        default=None,
        description="父版本 ID；为空时自动取当前 is_current 版本"
    )
    # 第十五批：乐观锁。客户端读取版本号后在提交时携带 base_version，
    # 与当前版本不一致时服务端返回 409 冲突（conflict/current_version/submitted_base_version）。
    base_version: int | None = Field(
        default=None,
        ge=1,
        description="客户端基于的版本号；为空表示不校验（兼容旧行为）"
    )


class ConflictResponse(BaseModel):
    """乐观锁冲突响应（第十五批规格二十二）。"""
    conflict: bool = True
    current_version: int
    submitted_base_version: int


class ChapterContentResponse(BaseModel):
    id: uuid.UUID
    outline_id: uuid.UUID
    content: str
    word_count: int
    version: int
    updated_at: datetime
    validation_summary: dict | None = Field(
        default=None,
        description=(
            "保存链路附带的确定性事实校验摘要（第一批「事实数据库与规则校验」），"
            "第一阶段起真实落库（R4 修复）；null 表示尚未产生校验结果"
        ),
    )
    # ---------- 第一阶段「生命周期与可追溯元数据」新增响应字段 ----------
    lifecycle_status: str = Field(
        default="draft",
        description=(
            "章节生命周期：draft / generated / validation_failed / "
            "pending_review / approved / published"
        ),
    )
    generation_meta: dict | None = Field(
        default=None,
        description="生成元数据（模型/参数/生成时间/生成状态/generation_record_id）",
    )
    validation_report: dict | None = Field(
        default=None,
        description="确定性校验问题报告（fact_validator 问题清单 + needs_recheck 标记）",
    )
    repair_records: dict | None = Field(
        default=None,
        description="修复记录（第一阶段仅建字段，不实现自动修复）",
    )
    outline_version_id: uuid.UUID | None = Field(
        default=None,
        description="生成所依据的已确认大纲版本 ID",
    )
    facts_version: int = Field(
        default=0,
        description="故事事实版本号（每次事实重算 +1）",
    )
    validation_updated_at: datetime | None = Field(
        default=None,
        description="最近一次确定性校验时间（真实落库）",
    )

    @field_validator(
        "generation_meta",
        "validation_report",
        "repair_records",
        "validation_summary",
        mode="before",
    )
    @classmethod
    def _json_before(cls, v):
        return _load_json_text(v)

    model_config = {
        "from_attributes": True
    }


class ChapterVersionResponse(BaseModel):
    id: uuid.UUID
    outline_id: uuid.UUID
    version: int
    content: str
    word_count: int
    source: str
    generation_record_id: uuid.UUID | None
    parent_version_id: uuid.UUID | None
    is_current: bool
    change_summary: str | None
    created_at: datetime

    model_config = {
        "from_attributes": True
    }


class LifecycleResponse(BaseModel):
    """生命周期最小状态转换结果（approve / publish）。"""
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    lifecycle_status: str
    approved: bool = True
    blocking: int = 0
    message: str | None = None
