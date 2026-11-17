# 异步任务队列模型（第十八批「性能优化、可靠性与安全加固」规格九/十）
# 状态：pending / running / succeeded / failed / retrying / cancelled / dead
# 重试间隔指数退避：第 1 次 10s / 第 2 次 30s / 第 3 次 2min / 第 4 次 10min
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    queue: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    task_type: Mapped[str] = mapped_column(
        String(80),
        nullable=False
    )

    payload: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        index=True
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

    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=3,
        nullable=False
    )

    available_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    completed_at: Mapped[datetime | None] = mapped_column(
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
