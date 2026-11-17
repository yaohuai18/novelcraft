# 国际化 / 多语言 / 多区域 API（用户端，第二十一批「国际化、多语言和多区域部署」）
# 语言选择：GET /api/i18n/languages；设置：PUT /api/i18n/locale；展示上下文：GET /api/i18n/display-context
# 翻译资源：GET /api/i18n/translations|translations/{key}
# 作品翻译：POST /api/i18n/projects/{project_id}/translations、/chapters/{chapter_id}/translations
# AI 多语言：POST /api/i18n/ai/profile|glossary、GET /api/i18n/ai/glossary
# 多币种报价：POST /api/i18n/quote；地区化通知：POST /api/i18n/notifications/render
# 合规：POST /api/i18n/consents、/data-requests；区域：/api/i18n/regions、/events、/assets、/metrics
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ok
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.cdn_asset import CdnAsset, DownloadLog
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_translation import ChapterTranslation, ProjectTranslation
from app.models.region_monitor import RegionStatus
from app.models.user import User
from app.schemas.i18n_schemas import (
    ChapterTranslationCreate,
    ChapterTranslationUpdate,
    DataRequestCreate,
    GlossaryTermCreate,
    ProjectLanguagesUpdate,
    ProjectTranslationCreate,
    ProjectTranslationUpdate,
    RenderNotificationRequest,
    UserLocaleUpdate,
)
from app.schemas.region_schemas import (
    RegionEventCreate,
)
from app.services import (
    ai_localization_service,
    cdn_service,
    compliance_service,
    i18n_service,
    locale_service,
    notification_i18n_service,
    pricing_service,
    region_monitor_service,
    region_service,
    sync_service,
    translation_service,
)
from app.services.currency_service import list_currencies

router = APIRouter(
    prefix="/api/i18n",
    tags=["国际化 / 多语言 / 多区域"],
)


def _asdict_locale(loc) -> dict:
    if loc is None:
        return {}
    return {
        "user_id": str(loc.user_id),
        "ui_language": loc.ui_language,
        "content_language": loc.content_language,
        "notification_language": loc.notification_language,
        "ai_output_language": loc.ai_output_language,
        "timezone": loc.timezone,
        "locale": loc.locale,
        "date_format": loc.date_format,
        "time_format": loc.time_format,
        "region": loc.region,
        "browser_language": loc.browser_language,
        "device_language": loc.device_language,
        "manual_languages": locale_service._loads(loc.manual_languages),
        "updated_at": loc.updated_at.isoformat() if loc.updated_at else None,
    }


def _asdict_asset(asset: CdnAsset) -> dict:
    return {
        "id": str(asset.id),
        "asset_key": asset.asset_key,
        "original_name": asset.original_name,
        "mime_type": asset.mime_type,
        "file_size": asset.file_size,
        "is_public": asset.is_public,
        "region_code": asset.region_code,
        "cache_control": asset.cache_control,
        "fingerprint": asset.fingerprint,
        "version_path": asset.version_path,
        "status": asset.status,
        "created_at": asset.created_at.isoformat(),
    }


def _asdict_region_event(evt) -> dict:
    return {
        "id": str(evt.id),
        "event_id": evt.event_id,
        "source_region": evt.source_region,
        "occurred_at": evt.occurred_at.isoformat(),
        "entity_type": evt.entity_type,
        "entity_id": evt.entity_id,
        "entity_version": evt.entity_version,
        "status": evt.status,
        "payload": evt.payload,
    }


# ==================== 语言 / 时区 / 地区设置 ====================


