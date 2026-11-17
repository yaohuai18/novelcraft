# 备份模型：project_backups 表（第十六批「数据导入、导出与备份恢复」规格实现）
# backup_type：manual（手动）/ automatic（自动）/ pre_publish（发布前）/ release_snapshot（发布快照）/
#             before_import（导入前）/ before_restore（恢复前，内部保护备份）
# snapshot 为 JSON 完整快照（含 schema_version）；checksum 为 sha256 完整性校验值；
# storage_key 为逻辑存储键（不暴露真实路径）；expires_at 为自动备份过期时间。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 备份类型（规格十二/十三）
BACKUP_TYPES = [
    "manual",
    "automatic",
    "pre_publish",
    "release_snapshot",
    "before_import",
    "before_restore",
]

# 备份状态
BACKUP_STATUSES = [
    "creating",
    "ready",
    "failed",
]

# 备份快照 schema 版本（字符串，规格：1.0 / 1.1 / 1.2 / 2.0 迁移）
BACKUP_SCHEMA_VERSION = "1.0"

# 当前支持的 schema 版本链（含迁移）
SUPPORTED_SCHEMA_VERSIONS = ["1.0"]
LEGACY_SCHEMA_VERSIONS = {"0.9", "1"}  # 旧整数版本 1 也接受


class ProjectBackup(Base):
    __tablename__ = "project_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    backup_type: Mapped[str] = mapped_column(
        String(30),
        default="manual",
        nullable=False
    )

    schema_version: Mapped[str] = mapped_column(
        String(20),
        default="1.0",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="ready",
        nullable=False
    )

    snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    checksum: Mapped[str] = mapped_column(
        String(64),
        nullable=False
    )

    size: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 逻辑存储键（形如 backup://{project_id}/{backup_id}，不暴露服务器内部路径）
    storage_key: Mapped[str | None] = mapped_column(String(255))

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
