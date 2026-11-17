# 开放 API v1 令牌管理路由（第十九批「移动端适配与开放 API」规格十三/十四）
# 个人访问令牌 / 应用令牌 / API Key：创建、查看权限、改名、撤销、过期时间、最近使用。
# 明文令牌仅在创建时返回一次；默认最小权限（空 scopes）。
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.api_token import API_TOKEN_TYPES, ApiToken
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import TokenCreateRequest, TokenUpdateRequest
from app.services import api_token_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/tokens", tags=["开放API-令牌", "实验"])

# 令牌管理属于自家客户端操作：JWT（internal）限流
async def guard_manage(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope=None, kind="general", credentials=credentials, db=db)


def _token_dict(t: ApiToken, raw: str | None = None) -> dict:
    d = {
        "id": str(t.id),
        "name": t.name,
        "token_type": t.token_type,
        "scopes": (t.scopes or "").split(",") if t.scopes else [],
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "last_used_ip": t.last_used_ip,
    }
    if raw is not None:
        d["token"] = raw
        d["note"] = "明文令牌仅此一次返回，请妥善保存"
    return d


@router.post("", status_code=201)
async def create_token(
    request: Request,
    payload: TokenCreateRequest,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    if payload.token_type not in API_TOKEN_TYPES:
        raise ApiError(
            "validation_failed",
            f"非法令牌类型: {payload.token_type}（可用: {', '.join(API_TOKEN_TYPES)}）",
            status_code=422,
        )
    expires_at = None
    if payload.expires_in_days and payload.expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=payload.expires_in_days)

    token, raw = await api_token_service.create_token(
        db,
        user_id=guard["principal"].user.id,
        name=payload.name,
        token_type=payload.token_type,
        scopes=payload.scopes or [],
        expires_at=expires_at,
    )
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"token": _token_dict(token, raw)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("")
async def list_tokens(
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    tokens = await api_token_service.list_tokens(db, guard["principal"].user.id)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"tokens": [_token_dict(t) for t in tokens], "count": len(tokens)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{token_id}")
async def get_token(
    token_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    token = await api_token_service.get_token_for_user(
        db, guard["principal"].user.id, token_id
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"token": _token_dict(token)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.patch("/{token_id}")
async def rename_token(
    token_id: UUID,
    payload: TokenUpdateRequest,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    token = await api_token_service.get_token_for_user(
        db, guard["principal"].user.id, token_id
    )
    if token.status == "revoked":
        raise ApiError("conflict", "令牌已撤销，无法修改", status_code=409)
    token = await api_token_service.rename_token(db, token, payload.name)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"token": _token_dict(token)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.delete("/{token_id}")
async def revoke_token(
    token_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    token = await api_token_service.get_token_for_user(
        db, guard["principal"].user.id, token_id
    )
    token = await api_token_service.revoke_token(db, token)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"token": _token_dict(token)},
        rate_headers=build_rate_headers(guard["rate"]),
    )
