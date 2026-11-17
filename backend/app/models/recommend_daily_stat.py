# 推荐效果监控统计模型（第二十二批「数据分析、推荐系统和智能运营」规格二十三）
# 按日（可选按地区）汇总：点击率/完读率/多样性/重复曝光率/屏蔽率/投诉率/响应时间/错误率。
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RecommendDailyStat(Base):
    __tablename__ = "recommend_daily_stats"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scene: Mapped[str] = mapped_column(String(30), default="homepage", nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(20), index=True)

    exposure_count: Mapped[int] = mapped_column(default=0, nullable=False)
    click_count: Mapped[int] = mapped_column(default=0, nullable=False)
    complete_count: Mapped[int] = mapped_column(default=0, nullable=False)
    hide_count: Mapped[int] = mapped_column(default=0, nullable=False)
    block_count: Mapped[int] = mapped_column(default=0, nullable=False)
    report_count: Mapped[int] = mapped_column(default=0, nullable=False)
    repeat_exposure_count: Mapped[int] = mapped_column(default=0, nullable=False)
    unique_item_count: Mapped[int] = mapped_column(default=0, nullable=False)
    total_response_ms: Mapped[int] = mapped_column(default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(default=0, nullable=False)

    click_rate: Mapped[float] = mapped_column(Float, default=0.0)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    diversity: Mapped[float] = mapped_column(Float, default=0.0)
    repeat_exposure_rate: Mapped[float] = mapped_column(Float, default=0.0)
    hide_rate: Mapped[float] = mapped_column(Float, default=0.0)
    report_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_response_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error_rate: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
