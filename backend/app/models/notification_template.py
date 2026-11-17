# 地区化通知模板模型（第二十一批「国际化、多语言和多区域部署」规格二十）
# NotificationTemplate：按语言/时区/地区/货币/时间格式渲染通知。
# 交易通知（transaction）/ 营销通知（marketing）/ 安全通知（security）分类管理，
# 营销通知用户可关闭，交易与安全通知不能完全关闭。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

NOTIFICATION_CATEGORIES = ["transaction", "marketing", "security"]


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint("template_key", "language", name="uq_notify_template"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    template_key: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True
    )

    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    category: Mapped[str] = mapped_column(
        String(20),
        default="transaction",
        nullable=False
    )

    title_template: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    content_template: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    # 占位符声明，逗号分隔，如 username,amount,currency
    placeholders: Mapped[str | None] = mapped_column(String(255))

    # 法律声明（隐私/合规文案，随模板渲染）
    legal_notice: Mapped[str | None] = mapped_column(Text)

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="published",
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
