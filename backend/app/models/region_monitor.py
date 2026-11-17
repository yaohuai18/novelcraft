# 区域化日志监控与容灾模型（第二十一批「国际化、多语言和多区域部署」规格二十三/二十四）
# RegionMetric：按区域记录请求量/响应时间/错误率/缓存命中率等。
# RegionStatus：区域状态检测与故障切换（切换遵守数据驻留规则）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

REGION_HEALTH_STATUSES = ["healthy", "degraded", "failed", "maintenance"]


class RegionMetric(Base):
    __tablename__ = "region_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    region_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    # 指标窗口开始时间（UTC）
    metric_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    error_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    total_response_ms: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    error_rate: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False
    )

    cache_hit_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    cache_miss_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    ai_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )


class RegionStatus(Base):
    __tablename__ = "region_status"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    region_code: Mapped[str] = mapped_column(
        String(16),
        unique=True,
        nullable=False,
        index=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="healthy",
        nullable=False
    )

    detected_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    reason: Mapped[str | None] = mapped_column(Text)

    # 故障切换目标区域
    failover_to: Mapped[str | None] = mapped_column(String(16))

    # 是否因数据驻留限制无法切换
    data_resident_blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
