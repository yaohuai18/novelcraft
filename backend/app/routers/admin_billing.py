# 商业化管理后台 API（第二十批「商业化、订阅与计费系统」规格十七/十八/二十二）
# GET  /api/admin/billing/orders|subscriptions|reconciliation|ledger|finance/reports|promotions|risks
# POST /api/admin/billing/reconciliation/run、reconciliation/{id}/resolve、quota-adjustments、refunds、promotions、risks/{id}/resolve
# PATCH /api/admin/billing/plans/{plan_id}
# 角色：billing_admin 订单订阅/额度；finance_admin 账务对账/报表；support_admin 退款；promotion_admin 优惠；super_admin 全部。
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ok
from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import require_admin_roles
from app.models.billing_finance import LedgerEntry, Reconciliation, RiskEvent
from app.models.billing_order import Order
from app.models.billing_subscription import Subscription
from app.models.user import User
from app.schemas.billing_schemas import (
    PlanUpdateRequest,
    PromotionCreateRequest,
    QuotaAdjustRequest,
    RefundRequest,
    ResolveReconciliationRequest,
    ResolveRiskRequest,
    RunReconciliationRequest,
)
from app.services import (
    ledger_service,
    order_service,
    payment_service,
    plan_service,
    promotion_service,
    quota_service,
    reconciliation_service,
    risk_service,
    subscription_service,
)

router = APIRouter(
    prefix="/api/admin/billing",
    tags=["商业化管理后台"],
)


def _asdict_order(order: Order) -> dict:
    return {
        "id": str(order.id),
        "order_no": order.order_no,
        "user_id": str(order.user_id),
        "organization_id": str(order.organization_id) if order.organization_id else None,
        "plan_id": str(order.plan_id) if order.plan_id else None,
        "order_type": order.order_type,
        "amount": float(order.amount),
        "currency": order.currency,
        "status": order.status,
        "payment_provider": order.payment_provider,
        "provider_order_id": order.provider_order_id,
        "promotion_applied": order.promotion_applied,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "created_at": order.created_at.isoformat(),
    }


def _asdict_recon(rec: Reconciliation) -> dict:
    return {
        "id": str(rec.id),
        "recon_date": rec.recon_date,
        "status": rec.status,
        "platform_total": float(rec.platform_total),
        "provider_total": float(rec.provider_total),
        "diff_amount": float(rec.diff_amount),
        "diff_type": rec.diff_type,
        "operator_name": rec.operator_name,
        "result": rec.result,
        "processed_at": rec.processed_at.isoformat() if rec.processed_at else None,
        "created_at": rec.created_at.isoformat(),
    }


