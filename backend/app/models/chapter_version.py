# 章节版本模型：chapter_versions 表（第七批规格实现 + 第一期「生成不丢失」扩展）
# 每次保存正文生成一个新版本记录；source 标识版本来源：
# manual（手动保存）/ ai_draft（AI 生成）/ ai_continue（AI 续写）/ ai_rewrite（AI 改写）/
# ai_polish（AI 润色）/ restore（恢复历史版本）
# 扩展字段：word_count / generation_record_id / parent_version_id / is_current
# （第一期规格：AI 返回先建 generation_record，用户接受才建版本；恢复历史版本创建 restore 版本，不覆盖旧版本）
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 版本来源常量（第一期规格）
VERSION_SOURCES = [
    "manual",
    "ai_draft",
    "ai_continue",
    "ai_rewrite",
    "ai_polish",
    "restore",
]


class ChapterVersion(Base):
    __tablename__ = "chapter_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    outline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    source: Mapped[str] = mapped_column(
        String(30),
        default="manual",
        nullable=False
    )

    generation_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chapter_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )

    change_summary: Mapped[str | None] = mapped_column(
        String(500)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
