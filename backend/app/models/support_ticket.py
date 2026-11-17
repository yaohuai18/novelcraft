# 支持工单模型：support_tickets 表（第十七批「统计分析与运营后台」规格十三/十四）
# support 角色负责查看用户问题与处理工单；后台首页展示待处理工单数。
# status: open / in_progress / resolved / closed
# priority: low / normal / high / urgent
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TICKET_STATUSES = ["open", "in_progress", "resolved", "closed"]
TICKET_PRIORITIES = ["low", "normal", "high", "urgent"]
TICKET_CATEGORIES = ["general", "account", "content", "payment", "ai", "other"]


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[str] = mapped_column(
        String(40),
        default="general",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        nullable=False,
        index=True
    )

    priority: Mapped[str] = mapped_column(
        String(10),
        default="normal",
        nullable=False
    )

    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    handled_at: Mapped[datetime | None] = mapped_column(DateTime)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    note: Mapped[str | None] = mapped_column(Text)
