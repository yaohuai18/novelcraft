# 商业化 API（用户端，第二十批「商业化、订阅与计费系统」规格二十二）
# GET /api/billing/plans|subscription|orders|invoices|quota|usage|events
# POST /api/billing/checkout|subscription/cancel|subscription/resume|promotion/validate|webhook|refund
# 组织：POST /api/billing/organizations、组织成员增删；风控申诉 POST /api/billing/risks/{risk_id}/appeal
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ok
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.billing_order import Invoice, Order
from app.models.billing_subscription import Subscription
from app.models.user import User
from app.schemas.billing_schemas import (
    AddMemberRequest,
    AppealRiskRequest,
    CancelSubscriptionRequest,
    CheckoutRequest,
    OrganizationCreateRequest,
    RefundRequest,
    ResumeSubscriptionRequest,
    ValidatePromotionRequest,
)
from app.services import (
    billing_event_service,
    entitlement_service,
    order_service,
    organization_service,
    payment_service,
    plan_service,
    promotion_service,
    quota_service,
    risk_service,
    subscription_service,
)

router = APIRouter(
    prefix="/api/billing",
    tags=["商业化 API", "实验"],
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


def _asdict_subscription(sub: Subscription) -> dict:
    return {
        "id": str(sub.id),
        "user_id": str(sub.user_id),
        "organization_id": str(sub.organization_id) if sub.organization_id else None,
        "plan_id": str(sub.plan_id),
        "provider": sub.provider,
        "status": sub.status,
        "current_period_start": sub.current_period_start.isoformat(),
        "current_period_end": sub.current_period_end.isoformat(),
        "cancel_at_period_end": sub.cancel_at_period_end,
        "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
    }


@router.get("/plans")
async def billing_plans(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """套餐列表（含能力项，服务端配置为准）。"""
    plans = await plan_service.list_plans(db, only_active=True)
    data = []
    for plan in plans:
        features = await plan_service.get_features(db, plan.id)
        data.append(
            {
                "id": str(plan.id),
                "name": plan.name,
                "plan_type": plan.plan_type,
                "billing_interval": plan.billing_interval,
                "price": float(plan.price),
                "currency": plan.currency,
                "description": plan.description,
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
        )
    return ok(request, {"plans": data})


@router.get("/subscription")
async def billing_subscription(
    request: Request,
    organization_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前订阅（用户或组织）。"""
    sub = await subscription_service.get_user_subscription(
        db, current_user.id, organization_id=organization_id
    )
    if sub:
        sub = await subscription_service.expire_if_ended(db, sub, user_id=current_user.id)
    return ok(
        request,
        {"subscription": _asdict_subscription(sub) if sub else None},
    )


@router.post("/checkout")
async def billing_checkout(
    req: CheckoutRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建订单并发起支付（本地 stub 自动确认）。"""
    plan = await plan_service.get_plan(db, req.plan_id, only_active=True)
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在或已停售")
    if req.organization_id is not None:
        org = await organization_service.get_organization(db, req.organization_id)
        if not org:
            raise HTTPException(status_code=404, detail="组织不存在")
        member = await organization_service.get_member(db, org.id, current_user.id)
        if not member:
            raise HTTPException(status_code=403, detail="非组织成员")
    try:
        order = await order_service.create_order(
            db,
            user_id=current_user.id,
            order_type=req.order_type,
            plan_id=req.plan_id,
            organization_id=req.organization_id,
            promotion_code=req.promotion_code,
            interval=req.interval,
            seats=req.seats,
            description=req.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await payment_service.confirm_order_payment(db, order)
    return ok(
        request,
        {
            "order": _asdict_order(order),
            "payment": result,
        },
        status_code=201,
    )


@router.post("/subscription/cancel")
async def billing_subscription_cancel(
    req: CancelSubscriptionRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消订阅（默认周期末取消，保留权益到周期末）。"""
    sub = await subscription_service.get_user_subscription(db, current_user.id)
    if not sub:
        raise HTTPException(status_code=404, detail="无有效订阅")
    try:
        sub = await subscription_service.cancel_subscription(
            db, sub, immediate=req.immediate, user_id=current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(request, {"subscription": _asdict_subscription(sub)})


@router.post("/subscription/resume")
async def billing_subscription_resume(
    req: ResumeSubscriptionRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复订阅（周期末取消未到期可恢复）。"""
    sub = await subscription_service.get_user_subscription(db, current_user.id)
    if not sub:
        raise HTTPException(status_code=404, detail="无订阅记录")
    try:
        sub = await subscription_service.resume_subscription(
            db, sub, user_id=current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(request, {"subscription": _asdict_subscription(sub)})


@router.get("/orders")
async def billing_orders(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """我的订单列表。"""
    orders = await order_service.list_orders(
        db, user_id=current_user.id, status=status, limit=limit
    )
    return ok(
        request,
        {"orders": [_asdict_order(o) for o in orders], "count": len(orders)},
    )


@router.get("/invoices")
async def billing_invoices(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """我的发票列表。"""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.user_id == current_user.id)
        .order_by(Invoice.created_at.desc())
        .limit(100)
    )
    invoices = result.scalars().all()
    return ok(
        request,
        {
            "invoices": [
                {
                    "id": str(inv.id),
                    "order_id": str(inv.order_id) if inv.order_id else None,
                    "title": inv.title,
                    "tax_no": inv.tax_no,
                    "email": inv.email,
                    "amount": float(inv.amount),
                    "currency": inv.currency,
                    "status": inv.status,
                    "created_at": inv.created_at.isoformat(),
                }
                for inv in invoices
            ]
        },
    )


@router.get("/quota")
async def billing_quota(
    request: Request,
    organization_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """我的额度账户（用户/组织）。"""
    owner_type = "org" if organization_id else "user"
    owner_id = organization_id or current_user.id
    summary = await quota_service.get_usage_summary(db, owner_type, owner_id)
    for item in summary:
        if item["available"] <= 0 and item["total"] > 0:
            await billing_event_service.notify_quota_warning(
                db, current_user.id, item["quota_type"], item["available"]
            )
            await db.commit()
    return ok(request, {"quota": summary})


@router.get("/usage")
async def billing_usage(
    request: Request,
    organization_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """有效套餐 + 权益限制 + 额度状态（综合视图）。"""
    state = await entitlement_service.usage_state(
        db,
        user_id=current_user.id,
        organization_id=organization_id,
    )
    return ok(request, {"usage": state})


@router.post("/promotion/validate")
async def billing_promotion_validate(
    req: ValidatePromotionRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """校验优惠码可用性（不落订单）。"""
    check = await promotion_service.validate_promotion(
        db, req.code, current_user.id
    )
    promo = check.get("promotion")
    return ok(
        request,
        {
            "valid": check["valid"],
            "reason": check["reason"],
            "promotion_id": str(promo.id) if promo else None,
            "promotion_name": promo.name if promo else None,
            "discount": check.get("discount", 0),
            "quota_grant": check.get("quota_grant", 0),
        },
    )


@router.post("/webhook")
async def billing_webhook(
    req: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """支付渠道回调（幂等；签名在 X-Billing-Signature 头）。"""
    signature = request.headers.get("X-Billing-Signature") or ""
    try:
        result = await payment_service.process_webhook(db, req, signature)
    except payment_service.PaymentCallbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(request, {"result": result})


@router.post("/refund")
async def billing_refund(
    req: RefundRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户申请退款（支持全额/部分；已消费 AI 额度不退款）。"""
    order = await order_service.get_order(db, order_no=req.order_no)
    if not order or order.user_id != current_user.id:
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


@router.post("/organizations")
async def billing_create_organization(
    req: OrganizationCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建团队组织（绑定团队套餐 + 共享额度账户）。"""
    org = await organization_service.create_organization(
        db,
        name=req.name,
        owner_id=current_user.id,
        billing_email=req.billing_email,
    )
    return ok(
        request,
        {
            "organization": {
                "id": str(org.id),
                "name": org.name,
                "owner_id": str(org.owner_id),
                "plan_id": str(org.plan_id) if org.plan_id else None,
                "billing_email": org.billing_email,
                "status": org.status,
            }
        },
        status_code=201,
    )


@router.post("/organizations/{org_id}/members")
async def billing_add_member(
    org_id: uuid.UUID = Path(...),
    req: AddMemberRequest = ...,
    request: Request = ...,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """添加团队成员（校验套餐成员上限；超限需购买额外席位）。"""
    org = await organization_service.get_organization(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在")
    if org.owner_id != current_user.id and current_user.role != "super_admin":
        raise HTTPException(status_code=403, detail="仅组织所有者可添加成员")
    try:
        member = await organization_service.add_member(
            db, org, req.user_id, role=req.role, operator_id=current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(
        request,
        {
            "member": {
                "organization_id": str(member.organization_id),
                "user_id": str(member.user_id),
                "role": member.role,
                "status": member.status,
            }
        },
        status_code=201,
    )


@router.delete("/organizations/{org_id}/members/{user_id}")
async def billing_remove_member(
    org_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
    request: Request = ...,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除团队成员（数据归属组织，不删除数据）。"""
    org = await organization_service.get_organization(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在")
    if org.owner_id != current_user.id and current_user.role != "super_admin":
        raise HTTPException(status_code=403, detail="仅组织所有者可移除成员")
    try:
        removed = await organization_service.remove_member(
            db, org, user_id, operator_id=current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="成员不存在")
    return ok(request, {"removed": True})


@router.get("/events")
async def billing_events(
    request: Request,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """订阅/支付/额度事件日志。"""
    events = await billing_event_service.list_events(
        db, user_id=current_user.id, event_type=event_type, limit=limit
    )
    return ok(
        request,
        {
            "events": [
                {
                    "id": str(ev.id),
                    "event_type": ev.event_type,
                    "payload": ev.payload,
                    "created_at": ev.created_at.isoformat(),
                }
                for ev in events
            ]
        },
    )


@router.post("/risks/{risk_id}/appeal")
async def billing_risk_appeal(
    risk_id: uuid.UUID = Path(...),
    req: AppealRiskRequest = ...,
    request: Request = ...,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """风控申诉渠道（避免误伤正常用户）。"""
    event = await risk_service.appeal_risk(
        db, risk_id, current_user.id, reason=req.reason
    )
    if not event:
        raise HTTPException(status_code=404, detail="风控事件不存在或无权限")
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
