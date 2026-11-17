# 开发者平台服务（第十九批「移动端适配与开放 API」规格二十三）
# 创建应用 / 设置名称与回调地址 / 申请权限 / 管理密钥 / 查看调用量与错误日志 /
# 配置 Webhook / 撤销应用授权。高权限应用（含写权限）需人工审核。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.models.api_token import READ_ONLY_SCOPES, API_SCOPES, ApiToken
from app.models.developer_app import APP_STATUSES, ApiCallLog, DeveloperApp


def _parse_scopes(raw: str) -> list[str]:
    return [s for s in (raw or "").split(",") if s.strip()]


async def create_app(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    description: str | None,
    callback_url: str | None,
    requested_scopes: list[str],
) -> DeveloperApp:
    """创建应用。仅申请只读权限 → 自动审核通过（enabled）；含写权限 → 人工审核（reviewing）。"""
    cleaned = []
    for s in requested_scopes or []:
        s = s.strip()
        if s not in API_SCOPES:
            raise ApiError(
                "validation_failed", f"非法权限范围: {s}", status_code=422
            )
        cleaned.append(s)

    requires_review = any(s not in READ_ONLY_SCOPES for s in cleaned)

    app = DeveloperApp(
        user_id=user_id,
        name=(name or "").strip()[:100],
        description=description,
        callback_url=callback_url,
        requested_scopes=",".join(cleaned),
        status="reviewing" if requires_review else "enabled",
        review_note=(
            "应用申请了写权限或 AI 权限，需人工审核" if requires_review else None
        ),
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)
    return app


async def get_app_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
) -> DeveloperApp:
    app = await db.get(DeveloperApp, app_id)
    if app is None or app.user_id != user_id:
        raise ApiError("not_found", "应用不存在", status_code=404)
    return app


async def update_app(
    db: AsyncSession,
    app: DeveloperApp,
    *,
    name: str | None = None,
    description: str | None = None,
    callback_url: str | None = None,
    requested_scopes: list[str] | None = None,
) -> DeveloperApp:
    if app.status == "revoked":
        raise ApiError("conflict", "已撤销的应用不能修改", status_code=409)

    if name is not None:
        app.name = name.strip()[:100] or app.name
    if description is not None:
        app.description = description
    if callback_url is not None:
        app.callback_url = callback_url
    if requested_scopes is not None:
        cleaned = []
        for s in requested_scopes:
            s = s.strip()
            if s not in API_SCOPES:
                raise ApiError(
                    "validation_failed", f"非法权限范围: {s}", status_code=422
                )
            cleaned.append(s)
        app.requested_scopes = ",".join(cleaned)
        requires_review = any(s not in READ_ONLY_SCOPES for s in cleaned)
        if requires_review and app.status != "revoked":
            app.status = "reviewing"
            app.review_note = "权限变更，需重新人工审核"

    await db.commit()
    await db.refresh(app)
    return app


async def list_apps(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[DeveloperApp]:
    result = await db.execute(
        select(DeveloperApp)
        .where(DeveloperApp.user_id == user_id)
        .order_by(DeveloperApp.created_at.desc())
    )
    return list(result.scalars().all())


async def get_call_stats(
    db: AsyncSession,
    app_id: uuid.UUID,
    days: int = 7,
) -> dict:
    """按天统计调用量与错误日志。"""
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(
            func.date(ApiCallLog.created_at),
            ApiCallLog.status_code,
        )
        .where(
            ApiCallLog.app_id == app_id,
            ApiCallLog.created_at >= since,
        )
        .order_by(func.date(ApiCallLog.created_at).desc())
    )
    rows = result.all()
    grouped: dict[str, list[int]] = {}
    for day, status_code in rows:
        grouped.setdefault(str(day), []).append(status_code)
    stats = []
    for day in sorted(grouped.keys(), reverse=True):
        codes = grouped[day]
        stats.append({
            "date": day,
            "calls": len(codes),
            "errors": sum(1 for c in codes if c >= 400),
        })
    return {"app_id": str(app_id), "days": days, "daily": stats}


async def get_error_logs(
    db: AsyncSession,
    app_id: uuid.UUID,
    limit: int = 50,
) -> list[dict]:
    result = await db.execute(
        select(ApiCallLog)
        .where(
            ApiCallLog.app_id == app_id,
            ApiCallLog.status_code >= 400,
        )
        .order_by(ApiCallLog.created_at.desc())
        .limit(min(limit, 100))
    )
    logs = []
    for log in list(result.scalars().all()):
        logs.append({
            "request_id": log.request_id,
            "method": log.method,
            "endpoint": log.endpoint,
            "status_code": log.status_code,
            "created_at": log.created_at.isoformat(),
        })
    return logs


async def pause_app(
    db: AsyncSession,
    app: DeveloperApp,
) -> DeveloperApp:
    """暂停应用：调用立刻被拒（unauthorized）。"""
    if app.status == "revoked":
        raise ApiError("conflict", "已撤销的应用不能暂停", status_code=409)
    app.status = "paused"
    await db.commit()
    await db.refresh(app)
    return app


async def resume_app(
    db: AsyncSession,
    app: DeveloperApp,
) -> DeveloperApp:
    if app.status in ("revoked",):
        raise ApiError("conflict", "已撤销的应用不能恢复", status_code=409)
    if app.status != "paused":
        raise ApiError("conflict", "应用当前未暂停", status_code=409)
    app.status = "enabled"
    await db.commit()
    await db.refresh(app)
    return app


async def revoke_app(
    db: AsyncSession,
    app: DeveloperApp,
) -> DeveloperApp:
    """撤销应用授权：状态置 revoked，撤销其全部应用令牌。"""
    app.status = "revoked"
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.app_id == app.id,
            ApiToken.status.in_(("active", "expired")),
        )
    )
    for t in list(result.scalars().all()):
        t.status = "revoked"
    await db.commit()
    await db.refresh(app)
    return app
