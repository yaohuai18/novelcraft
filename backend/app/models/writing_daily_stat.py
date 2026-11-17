# 写作每日快照模型：writing_daily_stats 表（第十七批「统计分析与运营后台」规格四）
# 每日保存一次聚合快照，避免每次查询都扫描全部正文。
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WritingDailyStat(Base):
    __tablename__ = "writing_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "stat_date",
            name="uq_writing_daily_user_project_date"
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

    stat_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True
    )

    word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    new_word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    edited_word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    deleted_word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    chapter_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    active_minutes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
