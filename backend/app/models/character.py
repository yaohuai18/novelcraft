# 人物模型：characters 表（第三批规格实现）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Character(Base):
    __tablename__ = "characters"

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
        String(100),
        nullable=False
    )

    identity: Mapped[str | None] = mapped_column(String(255))
    personality: Mapped[str | None] = mapped_column(Text)
    appearance: Mapped[str | None] = mapped_column(Text)
    background: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    conflict: Mapped[str | None] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(Text)
    character_status: Mapped[str | None] = mapped_column(Text)

    # ---------- 第三期「人物库」新增字段 ----------
    # 别名（JSON 数组字符串，如 ["沈沉","阿默"]）
    aliases: Mapped[str | None] = mapped_column(Text)
    # 角色定位：主角 | 配角 | 反派 | 路人
    role: Mapped[str] = mapped_column(
        String(20),
        default="配角",
        nullable=False
    )
    # 生存状态：在世 | 死亡 | 失踪 | 未知
    status: Mapped[str] = mapped_column(
        String(20),
        default="在世",
        nullable=False
    )
    # 综合档案（外貌/性格/背景）
    profile: Mapped[str | None] = mapped_column(Text)
    # 当前目标
    goals: Mapped[str | None] = mapped_column(Text)
    # 已知秘密
    secrets: Mapped[str | None] = mapped_column(Text)
    # 能力/技能
    abilities: Mapped[str | None] = mapped_column(Text)
    # 当前所在地点
    current_location: Mapped[str | None] = mapped_column(String(255))
    # 当前时间线状态描述
    current_timeline_status: Mapped[str | None] = mapped_column(Text)
    # 首次出场章节 / 最后出场章节
    first_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True
    )
    last_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True
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
