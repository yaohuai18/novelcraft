# Outbox 事件模型（第十八批「性能优化、可靠性与安全加固」规格十四/十五）
# 事务性发件箱：业务数据 + 事件在同一数据库事务内保存；
# 后台任务投递后标记完成；投递失败可重试，最终进入失败态待人工排查。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    event_type: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True
    )

    payload: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    available_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    error_message: Mapped[str | None] = mapped_column(
        Text
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
