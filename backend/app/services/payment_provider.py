# 支付渠道统一封装（第二十批「商业化、订阅与计费系统」规格五）
# PaymentProvider 接口：业务层不直接调用具体渠道，只处理统一支付结果。
# 当前实现为本地 stub（LocalPaymentProvider），支持：
#   - create_checkout 创建支付会话
#   - confirm_payment  模拟确认支付（成功/失败/超时）
#   - refund_payment   退款
#   - cancel_subscription 取消渠道侧订阅
#   - verify_webhook   HMAC-SHA256 签名校验（防伪造回调）
import hashlib
import hmac
import json
import time
import uuid

from app.core.config import settings


class PaymentProviderError(Exception):
    pass


class PaymentTimeoutError(PaymentProviderError):
    pass


class PaymentProvider:
    """统一支付渠道接口（抽象基类）。"""

    name = "base"

    async def create_checkout(self, order_no: str, amount: float, currency: str, *, provider_order_id: str | None = None) -> dict:
        raise NotImplementedError

    async def confirm_payment(self, provider_order_id: str) -> dict:
        raise NotImplementedError

    async def refund_payment(self, provider_order_id: str, amount: float) -> dict:
        raise NotImplementedError

    async def cancel_subscription(self, provider_subscription_id: str) -> dict:
        raise NotImplementedError

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        raise NotImplementedError


class LocalPaymentProvider(PaymentProvider):
    """本地 stub 支付渠道：内存记账，签名 HMAC-SHA256。

    测试可控性：
    - fail_on_confirm=True   模拟渠道支付失败
    - timeout_on_confirm=True 模拟支付超时
    - refund_deny=True       模拟退款被渠道拒绝
    """

    name = "local_stub"

    def __init__(
        self,
        *,
        fail_on_confirm: bool = False,
        timeout_on_confirm: bool = False,
        refund_deny: bool = False,
    ) -> None:
        self._fail_on_confirm = fail_on_confirm
        self._timeout_on_confirm = timeout_on_confirm
        self._refund_deny = refund_deny
        self._secret = settings.billing_provider_secret

    async def create_checkout(self, order_no: str, amount: float, currency: str, *, provider_order_id: str | None = None) -> dict:
        pid = provider_order_id or f"stub_{uuid.uuid4().hex[:16]}"
        return {
            "provider": self.name,
            "provider_order_id": pid,
            "checkout_url": f"https://pay.stub.local/checkout/{pid}",
            "amount": amount,
            "currency": currency,
            "expires_in": 900,
        }

    async def confirm_payment(self, provider_order_id: str) -> dict:
        if self._timeout_on_confirm:
            raise PaymentTimeoutError("支付渠道超时")
        if self._fail_on_confirm:
            return {
                "status": "failed",
                "provider_order_id": provider_order_id,
                "message": "支付失败（stub 模拟）",
            }
        return {
            "status": "paid",
            "provider_order_id": provider_order_id,
            "message": "支付成功（stub 模拟）",
            "provider_transaction_no": f"txn_{uuid.uuid4().hex[:16]}",
        }

    async def refund_payment(self, provider_order_id: str, amount: float) -> dict:
        if self._refund_deny:
            return {
                "status": "denied",
                "provider_order_id": provider_order_id,
                "message": "渠道拒绝退款（stub 模拟）",
            }
        return {
            "status": "refunded",
            "provider_order_id": provider_order_id,
            "refund_amount": amount,
            "provider_refund_no": f"ref_{uuid.uuid4().hex[:16]}",
        }

    async def cancel_subscription(self, provider_subscription_id: str) -> dict:
        return {
            "status": "cancelled",
            "provider_subscription_id": provider_subscription_id,
        }

    def _sign_payload(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.new(
            self._secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """HMAC-SHA256 验签：拒绝伪造/篡改回调；未配置密钥时一律拒绝（fail closed）。"""
        if not signature or not self._secret:
            return False
        expected = self._sign_payload(payload)
        return hmac.compare_digest(expected, signature)

    def build_webhook_payload(
        self,
        *,
        event_id: str,
        event_type: str,
        provider_order_id: str,
        order_no: str,
        amount: float,
        currency: str = "CNY",
    ) -> tuple[dict, str]:
        """构造渠道回调（测试用）：返回 (payload, signature)。"""
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "provider_order_id": provider_order_id,
            "order_no": order_no,
            "amount": float(amount),
            "currency": currency,
            "timestamp": int(time.time()),
        }
        return payload, self._sign_payload(payload)


_provider_instances: dict[str, PaymentProvider] = {}


def get_payment_provider(name: str = "local_stub") -> PaymentProvider:
    """获取单例支付渠道实例。"""
    if name not in _provider_instances:
        _provider_instances[name] = LocalPaymentProvider()
    return _provider_instances[name]


def reset_provider(name: str = "local_stub") -> None:
    """测试辅助：重置渠道实例（模拟失败/超时场景）。"""
    _provider_instances.pop(name, None)


def install_provider(provider: PaymentProvider, name: str | None = None) -> None:
    """测试辅助：注入自定义渠道实例。"""
    key = name or provider.name
    _provider_instances[key] = provider
