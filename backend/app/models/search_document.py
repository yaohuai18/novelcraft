# 搜索索引模型：search_documents 表（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# 统一搜索索引表：避免搜索时同时查询多张业务表。
# target_type 支持 outline / chapter_content / tag / character / world_setting / project。
# 第十四批增强：summary/tags_text/status/visibility/volume_title/deleted_at，支持权限与软删除过滤。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 支持的索引目标类型
SEARCH_TARGET_TYPES = [
    "outline",
    "chapter_content",
    "tag",
    "character",
    "world_setting",
    "project",
]


class SearchDocument(Base):
    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "target_type",
            "target_id",
            name="uq_search_doc_project_type_target"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    plain_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 摘要/简介字段：供摘要匹配与优先级加权（第十四批）
    summary: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 标签文本：该目标已关联标签名称的拼接文本，支持标签匹配（第十四批）
    tags_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 内容状态与可见性（第十四批）：对应业务状态，供权限与发布过滤
    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )

    visibility: Mapped[str] = mapped_column(
        String(30),
        default="private",
        nullable=False
    )

    # 所属分卷标题（章节/大纲条目用）
    volume_title: Mapped[str | None] = mapped_column(String(255))

    # 软删除标记（第十四批）：搜索查询必须排除 deleted_at IS NULL
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