@router.get("/orders")
async def admin_billing_orders(
    request: Request,
    user_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """订单列表（billing_admin）。"""
    orders = await order_service.list_orders(
        db, user_id=user_id, status=status, limit=limit
    )
    return ok(
        request,
        {"orders": [_asdict_order(o) for o in orders], "count": len(orders)},
    )


@router.get("/subscriptions")
async def admin_billing_subscriptions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """订阅列表（billing_admin）。"""
    subs = await subscription_service.list_subscriptions(db, status=status, limit=limit)
    return ok(
        request,
        {
            "subscriptions": [
                {
                    "id": str(s.id),
                    "user_id": str(s.user_id),
                    "organization_id": str(s.organization_id) if s.organization_id else None,
                    "plan_id": str(s.plan_id),
                    "provider": s.provider,
                    "status": s.status,
                    "current_period_start": s.current_period_start.isoformat(),
                    "current_period_end": s.current_period_end.isoformat(),
                    "cancel_at_period_end": s.cancel_at_period_end,
                }
                for s in subs
            ]
        },
    )


@router.get("/reconciliation")
async def admin_billing_reconciliation(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_admin_roles("finance_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """对账记录列表（finance_admin）。"""
    recs = await reconciliation_service.list_reconciliations(db, status=status, limit=limit)
    return ok(
        request,
        {"reconciliations": [_asdict_recon(r) for r in recs], "count": len(recs)},
    )


@router.post("/reconciliation/run")
async def admin_billing_reconciliation_run(
    req: RunReconciliationRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("finance_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """执行对账（finance_admin）。"""
    if req.start >= req.end:
        raise HTTPException(status_code=400, detail="时间范围无效")
    rec = await reconciliation_service.run_reconciliation(
        db,
        start=req.start,
        end=req.end,
        operator_id=current_user.id,
        override_provider_total=req.override_provider_total,
    )
    return ok(request, {"reconciliation": _asdict_recon(rec)}, status_code=201)


@router.post("/reconciliation/{recon_id}/resolve")
async def admin_billing_reconciliation_resolve(
    recon_id: uuid.UUID = Path(...),
    req: ResolveReconciliationRequest = ...,
    request: Request = ...,
    current_user: User = Depends(require_admin_roles("finance_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """人工处理对账差异（finance_admin）。"""
    rec = await reconciliation_service.get_reconciliation(db, recon_id)
    if not rec:
        raise HTTPException(status_code=404, detail="对账记录不存在")
    rec = await reconciliation_service.resolve_reconciliation(
        db,
        rec,
        result_text=req.result_text,
        operator_id=current_user.id,
    )
    return ok(request, {"reconciliation": _asdict_recon(rec)})


@router.get("/ledger")
async def admin_billing_ledger(
    request: Request,
    user_id: uuid.UUID | None = Query(default=None),
    organization_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_admin_roles("finance_admin", "billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """账务流水（finance_admin 只读，不可修改）。"""
    entries = await ledger_service.list_entries(
        db,
        user_id=user_id,
        organization_id=organization_id,
        limit=limit,
    )
    return ok(
        request,
        {
            "entries": [
                {
                    "id": str(e.id),
                    "entry_type": e.entry_type,
                    "user_id": str(e.user_id) if e.user_id else None,
                    "organization_id": str(e.organization_id) if e.organization_id else None,
                    "order_id": str(e.order_id) if e.order_id else None,
                    "amount": float(e.amount),
                    "currency": e.currency,
                    "direction": e.direction,
                    "reference_id": e.reference_id,
                    "operator_id": str(e.operator_id) if e.operator_id else None,
                    "reason": e.reason,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ]
        },
    )


@router.get("/finance/reports")
async def admin_billing_finance_reports(
    request: Request,
    year: int = Query(default=2026, ge=2000, le=2100),
    month: int = Query(default=1, ge=1, le=12),
    current_user: User = Depends(require_admin_roles("finance_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """财务报表（月度汇总：收入/退款/赠送/净收入）。"""
    summary = await ledger_service.monthly_summary(db, year, month)
    return ok(request, {"report": summary})


@router.post("/quota-adjustments")
async def admin_billing_quota_adjustments(
    req: QuotaAdjustRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """管理员调整额度（必须填写对象/类型/数量/有效期/操作原因/审批人）。"""
    expires_at = None
    if req.expires_in_days:
        expires_at = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=req.expires_in_days)
    if req.owner_type not in ("user", "org"):
        raise HTTPException(status_code=400, detail="owner_type 必须为 user/org")
    try:
        account = await quota_service.adjust_quota(
            db,
            req.owner_type,
            req.owner_id,
            req.quota_type,
            req.amount,
            current_user.id,
            reason=req.reason,
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(
        request,
        {
            "account": {
                "id": str(account.id),
                "owner_type": account.owner_type,
                "owner_id": str(account.owner_id),
                "quota_type": account.quota_type,
                "total": account.total_amount,
                "used": account.used_amount,
                "reserved": account.reserved_amount,
                "available": account.available_amount,
            }
        },
        status_code=201,
    )


@router.post("/refunds")
async def admin_billing_refunds(
    req: RefundRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("support_admin", "billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """处理退款申请（support_admin）。"""
    order = await order_service.get_order(db, order_no=req.order_no)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        order = await payment_service.refund_order(
            db,
            order,
            operator_id=current_user.id,
            reason=req.reason,
            amount=req.amount,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except payment_service.PaymentCallbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(request, {"order": _asdict_order(order)})


@router.patch("/plans/{plan_id}")
async def admin_billing_plan_update(
    plan_id: uuid.UUID = Path(...),
    req: PlanUpdateRequest = ...,
    request: Request = ...,
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """修改套餐配置（价格/名称/状态；版本自增 + 账务审计）。"""
    try:
        plan = await plan_service.update_plan(
            db,
            plan_id,
            current_user.id,
            name=req.name,
            price=req.price,
            status=req.status,
            description=req.description,
            reason=req.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")
    features = await plan_service.get_features(db, plan.id)
    return ok(
        request,
        {
            "plan": {
                "id": str(plan.id),
                "name": plan.name,
                "plan_type": plan.plan_type,
                "price": float(plan.price),
                "currency": plan.currency,
                "status": plan.status,
                "version": plan.version,
                "features": [
                    {
                        "feature_key": f.feature_key,
                        "limit_value": f.limit_value,
                        "limit_type": f.limit_type,
                        "enabled": f.enabled,
                    }
                    for f in features
                ],
            }
        },
    )


@router.get("/promotions")
async def admin_billing_promotions(
    request: Request,
    current_user: User = Depends(require_admin_roles("promotion_admin", "billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """优惠活动列表（promotion_admin）。"""
    promos = await promotion_service.list_promotions(db)
    return ok(
        request,
        {
            "promotions": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "code": p.code,
                    "promotion_type": p.promotion_type,
                    "discount_value": float(p.discount_value),
                    "quota_grant": p.quota_grant,
                    "usage_limit": p.usage_limit,
                    "per_user_limit": p.per_user_limit,
                    "starts_at": p.starts_at.isoformat() if p.starts_at else None,
                    "ends_at": p.ends_at.isoformat() if p.ends_at else None,
                    "status": p.status,
                }
                for p in promos
            ]
        },
    )


@router.post("/promotions")
async def admin_billing_promotions_create(
    req: PromotionCreateRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("promotion_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """创建优惠活动（promotion_admin）。"""
    try:
        promo = await promotion_service.create_promotion(
            db,
            name=req.name,
            promotion_type=req.promotion_type,
            discount_value=req.discount_value,
            quota_grant=req.quota_grant,
            code=req.code,
            usage_limit=req.usage_limit,
            per_user_limit=req.per_user_limit,
            starts_at=req.starts_at,
            ends_at=req.ends_at,
            operator_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(
        request,
        {
            "promotion": {
                "id": str(promo.id),
                "name": promo.name,
                "code": promo.code,
                "promotion_type": promo.promotion_type,
                "discount_value": float(promo.discount_value),
                "quota_grant": promo.quota_grant,
                "status": promo.status,
            }
        },
        status_code=201,
    )


@router.get("/risks")
async def admin_billing_risks(
    request: Request,
    user_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """风控事件列表（billing_admin）。"""
    events = await risk_service.list_risks(
        db, user_id=user_id, status=status, limit=limit
    )
    return ok(
        request,
        {
            "risks": [
                {
                    "id": str(ev.id),
                    "user_id": str(ev.user_id) if ev.user_id else None,
                    "event_type": ev.event_type,
                    "severity": ev.severity,
                    "action_taken": ev.action_taken,
                    "status": ev.status,
                    "evidence": ev.evidence,
                    "created_at": ev.created_at.isoformat(),
                }
                for ev in events
            ]
        },
    )


@router.post("/risks/{risk_id}/resolve")
async def admin_billing_risk_resolve(
    risk_id: uuid.UUID = Path(...),
    req: ResolveRiskRequest = ...,
    request: Request = ...,
    current_user: User = Depends(require_admin_roles("billing_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """风控处理（billing_admin）。"""
    event = await risk_service.resolve_risk(db, risk_id, result=req.result)
    if not event:
        raise HTTPException(status_code=404, detail="风控事件不存在")
    return ok(
        request,
        {
            "risk": {
                "id": str(event.id),
                "event_type": event.event_type,
                "status": event.status,
            }
        },
    )
