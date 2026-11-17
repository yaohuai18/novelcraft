# 限流记录模型（第十八批「性能优化、可靠性与安全加固」规格十一）
# 固定窗口计数：维度(dimension, dimension_value) + 作用域(scope) + 窗口(period_start/period_end)
# 维度：user_id / ip / device / project_id / api_key / global
# 覆盖：login / register / password_reset / comment / ai / import_export / search / upload / email
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RateLimitRecord(Base):
    __tablename__ = "rate_limit_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    dimension: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    dimension_value: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True
    )

    scope: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    period_start: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    period_end: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "dimension", "dimension_value", "scope", "period_start",
            name="uq_rate_limit_window",
        ),
    )
