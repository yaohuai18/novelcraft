# i18n 语言资源模型（第二十一批「国际化、多语言和多区域部署」规格三/四）
# I18nResource：翻译键资源（namespace/key/language/value）。
# I18nResourceVersion：翻译内容历史版本，支持审核/发布/回滚/版本管理。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 翻译资源状态：draft / reviewing / published / rolled_back
I18N_RESOURCE_STATUSES = ["draft", "reviewing", "published", "rolled_back"]


class I18nResource(Base):
    __tablename__ = "i18n_resources"
    __table_args__ = (
        UniqueConstraint("namespace", "key", "language", name="uq_i18n_resource"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    namespace: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        index=True
    )

    # 资源分类：common / errors / billing / notification / legal
    category: Mapped[str] = mapped_column(
        String(60),
        default="common",
        nullable=False,
        index=True
    )

    key: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        index=True
    )

    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    # 占位符声明（如 {username} {count}），防止变量名不一致
    placeholders: Mapped[str | None] = mapped_column(Text)

    # 最大长度限制，防止文本过长导致界面溢出（0 表示不限制）
    max_length: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
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


class I18nResourceVersion(Base):
    __tablename__ = "i18n_resource_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("i18n_resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    # 版本操作类型：create / modify / rollback
    action: Mapped[str] = mapped_column(
        String(30),
        default="modify",
        nullable=False
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )

    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
