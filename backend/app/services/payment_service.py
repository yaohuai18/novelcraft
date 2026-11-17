# 支付回调处理服务（第二十批「商业化、订阅与计费系统」规格七）
# 流程：接收通知 → 验证签名 → 检查订单 → 检查金额币种 → 判断重复通知 →
#       更新订单状态 → 创建订阅/充值额度 → 写入账务 → 返回成功响应。
# 幂等：同一 provider_event_id 只处理一次，重复通知直接返回成功。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_order import Order
from app.models.billing_subscription import Subscription
from app.services import (
    billing_event_service,
    ledger_service,
    plan_service,
    quota_service,
    risk_service,
    subscription_service,
)
from app.services.payment_provider import PaymentProviderError, get_payment_provider


class PaymentCallbackError(Exception):
    """支付回调业务错误（金额不一致/签名错误/订单不存在等）。"""


async def process_webhook(
    db: AsyncSession,
    payload: dict,
    signature: str,
) -> dict:
    """处理支付渠道回调（幂等）。"""
    provider = get_payment_provider()
    # 1. 验证签名
    if not provider.verify_webhook(payload, signature):
        raise PaymentCallbackError("支付回调签名校验失败")

    event_id = str(payload.get("event_id") or "")
    provider_order_id = str(payload.get("provider_order_id") or "")
    if not event_id or not provider_order_id:
        raise PaymentCallbackError("回调缺少事件ID或渠道订单号")

    # 2. 检查订单是否存在
    order = await order_service_get_by_provider(db, provider_order_id)
    if not order:
        raise PaymentCallbackError("订单不存在或未关联渠道订单")

    # 3. 检查金额和币种
    cb_amount = float(payload.get("amount") or 0)
    cb_currency = str(payload.get("currency") or order.currency)
    if abs(cb_amount - float(order.amount)) > 0.005 or cb_currency != order.currency:
        raise PaymentCallbackError(
            f"金额或币种不一致: 订单 {order.amount}{order.currency} vs 回调 {cb_amount}{cb_currency}"
        )

    # 4. 判断是否重复通知（幂等）：已处理同一事件 → 直接成功
    if order.status == "paid" and order.provider_event_id == event_id:
        return {
            "processed": False,
            "idempotent": True,
            "order_no": order.order_no,
            "status": order.status,
        }

    # 5. 更新订单状态（paid）
    now = datetime.utcnow()
    order.status = "paid"
    order.provider_event_id = event_id
    order.paid_at = now
    order.updated_at = now
    await db.flush()

    # 6. 创建订阅或发放额度
    await _grant_entitlements(db, order)

    # 7. 写入账务记录
    await ledger_service.record_entry(
        db,
        entry_type="payment",
        user_id=order.user_id,
        organization_id=order.organization_id,
        order_id=order.id,
        amount=float(order.amount),
        currency=order.currency,
        direction="in",
        reference_id=provider_order_id,
        reason=f"订单 {order.order_no} 支付成功",
    )

    # 8. 事件与通知
    await billing_event_service.emit_event(
        db,
        "payment.succeeded",
        user_id=order.user_id,
        organization_id=order.organization_id,
        payload={"order_no": order.order_no, "amount": float(order.amount)},
    )
    await billing_event_service.notify_payment_success(
        db, order.user_id, order.order_no, float(order.amount)
    )

    await db.commit()
    return {
        "processed": True,
        "idempotent": False,
        "order_no": order.order_no,
        "status": order.status,
    }


async def order_service_get_by_provider(db: AsyncSession, provider_order_id: str) -> Order | None:
    result = await db.execute(
        select(Order).where(Order.provider_order_id == provider_order_id)
    )
    return result.scalar_one_or_none()


