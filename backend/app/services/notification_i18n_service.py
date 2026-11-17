# 地区化通知服务（第二十一批「国际化、多语言和多区域部署」规格二十）
# NotificationTemplate：按语言/时区/地区/货币/时间格式渲染通知。
# 分类管理：transaction（交易，不可关闭）/ marketing（营销，可关闭）/ security（安全，不可关闭）。
# 渲染流程：确定用户语言/时区/区域/通知类型 → 选模板 → 占位符填充 → 本地化时间/货币。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_template import NOTIFICATION_CATEGORIES, NotificationTemplate
from app.models.user_locale import UserLocale
from app.services import notification_service
from app.services.locale_service import format_currency, format_datetime

# 营销类通知（用户可关闭）；交易/安全类不可关闭
MARKETING_TEMPLATE_KEYS = {"marketing.promo", "marketing.recommend"}
SECURITY_TEMPLATE_KEYS = {"security.login", "security.password", "security.permission"}

# 渲染占位符缺失时保留原文标记
PLACEHOLDER_PATTERN = "{" + "{key}" + "}"


async def get_or_create_template(
    db: AsyncSession,
    *,
    template_key: str,
    language: str,
    category: str,
    title_template: str,
    content_template: str,
    placeholders: list[str] | None = None,
    legal_notice: str | None = None,
) -> NotificationTemplate:
    if category not in NOTIFICATION_CATEGORIES:
        raise ValueError(f"非法通知分类: {category}")
    tpl = (
        await db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.template_key == template_key,
                NotificationTemplate.language == language,
            )
        )
    ).scalar_one_or_none()
    if tpl is None:
        tpl = NotificationTemplate(
            template_key=template_key,
            language=language,
            category=category,
            title_template=title_template,
            content_template=content_template,
            placeholders=",".join(placeholders or []),
            legal_notice=legal_notice,
            version=1,
            status="published",
        )
        db.add(tpl)
        await db.flush()
        return tpl
    tpl.title_template = title_template
    tpl.content_template = content_template
    tpl.placeholders = ",".join(placeholders or [])
    tpl.legal_notice = legal_notice
    tpl.version += 1
    tpl.updated_at = datetime.utcnow()
    await db.flush()
    return tpl


async def list_templates(
    db: AsyncSession,
    *,
    category: str | None = None,
    language: str | None = None,
) -> list[NotificationTemplate]:
    stmt = select(NotificationTemplate).order_by(
        NotificationTemplate.template_key.asc(),
        NotificationTemplate.language.asc(),
    )
    if category is not None:
        stmt = stmt.where(NotificationTemplate.category == category)
    if language is not None:
        stmt = stmt.where(NotificationTemplate.language == language)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def is_marketing(template: NotificationTemplate) -> bool:
    return template.template_key in MARKETING_TEMPLATE_KEYS or template.category == "marketing"


def is_security(template: NotificationTemplate) -> bool:
    return template.template_key in SECURITY_TEMPLATE_KEYS or template.category == "security"


def check_marketing_consent(
    db: AsyncSession,
    user_id: uuid.UUID,
    marketing_enabled: bool | None = None,
) -> bool:
    """营销通知开关：用户关闭后营销类通知不发送。交易/安全类不受影响。"""
    if marketing_enabled is False:
        return False
    return True


def _render_template(
    template: str,
    values: dict,
    *,
    keep_missing: bool = True,
) -> str:
    """占位符渲染：{username} 等；缺失占位符保留原文（便于审计缺失项）。"""
    out = template
    for key, value in (values or {}).items():
        out = out.replace("{" + key + "}", str(value))
    return out


async def render_and_create_notification(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    template_key: str,
    values: dict,
    language: str | None = None,
    timezone: str | None = None,
    region: str | None = None,
    currency: str = "CNY",
    project_id: uuid.UUID | None = None,
    marketing_enabled: bool | None = None,
    notify_type: str | None = None,
) -> dict:
    """按用户设置渲染地区化通知并创建站内通知。

    返回 {sent, notification?, title, content, category, reason?}。
    - 营销通知被关闭 → sent=False
    - 语言模板缺失 → 回退 zh-CN（保证必要通知可达）
    """
    loc = (
        await db.execute(
            select(UserLocale).where(UserLocale.user_id == user_id)
        )
    ).scalar_one_or_none()
    lang = language or (loc.notification_language if loc and loc.notification_language else "zh-CN")
    tz = timezone or (loc.timezone if loc and loc.timezone else "Asia/Shanghai")
    reg = region or (loc.region if loc and loc.region else "CN")

    tpl = (
        await db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.template_key == template_key,
                NotificationTemplate.language == lang,
            )
        )
    ).scalar_one_or_none()
    if tpl is None:
        tpl = (
            await db.execute(
                select(NotificationTemplate).where(
                    NotificationTemplate.template_key == template_key,
                    NotificationTemplate.language == "zh-CN",
                )
            )
        ).scalar_one_or_none()

    if tpl is None:
        return {
            "sent": False,
            "notification": None,
            "reason": "模板不存在",
            "category": None,
            "title": template_key,
            "content": "",
        }

    if is_marketing(tpl) and marketing_enabled is False:
        return {
            "sent": False,
            "notification": None,
            "reason": "营销通知已关闭",
            "category": tpl.category,
            "title": "",
            "content": "",
        }

    rendered_values = dict(values)
    # 时间/货币等本地化渲染
    if "time" in rendered_values and isinstance(rendered_values["time"], datetime):
        rendered_values["time"] = format_datetime(
            rendered_values["time"],
            tz_name=tz,
            date_format=(loc.date_format if loc and loc.date_format else "YYYY-MM-DD"),
            time_format=(loc.time_format if loc and loc.time_format else "HH:mm"),
        )
    if "amount_minor" in rendered_values:
        amount_minor = int(rendered_values.pop("amount_minor"))
        rendered_values["amount"] = format_currency(
            amount_minor,
            currency,
            locale=lang,
        )

    title = _render_template(tpl.title_template, rendered_values)
    content = _render_template(tpl.content_template, rendered_values)
    if tpl.legal_notice:
        content = f"{content}\n{tpl.legal_notice}"

    notification = await notification_service.create_notification(
        db,
        user_id=user_id,
        type=notify_type or _type_from_template(tpl),
        title=title,
        content=content,
        project_id=project_id,
    )
    return {
        "sent": True,
        "notification": notification,
        "reason": "ok",
        "category": tpl.category,
        "title": title,
        "content": content,
        "language": lang,
        "timezone": tz,
        "region": reg,
    }


def _type_from_template(tpl: NotificationTemplate) -> str:
    if tpl.category == "transaction":
        return "system"
    if tpl.category == "security":
        return "security"
    return "system"
