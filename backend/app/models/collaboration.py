# 协作模型（第十五批「通知、评论与协作反馈」规格实现）
# ProjectMember：项目成员与角色（owner/editor/reviewer/commenter/viewer）。
# ProjectInvitation：项目邀请（令牌只保存哈希 token_hash）。
# ActivityLog：协作活动日志（动作审计/通知生成/协作动态）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 项目角色（规格十四）
PROJECT_ROLES = ["owner", "editor", "reviewer", "commenter", "viewer"]

# 成员状态
MEMBER_STATUSES = ["active", "removed"]

# 邀请状态（规格十五）
INVITATION_STATUSES = ["pending", "accepted", "declined", "expired", "revoked"]

# 活动动作（规格十六：用于协作动态/审计/通知生成）
ACTIVITY_ACTIONS = [
    "comment.create",
    "comment.reply",
    "comment.resolve",
    "comment.reopen",
    "comment.hide",
    "comment.delete",
    "mention.create",
    "member.invite",
    "member.join",
    "member.invitation_accepted",
    "member.invitation_declined",
    "member.invitation_revoked",
    "member.role_changed",
    "member.removed",
    "content.update",
    "content.conflict",
    "version.create",
    "chapter.publish",
    "ai_task.completed",
    "ai_task.failed",
    "report.create",
    "report.handle",
]


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_member_project_user"),
    )

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

    role: Mapped[str] = mapped_column(
        String(20),
        default="viewer",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    joined_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class ProjectInvitation(Base):
    __tablename__ = "project_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    inviter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    invitee_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    role: Mapped[str] = mapped_column(
        String(20),
        default="viewer",
        nullable=False
    )

    # 邀请令牌只保存哈希，不存明文（规格十五）
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False
    )

    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True
    )

    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # 附加元数据（JSON 字符串），用于动态展示与审计
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
