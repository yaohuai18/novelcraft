# 大纲计划版本（OutlinePlanVersion）schema
# 用于「确认大纲」后端接口、版本列表 / 详情查询、draft 同步与确认影响面预览。
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def to_camel(s: str) -> str:
    """snake_case -> camelCase（仅作为 JSON 字段 alias，兼容前端约定）。"""
    parts = s.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class OutlinePlanVersionResponse(BaseModel):
    """版本概要（列表 / 状态展示用，不含快照正文）。"""

    model_config = ConfigDict(
        from_attributes=True, protected_namespaces=(), populate_by_name=True,
        alias_generator=to_camel,
    )

    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: str  # draft | confirmed | superseded
    confirmed_at: datetime | None
    confirmed_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    content_summary: dict[str, Any] | None = None


class OutlinePlanVersionDetailResponse(OutlinePlanVersionResponse):
    """版本详情：含完整 content_snapshot 与 chapter_plans_snapshot（解析后）。"""

    content_snapshot: dict[str, Any] | None = None
    chapter_plans_snapshot: dict[str, Any] | None = None


class OutlinePlanStatusResponse(BaseModel):
    """项目大纲版本化状态（预览页 / 章节编辑页展示用）。"""

    model_config = ConfigDict(
        from_attributes=True, protected_namespaces=(), populate_by_name=True,
        alias_generator=to_camel,
    )

    project_id: uuid.UUID
    has_confirmed: bool
    active_confirmed: OutlinePlanVersionResponse | None = None
    latest_draft: OutlinePlanVersionResponse | None = None
    has_unconfirmed_changes: bool = False
    version_count: int = 0


class ConfirmPreviewItem(BaseModel):
    """确认影响面中的单章条目。"""

    model_config = ConfigDict(
        from_attributes=True, protected_namespaces=(), populate_by_name=True,
        alias_generator=to_camel,
    )

    chapter_id: uuid.UUID
    chapter_number: int
    plan_title: str | None = None
    outline_title: str | None = None
    title_conflict: bool = False
    title_changed: bool = False
    has_written_content: bool = False
    following_plan: bool = False


class ConfirmOutlinePlanRequest(BaseModel):
    """确认大纲请求。dry_run=True 仅返回影响面预览，不落库。"""

    dry_run: bool = Field(default=False)
    note: str | None = None


class ConfirmOutlinePlanResponse(BaseModel):
    """确认大纲响应：确认后的 active confirmed 版本 + 影响面说明。"""

    model_config = ConfigDict(
        from_attributes=True, protected_namespaces=(), populate_by_name=True,
        alias_generator=to_camel,
    )

    version: OutlinePlanVersionDetailResponse
    superseded_version: uuid.UUID | None = None
    impact: list[ConfirmPreviewItem] = Field(default_factory=list)
    title_conflicts: list[ConfirmPreviewItem] = Field(default_factory=list)
    written_chapters_affected: list[ConfirmPreviewItem] = Field(
        default_factory=list
    )
    dry_run: bool = False
    unchanged: bool = False
