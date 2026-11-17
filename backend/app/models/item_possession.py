# 物品持有关系模型：item_possessions 表（第一批「事实数据库与规则校验」核心事实表）
# 记录物品从哪个章节起由谁持有（until_chapter_id=NULL 表示持有至今）。
# 用于 possession 规则校验：同一物品同一时间只能有一个持有人，禁止在
# until_chapter_id 之后仍出现在持有人快照中。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ItemPossession(Base):
    __tablename__ = "item_possessions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 以下四列在正式库均无库级外键（PRAGMA foreign_key_list 为空），
    # 引用合法性由服务层校验；ORM 不再声明 ForeignKey，仅按正式库保留索引/可空性。
    item_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False
    )

    owner_character_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False
    )

    # 持有起始章节
    since_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )
    # 持有结束章节（NULL=持有至今）
    until_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    # active（持有中）| ended（已移交/丢失）
    status: Mapped[str] = mapped_column(
        String(20),
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

    # 索引与正式库逐项对齐：ix_item_possessions_item / _owner / _locked。
    __table_args__ = (
        Index("ix_item_possessions_item", "item_id"),
        Index("ix_item_possessions_owner", "owner_character_id"),
        Index("ix_item_possessions_locked", "locked"),
    )
