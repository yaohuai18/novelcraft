# 世界观条目模型：world_settings 表（第三批规格实现）
# 支持多个独立条目：势力/地点/力量体系/历史事件/种族/物品/规则/禁忌
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 建议分类常量（与规格一致）
WORLD_SETTING_CATEGORIES = [
    "faction",    # 势力
    "location",   # 地点
    "power",      # 力量体系
    "history",    # 历史事件
    "race",       # 种族
    "item",       # 物品
    "rule",       # 世界规则
    "taboo",      # 禁忌
]


class WorldSetting(Base):
    __tablename__ = "world_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    importance: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
