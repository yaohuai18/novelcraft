# 开放 API 令牌模型（第十九批「移动端适配与开放 API」规格十三/十四）
# 支持三种令牌：personal（个人访问令牌）/ app（应用令牌）/ api_key（受限 API Key）。
# 只保存 token_hash（SHA-256），明文令牌仅在创建时返回一次。
# Scope 机制：projects:read|write / chapters:read|write / characters:read|write /
# comments:read|write / stats:read / ai:submit|read / exports:create，默认最小权限。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

API_TOKEN_TYPES = ["personal", "app", "api_key"]
API_TOKEN_STATUSES = ["active", "revoked", "expired"]

API_SCOPES = [
    "projects:read",
    "projects:write",
    "chapters:read",
    "chapters:write",
    "characters:read",
    "characters:write",
    "comments:read",
    "comments:write",
    "stats:read",
    "ai:submit",
    "ai:read",
    "exports:create",
]

# 只读权限集合：仅申请这些 scope 的应用可自动审核通过
READ_ONLY_SCOPES = {
    "projects:read",
    "chapters:read",
    "characters:read",
    "comments:read",
    "stats:read",
    "ai:read",
}


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    app_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("developer_apps.id", ondelete="CASCADE"),
        index=True,
        nullable=True
    )

    name: Mapped[str] = mapped_column(String(100), default="", nullable=False)

    token_type: Mapped[str] = mapped_column(
        String(20),
        default="personal",
        nullable=False
    )

    token_hash: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False
    )

    # 逗号分隔的 scope 列表，如 "projects:read,chapters:read"
    scopes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)

    last_used_ip: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
