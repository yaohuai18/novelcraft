# 世界观规则模型：world_rules 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 与既有 WorldSetting 知识库条目并存：world_rules 为结构化规则条目，
# 完整项目 JSON 结构中的 world_rules 数组来源；支持 CSV 表格导入。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorldRule(Base):
    __tablename__ = "world_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    category: Mapped[str | None] = mapped_column(String(60))
    content: Mapped[str] = mapped_column(
        Text,
        default="",
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
