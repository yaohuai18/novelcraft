# Webhook 模型（第十九批「移动端适配与开放 API」规格十八/十九）
# WebhookSubscription：第三方订阅（endpoint / events / secret / 状态 / 收发时间）。
# WebhookDelivery：每次发送尝试记录（事件 ID / 类型 / payload / 尝试次数 / 错误）。
# 发送头：X-Event-Id / X-Event-Type / X-Event-Timestamp / X-Event-Signature（HMAC-SHA256）。
# 失败策略：指数退避重试，超过最大重试次数后暂停订阅并记录失败原因。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

WEBHOOK_EVENTS = [
    "chapter.created",
    "chapter.updated",
    "chapter.published",
    "comment.created",
    "ai.task.completed",
    "ai.task.failed",
    "export.completed",
    "backup.completed",
]

WEBHOOK_STATUSES = ["active", "paused", "disabled"]


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    app_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("developer_apps.id", ondelete="CASCADE"),
        index=True,
        nullable=True
    )

    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)

    # 逗号分隔的事件类型列表
    events: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 签名密钥（仅创建时明文返回一次）
    secret: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)

    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 连续失败次数 / 暂停原因
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )

    event_id: Mapped[str] = mapped_column(String(64), nullable=False)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)

    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # pending / sent / failed
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    response_status: Mapped[int | None] = mapped_column(Integer)

    error_message: Mapped[str | None] = mapped_column(Text)

    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
