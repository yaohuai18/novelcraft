# 举报模型（第十五批「通知、评论与协作反馈」规格实现）
# Report：用户举报不当内容；状态 pending/reviewing/resolved/rejected；
#         处理人 handled_by 与处理时间 handled_at 由服务端记录。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

REPORT_STATUSES = ["pending", "reviewing", "resolved", "rejected"]


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    reporter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    # ===== 第二十四批「企业数据隔离」：举报归属组织，跨组织不可见/不可处理 =====
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True,
        nullable=True,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    # ===== 第二十四批「举报和申诉」：补充描述/证据/风险分级/申诉状态 =====
    description: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(
        String(20),
        default="low",
        nullable=False
    )
    appeal_status: Mapped[str] = mapped_column(
        String(20),
        default="none",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    handled_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
