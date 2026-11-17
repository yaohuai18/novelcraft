# 开放 API v1 Webhook 订阅路由（第十九批「移动端适配与开放 API」规格十八/十九）
# 创建 / 列表 / 详情 / 修改 / 撤销；事件投递含 HMAC 签名与指数退避重试（webhook_service）。
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.webhook import WebhookSubscription
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import (
    WebhookCreateRequest,
    WebhookUpdateRequest,
)
from app.services import webhook_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/webhooks", tags=["开放API-Webhook", "实验"])

async def guard_write(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:write", kind="general", credentials=credentials, db=db)


async def guard_read(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:read", kind="general", credentials=credentials, db=db)


def _sub_dict(s: WebhookSubscription, secret: str | None = None) -> dict:
    d = {
        "id": str(s.id),
        "app_id": str(s.app_id) if s.app_id else None,
        "endpoint": s.endpoint,
        "events": [e.strip() for e in (s.events or "").split(",") if e.strip()],
        "status": s.status,
        "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
        "last_failure_at": s.last_failure_at.isoformat() if s.last_failure_at else None,
        "failure_reason": s.failure_reason,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
    if secret is not None:
        d["secret"] = secret
        d["note"] = "签名密钥仅此一次返回，请妥善保存"
    return d


@router.post("", status_code=201)
async def create_webhook(
    request: Request,
    payload: WebhookCreateRequest,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    sub, secret = await webhook_service.create_subscription(
        db,
        user_id=guard["principal"].user.id,
        app_id=payload.app_id,
        endpoint=payload.endpoint,
        events=payload.events,
    )
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"webhook": _sub_dict(sub, secret)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("")
async def list_webhooks(
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    subs = await webhook_service.list_subscriptions(
        db, guard["principal"].user.id
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"webhooks": [_sub_dict(s) for s in subs], "count": len(subs)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{webhook_id}")
async def get_webhook(
    webhook_id: UUID,
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    sub = await webhook_service.get_subscription_for_user(
        db, guard["principal"].user.id, webhook_id
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"webhook": _sub_dict(sub)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.patch("/{webhook_id}")
async def update_webhook(
    webhook_id: UUID,
    payload: WebhookUpdateRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    sub = await webhook_service.get_subscription_for_user(
        db, guard["principal"].user.id, webhook_id
    )
    sub = await webhook_service.update_subscription(
        db,
        sub,
        endpoint=payload.endpoint,
        events=payload.events,
        status=payload.status,
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"webhook": _sub_dict(sub)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: UUID,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    sub = await webhook_service.get_subscription_for_user(
        db, guard["principal"].user.id, webhook_id
    )
    await webhook_service.delete_subscription(db, sub)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"deleted": True, "webhook_id": str(webhook_id)},
        rate_headers=build_rate_headers(guard["rate"]),
    )
