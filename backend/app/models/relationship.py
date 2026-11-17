# 人物关系模型：relationships 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 完整项目 JSON 结构中的 relationships 数组来源；支持 CSV 表格导入。
# from_name / to_name 为导入时记录的原始名称（冗余），恢复/迁移时按名称解析到角色 ID。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Relationship(Base):
    __tablename__ = "relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 关系两端（指向 characters.id；导入时若外部 ID 无法映射则保留名称）
    from_character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        index=True
    )
    to_character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        index=True
    )

    from_name: Mapped[str | None] = mapped_column(String(255))
    to_name: Mapped[str | None] = mapped_column(String(255))

    relation_type: Mapped[str] = mapped_column(
        String(60),
        default="related",
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
