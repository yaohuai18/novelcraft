# 缓存条目模型（第十八批「性能优化、可靠性与安全加固」规格三/四）
# Cache Aside：读取缓存 -> 未命中查库 -> 写缓存 -> 数据修改时主动失效。
# cache_key 示例：project:project-01:overview / chapter:chapter-02:content /
#               user:user-01:permissions / stats:project-01:2026-09
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    cache_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
