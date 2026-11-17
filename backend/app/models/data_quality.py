# 数据质量与管道治理模型（第二十二批「数据分析、推荐系统和智能运营」规格二十二）
# DataQualityMetric：监控指标（丢失率/重复率/缺失率/延迟/类型错误/ID不一致/统计异常）。
# AnalyticsDeadLetter：事件死信队列（校验失败/重试超限的事件，可回放）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

QUALITY_METRIC_TYPES = [
    "event_loss_rate", "event_duplicate_rate", "field_missing_rate",
    "event_time_delay", "type_error_rate", "cross_service_id_mismatch",
    "stat_anomaly",
]


class DataQualityMetric(Base):
    __tablename__ = "data_quality_metrics"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    metric_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime)
    period_end: Mapped[datetime | None] = mapped_column(DateTime)
    event_name: Mapped[str | None] = mapped_column(String(50), index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    extra: Mapped[str | None] = mapped_column(Text)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AnalyticsDeadLetter(Base):
    __tablename__ = "analytics_dead_letters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_name: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="dead", nullable=False, index=True)
    # dead / retried / replayed / dropped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