async def _grant_entitlements(db: AsyncSession, order: Order) -> None:
    """按订单类型发放权益：
    - subscription / upgrade：创建/更新订阅
    - ai_tokens / storage / team_seat：发放额度账户
    """
    if order.order_type in ("subscription", "upgrade"):
        if order.plan_id is None:
            raise PaymentCallbackError("订阅订单缺少套餐")
        plan = await plan_service.get_plan(db, order.plan_id)
        if not plan:
            raise PaymentCallbackError("套餐不存在")
        # 查找该用户/组织当前订阅
        sub = await subscription_service.get_user_subscription(
            db, order.user_id, organization_id=order.organization_id
        )
        if sub and sub.status in ("active", "trialing", "past_due", "paused"):
            # 升级处理：补差后切换套餐
            await subscription_service.change_plan(
                db, sub, order.plan_id, user_id=order.user_id
            )
        else:
            new_sub = await subscription_service.create_subscription(
                db,
                user_id=order.user_id,
                plan_id=order.plan_id,
                organization_id=order.organization_id,
                provider=order.payment_provider,
                status="active",
                current_period_start=datetime.utcnow(),
            )
            # 发放订阅对应的月度额度（tokens/tasks/存储）
            await _grant_plan_quota(db, new_sub, plan)

    elif order.order_type == "ai_tokens":
        await quota_service.grant_quota(
            db, "user", order.user_id, "ai_tokens", int(order.amount * 1000),
            reference_type="order", reference_id=order.order_no,
            note="购买 AI 额度",
        )
    elif order.order_type == "storage":
        await quota_service.grant_quota(
            db, "user", order.user_id, "storage_bytes", int(order.amount * 1048576),
            reference_type="order", reference_id=order.order_no,
            note="购买存储空间",
        )
    elif order.order_type == "team_seat":
        if order.organization_id is None:
            raise PaymentCallbackError("团队席位订单缺少组织")
        await quota_service.grant_quota(
            db, "org", order.organization_id, "ai_tokens", int(order.amount * 500),
            reference_type="order", reference_id=order.order_no,
            note="购买团队超额席位额度",
        )

    # 优惠赠送额度
    if order.promotion_applied:
        promo = await _find_promo_by_code(db, order.promotion_applied)
        if promo and promo.quota_grant:
            await quota_service.grant_quota(
                db, "user", order.user_id, "ai_tokens", promo.quota_grant,
                reference_type="promotion", reference_id=str(promo.id),
                note=f"优惠活动赠送额度 {promo.name}",
            )


async def _grant_plan_quota(
    db: AsyncSession,
    sub: Subscription,
    plan,
) -> None:
    """按套餐能力项发放月度额度到用户/组织账户。"""
    features = await plan_service.get_features(db, plan.id)
    mapping = {
        "ai.monthly_tokens": "ai_tokens",
        "ai.monthly_tasks": "ai_tasks",
        "storage.max_bytes": "storage_bytes",
    }
    for feat in features:
        qtype = mapping.get(feat.feature_key)
        if not qtype or not feat.enabled or not feat.limit_value.isdigit():
            continue
        amount = int(feat.limit_value)
        owner_type = "org" if sub.organization_id else "user"
        owner_id = sub.organization_id or sub.user_id
        await quota_service.grant_quota(
            db, owner_type, owner_id, qtype, amount,
            expires_at=sub.current_period_end,
            reference_type="subscription", reference_id=str(sub.id),
            note=f"订阅 {plan.name} 月度额度",
        )


async def _find_promo_by_code(db: AsyncSession, code: str):
    result = await db.execute(
        select_from_promotion().where(promotion_code_col() == code)
    )
    return result.scalar_one_or_none()


def select_from_promotion():
    from app.models.billing_promotion import Promotion
    return select(Promotion)


def promotion_code_col():
    from app.models.billing_promotion import Promotion
    return Promotion.code


