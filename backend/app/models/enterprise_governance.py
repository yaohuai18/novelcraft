# 审核与治理模型（第二十四批「企业级协作、审核与内容治理」规格十/十一/十三/十八）
# ContentReview：机器预审 + 人工审核 + 管理员复核（违规词/违法/广告垃圾/恶意链接/隐私/版权/重复/异常批量）。
# ContentGovernanceRecord：内容治理/下架记录（保留原因/操作人/时间/原始版本/申诉状态/恢复记录，禁止物理删除替代）。
# CopyrightRecord：版权管理（作者/共同作者/版权所有者/授权范围/起止时间/转载许可/商业使用许可/下载限制/水印）。
# CopyrightClaim：侵权举报 / 作品下架申请 / 版权申诉。
# HighRiskActionRecord：高风险操作二次验证记录（导出全部/删组织/改所有者/改安全策略/批量删/生成长期令牌）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

RISK_LEVELS = ["low", "medium", "high", "critical"]
REVIEW_RESULTS = ["approved", "suspected", "manual_review", "rejected", "restricted"]
CONTENT_STATUSES = ["normal", "reviewing", "restricted", "pending_fix", "takedown", "deleted"]
GOVERNANCE_STATUSES = ["active", "restored"]

COPYRIGHT_OWNER_TYPES = ["personal", "organization", "commissioned", "co_created"]


class ContentReview(Base):
    __tablename__ = "content_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    # 内容类型：article / comment / avatar / file / private_message / project
    content_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    resource_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    resource_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    risk_level: Mapped[str] = mapped_column(
        String(20),
        default="low",
        nullable=False
    )

    # 机器预审结果：pending / approved / suspected / manual
    machine_result: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    # 机器预审发现（JSON：违规词/违法/广告垃圾/恶意链接/隐私/版权/重复/异常批量）
    findings: Mapped[str | None] = mapped_column(Text)

    # 最终审核结果：通过/疑似风险/需人工复核/拒绝发布/限制可见
    review_result: Mapped[str] = mapped_column(
        String(20),
        default="manual_review",
        nullable=False
    )

    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class ContentGovernanceRecord(Base):
    __tablename__ = "content_governance_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    content_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    resource_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    resource_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    # 内容状态：正常/审核中/限制展示/待整改/已下架/已删除
    content_status: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False
    )

    reason: Mapped[str | None] = mapped_column(Text)

    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    operated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    original_version: Mapped[str | None] = mapped_column(String(64))

    # 申诉状态：none / appealing / approved / rejected
    appeal_status: Mapped[str] = mapped_column(
        String(20),
        default="none",
        nullable=False
    )

    # 恢复记录（JSON：restored_by / restored_at / reason / version）
    restore_record: Mapped[str | None] = mapped_column(Text)

    deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )


class CopyrightRecord(Base):
    __tablename__ = "copyright_records"

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

    # 创作类型：个人创作 / 组织创作 / 受委托创作 / 共同创作
    owner_type: Mapped[str] = mapped_column(
        String(20),
        default="personal",
        nullable=False
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    # 共同作者（JSON 数组：user_id/name/role）
    co_authors: Mapped[str | None] = mapped_column(Text)

    copyright_owner: Mapped[str] = mapped_column(String(255), nullable=False)

    # 授权范围（JSON：platforms/regions/derivative）
    authorization_scope: Mapped[str | None] = mapped_column(Text)

    start_at: Mapped[datetime | None] = mapped_column(DateTime)

    end_at: Mapped[datetime | None] = mapped_column(DateTime)

    repost_permission: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    commercial_permission: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    download_restricted: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    watermark: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
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


class CopyrightClaim(Base):
    __tablename__ = "copyright_claims"

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

    claimant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    # 类型：infringement（侵权举报）/ takedown_request（下架申请）/ appeal（申诉）
    claim_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    handled_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class HighRiskActionRecord(Base):
    __tablename__ = "high_risk_action_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    # 动作：export_all / delete_organization / change_owner / change_security_policy /
    #       batch_delete / long_lived_token
    action: Mapped[str] = mapped_column(
        String(60),
        nullable=False
    )

    reason: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 影响范围提示（JSON：targets / count / description）
    impact_scope: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
