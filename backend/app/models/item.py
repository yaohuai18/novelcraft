# 物品/秘密登记模型：items 表（第一批「事实数据库与规则校验」核心事实表）
# 普通物品 category=item；秘密（秘密本身也登记为物品）category=secret，
# 供 secret_knowers 以 secret_id 引用。status：active | archived。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 关联作品 UUID。正式库该表无任何库级外键（PRAGMA foreign_key_list 为空），
    # 归属校验由服务层显式比对完成；此处仅保留索引，不再声明 ForeignKey。
    project_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False
    )

    # 物品/秘密名称（如「玄铁剑」「陈九身世之谜」）
    # 正式库 items 无 name 单列索引（仅 ix_items_project / ix_items_locked），故不建索引。
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    # 物品类别：item（普通物品）| secret（秘密）
    category: Mapped[str] = mapped_column(
        String(20),
        default="item",
        server_default=text("'item'"),
        nullable=False
    )

    # 首次登场章节（可空：尚未登场；无库级外键）
    first_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default=text("'active'"),
        nullable=False
    )

    # 列序对齐正式库：created_at 位于 status 之后、authority_level 之前
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    # ---------- 阶段A「事实权威与锁定」新增字段 ----------
    # 权威等级：low（低，模型候选/推测）| normal（默认，自动提取）| high（高，人工确认/已批准设定）
    authority_level: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        server_default=text("'normal'"),
        nullable=False
    )
    # 置信度 0-1（NULL=未标注）
    confidence: Mapped[float | None] = mapped_column(Float)
    # 人工锁定（True=事实重算时跳过，不被自动提取覆盖）
    locked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False
    )
    # 来源记录 ID（关联 generation_record / 人工操作记录，可空）
    source_record_id: Mapped[str | None] = mapped_column(String(64))

    # 索引与正式库逐项对齐（名称/列/唯一性），不得新增正式库没有的索引：
    # ix_items_project(project_id)、ix_items_locked(locked)。
    __table_args__ = (
        Index("ix_items_project", "project_id"),
        Index("ix_items_locked", "locked"),
    )
