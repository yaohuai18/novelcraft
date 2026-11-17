# 通知模型（第十五批「通知、评论与协作反馈」规格实现）
# Notification：站内通知；状态 unread/read/archived（不直接删除未读，先归档便于审计恢复）。
# NotificationPreference：按类型控制 in_app/email/push 渠道；安全类通知不允许完全关闭。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 通知类型（规格一：系统/作品/章节/AI任务/版本/评论/协作/审核/安全）
NOTIFICATION_TYPES = [
    "system",
    "project",
    "chapter",
    "ai_task",
    "version",
    "comment",
    "collaboration",
    "review",
    "security",
]

# 安全类通知：不允许完全关闭站内通知（新设备登录/密码修改/权限变化/API密钥变更）
SECURE_NOTIFICATION_TYPES = [
    "new_device_login",
    "password_changed",
    "permission_changed",
    "api_key_changed",
]

# 通知状态
NOTIFICATION_STATUSES = ["unread", "read", "archived"]


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True
    )

    # 触发操作的用户，可为空（系统事件）
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    content: Mapped[str | None] = mapped_column(Text)

    # 关联对象类型与 ID（用于前端跳转）
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # 附加数据（跳转与展示），JSON 字符串
    payload: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20),
        default="unread",
        nullable=False,
        index=True
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )

    read_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    notification_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False
    )

    in_app_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    email_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    push_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
