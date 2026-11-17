# 第二十一批「国际化、多语言和多区域部署」管理端路由
# 翻译资源管理 / 货币汇率 / 地区价格 / 税费记录 / 通知模板 / 合规政策 / 数据申请
# / 数据区域 / 区域监控与容灾 / CDN 资产管理。风格与 admin_billing.py 一致。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ok
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permissions import require_admin_roles
from app.models.user import User
from app.schemas.i18n_schemas import (
    CurrencyCreate,
    CurrencyUpdate,
    DataRequestUpdate,
    I18nResourceCreate,
    NotificationTemplateCreate,
    PlanPriceCreate,
    PolicyPublishRequest,
)
from app.schemas.region_schemas import CdnAssetCreate, RegionMetricCreate
from app.services import (
    cdn_service,
    compliance_service,
    currency_service,
    i18n_service,
    notification_i18n_service,
    pricing_service,
    region_monitor_service,
    region_service,
)

router = APIRouter(prefix="/api/admin/i18n", tags=["admin-i18n"])


# ==================== 翻译资源管理 ====================

@router.post("/resources")
async def admin_upsert_resource(
    request: Request,
    payload: I18nResourceCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    try:
        resource = await i18n_service.upsert_resource(
            db,
            language=payload.language,
            key=payload.key,
            value=payload.value,
            category=payload.category,
            placeholders=payload.placeholders,
            action=payload.action,
            max_length=payload.max_length,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return ok(request, {
        "id": str(resource.id),
        "language": resource.language,
        "key": resource.key,
        "value": resource.value,
        "status": resource.status,
        "version": resource.version,
    })


@router.get("/resources")
async def admin_list_resources(
    request: Request,
    language: str | None = None,
    category: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await i18n_service.list_resources(
        db, language=language, category=category, status=status
    )
    return ok(request, {
        "items": [
            {
                "id": str(r.id),
                "language": r.language,
                "key": r.key,
                "value": r.value,
                "category": r.category,
                "status": r.status,
                "version": r.version,
            }
            for r in items
        ],
        "total": len(items),
    })


@router.post("/resources/check-missing")
async def admin_check_missing(
    request: Request,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    missing = await i18n_service.check_missing(
        db,
        reference_language=payload.get("reference_language", "zh-CN"),
        languages=payload.get("languages"),
    )
    return ok(request, {"missing": missing, "total": len(missing)})


@router.post("/resources/{language}/{key}/check-placeholders")
async def admin_check_placeholders(
    request: Request,
    language: str,
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    result = await i18n_service.check_placeholders(db, language, key)
    return ok(request, result)


@router.post("/resources/{language}/{key}/rollback")
async def admin_rollback_resource(
    request: Request,
    language: str,
    key: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    resource = await i18n_service.rollback_resource(
        db,
        language=language,
        key=key,
        version=payload.get("version"),
    )
    await db.commit()
    return ok(request, {
        "id": str(resource.id),
        "language": resource.language,
        "key": resource.key,
        "value": resource.value,
        "status": resource.status,
        "version": resource.version,
    })


@router.get("/resources/{language}/{key}/versions")
async def admin_list_versions(
    request: Request,
    language: str,
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    versions = await i18n_service.list_versions(db, language=language, key=key)
    return ok(request, {
        "items": [
            {
                "id": str(v.id),
                "version": v.version,
                "value": v.value,
                "action": v.action,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
        "total": len(versions),
    })


# ==================== 多币种 ====================

@router.post("/currencies")
async def admin_create_currency(
    request: Request,
    payload: CurrencyCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    cur = await currency_service.create_currency(
        db,
        code=payload.code,
        symbol=payload.symbol,
        decimal_digits=payload.decimal_digits,
        exchange_rate=payload.exchange_rate,
        status=payload.status,
    )
    await db.commit()
    return ok(request, {
        "id": str(cur.id),
        "code": cur.code,
        "symbol": cur.symbol,
        "decimal_digits": cur.decimal_digits,
        "exchange_rate": cur.exchange_rate,
        "status": cur.status,
    })


@router.put("/currencies/{code}/rate")
async def admin_update_rate(
    request: Request,
    code: str,
    payload: CurrencyUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    cur = await currency_service.update_exchange_rate(
        db, code=code, exchange_rate=payload.exchange_rate
    )
    await db.commit()
    return ok(request, {
        "id": str(cur.id),
        "code": cur.code,
        "exchange_rate": cur.exchange_rate,
        "updated_at": cur.updated_at.isoformat(),
    })


@router.get("/currencies")
async def admin_list_currencies(
    request: Request,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await currency_service.list_currencies(db, status=status)
    return ok(request, {
        "items": [
            {
                "code": c.code,
                "symbol": c.symbol,
                "decimal_digits": c.decimal_digits,
                "exchange_rate": c.exchange_rate,
                "status": c.status,
            }
            for c in items
        ],
        "total": len(items),
    })


# ==================== 地区价格 / 税费 ====================

@router.post("/plan-prices")
async def admin_upsert_plan_price(
    request: Request,
    payload: PlanPriceCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    price = await pricing_service.upsert_plan_price(
        db,
        plan_id=payload.plan_id,
        region=payload.region,
        currency=payload.currency,
        amount_minor=payload.amount,
        billing_interval=payload.billing_interval or "monthly",
        tax_included=payload.tax_included,
        status=payload.status,
    )
    await db.commit()
    return ok(request, {
        "id": str(price.id),
        "plan_id": str(price.plan_id),
        "region": price.region,
        "currency": price.currency,
        "amount_minor": price.amount_minor,
        "billing_interval": price.billing_interval,
        "tax_included": price.tax_included,
        "status": price.status,
        "version": price.version,
    })


@router.get("/tax-records")
async def admin_list_tax_records(
    request: Request,
    order_id: uuid.UUID | None = None,
    region: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await pricing_service.get_tax_records(
        db, order_id=order_id, region=region
    )
    return ok(request, {
        "items": [
            {
                "id": str(t.id),
                "order_id": str(t.order_id) if t.order_id else None,
                "region": t.region,
                "currency": t.currency,
                "rate": t.rate,
                "tax_type": t.tax_type,
                "tax_amount_minor": t.tax_amount_minor,
                "amount_excl_minor": t.amount_excl_minor,
                "amount_incl_minor": t.amount_incl_minor,
                "tax_id_no": t.tax_id_no,
            }
            for t in items
        ],
        "total": len(items),
    })


# ==================== 地区化通知模板 ====================

@router.post("/notification-templates")
async def admin_upsert_template(
    request: Request,
    payload: NotificationTemplateCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    tpl = await notification_i18n_service.get_or_create_template(
        db,
        template_key=payload.template_key,
        language=payload.language,
        category=payload.category,
        title_template=payload.title_template,
        content_template=payload.content_template,
        placeholders=payload.placeholders,
        legal_notice=payload.legal_notice,
    )
    await db.commit()
    return ok(request, {
        "id": str(tpl.id),
        "template_key": tpl.template_key,
        "language": tpl.language,
        "category": tpl.category,
        "version": tpl.version,
        "status": tpl.status,
    })


@router.get("/notification-templates")
async def admin_list_templates(
    request: Request,
    category: str | None = None,
    language: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await notification_i18n_service.list_templates(
        db, category=category, language=language
    )
    return ok(request, {
        "items": [
            {
                "id": str(t.id),
                "template_key": t.template_key,
                "language": t.language,
                "category": t.category,
                "title_template": t.title_template,
                "content_template": t.content_template,
                "version": t.version,
                "status": t.status,
            }
            for t in items
        ],
        "total": len(items),
    })


# ==================== 地区合规 ====================

@router.post("/policies")
async def admin_publish_policy(
    request: Request,
    payload: PolicyPublishRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    policy = await compliance_service.publish_policy(
        db,
        policy_type=payload.policy_type,
        region=payload.region,
        title=payload.title,
        content=payload.content,
    )
    await db.commit()
    return ok(request, {
        "id": str(policy.id),
        "policy_type": policy.policy_type,
        "region": policy.region,
        "version": policy.version,
        "title": policy.title,
        "effective_at": policy.effective_at.isoformat(),
    })


@router.get("/policies")
async def admin_list_policies(
    request: Request,
    policy_type: str | None = None,
    region: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await compliance_service.list_policies(
        db, policy_type=policy_type, region=region
    )
    return ok(request, {
        "items": [
            {
                "id": str(p.id),
                "policy_type": p.policy_type,
                "region": p.region,
                "version": p.version,
                "title": p.title,
                "status": p.status,
                "published_at": p.published_at.isoformat(),
            }
            for p in items
        ],
        "total": len(items),
    })


@router.get("/data-requests")
async def admin_list_data_requests(
    request: Request,
    user_id: uuid.UUID | None = None,
    request_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await compliance_service.list_data_requests(
        db, user_id=user_id, request_type=request_type
    )
    return ok(request, {
        "items": [
            {
                "id": str(d.id),
                "user_id": str(d.user_id),
                "request_type": d.request_type,
                "status": d.status,
                "region": d.region,
                "requested_at": d.requested_at.isoformat(),
            }
            for d in items
        ],
        "total": len(items),
    })


@router.post("/data-requests/{request_id}/update")
async def admin_update_data_request(
    request: Request,
    request_id: uuid.UUID,
    payload: DataRequestUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    req = await compliance_service.update_data_request(
        db,
        request_id=request_id,
        status=payload.status,
        result=payload.result,
    )
    await db.commit()
    if req is None:
        return ok(request, {"updated": False, "reason": "申请不存在"})
    return ok(request, {
        "updated": True,
        "id": str(req.id),
        "status": req.status,
        "result": req.result,
    })


# ==================== 数据区域 ====================

@router.post("/regions")
async def admin_create_region(
    request: Request,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    region = await region_service.create_region(
        db,
        region_code=payload["region_code"],
        name=payload.get("name", payload["region_code"]),
        storage_endpoint=payload.get("storage_endpoint") or f"oss-{payload['region_code']}.novelcraft.local",
        database_cluster=payload.get("database_cluster") or f"cluster-{payload['region_code']}",
        ai_provider=payload.get("ai_provider") or "default",
        status=payload.get("status", "active"),
        is_main=payload.get("is_main", False),
    )
    await db.commit()
    return ok(request, {
        "id": str(region.id),
        "region_code": region.region_code,
        "name": region.name,
        "status": region.status,
        "is_main": region.is_main,
    })


@router.get("/regions")
async def admin_list_regions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await region_service.list_regions(db)
    return ok(request, {
        "items": [
            {
                "id": str(r.id),
                "region_code": r.region_code,
                "name": r.name,
                "storage_endpoint": r.storage_endpoint,
                "database_cluster": r.database_cluster,
                "ai_provider": r.ai_provider,
                "status": r.status,
                "is_main": r.is_main,
            }
            for r in items
        ],
        "total": len(items),
    })


@router.put("/regions/{region_code}/status")
async def admin_update_region_status(
    request: Request,
    region_code: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    region = await region_service.update_region_status(
        db,
        region_code=region_code,
        status=payload["status"],
        operator_id=payload.get("operator_id"),
    )
    await db.commit()
    return ok(request, {
        "region_code": region.region_code,
        "status": region.status,
        "updated_at": region.updated_at.isoformat(),
    })


# ==================== 区域监控与容灾 ====================

@router.post("/regions/{region_code}/metrics")
async def admin_record_metric(
    request: Request,
    region_code: str,
    payload: RegionMetricCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    metric = await region_monitor_service.record_metric(
        db,
        region_code=region_code,
        request_count=payload.request_count,
        error_count=payload.error_count,
        total_response_ms=payload.total_response_ms,
        cache_hit_count=payload.cache_hit_count,
        cache_miss_count=payload.cache_miss_count,
        ai_available=payload.ai_available,
    )
    await db.commit()
    return ok(request, {
        "id": str(metric.id),
        "region_code": metric.region_code,
        "request_count": metric.request_count,
        "error_count": metric.error_count,
        "error_rate": metric.error_rate,
        "metric_at": metric.metric_at.isoformat(),
    })


@router.get("/regions/{region_code}/metrics")
async def admin_list_metrics(
    request: Request,
    region_code: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await region_monitor_service.list_metrics(
        db, region_code=region_code, limit=limit
    )
    return ok(request, {
        "items": [
            {
                "id": str(m.id),
                "region_code": m.region_code,
                "request_count": m.request_count,
                "error_count": m.error_count,
                "error_rate": m.error_rate,
                "avg_response_ms": round(m.total_response_ms / m.request_count, 1) if m.request_count else 0,
                "ai_available": m.ai_available,
                "metric_at": m.metric_at.isoformat(),
            }
            for m in items
        ],
        "total": len(items),
    })


@router.post("/regions/{region_code}/detect")
async def admin_detect_region(
    request: Request,
    region_code: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    status = await region_monitor_service.detect_and_update_status(
        db,
        region_code=region_code,
        request_count=payload.get("request_count", 0),
        error_count=payload.get("error_count", 0),
        total_response_ms=payload.get("total_response_ms", 0),
        ai_available=payload.get("ai_available", True),
    )
    await db.commit()
    return ok(request, {
        "region_code": status.region_code,
        "status": status.status,
        "reason": status.reason,
        "detected_at": status.detected_at.isoformat(),
    })


@router.get("/regions/{region_code}/status")
async def admin_get_region_status(
    request: Request,
    region_code: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    status = await region_monitor_service.get_region_status(db, region_code)
    if status is None:
        return ok(request, {"region_code": region_code, "status": "unknown", "reason": None})
    return ok(request, {
        "region_code": status.region_code,
        "status": status.status,
        "reason": status.reason,
        "failover_to": status.failover_to,
        "data_resident_blocked": status.data_resident_blocked,
        "detected_at": status.detected_at.isoformat() if status.detected_at else None,
    })


@router.post("/regions/{region_code}/failover")
async def admin_failover_region(
    request: Request,
    region_code: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    result = await region_monitor_service.failover_region(
        db,
        region_code=region_code,
        target_region=payload["target_region"],
        operator_id=payload.get("operator_id"),
        sensitive_data_types=payload.get("sensitive_data_types"),
    )
    await db.commit()
    return ok(request, result)


# ==================== CDN 静态资源 ====================

@router.post("/cdn/assets")
async def admin_register_asset(
    request: Request,
    payload: CdnAssetCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    asset = await cdn_service.register_asset(
        db,
        asset_key=payload.object_key,
        original_name=payload.object_key.split("/")[-1],
        mime_type=payload.content_type,
        file_size=payload.size_bytes,
        is_public=(payload.visibility == "public"),
        region_code=payload.region or "CN",
        owner_id=payload.project_id,
    )
    await db.commit()
    return ok(request, {
        "id": str(asset.id),
        "asset_key": asset.asset_key,
        "mime_type": asset.mime_type,
        "file_size": asset.file_size,
        "is_public": asset.is_public,
        "fingerprint": asset.fingerprint,
        "version_path": asset.version_path,
        "status": asset.status,
    })


@router.get("/cdn/assets")
async def admin_list_assets(
    request: Request,
    is_public: bool | None = None,
    region_code: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await cdn_service.list_assets(
        db, is_public=is_public, region_code=region_code
    )
    return ok(request, {
        "items": [
            {
                "id": str(a.id),
                "asset_key": a.asset_key,
                "mime_type": a.mime_type,
                "file_size": a.file_size,
                "is_public": a.is_public,
                "region_code": a.region_code,
                "fingerprint": a.fingerprint,
                "status": a.status,
            }
            for a in items
        ],
        "total": len(items),
    })


@router.get("/cdn/download-logs")
async def admin_list_download_logs(
    request: Request,
    asset_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin_roles()),
):
    items = await cdn_service.list_download_logs(
        db, asset_id=asset_id, user_id=user_id
    )
    return ok(request, {
        "items": [
            {
                "id": str(l.id),
                "asset_id": str(l.asset_id),
                "user_id": str(l.user_id) if l.user_id else None,
                "ip_hash": l.ip_hash,
                "downloaded_at": l.downloaded_at.isoformat(),
            }
            for l in items
        ],
        "total": len(items),
    })
