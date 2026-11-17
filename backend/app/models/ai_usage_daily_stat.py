# AI 使用统计模型（第十七批「统计分析与运营后台」规格十/十一）
# AIUsageDailyStat：每日聚合的 AI 调用统计（按 项目+用户+供应商+模型+任务类型 每日一条）。
# AIFailureStat：AI 任务失败原因统计（超时/限频/上下文过长/服务异常/审核拒绝/余额不足/参数错误）。
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

AI_FAILURE_REASONS = [
    "timeout",               # 超时
    "rate_limit",            # 请求频率过高
    "context_length",        # 上下文过长
    "service_error",         # 模型服务异常
    "content_review",        # 内容审核拒绝
    "insufficient_balance",  # 余额不足
    "invalid_parameter",     # 参数错误
]


class AIUsageDailyStat(Base):
    __tablename__ = "ai_usage_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "provider",
            "model",
            "task_type",
            "stat_date",
            name="uq_ai_usage_daily_dim_date"
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

    provider: Mapped[str] = mapped_column(
        String(50),
        default="openai",
        nullable=False
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True
    )

    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    success_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    estimated_cost: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False
    )

    stat_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class AIFailureStat(Base):
    __tablename__ = "ai_failure_stats"
    __table_args__ = (
        UniqueConstraint(
            "stat_date",
            "project_id",
            "model",
            "task_type",
            "failure_reason",
            name="uq_ai_failure_dim_date"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    stat_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True
    )

    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    failure_reason: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
