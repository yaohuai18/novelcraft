# 订阅与商业化事件模型（第二十批「商业化、订阅与计费系统」规格四/十九）
# Subscription：用户/组织的套餐订阅，状态机 trialing/active/past_due/paused/cancelled/expired。
# BillingEvent：订阅与支付事件日志（subscription.created/renewed/upgraded/downgraded/
#   cancelled/expired、payment.succeeded/failed/refunded、quota.granted/exhausted）。
import json
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SUBSCRIPTION_STATUSES = [
    "trialing",
    "active",
    "past_due",
    "paused",
    "cancelled",
    "expired",
]

BILLING_EVENT_TYPES = [
    "subscription.created",
    "subscription.renewed",
    "subscription.upgraded",
    "subscription.downgraded",
    "subscription.cancelled",
    "subscription.expired",
    "payment.succeeded",
    "payment.failed",
    "payment.refunded",
    "quota.granted",
    "quota.exhausted",
]


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id"),
        nullable=False
    )

    provider: Mapped[str] = mapped_column(
        String(30),
        default="local_stub",
        nullable=False
    )

    provider_subscription_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(
        String(20),
        default="trialing",
        nullable=False,
        index=True
    )

    current_period_start: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    current_period_end: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)

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


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True
    )

    event_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    # JSON 字符串 payload
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    def payload_dict(self) -> dict:
        try:
            return json.loads(self.payload or "{}")
        except (ValueError, TypeError):
            return {}
