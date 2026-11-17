# Webhook 服务（第十九批「移动端适配与开放 API」规格十八/十九）
# 订阅管理：创建 / 更新 / 删除 / 列表 / 测试发送。
# 事件投递：签名（HMAC-SHA256）、指数退避重试、超限暂停订阅、记录失败原因。
# 发送头：X-Event-Id / X-Event-Type / X-Event-Timestamp / X-Event-Signature。
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.core.config import settings
from app.models.webhook import (
    WEBHOOK_EVENTS,
    WEBHOOK_STATUSES,
    WebhookDelivery,
    WebhookSubscription,
)

# 最大重试次数与指数退避基数
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2  # 首次重试 2s，之后 4s / 8s / 16s
DELIVERY_TIMEOUT = 10.0


def _validate_events(events: list[str]) -> list[str]:
    cleaned = []
    for e in events or []:
        e = e.strip()
        if e not in WEBHOOK_EVENTS:
            raise ApiError(
                "validation_failed",
                f"非法事件类型: {e}（可用: {', '.join(WEBHOOK_EVENTS)}）",
                status_code=422,
            )
        cleaned.append(e)
    if not cleaned:
        raise ApiError(
            "validation_failed", "必须至少订阅一个事件类型", status_code=422
        )
    return cleaned


async def create_subscription(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID | None,
    endpoint: str,
    events: list[str],
) -> tuple[WebhookSubscription, str]:
    """创建订阅，返回 (订阅记录, 明文 secret)。secret 仅返回一次。"""
    cleaned = _validate_events(events)
    if not endpoint.startswith(("http://", "https://")):
        raise ApiError(
            "validation_failed", "endpoint 必须是 http(s) 地址", status_code=422
        )

    secret = secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        user_id=user_id,
        app_id=app_id,
        endpoint=endpoint.strip()[:500],
        events=",".join(cleaned),
        secret=secret,
        status="active",
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub, secret


async def get_subscription_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    sub_id: uuid.UUID,
) -> WebhookSubscription:
    sub = await db.get(WebhookSubscription, sub_id)
    if sub is None or sub.user_id != user_id:
        raise ApiError("not_found", "Webhook 订阅不存在", status_code=404)
    return sub


async def update_subscription(
    db: AsyncSession,
    sub: WebhookSubscription,
    *,
    endpoint: str | None = None,
    events: list[str] | None = None,
    status: str | None = None,
) -> WebhookSubscription:
    if endpoint is not None:
        if not endpoint.startswith(("http://", "https://")):
            raise ApiError(
                "validation_failed", "endpoint 必须是 http(s) 地址", status_code=422
            )
        sub.endpoint = endpoint.strip()[:500]
    if events is not None:
        cleaned = _validate_events(events)
        sub.events = ",".join(cleaned)
    if status is not None:
        if status not in WEBHOOK_STATUSES:
            raise ApiError(
                "validation_failed", f"非法状态: {status}", status_code=422
            )
        sub.status = status
        if status == "active":
            sub.consecutive_failures = 0
            sub.failure_reason = None
    await db.commit()
    await db.refresh(sub)
    return sub


async def delete_subscription(
    db: AsyncSession,
    sub: WebhookSubscription,
) -> None:
    await db.delete(sub)
    await db.commit()


