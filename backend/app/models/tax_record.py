# 税费历史记录模型（第二十一批「国际化、多语言和多区域部署」规格十二）
# 记录账单地区/税率/税额/含税金额/不含税金额/税务识别号。
# 税率变化时保留历史订单使用过的税率，旧订单数据不可篡改（只追加，不提供更新接口）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TAX_TYPES = ["vat", "sales_tax", "dst", "gst", "none"]


class TaxRecord(Base):
    __tablename__ = "tax_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"),
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

    # 税率（0.13 = 13%）
    rate: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False
    )

    tax_type: Mapped[str] = mapped_column(
        String(20),
        default="vat",
        nullable=False
    )

    # 最小货币单位
    tax_amount_minor: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    amount_excl_minor: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    amount_incl_minor: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 税务识别号（企业税号）
    tax_id_no: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
