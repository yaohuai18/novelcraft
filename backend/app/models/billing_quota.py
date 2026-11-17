# AI 额度账户与使用流水（第二十批「商业化、订阅与计费系统」规格九/十）
# QuotaAccount：额度账户（用户/组织维度 × 额度类型：ai_tokens / ai_tasks /
#   storage_bytes / exports），总发放、已用、预扣（reserved）三段式。
# QuotaLedger：不可变的额度使用流水，action 含 grant/reserve/consume/release/
#   refund/expire/adjust，记录 balance_after 供审计。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

QUOTA_TYPES = [
    "ai_tokens",
    "ai_tasks",
    "storage_bytes",
    "exports",
]

QUOTA_ACTIONS = [
    "grant",
    "reserve",
    "consume",
    "release",
    "refund",
    "expire",
    "adjust",
]


class QuotaAccount(Base):
    __tablename__ = "quota_accounts"
    __table_args__ = (
        UniqueConstraint(
            "owner_type", "owner_id", "quota_type",
            name="uq_quota_account_owner_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # owner_type: user / org
    owner_type: Mapped[str] = mapped_column(
        String(10),
        default="user",
        nullable=False
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    quota_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    total_amount: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    used_amount: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    reserved_amount: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

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

    @property
    def available_amount(self) -> int:
        return max(
            0,
            self.total_amount - self.used_amount - self.reserved_amount,
        )


class QuotaLedger(Base):
    __tablename__ = "quota_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quota_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 引用来源：ai_task / order / promotion / adjust / admin
    reference_type: Mapped[str | None] = mapped_column(String(30))

    reference_id: Mapped[str | None] = mapped_column(String(128))

    action: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True
    )

    # 正数表示增加/扣减数量（consume/release/refund 均按绝对值记录，balance_after 表示方向）
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 操作后账户可用余额（total - used - reserved）
    balance_after: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
