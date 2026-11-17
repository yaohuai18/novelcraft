# 高风险操作确认令牌模型（第十八批「性能优化、可靠性与安全加固」规格十九）
# 权限二次确认：删项目 / 覆盖恢复 / 改所有者 / 导出协作数据 /
# 查看私人内容 / 改 API 密钥 —— 先签发一次性确认令牌，再携带令牌执行。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConfirmationToken(Base):
    __tablename__ = "confirmation_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    action: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True
    )

    token_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    used_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
