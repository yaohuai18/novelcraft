# 阅读设置与阅读进度模型：reader_preferences / reading_progress 表（第十三批规格实现）
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReaderPreference(Base):
    """读者阅读偏好：只影响当前读者，不修改作品正文。"""

    __tablename__ = "reader_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )

    font_size: Mapped[int] = mapped_column(
        Integer,
        default=16,
        nullable=False
    )
    line_height: Mapped[float] = mapped_column(
        Float,
        default=1.6,
        nullable=False
    )
    content_width: Mapped[int] = mapped_column(
        Integer,
        default=720,
        nullable=False
    )
    theme: Mapped[str] = mapped_column(
        String(20),
        default="light",
        nullable=False
    )
    font_family: Mapped[str] = mapped_column(
        String(50),
        default="default",
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class ReadingProgress(Base):
    """阅读进度：记录用户在某作品某章节的滚动位置与已读状态。"""

    __tablename__ = "reading_progress"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "chapter_id",
            name="uq_reading_progress_user_project_chapter"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    position: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    completed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
