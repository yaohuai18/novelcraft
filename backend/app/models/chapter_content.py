# 章节正文模型：chapter_contents 表（第七批规格实现）
# 一个章节对应一份当前正文；历史版本单独保存（chapter_versions），不直接覆盖。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.database import Base


class ChapterContent(Base):
    __tablename__ = "chapter_contents"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    outline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )

    content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 批次 5（2026-11-17）：正文的字符 bigram 串，供 chapters_fts（FTS5）索引。
    # 由 @validates 随 content 自动维护；批量 CAS UPDATE 需显式写入。
    content_bigrams: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    @validates("content")
    def _sync_content_bigrams(self, key: str, value: str) -> str:
        from app.core.text_utils import bigrams

        self.content_bigrams = bigrams(value or "")
        return value

    word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
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

    # ---------- 第一阶段「生命周期与可追溯元数据」新增字段 ----------
    # 与 outlines.lifecycle_status 同步的章节生命周期（正文侧冗余，便于按表查询）
    lifecycle_status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    generation_meta: Mapped[str | None] = mapped_column(Text)
    validation_report: Mapped[str | None] = mapped_column(Text)
    repair_records: Mapped[str | None] = mapped_column(Text)
    outline_version_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True,
    )
    # 故事事实版本号：正文侧冗余（与 outlines.facts_version 同步）
    facts_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    # 修复 R4：validation_summary / validation_updated_at 真实落库（原为动态属性不持久化）
    validation_summary: Mapped[str | None] = mapped_column(Text)
    validation_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
