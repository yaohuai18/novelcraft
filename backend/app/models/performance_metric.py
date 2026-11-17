# 性能监控指标模型（第十八批「性能优化、可靠性与安全加固」规格二）
# 记录每个请求的性能明细：接口/方法/状态/耗时/DB耗时/AI耗时/搜索耗时/
# 缓存命中/队列等待/慢SQL；聚合时计算 P50/P95/P99、最慢接口与最慢 SQL。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PerformanceMetric(Base):
    __tablename__ = "performance_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    endpoint: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        index=True
    )

    method: Mapped[str] = mapped_column(
        String(10),
        nullable=False
    )

    status_code: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    duration_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0
    )

    db_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0
    )

    ai_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0
    )

    search_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0
    )

    queue_wait_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0
    )

    cache_hit: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False
    )

    is_error: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False
    )

    is_timeout: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False
    )

    slow_sql: Mapped[str | None] = mapped_column(
        Text
    )

    user_id: Mapped[str | None] = mapped_column(
        String(36),
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
