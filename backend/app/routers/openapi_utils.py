# 开放 API 公共依赖（第十九批「移动端适配与开放 API」）
# 组合：认证（principal）+ 独立分级限流（规格二十）+ 调用日志（开发者后台）。
# 客户端分级：internal（自家 JWT）/ trusted（已启用应用的应用令牌）/
#             normal（个人令牌 / api_key / 普通应用令牌）/ anonymous（匿名）。
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.api_auth import ApiPrincipal, get_api_principal
from app.core.api_errors import ApiError
from app.core.api_response import get_request_id
from app.core.database import get_db
from app.models.developer_app import ApiCallLog
from app.services import openapi_rate_limit_service as rl
from sqlalchemy.ext.asyncio import AsyncSession

bearer_scheme = HTTPBearer(auto_error=False)

ANONYMOUS_USER_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _client_class(principal: ApiPrincipal | None) -> str:
    if principal is None:
        return "anonymous"
    if principal.is_internal:
        return "internal"
    if principal.is_trusted:
        return "trusted"
    return "normal"


async def openapi_guard(
    request: Request,
    *,
    required_scope: str | None = None,
    kind: str = "general",
    plan: dict | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """认证 + 权限 + 分级限流组合依赖。返回 {"principal", "rate", "client_class"}。"""
    principal = await get_api_principal(
        request, credentials=credentials, db=db
    )
    if principal is None:
        raise ApiError(
            "unauthorized", "需要认证后才能访问",
            status_code=401, request_id=get_request_id(request),
        )
    if not principal.is_internal:
        if required_scope is not None and required_scope not in principal.scopes:
            raise ApiError(
                "permission_denied", "令牌权限范围不足，无法访问该资源",
                status_code=403, request_id=get_request_id(request),
            )

    client_class = _client_class(principal)
    effective_plan = plan or rl.RATE_PLAN_DEFAULT
    rate_result = await rl.check_and_bump(
        db,
        client_key=client_class,
        plan=effective_plan,
        timestamp=int(time.time()),
        kind=kind,
    )
    if not rate_result["allowed"]:
        raise ApiError(
            "rate_limit_exceeded", "请求过于频繁，请稍后再试",
            status_code=429,
            details={"retry_after_seconds": rate_result["retry_after"]},
            request_id=get_request_id(request),
        )
    return {
        "principal": principal,
        "rate": rate_result,
        "client_class": client_class,
    }


async def log_api_call(
    db: AsyncSession,
    request: Request,
    principal: ApiPrincipal | None,
    status_code: int,
) -> None:
    """记录一次开放 API 调用（用于开发者后台调用量 / 错误日志）。"""
    try:
        user_id = (
            principal.user.id if principal and principal.user else ANONYMOUS_USER_UUID
        )
        token_id = principal.token.id if principal and principal.token else None
        app_id = principal.token.app_id if principal and principal.token else None
        log = ApiCallLog(
            app_id=app_id,
            token_id=token_id,
            user_id=user_id,
            method=request.method,
            endpoint=request.url.path[:500],
            status_code=status_code,
            request_id=get_request_id(request),
        )
        db.add(log)
        await db.commit()
    except Exception:  # noqa: BLE001  日志失败不影响主流程
        pass
