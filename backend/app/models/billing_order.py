# 订单与发票模型（第二十批「商业化、订阅与计费系统」规格六/十五）
# Order：唯一订单号 order_no、服务端计算金额、支付渠道、退款状态。
# Invoice：面向企业用户的发票抬头/税务信息/账单地址/账单邮箱。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ORDER_TYPES = [
    "subscription",
    "ai_tokens",
    "storage",
    "team_seat",
    "upgrade",
]

ORDER_STATUSES = [
    "pending",
    "processing",
    "paid",
    "failed",
    "cancelled",
    "refunded",
    "partially_refunded",
]

INVOICE_STATUSES = ["issued", "paid", "refunded"]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 唯一订单号（服务端生成，防重复创建）
    order_no: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        index=True
    )

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL")
    )

    order_type: Mapped[str] = mapped_column(
        String(20),
        default="subscription",
        nullable=False
    )

    # 金额由服务端根据套餐/优惠/补差计算，不信任客户端
    amount: Mapped[float] = mapped_column(
        Numeric(12, 2),
        nullable=False
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    payment_provider: Mapped[str] = mapped_column(
        String(30),
        default="local_stub",
        nullable=False
    )

    provider_order_id: Mapped[str | None] = mapped_column(String(128))

    # 支付回调事件幂等键（同一事件只处理一次）
    provider_event_id: Mapped[str | None] = mapped_column(String(128))

    promotion_applied: Mapped[str | None] = mapped_column(String(128))

    description: Mapped[str | None] = mapped_column(Text)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime)

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


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )

    # 发票抬头
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # 税务信息
    tax_no: Mapped[str | None] = mapped_column(String(100))

    # 账单地址
    address: Mapped[str | None] = mapped_column(String(300))

    # 账单邮箱
    email: Mapped[str | None] = mapped_column(String(200))

    amount: Mapped[float] = mapped_column(
        Numeric(12, 2),
        nullable=False
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="issued",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
