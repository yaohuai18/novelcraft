# 货币模型（第二十一批「国际化、多语言和多区域部署」规格十）
# Currency：code/symbol/decimal_digits/exchange_rate/status。
# 价格使用最小货币单位存储，避免浮点数计算误差。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Currency(Base):
    __tablename__ = "currencies"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    code: Mapped[str] = mapped_column(
        String(8),
        unique=True,
        nullable=False,
        index=True
    )

    symbol: Mapped[str] = mapped_column(
        String(8),
        nullable=False
    )

    # 小数位数：CNY/USD=2，JPY/KRW=0
    decimal_digits: Mapped[int] = mapped_column(
        Integer,
        default=2,
        nullable=False
    )

    # 相对基准货币（CNY）的汇率
    exchange_rate: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
