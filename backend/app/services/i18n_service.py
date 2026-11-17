# i18n 资源服务（第二十一批「国际化、多语言和多区域部署」规格三/四）
# 语言资源体系：zh-CN / zh-TW / en-US / ja-JP
# 翻译管理：新增/修改/审核/发布/回滚/版本管理/缺失项检查/占位符检查。
import json
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.i18n_resource import I18nResource, I18nResourceVersion

SUPPORTED_LANGUAGES = ["zh-CN", "zh-TW", "en-US", "ja-JP"]
DEFAULT_LANGUAGE = "zh-CN"
# 分类：common / errors / billing / notification / legal
SUPPORTED_CATEGORIES = ["common", "errors", "billing", "notification", "legal"]

# 资源状态：draft / reviewing / published / retired
RESOURCE_STATUSES = ["draft", "reviewing", "published", "retired"]

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def extract_placeholders(text: str) -> set[str]:
    """提取文本中的 {var} 占位符集合。"""
    return set(_PLACEHOLDER_RE.findall(text))


def _load_placeholders(raw) -> set[str]:
    """从数据库 Text 列读取占位符（JSON 数组存储，兼容旧的空值）。"""
    if not raw:
        return set()
    if isinstance(raw, (list, set)):
        return set(raw)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return set(data)
    except (ValueError, TypeError):
        pass
    return set(raw.split(",")) if isinstance(raw, str) and raw.strip() else set()


def _check_max_length(value: str, *, max_len: int = 500) -> None:
    """防止文本过长导致界面溢出（规格四）。"""
    if len(value) > max_len:
        raise ValueError(f"翻译文本过长（{len(value)} > {max_len}），可能造成界面溢出")


