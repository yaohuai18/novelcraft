# 团队/组织计费模型（第二十批「商业化、订阅与计费系统」规格十一）
# Organization：组织主档（owner、billing_email、套餐、状态）。
# OrganizationMember：成员与角色（owner/admin/member），支持邀请/离开状态。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ORGANIZATION_STATUSES = ["active", "frozen"]
# 第二十四批「企业级协作、审核与内容治理」：组织角色扩展为十种
# 所有者/管理员/项目负责人/主编/编辑/作者/审稿人/评论员/只读成员/访客（member 保留兼容旧数据）
ORGANIZATION_ROLES = [
    "owner", "admin", "project_lead", "chief_editor", "editor",
    "author", "reviewer", "commenter", "readonly", "guest", "member",
]
MEMBER_ROLES = ORGANIZATION_ROLES
MEMBER_STATUSES = ["active", "invited", "left"]


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL")
    )

    # 第二十四批「企业级协作、审核与内容治理」规格二：Organization.plan（套餐标识）
    plan: Mapped[str] = mapped_column(
        String(20),
        default="free",
        nullable=False
    )

    billing_email: Mapped[str | None] = mapped_column(String(200))

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

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    role: Mapped[str] = mapped_column(
        String(20),
        default="member",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
