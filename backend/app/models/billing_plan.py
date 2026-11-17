# 套餐模型（第二十批「商业化、订阅与计费系统」规格二/三）
# Plan：套餐主档（免费版/个人会员/专业版/团队版/企业版）
# PlanFeature：套餐能力项（project.max_count / storage.max_bytes /
#   ai.monthly_tokens / ai.monthly_tasks / team.max_members /
#   export.monthly_count / stats.history_days / api.requests_per_minute 等）
# limit_value 使用字符串存储：数字以字符串存，特殊值如 "unlimited" 直接存文本。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 套餐类型 / 计费周期 / 状态
PLAN_TYPES = ["free", "personal", "professional", "team", "enterprise"]
BILLING_INTERVALS = ["monthly", "yearly", "one_time", "trial"]
PLAN_STATUSES = ["active", "inactive", "archived"]

# 常见能力键（用于文档与校验）
PLAN_FEATURE_KEYS = [
    "project.max_count",
    "storage.max_bytes",
    "ai.monthly_tokens",
    "ai.monthly_tasks",
    "team.max_members",
    "export.monthly_count",
    "stats.history_days",
    "api.requests_per_minute",
]


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    plan_type: Mapped[str] = mapped_column(
        String(20),
        default="free",
        nullable=False,
        index=True
    )

    description: Mapped[str | None] = mapped_column(Text)

    billing_interval: Mapped[str] = mapped_column(
        String(20),
        default="monthly",
        nullable=False
    )

    # 金额（服务端唯一价格源，不信任客户端）
    price: Mapped[float] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
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


class PlanFeature(Base):
    __tablename__ = "plan_features"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    feature_key: Mapped[str] = mapped_column(
        String(60),
        nullable=False
    )

    # 字符串形式：数字以字符串存，特殊值 "unlimited" 直接存文本
    limit_value: Mapped[str] = mapped_column(
        String(100),
        default="0",
        nullable=False
    )

    # count / bytes / tokens / tasks / members / days / requests / bool / none
    limit_type: Mapped[str] = mapped_column(
        String(20),
        default="count",
        nullable=False
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )
