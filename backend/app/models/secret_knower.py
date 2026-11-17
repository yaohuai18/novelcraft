# 秘密知情者模型：secret_knowers 表（第一批「事实数据库与规则校验」核心事实表）
# 记录某个秘密（items 表 category=secret）从哪个章节起被哪个角色知晓。
# 用于 secret 规则校验：禁止角色在 learned_chapter_id 之前章节提前知晓秘密。
# reveal_chapter_id / forbidden_before_chapter_id 为本批预留字段（只建字段，
# 不做强制闸门，后续批次启用）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SecretKnower(Base):
    __tablename__ = "secret_knowers"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # 以下四列在正式库均无库级外键（PRAGMA foreign_key_list 为空），
    # 引用合法性由服务层校验；ORM 不再声明 ForeignKey。
    secret_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False
    )

    character_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False
    )

    # 自哪个章节起知晓（NULL=在故事开始前已知）
    learned_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    # 来源：ai_extracted（自动提取）| manual（手工登记）
    source: Mapped[str] = mapped_column(
        String(20),
        default="ai_extracted",
        server_default=text("'ai_extracted'"),
        nullable=False
    )

    # 预留字段：秘密公开章节 / 禁止提前知晓的截止章节（本批只建字段，不做强制闸门）
    reveal_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )
    forbidden_before_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    # 列序对齐正式库：created_at 位于 forbidden_before_chapter_id 之后、authority_level 之前
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

    # 索引与正式库逐项对齐：ix_secret_knowers_secret / _character / _locked。
    __table_args__ = (
        Index("ix_secret_knowers_secret", "secret_id"),
        Index("ix_secret_knowers_character", "character_id"),
        Index("ix_secret_knowers_locked", "locked"),
    )
