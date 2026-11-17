# 地区价格模型（第二十一批「国际化、多语言和多区域部署」规格十一）
# PlanPrice：套餐在不同地区/货币/计费周期下的价格（最小货币单位存储）。
# 客户端提交订单时不能直接提交价格，由服务端根据套餐/地区/货币/税费计算。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlanPrice(Base):
    __tablename__ = "plan_prices"
    __table_args__ = (
        UniqueConstraint(
            "plan_id", "region", "currency", "billing_interval",
            name="uq_plan_price",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    region: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False
    )

    # 最小货币单位金额：CNY 99.00 元 -> 9900 分；JPY 无小数位
    amount_minor: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    billing_interval: Mapped[str] = mapped_column(
        String(20),
        default="monthly",
        nullable=False
    )

    tax_included: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
