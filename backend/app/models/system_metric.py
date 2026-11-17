# 系统监控采样模型：system_metrics 表（第十七批「统计分析与运营后台」规格十六）
# monitor_service 周期性采样并落库，用于趋势查询；
# 实时指标由进程内采样器聚合返回（请求量/响应时间/错误率/缓存命中率等）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SystemMetric(Base):
    __tablename__ = "system_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    metric_name: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        index=True
    )

    metric_value: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
