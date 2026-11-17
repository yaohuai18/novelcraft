# 移动端本地草稿模型（第十九批「移动端适配与开放 API」规格五/六/七）
# 移动端自动保存的本地草稿：断网时保存在本地（local_only），网络恢复后同步。
# base_version 记录上次成功同步时的服务器章节版本标识（ChapterContent.version 的字符串形式）。
# sync_status: local_only / syncing / synced / conflict / failed
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

MOBILE_DRAFT_SYNC_STATUSES = [
    "local_only",  # 仅保存在本地，尚未同步到服务器
    "syncing",     # 同步中
    "synced",      # 已同步到服务器
    "conflict",    # 与服务器版本冲突，需要用户选择处理方式
    "failed",      # 同步失败
]


class MobileDraft(Base):
    __tablename__ = "mobile_drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )

    # 上次成功同步时的服务器章节版本标识（如 "3"）
    base_version: Mapped[str | None] = mapped_column(String(50))

    content: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 本地内容 SHA-256（十六进制），用于快速判断内容是否变化
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # 最近编辑设备（冲突信息展示用，如 "iPhone 15 / NovelCraft iOS 1.2.0"）
    device_info: Mapped[str | None] = mapped_column(String(255))

    last_edited_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    sync_status: Mapped[str] = mapped_column(
        String(20),
        default="local_only",
        nullable=False
    )

    sync_error: Mapped[str | None] = mapped_column(Text)

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
