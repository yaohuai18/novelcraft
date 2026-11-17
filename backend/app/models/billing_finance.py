# 财务/风控模型（第二十批「商业化、订阅与计费系统」规格十五/十六/二十一）
# LedgerEntry：不可修改的账务流水（收入/支出/退款/额度赠送），可追溯、可对账。
# Reconciliation：每日/每月对账记录，含差异明细与处理轨迹。
# RiskEvent：风控事件（批量注册/试用滥用/支付失败/异常退款/额度快速消耗/优惠滥用）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

LEDGER_ENTRY_TYPES = [
    "payment",
    "refund",
    "quota_grant",
    "quota_adjust",
    "fee",
    "adjustment",
]

LEDGER_DIRECTIONS = ["in", "out"]

RECONCILIATION_STATUSES = [
    "pending",
    "matched",
    "unmatched",
    "manual_review",
    "resolved",
]

RISK_SEVERITIES = ["low", "medium", "high"]


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    entry_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL")
    )

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )

    amount: Mapped[float] = mapped_column(
        Numeric(12, 2),
        nullable=False
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False
    )

    direction: Mapped[str] = mapped_column(
        String(10),
        default="in",
        nullable=False
    )

    # 外部参考号（渠道交易号等）
    reference_id: Mapped[str | None] = mapped_column(String(128))

    operator_id: Mapped[uuid.UUID | None] = mapped_column()

    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class Reconciliation(Base):
    __tablename__ = "reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 对账日期（YYYY-MM-DD）
    recon_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    platform_total: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False
    )

    provider_total: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False
    )

    diff_amount: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False
    )

    # 差异类型：amount_diff / missing_order / extra_provider / refund_diff
    diff_type: Mapped[str | None] = mapped_column(String(40))

    # 差异订单号
    order_no: Mapped[str | None] = mapped_column(String(64))

    # 渠道交易号
    channel_transaction_no: Mapped[str | None] = mapped_column(String(128))

    operator_id: Mapped[uuid.UUID | None] = mapped_column()

    operator_name: Mapped[str | None] = mapped_column(String(100))

    result: Mapped[str | None] = mapped_column(Text)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)

    # mass_register / trial_abuse / payment_failure / refund_anomaly /
    # quota_burn / device_multi_account / promo_abuse
    event_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    severity: Mapped[str] = mapped_column(
        String(10),
        default="medium",
        nullable=False
    )

    # flag / freeze / review / delay / verify
    action_taken: Mapped[str] = mapped_column(
        String(20),
        default="flag",
        nullable=False
    )

    # open / reviewed / appealed / resolved
    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        nullable=False
    )

    evidence: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
