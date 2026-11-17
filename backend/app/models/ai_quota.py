# AI 额度模型（第十八批「性能优化、可靠性与安全加固」规格十二）
# AI 成本保护：每日调用次数 / 项目预算 / 用户预算 / 并发限制
# 提交 AI 任务前检查：用户权限、剩余额度、内容长度、模型可用性、项目状态
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIQuota(Base):
    __tablename__ = "ai_quotas"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    period_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False
    )

    daily_limit: Mapped[int] = mapped_column(
        Integer,
        default=50,
        nullable=False
    )

    daily_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    project_budget: Mapped[int | None] = mapped_column(
        Integer
    )

    project_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    user_budget: Mapped[int | None] = mapped_column(
        Integer
    )

    user_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    concurrency_limit: Mapped[int] = mapped_column(
        Integer,
        default=3,
        nullable=False
    )

    concurrency_active: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", "period_date",
            name="uq_ai_quota_dim",
        ),
    )
