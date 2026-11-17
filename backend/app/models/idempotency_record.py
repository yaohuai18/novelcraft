# 幂等记录模型（第十八批「性能优化、可靠性与安全加固」规格十三）
# Idempotency-Key 支持：创建备份/提交AI任务/发布章节/发送邀请/导入项目
# 相同 key + endpoint 的重复请求直接返回首次处理结果。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False
    )

    endpoint: Mapped[str] = mapped_column(
        String(200),
        nullable=False
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    response_status: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    response_body: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", "endpoint",
            name="uq_idem_key_endpoint",
        ),
    )
