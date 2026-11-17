# 企业文件管理模型（第二十四批「企业级协作、审核与内容治理」规格十六）
# FileShareLink：公开链接（关闭/过期/密码/限下载/限次数/访问日志）。
# FileAccessLog：文件操作与访问日志（查看/下载/分享/上传/替换/删除/恢复）。
# 删除走回收站软删除（StoredFile.deleted_at）+ 恢复期限 + 最终删除，不做物理永久删除替代。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

FILE_SHARE_STATUSES = ["active", "closed", "expired"]
FILE_ACCESS_ACTIONS = ["view", "download", "share", "upload", "replace", "delete", "restore"]


class FileShareLink(Base):
    __tablename__ = "file_share_links"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    creator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    token: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False
    )

    password_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    allow_download: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    max_visits: Mapped[int | None] = mapped_column(Integer)

    visit_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class FileAccessLog(Base):
    __tablename__ = "file_access_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    action: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    ip_hash: Mapped[str | None] = mapped_column(String(64))

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
