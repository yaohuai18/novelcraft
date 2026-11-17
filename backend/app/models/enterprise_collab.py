# 企业协作模型（第二十四批「企业级协作、审核与内容治理」规格六/九）
# EditLock：章节/文档「锁定编辑」模式（开始编辑锁定 → 他人只读可评论 → 保存/退出释放），
#           处理异常退出/浏览器关闭/网络中断/长时未操作/多设备/超时/管理员强制解除。
# ReviewTask：审批流程（作者提交 → 编辑审阅 → 退回修改或通过 → 主编确认 → 发布）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EDIT_LOCK_STATUSES = ["locked", "released", "expired", "force_released"]

REVIEW_STAGES = ["draft", "review", "modify", "re_review", "approved", "rejected", "withdrawn"]
REVIEW_STATUSES = ["unsubmitted", "reviewing", "needs_changes", "re_reviewing", "approved", "rejected", "withdrawn"]


class EditLock(Base):
    __tablename__ = "edit_locks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 锁定对象：chapter / outline / document
    resource_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="locked",
        nullable=False
    )

    acquired_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    released_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    resource_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    submitter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    stage: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="unsubmitted",
        nullable=False,
        index=True
    )

    comment: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
