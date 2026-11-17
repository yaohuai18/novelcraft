# 语言选择与时区格式化服务（第二十一批「国际化、多语言和多区域部署」规格二/五/八/九）
# 语言来源优先级：用户手动选择 → 用户账户语言设置 → 浏览器语言 → 设备语言 → 系统默认。
# 四类语言独立设置：界面语言 / 内容语言 / 通知语言 / AI 输出语言。
# 时间统一 UTC 存储，展示时按用户时区转换（含夏令时），日期/数字/货币按 locale 格式化。
import json
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_locale import UserLocale
from app.services.i18n_service import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

LANGUAGE_KEYS = {
    "ui": "ui_language",
    "content": "content_language",
    "notification": "notification_language",
    "ai_output": "ai_output_language",
}

DEFAULT_TZ = "Asia/Shanghai"

# locale → 数字格式规则
_NUMBER_FORMATS = {
    "zh-CN": {"group": ",", "decimal": "."},
    "zh-TW": {"group": ",", "decimal": "."},
    "en-US": {"group": ",", "decimal": "."},
    "ja-JP": {"group": ",", "decimal": "."},
    "de-DE": {"group": ".", "decimal": ","},
    "fr-FR": {"group": " ", "decimal": ","},
}

_DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
}

_TIME_FORMATS = {
    "HH:mm": "%H:%M",
    "hh:mm A": "%I:%M %p",
}


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


async def get_or_create_user_locale(
    db: AsyncSession, user_id: uuid.UUID
) -> UserLocale:
    loc = (
        await db.execute(
            select(UserLocale).where(UserLocale.user_id == user_id)
        )
    ).scalar_one_or_none()
    if loc is None:
        loc = UserLocale(user_id=user_id)
        db.add(loc)
        await db.flush()
    return loc


async def update_user_locale(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    ui_language: str | None = None,
    content_language: str | None = None,
    notification_language: str | None = None,
    ai_output_language: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    date_format: str | None = None,
    time_format: str | None = None,
    region: str | None = None,
    manual_languages: dict | None = None,
) -> UserLocale:
    """更新用户语言/时区/地区设置；四类语言在 manual_languages 中独立保存。"""
    for lang in [ui_language, content_language, notification_language, ai_output_language]:
        if lang is not None and lang not in SUPPORTED_LANGUAGES:
            raise ValueError(f"不支持的语言: {lang}")
    if timezone is not None and timezone not in available_timezones():
        raise ValueError(f"无效时区: {timezone}")

    loc = await get_or_create_user_locale(db, user_id)
    if ui_language is not None:
        loc.ui_language = ui_language
    if content_language is not None:
        loc.content_language = content_language
    if notification_language is not None:
        loc.notification_language = notification_language
    if ai_output_language is not None:
        loc.ai_output_language = ai_output_language
    if timezone is not None:
        loc.timezone = timezone
    if locale is not None:
        loc.locale = locale
    if date_format is not None:
        loc.date_format = date_format
    if time_format is not None:
        loc.time_format = time_format
    if region is not None:
        loc.region = region
    if manual_languages is not None:
        loc.manual_languages = _dumps(manual_languages)
    await db.flush()
    return loc


async def get_user_locale(db: AsyncSession, user_id: uuid.UUID) -> UserLocale | None:
    return (
        await db.execute(
            select(UserLocale).where(UserLocale.user_id == user_id)
        )
    ).scalar_one_or_none()


def resolve_language(
    *,
    kind: str,
    manual_languages: dict | None,
    account_language: str | None,
    browser_language: str | None,
    device_language: str | None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """按优先级解析某类语言：手动选择 → 账户设置 → 浏览器 → 设备 → 默认。"""
    manual = (manual_languages or {}).get(kind)
    for candidate in (manual, account_language, browser_language, device_language):
        if candidate and candidate in SUPPORTED_LANGUAGES:
            return candidate
    return default


async def resolve_user_languages(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    browser_language: str | None = None,
    device_language: str | None = None,
) -> dict:
    """返回用户四类语言的最终解析结果。"""
    loc = await get_user_locale(db, user_id) if user_id else None
    manual = _loads(loc.manual_languages) if loc else {}
    browser = browser_language or (loc.browser_language if loc else None)
    device = device_language or (loc.device_language if loc else None)
    result = {}
    for kind, field in LANGUAGE_KEYS.items():
        account = getattr(loc, field) if loc else None
        result[kind] = resolve_language(
            kind=kind,
            manual_languages=manual,
            account_language=account,
            browser_language=browser,
            device_language=device,
        )
    return result


def convert_timezone(dt: datetime, tz_name: str) -> datetime:
    """UTC → 目标时区（zoneinfo 自动处理夏令时）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz_name))


def format_datetime(
    dt: datetime,
    *,
    tz_name: str = DEFAULT_TZ,
    date_format: str = "YYYY-MM-DD",
    time_format: str = "HH:mm",
) -> str:
    """按用户时区/格式渲染日期时间（展示用）。"""
    local = convert_timezone(dt, tz_name)
    df = _DATE_FORMATS.get(date_format, "%Y-%m-%d")
    tf = _TIME_FORMATS.get(time_format, "%H:%M")
    return f"{local.strftime(df)} {local.strftime(tf)}"


def format_number(value: float, locale: str = "en-US") -> str:
    """按 locale 渲染数字（千分位/小数分隔符）。"""
    fmt = _NUMBER_FORMATS.get(locale, _NUMBER_FORMATS["en-US"])
    s = f"{value:,.2f}"
    if fmt["decimal"] != ".":
        s = s.replace(",", "\x00").replace(".", fmt["decimal"]).replace("\x00", fmt["group"])
    return s


def format_currency(
    amount_minor: int,
    currency_code: str,
    symbol: str | None = None,
    decimal_digits: int | None = None,
    locale: str = "en-US",
) -> str:
    """按最小货币单位渲染货币金额（如 CNY 9900 → ¥99.00）。"""
    digits = decimal_digits if decimal_digits is not None else (
        2 if currency_code not in ("JPY", "KRW") else 0
    )
    factor = 10 ** digits
    value = Decimal(amount_minor) / Decimal(factor)
    q = Decimal("1." + "0" * digits)
    value = value.quantize(q, rounding=ROUND_HALF_UP)
    num = format_number(float(value), locale)
    prefix = symbol or ""
    return f"{prefix}{num}"


async def get_user_display_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
) -> dict:
    """获取用户展示上下文：语言/时区/地区/日期时间格式。"""
    loc = await get_user_locale(db, user_id) if user_id else None
    return {
        "timezone": (loc.timezone if loc else None) or DEFAULT_TZ,
        "locale": (loc.locale if loc else None) or "zh-CN",
        "date_format": (loc.date_format if loc else None) or "YYYY-MM-DD",
        "time_format": (loc.time_format if loc else None) or "HH:mm",
        "region": (loc.region if loc else None) or "CN",
        "languages": await resolve_user_languages(
            db, user_id=user_id,
            browser_language=loc.browser_language if loc else None,
            device_language=loc.device_language if loc else None,
        ) if user_id else {},
    }
