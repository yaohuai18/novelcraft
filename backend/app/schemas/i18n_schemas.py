# 第二十一批「国际化、多语言和多区域部署」Schema（Pydantic）
# i18n 资源 / 用户语言时区 / 作品翻译 / AI 术语表 / 多币种 / 地区价格 / 税费 / 通知模板 / 合规
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ---------- i18n 资源 ----------
class I18nResourceCreate(BaseModel):
    language: str
    key: str
    value: str
    category: str = "common"
    placeholders: list[str] | None = None
    action: str = "modify"
    max_length: int = 500


class I18nResourceResponse(BaseModel):
    id: uuid.UUID
    language: str
    key: str
    value: str
    category: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class I18nResourceVersionResponse(BaseModel):
    id: uuid.UUID
    resource_id: uuid.UUID
    version: int
    value: str
    action: str
    created_at: datetime

    model_config = {"from_attributes": True}


class I18nCheckResult(BaseModel):
    language: str
    missing_keys: list[str]
    placeholder_inconsistencies: list[str]
    long_text_keys: list[str]
    total_keys: int


class I18nResourceListResponse(BaseModel):
    items: list[I18nResourceResponse]
    total: int


# ---------- 用户语言/时区设置 ----------
class UserLocaleUpdate(BaseModel):
    ui_language: str | None = None
    content_language: str | None = None
    notification_language: str | None = None
    ai_output_language: str | None = None
    timezone: str | None = None
    locale: str | None = None
    date_format: str | None = None
    time_format: str | None = None
    region: str | None = None
    browser_language: str | None = None
    device_language: str | None = None
    manual_languages: dict | None = None


class UserLocaleResponse(BaseModel):
    user_id: uuid.UUID
    ui_language: str | None
    content_language: str | None
    notification_language: str | None
    ai_output_language: str | None
    timezone: str | None
    locale: str | None
    date_format: str | None
    time_format: str | None
    region: str | None
    browser_language: str | None
    device_language: str | None
    manual_languages: dict
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResolvedLanguagesResponse(BaseModel):
    ui: str
    content: str
    notification: str
    ai_output: str


class UserDisplayContextResponse(BaseModel):
    timezone: str
    locale: str
    date_format: str
    time_format: str
    region: str
    languages: ResolvedLanguagesResponse


# ---------- 作品翻译 ----------
class ProjectLanguagesUpdate(BaseModel):
    default_language: str | None = None
    supported_languages: list[str] | None = None


class ProjectTranslationCreate(BaseModel):
    project_id: uuid.UUID
    language: str
    title: str
    description: str | None = None


class ProjectTranslationUpdate(BaseModel):
    title: str | None = None
    description: str | None = None


class ProjectTranslationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    language: str
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChapterTranslationCreate(BaseModel):
    chapter_id: uuid.UUID
    language: str
    title: str
    content: str
    translation_status: str = "machine_translated"


class ChapterTranslationUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    translation_status: str | None = None


class ChapterTranslationResponse(BaseModel):
    id: uuid.UUID
    chapter_id: uuid.UUID
    language: str
    title: str
    content: str
    translation_status: str
    source_version_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------- AI 术语表 ----------
class GlossaryTermCreate(BaseModel):
    project_id: uuid.UUID | None = None
    source_language: str
    target_language: str
    source_text: str
    target_text: str
    term_type: str = "proper_noun"
    force_exact: bool = True
    scope: str = "project"


class GlossaryTermResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    source_language: str
    target_language: str
    source_text: str
    target_text: str
    term_type: str
    force_exact: bool
    scope: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AIProfileResponse(BaseModel):
    input_language: str
    output_language: str
    variant: str | None
    tone: str | None
    glossary_terms: list[GlossaryTermResponse]


# ---------- 多币种 ----------
class CurrencyCreate(BaseModel):
    code: str
    symbol: str
    decimal_digits: int
    exchange_rate: str
    status: str = "active"


class CurrencyUpdate(BaseModel):
    exchange_rate: str


class CurrencyResponse(BaseModel):
    id: uuid.UUID
    code: str
    symbol: str
    decimal_digits: int
    exchange_rate: str
    status: str
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------- 地区价格 / 税费 ----------
class PlanPriceCreate(BaseModel):
    plan_id: uuid.UUID
    region: str
    currency: str
    amount: int = Field(description="最小货币单位金额")
    billing_interval: str = "month"
    tax_included: bool = True
    tax_rate: float = 0.0
    status: str = "active"


class PlanPriceUpdate(BaseModel):
    amount: int | None = None
    tax_rate: float | None = None
    tax_included: bool | None = None
    status: str | None = None


class PlanPriceResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    region: str
    currency: str
    amount: int
    billing_interval: str
    tax_included: bool
    tax_rate: float
    status: str

    model_config = {"from_attributes": True}


class PriceQuoteResponse(BaseModel):
    plan_id: uuid.UUID
    region: str
    currency: str
    gross_amount_minor: int
    tax_rate: float
    tax_amount_minor: int
    net_amount_minor: int
    tax_included: bool


class TaxRecordResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    region: str
    tax_rate: float
    tax_amount_minor: int
    gross_amount_minor: int
    net_amount_minor: int
    tax_included: bool
    tax_identifier: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 地区化通知模板 ----------
class NotificationTemplateCreate(BaseModel):
    template_key: str
    language: str
    category: str = "transaction"
    title_template: str
    content_template: str
    placeholders: list[str] | None = None
    legal_notice: str | None = None


class NotificationTemplateResponse(BaseModel):
    id: uuid.UUID
    template_key: str
    language: str
    category: str
    title_template: str
    content_template: str
    placeholders: str
    legal_notice: str | None
    version: int
    status: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class RenderNotificationRequest(BaseModel):
    template_key: str
    values: dict
    language: str | None = None
    timezone: str | None = None
    region: str | None = None
    currency: str = "CNY"
    project_id: uuid.UUID | None = None
    marketing_enabled: bool | None = None


class RenderNotificationResponse(BaseModel):
    sent: bool
    reason: str
    category: str | None
    title: str
    content: str
    language: str | None = None
    timezone: str | None = None
    region: str | None = None


# ---------- 地区合规 ----------
class PolicyPublishRequest(BaseModel):
    policy_type: str
    region: str = "CN"
    title: str
    content: str


class PolicyVersionResponse(BaseModel):
    id: uuid.UUID
    policy_type: str
    region: str
    version: int
    title: str
    content: str
    effective_at: datetime
    published_at: datetime
    status: str

    model_config = {"from_attributes": True}


class ConsentRecordResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    policy_id: uuid.UUID
    policy_type: str
    policy_version: int
    granted_at: datetime
    region: str
    ip_hash: str | None
    status: str
    withdrawn_at: datetime | None
    policy_updated_at: datetime | None

    model_config = {"from_attributes": True}


class DataRequestCreate(BaseModel):
    request_type: str
    region: str = "CN"


class DataRequestUpdate(BaseModel):
    status: str
    result: str | None = None


class DataRequestResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    request_type: str
    status: str
    region: str
    requested_at: datetime
    completed_at: datetime | None
    result: str | None

    model_config = {"from_attributes": True}
