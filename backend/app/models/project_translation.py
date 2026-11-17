# 多语言作品模型（第二十一批「国际化、多语言和多区域部署」规格六）
# ProjectTranslation：作品副本（语言/标题/简介/状态）。
# ChapterTranslation：章节翻译（语言/标题/正文/翻译状态/源版本）。
# 翻译状态：not_started / draft / machine_translated / reviewing / published / outdated。
# 原文更新时，译文标记为 outdated，提示重新翻译或校对。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TRANSLATION_STATUSES = [
    "not_started",
    "draft",
    "machine_translated",
    "reviewing",
    "published",
    "outdated",
]


class ProjectTranslation(Base):
    __tablename__ = "project_translations"
    __table_args__ = (
        UniqueConstraint("project_id", "language", name="uq_project_translation"),
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

    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(30),
        default="not_started",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class ChapterTranslation(Base):
    __tablename__ = "chapter_translations"
    __table_args__ = (
        UniqueConstraint("chapter_id", "language", name="uq_chapter_translation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 章节大纲（outlines.id）
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    translation_status: Mapped[str] = mapped_column(
        String(30),
        default="not_started",
        nullable=False
    )

    # 源版本：对应 ChapterContent.id 与 version，用于原文更新检测与乐观锁
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chapter_contents.id", ondelete="SET NULL")
    )

    source_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 译文自身的版本号（乐观锁：客户端提交 base_version）
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