async def upsert_resource(
    db: AsyncSession,
    *,
    language: str,
    key: str,
    value: str,
    category: str = "common",
    placeholders: list[str] | None = None,
    action: str = "modify",
    max_length: int = 500,
    namespace: str = "app",
) -> I18nResource:
    """新增/修改翻译键。action: create / modify / review / publish / rollback。
    修改/发布/回滚写入 I18nResourceVersion 版本历史；发布后旧的 published 版本保留可回滚。
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"不支持的语言: {language}")
    if category not in SUPPORTED_CATEGORIES:
        raise ValueError(f"不支持的资源分类: {category}")
    _check_max_length(value, max_len=max_length)

    resource = (
        await db.execute(
            select(I18nResource).where(
                I18nResource.language == language,
                I18nResource.key == key,
            )
        )
    ).scalar_one_or_none()

    declared = set(placeholders or [])
    actual = extract_placeholders(value)
    if declared and declared != actual:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        raise ValueError(
            f"占位符不一致: 声明缺失 {missing or '无'}, 多余 {extra or '无'}"
        )

    if resource is None:
        resource = I18nResource(
            namespace=namespace,
            language=language,
            key=key,
            value=value,
            category=category,
            placeholders=json.dumps(sorted(actual), ensure_ascii=False) if actual else None,
            status="published" if action == "publish" else "draft",
            version=1,
        )
        db.add(resource)
        await db.flush()
        version_record = I18nResourceVersion(
            resource_id=resource.id,
            version=1,
            value=value,
            action="create",
            created_at=datetime.utcnow(),
        )
        db.add(version_record)
        await db.flush()
        return resource

    # 存在则按动作流转状态
    resource.value = value
    resource.placeholders = json.dumps(sorted(actual), ensure_ascii=False) if actual else None
    if action == "publish":
        resource.status = "published"
    elif action == "review":
        resource.status = "reviewing"
    elif action == "rollback":
        resource.status = "draft"
    else:  # modify
        resource.status = resource.status if resource.status == "published" else "draft"
        if resource.status == "published":
            # 修改已发布内容不直接生效：进入 reviewing 待审核（防止错误翻译覆盖原文）
            resource.status = "reviewing"
    resource.version += 1
    resource.updated_at = datetime.utcnow()

    version_record = I18nResourceVersion(
        resource_id=resource.id,
        version=resource.version,
        value=value,
        action=action,
        created_at=datetime.utcnow(),
    )
    db.add(version_record)
    await db.flush()
    return resource


async def get_translation(db: AsyncSession, language: str, key: str) -> str:
    """按语言取翻译值；缺失或未发布时回退默认语言，仍缺失返回 key 本身。"""
    for lang in [language, DEFAULT_LANGUAGE]:
        resource = (
            await db.execute(
                select(I18nResource).where(
                    I18nResource.language == lang,
                    I18nResource.key == key,
                    I18nResource.status == "published",
                )
            )
        ).scalar_one_or_none()
        if resource is not None:
            return resource.value
    return key


async def list_resources(
    db: AsyncSession,
    *,
    language: str | None = None,
    category: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[I18nResource]:
    stmt = select(I18nResource).order_by(
        I18nResource.language.asc(), I18nResource.key.asc()
    )
    if language:
        stmt = stmt.where(I18nResource.language == language)
    if category:
        stmt = stmt.where(I18nResource.category == category)
    if status:
        stmt = stmt.where(I18nResource.status == status)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def check_missing(
    db: AsyncSession,
    *,
    reference_language: str = DEFAULT_LANGUAGE,
    languages: list[str] | None = None,
) -> list[dict]:
    """缺失项检查：以参考语言（默认 zh-CN）的已发布键为基准，
    对比目标语言（未传则全部 SUPPORTED_LANGUAGES）的已发布键，返回缺失列表。"""
    ref_keys = set(
        (
            await db.execute(
                select(I18nResource.key).where(
                    I18nResource.language == reference_language,
                    I18nResource.status == "published",
                )
            )
        ).scalars().all()
    )
    targets = languages or [lang for lang in SUPPORTED_LANGUAGES if lang != reference_language]
    missing: list[dict] = []
    for lang in targets:
        lang_keys = set(
            (
                await db.execute(
                    select(I18nResource.key).where(
                        I18nResource.language == lang,
                        I18nResource.status == "published",
                    )
                )
            ).scalars().all()
        )
        for key in sorted(ref_keys - lang_keys):
            missing.append({"language": lang, "key": key})
    return missing


async def check_placeholders(db: AsyncSession, language: str, key: str) -> dict:
    """占位符检查：比较资源声明占位符与实际文本占位符。"""
    resource = (
        await db.execute(
            select(I18nResource).where(
                I18nResource.language == language,
                I18nResource.key == key,
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ValueError(f"翻译键不存在: {language}/{key}")
    declared = _load_placeholders(resource.placeholders)
    actual = extract_placeholders(resource.value)
    return {
        "language": language,
        "key": key,
        "declared": sorted(declared),
        "actual": sorted(actual),
        "missing": sorted(declared - actual),
        "extra": sorted(actual - declared),
        "ok": declared == actual,
    }


async def rollback_resource(
    db: AsyncSession,
    *,
    language: str,
    key: str,
    version: int | None = None,
) -> I18nResource:
    """回滚到指定版本（不传 version 则回退到上一版本）。"""
    resource = (
        await db.execute(
            select(I18nResource).where(
                I18nResource.language == language,
                I18nResource.key == key,
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ValueError(f"翻译键不存在: {language}/{key}")
    stmt = (
        select(I18nResourceVersion)
        .where(I18nResourceVersion.resource_id == resource.id)
        .order_by(I18nResourceVersion.version.desc())
    )
    if version is not None:
        stmt = stmt.where(I18nResourceVersion.version == version)
    versions = list((await db.execute(stmt.limit(2))).scalars().all())
    if not versions:
        raise ValueError(f"无可回滚版本: {language}/{key}")
    target = versions[0]
    resource.value = target.value
    ph = extract_placeholders(target.value)
    resource.placeholders = json.dumps(sorted(ph), ensure_ascii=False) if ph else None
    resource.status = "draft"
    resource.version += 1
    resource.updated_at = datetime.utcnow()
    db.add(
        I18nResourceVersion(
            resource_id=resource.id,
            version=resource.version,
            value=target.value,
            action="rollback",
            created_at=datetime.utcnow(),
        )
    )
    await db.flush()
    return resource


async def list_versions(
    db: AsyncSession,
    *,
    language: str,
    key: str,
) -> list[I18nResourceVersion]:
    resource = (
        await db.execute(
            select(I18nResource).where(
                I18nResource.language == language,
                I18nResource.key == key,
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ValueError(f"翻译键不存在: {language}/{key}")
    result = await db.execute(
        select(I18nResourceVersion)
        .where(I18nResourceVersion.resource_id == resource.id)
        .order_by(I18nResourceVersion.version.desc())
    )
    return list(result.scalars().all())


def render_with_values(template: str, values: dict[str, Any]) -> str:
    """按占位符渲染翻译/模板文本。缺失变量保持原样，方便调试。"""
    def _repl(match: re.Match) -> str:
        name = match.group(1)
        return str(values.get(name, match.group(0)))
    return _PLACEHOLDER_RE.sub(_repl, template)


async def seed_default_resources(db: AsyncSession) -> None:
    """初始化四语言基础资源（幂等）。"""
    base = {
        "save": {"zh-CN": "保存", "zh-TW": "儲存", "en-US": "Save", "ja-JP": "保存"},
        "cancel": {"zh-CN": "取消", "zh-TW": "取消", "en-US": "Cancel", "ja-JP": "キャンセル"},
        "chapter.publish": {"zh-CN": "发布章节", "zh-TW": "發佈章節", "en-US": "Publish chapter", "ja-JP": "章を公開"},
        "common.confirm": {"zh-CN": "确认", "zh-TW": "確認", "en-US": "Confirm", "ja-JP": "確認"},
        "common.delete": {"zh-CN": "删除", "zh-TW": "刪除", "en-US": "Delete", "ja-JP": "削除"},
        "errors.not_found": {"zh-CN": "资源不存在", "zh-TW": "資源不存在", "en-US": "Resource not found", "ja-JP": "リソースが見つかりません"},
        "errors.permission_denied": {"zh-CN": "没有访问权限", "zh-TW": "沒有訪問權限", "en-US": "Permission denied", "ja-JP": "アクセス権限がありません"},
        "billing.payment_success": {"zh-CN": "支付成功", "zh-TW": "支付成功", "en-US": "Payment succeeded", "ja-JP": "支払いが成功しました"},
        "notification.payment_received": {"zh-CN": "收到付款 {amount} {currency}", "zh-TW": "收到付款 {amount} {currency}", "en-US": "Payment received {amount} {currency}", "ja-JP": "支払い {amount} {currency} を受け取りました"},
        "notification.security_alert": {"zh-CN": "安全提醒：{message}", "zh-TW": "安全提醒：{message}", "en-US": "Security alert: {message}", "ja-JP": "セキュリティ通知：{message}"},
    }
    for key, values in base.items():
        for lang, text in values.items():
            existing = (
                await db.execute(
                    select(I18nResource.id).where(
                        I18nResource.language == lang,
                        I18nResource.key == key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                await upsert_resource(
                    db,
                    language=lang,
                    key=key,
                    value=text,
                    category="common" if key in ("save", "cancel") else (
                        "errors" if key.startswith("errors.") else
                        "billing" if key.startswith("billing.") else "notification"
                    ),
                    action="publish",
                )
    await db.commit()
