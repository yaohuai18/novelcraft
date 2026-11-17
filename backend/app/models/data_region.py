# 数据区域模型（第二十一批「国际化、多语言和多区域部署」规格十五/十六）
# DataRegion：region_code / storage_endpoint / database_cluster / ai_provider / status。
# RegionAccessLog：区域化访问入口记录（用户请求 → 就近接入区域）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

REGION_STATUSES = ["active", "degraded", "failed", "standby"]


class DataRegion(Base):
    __tablename__ = "data_regions"

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

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    storage_endpoint: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    database_cluster: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    ai_provider: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    # 是否主数据区域
    is_main: Mapped[bool] = mapped_column(
        default=False,
        nullable=False
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


class RegionAccessLog(Base):
    __tablename__ = "region_access_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True
    )

    region_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    entry_route: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    request_ip_hash: Mapped[str | None] = mapped_column(String(64))

    user_agent: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
