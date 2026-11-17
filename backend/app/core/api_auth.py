# 开放 API 认证与权限（第十九批「移动端适配与开放 API」规格十三/十四/十五）
# 认证方式：Authorization: Bearer <token>
#   1) JWT（自家 Web/移动端登录态）→ 视为内部客户端，拥有全部权限
#   2) ApiToken（personal / app / api_key）→ 按 token 的 scopes 校验
# 未携带令牌视为匿名（principal=None），由限流层按匿名严格限制。
# 令牌最近使用时间 / IP 在每次认证时更新。
import uuid
from dataclasses import dataclass, field

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.core.api_response import get_request_id
from app.core.database import get_db
from app.models.api_token import ApiToken
from app.models.developer_app import DeveloperApp
from app.models.user import User
from app.utils.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class ApiPrincipal:
    user: User
    token: ApiToken | None = None
    scopes: set[str] = field(default_factory=set)
    is_internal: bool = False  # 自家 JWT 客户端视为内部
    is_trusted: bool = False   # 已启用应用的应用令牌视为可信


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def get_api_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> ApiPrincipal | None:
    """解析开放 API 请求的身份主体；匿名请求返回 None。"""
    if credentials is None:
        return None

    token_str = credentials.credentials

    # 1) 尝试 JWT（自家客户端）
    try:
        user_id = decode_access_token(token_str)
        user_uuid = uuid.UUID(user_id)
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if user is not None:
            return ApiPrincipal(
                user=user,
                scopes=set(),  # 内部客户端不限 scope
                is_internal=True,
            )
    except (JWTError, ValueError):
        pass

    # 2) 尝试 ApiToken
    import hashlib
    token_hash = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
    result = await db.execute(
        select(ApiToken).where(ApiToken.token_hash == token_hash)
    )
    api_token = result.scalar_one_or_none()

    if api_token is None:
        raise ApiError(
            "unauthorized", "无效或已过期的访问令牌",
            status_code=401, request_id=get_request_id(request),
        )

    if api_token.status == "revoked":
        raise ApiError(
            "unauthorized", "令牌已被撤销",
            status_code=401, request_id=get_request_id(request),
        )

    from datetime import datetime
    if api_token.expires_at is not None and api_token.expires_at <= datetime.utcnow():
        api_token.status = "expired"
        await db.commit()
        raise ApiError(
            "unauthorized", "令牌已过期",
            status_code=401, request_id=get_request_id(request),
        )

    # 更新最近使用时间 / IP
    api_token.last_used_at = datetime.utcnow()
    api_token.last_used_ip = _client_ip(request)[:64]
    await db.commit()

    # 可信判定：已启用应用的应用令牌
    is_trusted = False
    if api_token.token_type == "app" and api_token.app_id is not None:
        app = await db.get(DeveloperApp, api_token.app_id)
        if app is not None and app.status == "enabled":
            is_trusted = True

    scopes = {
        s.strip() for s in (api_token.scopes or "").split(",") if s.strip()
    }

    return ApiPrincipal(
        user=api_token.user_id and await db.get(User, api_token.user_id),
        token=api_token,
        scopes=scopes,
        is_trusted=is_trusted,
    )


def require_scopes(*required: str):
    """返回一个依赖：校验 principal 是否具备任一所需 scope。

    内部客户端（自家 JWT）不做 scope 限制；匿名请求视为未认证。
    """
    async def dependency(
        principal: ApiPrincipal | None = Depends(get_api_principal),
        request: Request = None,
    ) -> ApiPrincipal:
        if principal is None:
            raise ApiError(
                "unauthorized", "需要认证后才能访问",
                status_code=401, request_id=get_request_id(request),
            )
        if principal.is_internal:
            return principal
        if not required or required[0] in principal.scopes:
            return principal
        raise ApiError(
            "permission_denied", "令牌权限范围不足，无法访问该资源",
            status_code=403, request_id=get_request_id(request),
        )

    return dependency
