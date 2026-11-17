# 企业安全与 AI 数据模型（第二十四批「企业级协作、审核与内容治理」规格十八/二十三）
# EnterpriseSecurityPolicy：邮箱域名限制 / SSO(SAML/OAuth) / MFA / IP限制 / 设备管理 /
#                           会话管理 / 强制退出 / 密码策略（本批实现配置 + 高风险复核接口，SSO/MFA 占位）。
# AIEnterpriseConfig：企业内容是否用于模型改进、AI 读取私有项目权限、AI 输出保存与可见范围、
#                     AI 任务进审计、离职 AI 历史处理；默认企业私有内容不用于公共训练。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EnterpriseSecurityPolicy(Base):
    __tablename__ = "enterprise_security_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        unique=True,
        nullable=False,
        index=True
    )

    # 邮箱域名限制（JSON 数组，空表示不限）
    email_domain_restriction: Mapped[str | None] = mapped_column(String(500))

    # SSO：sso_type = saml / oauth，sso_config 存配置 JSON（占位）
    sso_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    sso_type: Mapped[str | None] = mapped_column(String(20))

    sso_config: Mapped[str | None] = mapped_column(Text)

    mfa_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    # IP 访问限制（JSON 数组 CIDR）
    ip_restrictions: Mapped[str | None] = mapped_column(Text)

    # 设备管理策略（JSON）
    device_policy: Mapped[str | None] = mapped_column(Text)

    # 会话管理策略（JSON：session_timeout / max_sessions / force_logout_all）
    session_policy: Mapped[str | None] = mapped_column(Text)

    # 密码策略（JSON：min_length / require_special / expire_days）
    password_policy: Mapped[str | None] = mapped_column(Text)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class AIEnterpriseConfig(Base):
    __tablename__ = "ai_enterprise_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        unique=True,
        nullable=False,
        index=True
    )

    # 企业内容是否允许用于模型改进（默认 False：不用于公共训练）
    allow_training: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    # AI 是否可以读取私有项目（默认 False：AI 仅能访问有权限内容）
    allow_read_private: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    # AI 输出是否保存（默认 True）
    allow_save_output: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    # AI 结果可见范围：private / project / organization
    ai_output_visibility: Mapped[str] = mapped_column(
        String(20),
        default="organization",
        nullable=False
    )

    # AI 任务是否进入审计（默认 True）
    audit_ai_tasks: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    # 敏感内容提交 AI 前提示（默认 True）
    sensitive_hint: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
