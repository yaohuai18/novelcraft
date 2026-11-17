# 跨区域同步事件模型（第二十一批「国际化、多语言和多区域部署」规格十七/十八）
# RegionEvent：所有跨区域事件包含 event_id / source_region / occurred_at /
#              entity_id / entity_version。
# 处理重复事件（幂等）与事件乱序（版本落后标记 stale/duplicate）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

REGION_EVENT_STATUSES = ["pending", "delivered", "failed", "duplicate", "stale"]


class RegionEvent(Base):
    __tablename__ = "region_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_region_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    event_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    source_region: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    entity_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        index=True
    )

    entity_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    entity_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    payload: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
