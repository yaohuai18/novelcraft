# CDN 与静态资源模型（第二十一批「国际化、多语言和多区域部署」规格十九）
# CdnAsset：公开资源可缓存；私有资源短期签名 URL + 权限校验。
# DownloadLog：下载日志（私有资源访问留痕）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CdnAsset(Base):
    __tablename__ = "cdn_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    asset_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True
    )

    original_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    file_size: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    region_code: Mapped[str] = mapped_column(
        String(16),
        default="CN",
        nullable=False
    )

    cache_control: Mapped[str] = mapped_column(
        String(100),
        default="public, max-age=86400",
        nullable=False
    )

    # 文件指纹（发布新版本用版本化路径）
    fingerprint: Mapped[str] = mapped_column(
        String(64),
        default="",
        nullable=False
    )

    version_path: Mapped[str | None] = mapped_column(String(255))

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class DownloadLog(Base):
    __tablename__ = "cdn_download_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cdn_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    ip_hash: Mapped[str | None] = mapped_column(String(64))

    user_agent: Mapped[str | None] = mapped_column(String(255))

    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
