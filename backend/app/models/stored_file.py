# 文件存储对象模型（第十八批「性能优化、可靠性与安全加固」规格二十二）
# 文件存储安全：限制大小/扩展名/MIME 校验 / 随机存储名 / 公开私有隔离 /
# 签名 URL / 下载次数限制 / 操作日志。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StoredFile(Base):
    __tablename__ = "stored_files"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    original_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    stored_name: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False
    )

    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    mime_type: Mapped[str] = mapped_column(
        String(120),
        nullable=False
    )

    is_private: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    download_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    max_downloads: Mapped[int | None] = mapped_column(
        Integer
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    # ===== 第二十四批「企业文件管理」：组织归属 + 回收站软删除 =====
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    restore_deadline: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        index=True
    )
