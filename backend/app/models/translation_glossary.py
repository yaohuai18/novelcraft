# AI 术语表模型（第二十一批「国际化、多语言和多区域部署」规格七）
# TranslationGlossary：角色名/专有名词/地名/组织名/技能名固定译法。
# force_exact=True 的专有名词禁止 AI 随意翻译（翻译后须校验固定译法保留）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 术语类型
GLOSSARY_TERM_TYPES = [
    "character",
    "proper_noun",
    "place",
    "organization",
    "skill",
    "fixed_term",
]

# 术语作用域
GLOSSARY_SCOPES = ["project", "global"]


class TranslationGlossary(Base):
    """AI 多语言术语表。

    - source_language → target_language 的固定译法条目。
    - project_id 为空表示全局术语；否则为项目级术语（项目级优先）。
    - force_exact=True 时 AI 翻译必须使用 target_text，禁止随意翻译。
    """

    __tablename__ = "translation_glossaries"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_language", "target_language", "source_text",
            name="uq_glossary_project_lang_text",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    source_language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    target_language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    source_text: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    target_text: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    term_type: Mapped[str] = mapped_column(
        String(40),
        default="proper_noun",
        nullable=False
    )

    # 强制匹配：True 时 AI 必须使用固定译法，禁止随意翻译
    force_exact: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    scope: Mapped[str] = mapped_column(
        String(20),
        default="project",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
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
