# 写作目标模型：writing_goals 表（第十七批「统计分析与运营后台」规格三）
# goal_type: daily_words / weekly_words / project_words / chapter_count / publish_count
# 第二十二批扩展：month_chapters / project_deadline / consecutive_days / publish_frequency
# period: daily / weekly / monthly / deadline / streak / frequency / period
# status: active / paused / completed / archived
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

GOAL_TYPES = [
    "daily_words",
    "weekly_words",
    "project_words",
    "chapter_count",
    "publish_count",
    "month_chapters",
    "project_deadline",
    "consecutive_days",
    "publish_frequency",
]

GOAL_PERIODS = ["daily", "weekly", "monthly", "deadline", "streak", "frequency", "period"]

GOAL_STATUSES = ["active", "paused", "completed", "archived"]


class WritingGoal(Base):
    __tablename__ = "writing_goals"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 第二十二批扩展：允许创建无项目维度的全局目标（每日字数/连续创作天数等）
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    goal_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    target_value: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    current_value: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    start_date: Mapped[date | None] = mapped_column(Date)

    end_date: Mapped[date | None] = mapped_column(Date)

    # 第二十二批新增：目标周期（daily/weekly/monthly/deadline/streak/frequency）
    period: Mapped[str | None] = mapped_column(String(20))

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
        index=True
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
