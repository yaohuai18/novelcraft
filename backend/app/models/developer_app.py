# 开发者平台模型（第十九批「移动端适配与开放 API」规格二十三）
# DeveloperApp：第三方应用（名称/回调地址/申请权限/状态）。
# 应用状态：developing（开发中）/ reviewing（审核中）/ enabled（已启用）/
#           paused（已暂停）/ revoked（已撤销）。
# ApiCallLog：开放 API 调用量 / 错误日志（用于开发者后台查看调用情况）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

APP_STATUSES = [
    "developing",  # 开发中
    "reviewing",   # 审核中
    "enabled",     # 已启用
    "paused",      # 已暂停
    "revoked",     # 已撤销
]


class DeveloperApp(Base):
    __tablename__ = "developer_apps"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)

    callback_url: Mapped[str | None] = mapped_column(String(500))

    # 逗号分隔的申请权限 scope 列表
    requested_scopes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default="developing",
        nullable=False
    )

    review_note: Mapped[str | None] = mapped_column(Text)

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


class ApiCallLog(Base):
    __tablename__ = "api_call_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    app_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True,
        nullable=True
    )

    token_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True,
        nullable=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    method: Mapped[str] = mapped_column(String(10), nullable=False)

    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)

    status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)

    request_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
