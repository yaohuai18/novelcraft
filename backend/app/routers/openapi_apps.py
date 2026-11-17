# 开放 API v1 开发者平台路由（第十九批「移动端适配与开放 API」规格二十三）
# 创建应用 / 名称与回调地址 / 申请权限 / 查看调用量与错误日志 / 暂停 / 恢复 / 撤销授权。
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.developer_app import DeveloperApp
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import (
    AppCreateRequest,
    AppUpdateRequest,
)
from app.services import developer_app_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/apps", tags=["开放API-开发者平台", "实验"])

async def guard_manage(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope=None, kind="general", credentials=credentials, db=db)


def _app_dict(a: DeveloperApp) -> dict:
    return {
        "id": str(a.id),
        "name": a.name,
        "description": a.description,
        "callback_url": a.callback_url,
        "requested_scopes": (
            [s.strip() for s in (a.requested_scopes or "").split(",") if s.strip()]
        ),
        "status": a.status,
        "review_note": a.review_note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("", status_code=201)
async def create_app(
    request: Request,
    payload: AppCreateRequest,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.create_app(
        db,
        user_id=guard["principal"].user.id,
        name=payload.name,
        description=payload.description,
        callback_url=payload.callback_url,
        requested_scopes=payload.requested_scopes or [],
    )
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"app": _app_dict(app)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("")
async def list_apps(
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    apps = await developer_app_service.list_apps(db, guard["principal"].user.id)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"apps": [_app_dict(a) for a in apps], "count": len(apps)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{app_id}")
async def get_app(
    app_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"app": _app_dict(app)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.patch("/{app_id}")
async def update_app(
    app_id: UUID,
    payload: AppUpdateRequest,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    app = await developer_app_service.update_app(
        db,
        app,
        name=payload.name,
        description=payload.description,
        callback_url=payload.callback_url,
        requested_scopes=payload.requested_scopes,
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"app": _app_dict(app)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{app_id}/stats")
async def app_call_stats(
    app_id: UUID,
    request: Request,
    days: int = 7,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    stats = await developer_app_service.get_call_stats(
        db, app_id=app.id, days=min(max(1, days), 30)
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        stats,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{app_id}/error-logs")
async def app_error_logs(
    app_id: UUID,
    request: Request,
    limit: int = 50,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    logs = await developer_app_service.get_error_logs(
        db, app_id=app.id, limit=limit
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"logs": logs, "count": len(logs)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/{app_id}/pause")
async def pause_app(
    app_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    app = await developer_app_service.pause_app(db, app)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"app": _app_dict(app)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/{app_id}/resume")
async def resume_app(
    app_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    app = await developer_app_service.resume_app(db, app)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"app": _app_dict(app)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/{app_id}/revoke")
async def revoke_app(
    app_id: UUID,
    request: Request,
    guard: dict = Depends(guard_manage),
    db: AsyncSession = Depends(get_db),
):
    app = await developer_app_service.get_app_for_user(
        db, guard["principal"].user.id, app_id
    )
    app = await developer_app_service.revoke_app(db, app)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"app": _app_dict(app)},
        rate_headers=build_rate_headers(guard["rate"]),
    )