async def list_subscriptions(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[WebhookSubscription]:
    result = await db.execute(
        select(WebhookSubscription)
        .where(WebhookSubscription.user_id == user_id)
        .order_by(WebhookSubscription.created_at.desc())
    )
    return list(result.scalars().all())


# ---------- 事件投递 ----------

def _sign_payload(secret: str, payload: str, timestamp: str, event_id: str) -> str:
    msg = f"{event_id}.{timestamp}.{payload}".encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


async def _send_once(
    sub: WebhookSubscription,
    delivery: WebhookDelivery,
) -> bool:
    """单次发送：返回是否成功。失败时更新 delivery 错误信息。"""
    event_type = delivery.event_type
    payload_text = delivery.payload
    event_id = delivery.event_id
    timestamp = str(int(datetime.utcnow().timestamp()))

    headers = {
        "Content-Type": "application/json",
        "X-Event-Id": event_id,
        "X-Event-Type": event_type,
        "X-Event-Timestamp": timestamp,
        "X-Event-Signature": _sign_payload(
            sub.secret, payload_text, timestamp, event_id
        ),
        "X-Request-Id": f"wh-{delivery.id.hex[:12]}",
        "User-Agent": "NovelCraft-Webhook/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as client:
            resp = await client.post(sub.endpoint, content=payload_text, headers=headers)
        delivery.attempts += 1
        delivery.response_status = resp.status_code
        if 200 <= resp.status_code < 300:
            delivery.status = "sent"
            return True
        delivery.status = "failed"
        delivery.error_message = f"HTTP {resp.status_code}"
        return False
    except Exception as exc:  # noqa: BLE001 - 网络异常统一视为失败
        delivery.attempts += 1
        delivery.status = "failed"
        delivery.error_message = str(exc)[:500]
        return False


async def deliver_event(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    event_type: str,
    payload: dict,
) -> list[WebhookDelivery]:
    """向该用户全部活跃订阅投递事件。返回创建的投递记录。"""
    if event_type not in WEBHOOK_EVENTS:
        return []
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.user_id == user_id,
            WebhookSubscription.status.in_(("active",)),
        )
    )
    subs = list(result.scalars().all())

    event_id = f"{uuid.uuid4().hex[:24]}"
    payload_text = json.dumps(payload, ensure_ascii=False, default=str)
    deliveries = []

    for sub in subs:
        subscribed = {
            e.strip() for e in (sub.events or "").split(",") if e.strip()
        }
        if event_type not in subscribed:
            continue
        delivery = WebhookDelivery(
            subscription_id=sub.id,
            event_id=event_id,
            event_type=event_type,
            payload=payload_text,
            status="pending",
        )
        db.add(delivery)
        deliveries.append(delivery)

    await db.commit()
    for d in deliveries:
        await db.refresh(d)
    return deliveries


async def _process_delivery(
    db: AsyncSession,
    delivery: WebhookDelivery,
    sub: WebhookSubscription,
) -> None:
    """处理一次投递（含指数退避重试）。失败累计超限则暂停订阅。"""
    if sub.status != "active":
        delivery.status = "failed"
        delivery.error_message = "订阅已暂停，跳过投递"
        await db.commit()
        return

    ok = await _send_once(sub, delivery)
    now = datetime.utcnow()

    if ok:
        delivery.status = "sent"
        delivery.next_attempt_at = None
        sub.last_success_at = now
        sub.consecutive_failures = 0
        sub.failure_reason = None
    else:
        if delivery.attempts < MAX_ATTEMPTS:
            delivery.status = "pending"
            delay = BACKOFF_BASE_SECONDS * (2 ** (delivery.attempts - 1))
            delivery.next_attempt_at = now + timedelta(seconds=delay)
        else:
            delivery.status = "failed"
            delivery.next_attempt_at = None
            sub.status = "paused"
            sub.last_failure_at = now
            sub.consecutive_failures = (sub.consecutive_failures or 0) + 1
            sub.failure_reason = (
                f"连续失败超过 {MAX_ATTEMPTS} 次，已暂停订阅"
            )
    await db.commit()


async def run_pending_deliveries(
    db: AsyncSession,
    max_items: int = 50,
) -> dict:
    """后台重试任务：处理所有到期的 pending 投递。"""
    now = datetime.utcnow()
    result = await db.execute(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == "pending",
            (
                WebhookDelivery.next_attempt_at.is_(None)
                | (WebhookDelivery.next_attempt_at <= now)
            ),
        )
        .order_by(WebhookDelivery.created_at.asc())
        .limit(max_items)
    )
    items = list(result.scalars().all())

    sent = 0
    failed = 0
    for d in items:
        sub = await db.get(WebhookSubscription, d.subscription_id)
        if sub is None:
            d.status = "failed"
            d.error_message = "订阅不存在"
            await db.commit()
            continue
        await _process_delivery(db, d, sub)
        if d.status == "sent":
            sent += 1
        else:
            failed += 1

    return {"processed": len(items), "sent": sent, "failed": failed}
