# 开放 API 令牌服务（第十九批「移动端适配与开放 API」规格十三/十四）
# 创建 / 查看权限 / 改名 / 撤销 / 过期时间 / 最近使用时间与 IP。
# 明文令牌仅在创建时返回一次，数据库只保存 SHA-256 哈希。
import hashlib
import secrets
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.core.api_response import get_request_id
from app.models.api_token import (
    API_SCOPES,
    API_TOKEN_STATUSES,
    API_TOKEN_TYPES,
    ApiToken,
)


def generate_token_raw(token_type: str) -> str:
    """生成明文令牌：nct_<type>_<48位随机>。"""
    return f"nct_{token_type}_{secrets.token_urlsafe(32)}"


def hash_token_raw(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_scopes(scopes: list[str]) -> list[str]:
    """scope 白名单校验；非法 scope 抛 ApiError(validation_failed)。"""
    cleaned = []
    for s in scopes or []:
        s = s.strip()
        if not s:
            continue
        if s not in API_SCOPES:
            raise ApiError(
                "validation_failed",
                f"非法权限范围: {s}（可用: {', '.join(API_SCOPES)}）",
                status_code=422,
            )
        cleaned.append(s)
    return cleaned


async def create_token(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    token_type: str = "personal",
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    app_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> tuple[ApiToken, str]:
    """创建令牌，返回 (token 记录, 明文令牌)。明文仅此一次。"""
    if token_type not in API_TOKEN_TYPES:
        raise ApiError(
            "validation_failed", f"非法令牌类型: {token_type}",
            status_code=422, request_id=request_id,
        )
    cleaned_scopes = validate_scopes(scopes or [])

    raw = generate_token_raw(token_type)
    token = ApiToken(
        user_id=user_id,
        app_id=app_id,
        name=name or token_type,
        token_type=token_type,
        token_hash=hash_token_raw(raw),
        scopes=",".join(cleaned_scopes),
        status="active",
        expires_at=expires_at,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token, raw


async def get_token_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    token_id: uuid.UUID,
) -> ApiToken:
    token = await db.get(ApiToken, token_id)
    if token is None or token.user_id != user_id:
        raise ApiError("not_found", "令牌不存在", status_code=404)
    return token


async def rename_token(
    db: AsyncSession,
    token: ApiToken,
    name: str,
) -> ApiToken:
    token.name = name.strip() or token.name
    await db.commit()
    await db.refresh(token)
    return token


async def revoke_token(
    db: AsyncSession,
    token: ApiToken,
) -> ApiToken:
    if token.status == "revoked":
        raise ApiError("conflict", "令牌已撤销", status_code=409)
    token.status = "revoked"
    await db.commit()
    await db.refresh(token)
    return token


async def list_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[ApiToken]:
    result = await db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user_id)
        .order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_app_tokens(
    db: AsyncSession,
    app_id: uuid.UUID,
) -> int:
    """撤销某应用的全部应用令牌（撤销应用授权时调用）。"""
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.app_id == app_id,
            ApiToken.status.in_(("active", "expired")),
        )
    )
    tokens = list(result.scalars().all())
    for t in tokens:
        t.status = "revoked"
    await db.commit()
    return len(tokens)
