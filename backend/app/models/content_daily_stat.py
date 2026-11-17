# 内容每日汇总模型：content_daily_stats 表（第十七批「统计分析与运营后台」规格七）
# 长期聚合统计：按 (project_id, target_type, target_id, stat_date) 每日一条。
# target_type: project / chapter；target_id 为对应对象 id 字符串。
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONTENT_TARGET_TYPES = ["project", "chapter"]


class ContentDailyStat(Base):
    __tablename__ = "content_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "target_type",
            "target_id",
            "stat_date",
            name="uq_content_daily_project_type_target_date"
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
        nullable=False,
        index=True
    )

    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    stat_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True
    )

    view_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    unique_reader_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    favorite_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    comment_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    completion_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    average_read_seconds: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
