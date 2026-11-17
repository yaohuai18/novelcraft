# 商业化 API 请求/响应模型（第二十批「商业化、订阅与计费系统」规格二十二）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

VALID_ORDER_TYPES = ["subscription", "ai_tokens", "storage", "team_seat", "upgrade"]


class CheckoutRequest(BaseModel):
    plan_id: uuid.UUID = Field(..., description="目标套餐 ID")
    order_type: str = Field("subscription", description="订单类型")
    interval: str = Field("monthly", description="计费周期 monthly/yearly/one_time")
    organization_id: uuid.UUID | None = None
    promotion_code: str | None = None
    seats: int = Field(0, ge=0, description="额外席位数量（team_seat）")
    description: str | None = None

    def model_post_init(self, __context) -> None:
        if self.order_type not in VALID_ORDER_TYPES:
            raise ValueError(f"无效订单类型: {self.order_type}")


class CancelSubscriptionRequest(BaseModel):
    immediate: bool = False


class ResumeSubscriptionRequest(BaseModel):
    pass


class RefundRequest(BaseModel):
    order_no: str
    amount: float | None = Field(None, gt=0, description="退款金额，缺省全额")
    reason: str = Field("用户申请退款", max_length=300)


class QuotaAdjustRequest(BaseModel):
    owner_type: str = Field("user", description="user / org")
    owner_id: uuid.UUID
    quota_type: str = "ai_tokens"
    amount: int
    reason: str = Field("管理员调整额度", max_length=300)
    expires_in_days: int | None = Field(None, gt=0)


class PlanUpdateRequest(BaseModel):
    name: str | None = None
    price: float | None = Field(None, ge=0)
    description: str | None = None
    status: str | None = None
    reason: str = Field("套餐配置变更", max_length=300)


class PromotionCreateRequest(BaseModel):
    name: str
    promotion_type: str = Field(..., description="percent/fixed/grant_quota/upgrade_diff/trial")
    discount_value: float = 0
    quota_grant: int | None = None
    code: str | None = None
    usage_limit: int | None = Field(None, ge=1)
    per_user_limit: int = Field(1, ge=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class ValidatePromotionRequest(BaseModel):
    code: str


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    billing_email: str | None = None


class AddMemberRequest(BaseModel):
    user_id: uuid.UUID
    role: str = Field("member", description="owner/admin/member")


class AppealRiskRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class ResolveRiskRequest(BaseModel):
    result: str = Field(min_length=1, max_length=300)


class ResolveReconciliationRequest(BaseModel):
    result_text: str = Field(min_length=1, max_length=1000)


class RunReconciliationRequest(BaseModel):
    start: datetime
    end: datetime
    override_provider_total: float | None = None


class WebhookPayloadRequest(BaseModel):
    provider: str = "local_stub"
    event: str = "payment.success"
    provider_order_id: str | None = None
    event_id: str | None = None
    amount: float | None = None
    currency: str = "CNY"
    extra: dict | None = None
