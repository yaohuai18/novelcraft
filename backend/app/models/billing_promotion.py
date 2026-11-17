# 优惠与促销模型（第二十批「商业化、订阅与计费系统」规格十二）
# Promotion：优惠码/限时折扣/首月优惠/年付折扣/邀请奖励/赠送 AI 额度/升级补差价。
# PromotionRedemption：使用记录，用于防重复使用/超限/套利。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PROMOTION_TYPES = [
    "percent",       # 折扣百分比（discount_value 0-100）
    "fixed",         # 固定金额优惠（discount_value 元）
    "grant_quota",   # 赠送 AI 额度（quota_grant 数量）
    "upgrade_diff",  # 升级补差价
    "trial",         # 试用延长
]

PROMOTION_STATUSES = ["active", "inactive", "expired"]


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # 优惠码（可为空，如限时折扣按活动而非码）
    code: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        index=True
    )

    promotion_type: Mapped[str] = mapped_column(
        String(20),
        default="percent",
        nullable=False
    )

    # percent 时为 0-100；fixed 时为金额；其他类型可为 0
    discount_value: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False
    )

    # grant_quota 类型赠送的额度数量
    quota_grant: Mapped[int | None] = mapped_column(Integer)

    # 全局使用次数上限
    usage_limit: Mapped[int | None] = mapped_column(Integer)

    # 每用户使用次数上限
    per_user_limit: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    starts_at: Mapped[datetime | None] = mapped_column(DateTime)

    ends_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class PromotionRedemption(Base):
    __tablename__ = "promotion_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    promotion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("promotions.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )

    # 是否已发放赠送额度
    quota_granted: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
    )

    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
