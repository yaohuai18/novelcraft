# 最近访问模型：recent_items 表（第九批规格实现）
# 记录用户最近打开的内容，限制保留最近 20 条。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RecentItem(Base):
    __tablename__ = "recent_items"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    accessed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