@router.get("/languages")
async def get_languages(
    request: Request,
    browser_language: str | None = Query(None),
    device_language: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按优先级解析用户四类语言：手动 → 账户 → 浏览器 → 设备 → 默认。"""
    resolved = await locale_service.resolve_user_languages(
        db,
        user_id=user.id,
        browser_language=browser_language,
        device_language=device_language,
    )
    return ok(request, {"languages": resolved})


@router.put("/locale")
async def update_locale(
    request: Request,
    payload: UserLocaleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新用户语言/时区/地区设置（四类语言独立）。"""
    loc = await locale_service.update_user_locale(
        db,
        user_id=user.id,
        ui_language=payload.ui_language,
        content_language=payload.content_language,
        notification_language=payload.notification_language,
        ai_output_language=payload.ai_output_language,
        timezone=payload.timezone,
        locale=payload.locale,
        date_format=payload.date_format,
        time_format=payload.time_format,
        region=payload.region,
        manual_languages=payload.manual_languages,
    )
    await db.commit()
    return ok(request, {"locale": _asdict_locale(loc)})


@router.get("/locale")
async def get_locale(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    loc = await locale_service.get_user_locale(db, user.id)
    return ok(request, {"locale": _asdict_locale(loc)})


@router.get("/display-context")
async def get_display_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户展示上下文：时区/地区/日期时间格式/四类语言。"""
    ctx = await locale_service.get_user_display_context(db, user_id=user.id)
    return ok(request, {"context": ctx})


@router.post("/time/convert")
async def convert_time(
    request: Request,
    payload: dict = Body(...),
):
    """UTC 时间 → 用户时区展示（含夏令时），date/time 格式可选。"""
    utc = payload.get("utc")
    tz = payload.get("timezone", "Asia/Shanghai")
    date_format = payload.get("date_format", "YYYY-MM-DD")
    time_format = payload.get("time_format", "HH:mm")
    from datetime import datetime
    dt = datetime.fromisoformat(str(utc)) if utc else datetime.utcnow()
    return ok(
        request,
        {
            "utc": dt.isoformat(),
            "local": locale_service.format_datetime(
                dt,
                tz_name=tz,
                date_format=date_format,
                time_format=time_format,
            ),
            "timezone": tz,
        },
    )


# ==================== 翻译资源 ====================


@router.get("/translations")
async def list_translations(
    request: Request,
    language: str | None = Query(None),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """已发布翻译资源列表（界面文案/错误消息/账单信息等）。"""
    items = await i18n_service.list_resources(
        db, language=language, category=category, status="published", limit=500
    )
    return ok(
        request,
        {
            "items": [
                {
                    "language": r.language,
                    "key": r.key,
                    "value": r.value,
                    "category": r.category,
                    "version": r.version,
                }
                for r in items
            ],
            "total": len(items),
        },
    )


@router.get("/translations/{key}")
async def get_translation(
    request: Request,
    key: str,
    language: str = Query("zh-CN"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取翻译值；缺失时回退默认语言，仍缺失返回 key 本身。"""
    value = await i18n_service.get_translation(db, language, key)
    return ok(request, {"key": key, "language": language, "value": value})


# ==================== 多语言作品 ====================


async def _ensure_project_owner(db: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    """校验作品归属：非作者统一 404（与全仓信息隐藏策略一致）。"""
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="作品不存在")


@router.post("/projects/{project_id}/languages")
async def set_project_languages(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectLanguagesUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="作品不存在")
    await translation_service.set_project_languages(
        db,
        project,
        default_language=payload.default_language,
        supported_languages=payload.supported_languages,
    )
    await db.commit()
    return ok(
        request,
        {
            "project_id": str(project_id),
            "default_language": project.default_language,
            "supported_languages": translation_service.get_supported_languages(project),
        },
    )


@router.post("/projects/{project_id}/translations")
async def create_project_translation(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectTranslationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _ensure_project_owner(db, project_id, user)
    pt = await translation_service.create_project_translation(
        db,
        project_id=project_id,
        language=payload.language,
        title=payload.title,
        description=payload.description,
    )
    await db.commit()
    return ok(
        request,
        {
            "id": str(pt.id),
            "project_id": str(pt.project_id),
            "language": pt.language,
            "title": pt.title,
            "status": pt.status,
        },
    )


@router.patch("/project-translations/{pt_id}")
async def update_project_translation(
    request: Request,
    pt_id: uuid.UUID,
    payload: ProjectTranslationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pt_row = (
        await db.execute(
            select(ProjectTranslation).where(ProjectTranslation.id == pt_id)
        )
    ).scalar_one_or_none()
    if pt_row is None:
        raise HTTPException(status_code=404, detail="翻译不存在")
    await _ensure_project_owner(db, pt_row.project_id, user)
    pt = await translation_service.update_project_translation(
        db,
        project_translation_id=pt_id,
        title=payload.title,
        description=payload.description,
    )
    await db.commit()
    return ok(
        request,
        {
            "id": str(pt.id),
            "language": pt.language,
            "title": pt.title,
            "status": pt.status,
        },
    )


@router.get("/projects/{project_id}/translations")
async def list_project_translations(
    request: Request,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await translation_service.list_project_translations(db, project_id)
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(pt.id),
                    "language": pt.language,
                    "title": pt.title,
                    "description": pt.description,
                    "status": pt.status,
                    "updated_at": pt.updated_at.isoformat(),
                }
                for pt in items
            ]
        },
    )


@router.post("/chapters/{chapter_id}/translations")
async def create_chapter_translation(
    request: Request,
    chapter_id: uuid.UUID,
    payload: ChapterTranslationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    outline = (
        await db.execute(select(Outline).where(Outline.id == chapter_id))
    ).scalar_one_or_none()
    if outline is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    await _ensure_project_owner(db, outline.project_id, user)
    try:
        ct = await translation_service.create_chapter_translation(
            db,
            chapter_id=chapter_id,
            language=payload.language,
            title=payload.title,
            content=payload.content,
            translation_status=payload.translation_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return ok(
        request,
        {
            "id": str(ct.id),
            "chapter_id": str(ct.chapter_id),
            "language": ct.language,
            "translation_status": ct.translation_status,
            "source_version": ct.source_version,
        },
    )


@router.patch("/chapter-translations/{ct_id}")
async def update_chapter_translation(
    request: Request,
    ct_id: uuid.UUID,
    payload: ChapterTranslationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新章节翻译；发布前校验与原文版本一致（outdated 不允许直接发布）。"""
    ct_row = (
        await db.execute(
            select(ChapterTranslation).where(ChapterTranslation.id == ct_id)
        )
    ).scalar_one_or_none()
    if ct_row is None:
        raise HTTPException(status_code=404, detail="翻译不存在")
    outline = (
        await db.execute(select(Outline).where(Outline.id == ct_row.chapter_id))
    ).scalar_one_or_none()
    if outline is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    await _ensure_project_owner(db, outline.project_id, user)
    try:
        ct = await translation_service.update_chapter_translation(
            db,
            chapter_translation_id=ct_id,
            title=payload.title,
            content=payload.content,
            translation_status=payload.translation_status,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return ok(
        request,
        {
            "id": str(ct.id),
            "language": ct.language,
            "translation_status": ct.translation_status,
            "source_version": ct.source_version,
            "updated_at": ct.updated_at.isoformat(),
        },
    )


@router.get("/chapters/{chapter_id}/translations")
async def list_chapter_translations(
    request: Request,
    chapter_id: uuid.UUID,
    translation_status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await translation_service.list_chapter_translations(
        db, chapter_id, translation_status=translation_status
    )
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(ct.id),
                    "chapter_id": str(ct.chapter_id),
                    "language": ct.language,
                    "title": ct.title,
                    "translation_status": ct.translation_status,
                    "source_version": ct.source_version,
                    "updated_at": ct.updated_at.isoformat(),
                }
                for ct in items
            ]
        },
    )


@router.post("/chapters/{chapter_id}/mark-outdated")
async def mark_chapter_translations_outdated(
    request: Request,
    chapter_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """原文更新后调用：把该章节已发布译文标记 outdated（提示重新翻译）。"""
    count = await translation_service.mark_chapter_translations_outdated(db, chapter_id)
    await db.commit()
    return ok(request, {"chapter_id": str(chapter_id), "marked_outdated": count})


# ==================== AI 多语言 ====================


@router.post("/ai/profile")
async def prepare_ai_profile(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """组装 AI 多语言任务上下文：输入/输出语言、variant、tone、spelling、术语表。"""
    raw_pid = payload.get("project_id")
    pid = uuid.UUID(raw_pid) if raw_pid else None
    ctx = await ai_localization_service.prepare_ai_request(
        db,
        project_id=pid,
        input_language=payload.get("input_language", "zh-CN"),
        output_language=payload.get("output_language", "en-US"),
        variant=payload.get("variant"),
        tone=payload.get("tone"),
        spelling=payload.get("spelling"),
    )
    return ok(request, {"profile": ctx})


@router.post("/ai/glossary")
async def create_glossary(
    request: Request,
    payload: GlossaryTermCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新增术语表条目（force_exact 专有名词禁止 AI 随意翻译）。"""
    term = await ai_localization_service.create_glossary_term(
        db,
        project_id=payload.project_id,
        source_language=payload.source_language,
        target_language=payload.target_language,
        source_text=payload.source_text,
        target_text=payload.target_text,
        term_type=payload.term_type,
        force_exact=payload.force_exact,
        scope=payload.scope,
    )
    await db.commit()
    return ok(
        request,
        {
            "id": str(term.id),
            "source_text": term.source_text,
            "target_text": term.target_text,
            "term_type": term.term_type,
            "force_exact": term.force_exact,
        },
    )


@router.get("/ai/glossary")
async def list_glossary(
    request: Request,
    project_id: uuid.UUID | None = Query(None),
    source_language: str | None = Query(None),
    target_language: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await ai_localization_service.list_glossary_terms(
        db,
        project_id=project_id,
        source_language=source_language,
        target_language=target_language,
    )
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(t.id),
                    "project_id": str(t.project_id) if t.project_id else None,
                    "source_text": t.source_text,
                    "target_text": t.target_text,
                    "term_type": t.term_type,
                    "force_exact": t.force_exact,
                    "scope": t.scope,
                    "status": t.status,
                }
                for t in items
            ]
        },
    )


@router.post("/ai/glossary/validate")
async def validate_glossary(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """校验译文是否保留术语固定译法（违规项逐条返回）。"""
    raw_pid = payload.get("project_id")
    pid = uuid.UUID(raw_pid) if raw_pid else None
    glossary = await ai_localization_service.get_glossary_terms(
        db,
        project_id=pid,
        source_language=payload.get("input_language", "zh-CN"),
        target_language=payload.get("output_language", "en-US"),
    )
    result = ai_localization_service.validate_translated_glossary(
        payload.get("source_text", ""),
        payload.get("translated_text", ""),
        glossary,
    )
    return ok(request, result)


# ==================== 多币种 / 报价 / 税费 ====================


@router.get("/currencies")
async def get_currencies(
    request: Request,
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await list_currencies(db, status=status)
    return ok(
        request,
        {
            "items": [
                {
                    "code": c.code,
                    "symbol": c.symbol,
                    "decimal_digits": c.decimal_digits,
                    "exchange_rate": c.exchange_rate,
                    "status": c.status,
                }
                for c in items
            ]
        },
    )


@router.post("/quote")
async def quote_price(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """服务端计算价格（多币种 + 地区价格 + 税费 + 优惠），客户端不可提交价格。"""
    try:
        quote = await pricing_service.calc_price(
            db,
            plan_id=uuid.UUID(payload["plan_id"]),
            region=payload.get("region", "CN"),
            currency=payload.get("currency", "CNY"),
            billing_interval=payload.get("billing_interval", "monthly"),
            promotion_code=payload.get("promotion_code"),
            user_id=user.id,
            tax_id_no=payload.get("tax_id_no"),
            seats=payload.get("seats", 0),
            seat_price_minor=payload.get("seat_price_minor", 0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return ok(request, {"quote": quote})


# ==================== 地区化通知 ====================


@router.get("/notifications/templates")
async def list_notification_templates(
    request: Request,
    category: str | None = Query(None),
    language: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await notification_i18n_service.list_templates(
        db, category=category, language=language
    )
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(t.id),
                    "template_key": t.template_key,
                    "language": t.language,
                    "category": t.category,
                    "title_template": t.title_template,
                    "content_template": t.content_template,
                    "version": t.version,
                    "status": t.status,
                }
                for t in items
            ]
        },
    )


@router.post("/notifications/render")
async def render_notification(
    request: Request,
    payload: RenderNotificationRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按用户语言/时区/地区/货币渲染通知并创建站内通知；营销通知可关闭。"""
    result = await notification_i18n_service.render_and_create_notification(
        db,
        user_id=user.id,
        template_key=payload.template_key,
        values=payload.values,
        language=payload.language,
        timezone=payload.timezone,
        region=payload.region,
        currency=payload.currency,
        project_id=payload.project_id,
        marketing_enabled=payload.marketing_enabled,
    )
    await db.commit()
    return ok(
        request,
        {
            "sent": result["sent"],
            "reason": result["reason"],
            "category": result["category"],
            "title": result["title"],
            "content": result["content"],
            "language": result.get("language"),
            "timezone": result.get("timezone"),
            "region": result.get("region"),
        },
    )


# ==================== 地区合规 ====================


@router.get("/policies")
async def get_policy(
    request: Request,
    policy_type: str = Query(...),
    region: str = Query("CN"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    policy = await compliance_service.get_policy(
        db, policy_type=policy_type, region=region
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="政策不存在")
    return ok(
        request,
        {
            "policy_type": policy.policy_type,
            "region": policy.region,
            "version": policy.version,
            "title": policy.title,
            "content": policy.content,
            "effective_at": policy.effective_at.isoformat(),
        },
    )


@router.post("/consents")
async def record_consent(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录用户明确同意（内容版本/时间/地区/IP）。"""
    consent = await compliance_service.record_consent(
        db,
        user_id=user.id,
        policy_type=payload["policy_type"],
        region=payload.get("region", "CN"),
        ip_hash=payload.get("ip_hash"),
        policy_version=payload.get("policy_version"),
    )
    await db.commit()
    return ok(
        request,
        {
            "id": str(consent.id),
            "policy_type": consent.policy_type,
            "policy_version": consent.policy_version,
            "region": consent.region,
            "status": consent.status,
            "granted_at": consent.granted_at.isoformat(),
        },
    )


@router.post("/consents/withdraw")
async def withdraw_consent(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤回同意（记录撤回时间）。"""
    rec = await compliance_service.withdraw_consent(
        db, user_id=user.id, policy_type=payload["policy_type"]
    )
    await db.commit()
    if rec is None:
        return ok(request, {"withdrawn": False, "reason": "无有效同意"})
    return ok(
        request,
        {
            "withdrawn": True,
            "policy_type": rec.policy_type,
            "withdrawn_at": rec.withdrawn_at.isoformat(),
        },
    )


@router.get("/consents")
async def list_consents(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await compliance_service.list_consents(db, user.id)
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(c.id),
                    "policy_type": c.policy_type,
                    "policy_version": c.policy_version,
                    "region": c.region,
                    "status": c.status,
                    "granted_at": c.granted_at.isoformat(),
                    "withdrawn_at": c.withdrawn_at.isoformat() if c.withdrawn_at else None,
                }
                for c in items
            ]
        },
    )


@router.post("/data-requests")
async def create_data_request(
    request: Request,
    payload: DataRequestCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """数据导出 / 账号删除 / 数据访问申请。"""
    req = await compliance_service.create_data_request(
        db,
        user_id=user.id,
        request_type=payload.request_type,
        region=payload.region,
    )
    await db.commit()
    return ok(
        request,
        {
            "id": str(req.id),
            "request_type": req.request_type,
            "status": req.status,
            "region": req.region,
        },
    )


@router.get("/data-requests")
async def list_my_data_requests(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await compliance_service.list_data_requests(db, user_id=user.id)
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(r.id),
                    "request_type": r.request_type,
                    "status": r.status,
                    "region": r.region,
                    "requested_at": r.requested_at.isoformat(),
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                for r in items
            ]
        },
    )


# ==================== 多区域 / 数据驻留 / 内容可见性 ====================


@router.get("/regions")
async def list_regions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await region_service.list_regions(db)
    return ok(
        request,
        {
            "items": [
                {
                    "region_code": r.region_code,
                    "name": r.name,
                    "status": r.status,
                    "is_main": r.is_main,
                    "storage_endpoint": r.storage_endpoint,
                    "database_cluster": r.database_cluster,
                    "ai_provider": r.ai_provider,
                }
                for r in items
            ]
        },
    )


@router.post("/regions/resolve-access")
async def resolve_access_region(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """就近接入区域解析（偏好区域 → IP 区域 → 主区域）并记录访问日志。"""
    regions = await region_service.list_regions(db)
    region_code = region_service.resolve_access_region(
        preferred_region=payload.get("preferred_region"),
        request_ip_region=payload.get("request_ip_region"),
        available_regions=regions,
    )
    log = await region_service.log_region_access(
        db,
        user_id=user.id,
        region_code=region_code,
        entry_route=payload.get("entry_route", "/api/i18n/regions/resolve-access"),
        request_ip_hash=payload.get("request_ip_hash"),
        user_agent=payload.get("user_agent"),
    )
    await db.commit()
    return ok(
        request,
        {"region_code": region_code, "log_id": str(log.id)},
    )


@router.post("/content-visibility")
async def check_content_visibility(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """服务端执行地区与年龄权限判断（不依赖客户端隐藏）。"""
    project = (
        await db.execute(
            select(Project).where(Project.id == uuid.UUID(payload["project_id"]))
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    result = region_service.check_content_visibility(
        project,
        user_region=payload.get("user_region", "CN"),
        user_age=payload.get("user_age"),
    )
    return ok(request, result)


# ==================== CDN 静态资源 ====================


@router.get("/assets")
async def list_public_assets(
    request: Request,
    is_public: bool | None = Query(None),
    region_code: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await cdn_service.list_assets(
        db, is_public=is_public, region_code=region_code
    )
    return ok(
        request,
        {"items": [_asdict_asset(a) for a in items], "total": len(items)},
    )


@router.post("/assets/{asset_key}/sign")
async def sign_asset_url(
    request: Request,
    asset_key: str,
    ttl_seconds: int = Body(300, embed=True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """私有资源短期签名 URL（默认 5 分钟）。"""
    asset = await cdn_service.get_asset(db, asset_key)
    if asset is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    if asset.is_public:
        raise HTTPException(status_code=400, detail="公开资源无需签名")
    url = cdn_service.sign_url(asset, ttl_seconds=ttl_seconds)
    return ok(request, {"asset_key": asset_key, "url": url, "ttl_seconds": ttl_seconds})


@router.post("/assets/{asset_key}/access")
async def check_asset_access(
    request: Request,
    asset_key: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """私有资源访问：签名 URL + 权限校验；通过后记录下载日志。"""
    result = await cdn_service.check_asset_access(
        db,
        asset_key=asset_key,
        user_id=user.id,
        signed_url=payload.get("signed_url"),
    )
    if not result["allowed"]:
        return ok(request, {"allowed": False, "reason": result["reason"]})
    asset = result["asset"]
    log = None
    if not asset.is_public:
        log = await cdn_service.log_download(
            db, asset_id=asset.id, user_id=user.id,
            ip_hash=payload.get("ip_hash"),
            user_agent=payload.get("user_agent"),
        )
        await db.commit()
    return ok(
        request,
        {
            "allowed": True,
            "reason": result["reason"],
            "url": result["url"],
            "cache_control": result.get("cache_control"),
            "download_log_id": str(log.id) if log else None,
        },
    )


# ==================== 跨区域事件同步 / 乐观锁 ====================


@router.post("/events")
async def ingest_region_event(
    request: Request,
    payload: RegionEventCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接收跨区域事件（幂等去重 + 乱序检测）。"""
    event_id = payload.event_id or f"evt-{uuid.uuid4().hex[:16]}"
    result = await sync_service.apply_entity_event(
        db,
        event_id=event_id,
        source_region=payload.source_region,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        entity_version=payload.entity_version,
        payload=payload.payload,
    )
    await db.commit()
    return ok(
        request,
        {
            "status": result["status"],
            "event_id": event_id,
            "stored_event_id": str(result["event"].id),
        },
    )


@router.get("/events")
async def list_region_events(
    request: Request,
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    source_region: str | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await sync_service.get_entity_events(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        source_region=source_region,
        status=status,
    )
    return ok(
        request,
        {"items": [_asdict_region_event(e) for e in items], "total": len(items)},
    )


@router.post("/chapters/{chapter_id}/update-with-version")
async def update_chapter_with_version(
    request: Request,
    chapter_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """章节乐观锁更新：base_version 不一致返回 409 version_conflict。"""
    content = payload.get("content", "")
    base_version = payload.get("base_version", 0)
    chapter, conflict = await sync_service.update_chapter_with_version_returning_conflict(
        db,
        chapter_id=chapter_id,
        content=content,
        base_version=base_version,
    )
    if conflict is not None:
        await db.rollback()
        return JSONResponse(status_code=409, content={"error": conflict["error"]})
    await db.commit()
    return ok(
        request,
        {
            "ok": True,
            "chapter_id": str(chapter_id),
            "version": chapter.version,
            "word_count": len(chapter.content),
        },
    )


# ==================== 区域监控 ====================


@router.get("/metrics")
async def list_region_metrics(
    request: Request,
    region_code: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await region_monitor_service.list_metrics(db, region_code=region_code)
    return ok(
        request,
        {
            "items": [
                {
                    "id": str(m.id),
                    "region_code": m.region_code,
                    "metric_at": m.metric_at.isoformat(),
                    "request_count": m.request_count,
                    "error_count": m.error_count,
                    "error_rate": m.error_rate,
                    "total_response_ms": m.total_response_ms,
                    "cache_hit_count": m.cache_hit_count,
                    "cache_miss_count": m.cache_miss_count,
                    "ai_available": m.ai_available,
                }
                for m in items
            ]
        },
    )


@router.get("/regions/health")
async def list_region_health(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = (
        await db.execute(select(RegionStatus).order_by(RegionStatus.region_code.asc()))
    ).scalars().all()
    return ok(
        request,
        {
            "items": [
                {
                    "region_code": s.region_code,
                    "status": s.status,
                    "reason": s.reason,
                    "failover_to": s.failover_to,
                    "data_resident_blocked": s.data_resident_blocked,
                    "detected_at": s.detected_at.isoformat() if s.detected_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                }
                for s in items
            ]
        },
    )
