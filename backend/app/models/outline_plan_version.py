# 大纲计划版本（OutlinePlanVersion）
# 「已确认大纲版本化」改造核心模型：
#   每次 AI 生成或人工编辑目录大纲都会产生 draft 版本；用户「确认大纲」时把
#   当时完整目录与结构化章节计划持久化为 status=confirmed 版本快照，并记录
#   confirmedAt / confirmedBy。
# 版本状态机：draft → confirmed → superseded（唯一 active confirmed，旧版标记
# superseded 且不删除；草稿不得覆盖 confirmed 版本）。
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OutlinePlanVersion(Base):
    """大纲计划版本快照（目录 + 结构化章节计划）。"""

    __tablename__ = "outline_plan_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_opv_project_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        "id", primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        "project_id",
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 项目内自增版本号（v1 / v2 / ...），确认时在事务内取 max(version)+1
    version: Mapped[int] = mapped_column("version", Integer, nullable=False)
    # draft | confirmed | superseded
    status: Mapped[str] = mapped_column(
        "status", String(16), default="draft", nullable=False, index=True
    )
    # 完整目录+大纲快照：project 级信息 + volumes + chapters + scenes 树（JSON 文本）
    content_snapshot: Mapped[str | None] = mapped_column(
        "content_snapshot", Text, nullable=True
    )
    # 结构化章节计划快照：按 chapterId 为 key 的 ChapterPlan 映射（JSON 文本）
    chapter_plans_snapshot: Mapped[str | None] = mapped_column(
        "chapter_plans_snapshot", Text, nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        "confirmed_at", DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        "confirmed_by",
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at",
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
