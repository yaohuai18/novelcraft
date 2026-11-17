# 刷新令牌模型（第十八批「性能优化、可靠性与安全加固」规格十八）
# 刷新令牌轮换：每次刷新签发新令牌并使旧令牌失效，吊销即撤销所有会话。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    token_hash: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    ip_hash: Mapped[str | None] = mapped_column(
        String(64)
    )

    device_info: Mapped[str | None] = mapped_column(
        String(255)
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    used_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
