# 地区合规模型（第二十一批「国际化、多语言和多区域部署」规格二十一）
# PolicyVersion：隐私政策/服务条款/Cookie 政策/数据处理说明版本管理。
# ConsentRecord：用户同意记录（同意内容版本/时间/地区/IP/撤回时间/政策更新时间）。
# DataRequest：数据导出/账号删除/数据访问申请。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

POLICY_TYPES = ["privacy", "terms", "cookie", "data_processing"]

CONSENT_STATUSES = ["active", "withdrawn"]

DATA_REQUEST_TYPES = ["export", "delete", "access", "correct"]
DATA_REQUEST_STATUSES = ["pending", "processing", "completed", "rejected"]


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "policy_type", "region", "version",
            name="uq_policy_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    policy_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    region: Mapped[str] = mapped_column(
        String(16),
        default="CN",
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    effective_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    published_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="published",
        nullable=False
    )


class ConsentRecord(Base):
    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL"),
        nullable=False
    )

    policy_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    policy_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )

    # 同意时间
    granted_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    # 用户地区 / IP
    region: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    ip_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    # 撤回时间
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 政策更新时间（政策版本变化后提示重新同意）
    policy_updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class DataRequest(Base):
    __tablename__ = "data_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    request_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    region: Mapped[str] = mapped_column(
        String(16),
        nullable=False
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 处理结果/导出内容（摘要）
    result: Mapped[str | None] = mapped_column(Text)