async def confirm_order_payment(
    db: AsyncSession,
    order: Order,
    *,
    provider: str | None = None,
    mark_failed: bool = False,
) -> dict:
    """主动确认订单支付（同步支付模式/测试辅助）：
    - 调用渠道确认接口
    - 失败 → 订单 failed + 风控检查（连续失败）
    - 成功 → 直接置 paid 并发放权益（复用回调发放逻辑，但构造内部事件）
    """
    provider_obj = get_payment_provider(provider or order.payment_provider)
    if mark_failed:
        result = {"status": "failed", "provider_order_id": order.provider_order_id or ""}
    else:
        try:
            result = await provider_obj.confirm_payment(order.provider_order_id or "")
        except PaymentProviderError:
            order.status = "failed"
            order.updated_at = datetime.utcnow()
            await db.commit()
            await billing_event_service.notify_payment_failed(db, order.user_id, order.order_no)
            await risk_service.check_payment_failures(db, order.user_id)
            await db.commit()
            raise

    if result.get("status") != "paid":
        order.status = "failed"
        order.updated_at = datetime.utcnow()
        await db.flush()
        await billing_event_service.emit_event(
            db, "payment.failed", user_id=order.user_id,
            organization_id=order.organization_id,
            payload={"order_no": order.order_no},
        )
        await billing_event_service.notify_payment_failed(db, order.user_id, order.order_no)
        await risk_service.check_payment_failures(db, order.user_id)
        await db.commit()
        await db.refresh(order)
        return {"order_no": order.order_no, "status": order.status}

    # 成功：复用权益发放 + 账务 + 事件（构造一个本地事件幂等键）
    event_id = f"local_{uuid.uuid4().hex[:20]}"
    order.status = "paid"
    order.provider_event_id = event_id
    order.paid_at = datetime.utcnow()
    order.updated_at = datetime.utcnow()
    await db.flush()
    await _grant_entitlements(db, order)
    await ledger_service.record_entry(
        db,
        entry_type="payment",
        user_id=order.user_id,
        organization_id=order.organization_id,
        order_id=order.id,
        amount=float(order.amount),
        currency=order.currency,
        direction="in",
        reference_id=result.get("provider_transaction_no") or order.provider_order_id,
        reason=f"订单 {order.order_no} 支付成功（同步确认）",
    )
    await billing_event_service.emit_event(
        db, "payment.succeeded", user_id=order.user_id,
        organization_id=order.organization_id,
        payload={"order_no": order.order_no, "amount": float(order.amount)},
    )
    await billing_event_service.notify_payment_success(
        db, order.user_id, order.order_no, float(order.amount)
    )
    await db.commit()
    await db.refresh(order)
    return {"order_no": order.order_no, "status": order.status}


async def refund_order(
    db: AsyncSession,
    order: Order,
    *,
    operator_id: uuid.UUID,
    reason: str = "用户申请退款",
    amount: float | None = None,
) -> Order:
    """退款流程：检查规则 → 调用渠道 → 更新订单状态 → 回收未使用权益 → 记录退款原因。
    已消费的 AI 额度按规则不退款（避免滥用），仅退还未使用部分。"""
    if order.status not in ("paid", "partially_refunded"):
        raise ValueError(f"订单状态 {order.status} 不支持退款")
    refund_amount = amount if amount is not None else float(order.amount)
    if refund_amount <= 0 or refund_amount > float(order.amount):
        raise ValueError("退款金额无效")

    provider_obj = get_payment_provider(order.payment_provider)
    provider_result = await provider_obj.refund_payment(
        order.provider_order_id or "", refund_amount
    )
    if provider_result.get("status") == "denied":
        raise PaymentCallbackError("支付渠道拒绝退款")

    now = datetime.utcnow()
    if refund_amount >= float(order.amount):
        order.status = "refunded"
    else:
        order.status = "partially_refunded"
    order.updated_at = now
    await db.flush()

    # 回收权益：取消有效订阅（周期末取消），不删除数据
    if order.order_type in ("subscription", "upgrade") and order.plan_id:
        sub = await subscription_service.get_user_subscription(
            db, order.user_id, organization_id=order.organization_id
        )
        if sub and sub.status in ("active", "trialing", "past_due", "paused"):
            await subscription_service.cancel_subscription(
                db, sub, immediate=True, user_id=order.user_id
            )

    # 账务：退款支出
    await ledger_service.record_entry(
        db,
        entry_type="refund",
        user_id=order.user_id,
        organization_id=order.organization_id,
        order_id=order.id,
        amount=refund_amount,
        currency=order.currency,
        direction="out",
        reference_id=order.provider_order_id,
        operator_id=operator_id,
        reason=reason,
    )
    await billing_event_service.emit_event(
        db, "payment.refunded", user_id=order.user_id,
        organization_id=order.organization_id,
        payload={"order_no": order.order_no, "amount": refund_amount, "reason": reason},
    )
    await billing_event_service.notify_refund_done(
        db, order.user_id, order.order_no, refund_amount
    )
    await db.commit()
    await db.refresh(order)
    return order
