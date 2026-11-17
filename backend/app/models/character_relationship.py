# 角色关系模型：character_relationships 表（第三期「人物和时间不漂」）
# 记录人物之间的动态关系，随章节变化可起止。
# relation_type 取值：师徒 | 敌对 | 同盟 | 亲属 | 怀疑 | 爱慕 | 主仆 | 同僚 | 其他
# status 取值：draft（AI 提取候选/待确认）| confirmed（已确认）| archived（已归档/关系结束）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CharacterRelationship(Base):
    __tablename__ = "character_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    source_character_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_character_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    relation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    # 关系起始章节 / 结束章节（可空表示延续至今/未知）
    start_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True
    )
    end_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
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
