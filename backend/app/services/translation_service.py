# 多语言作品翻译服务（第二十一批「国际化、多语言和多区域部署」规格六）
# Project 增加 default_language / supported_languages。
# ProjectTranslation / ChapterTranslation 状态流转：
#   not_started → draft → machine_translated → reviewing → published
#   published → outdated（原文更新自动标记）
# 原文更新：ChapterContent.version 变化 → 对应译文 source_version_id 落后 → 标 outdated。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_translation import ChapterTranslation, ProjectTranslation
from app.services.i18n_service import SUPPORTED_LANGUAGES

TRANSLATION_STATUSES = [
    "not_started",
    "draft",
    "machine_translated",
    "reviewing",
    "published",
    "outdated",
]


def _loads(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _dumps(data: list) -> str:
    return json.dumps(data, ensure_ascii=False)


async def set_project_languages(
    db: AsyncSession,
    project: Project,
    *,
    default_language: str | None = None,
    supported_languages: list[str] | None = None,
) -> Project:
    if default_language is not None:
        if default_language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"不支持的语言: {default_language}")
        project.default_language = default_language
    if supported_languages is not None:
        for lang in supported_languages:
            if lang not in SUPPORTED_LANGUAGES:
                raise ValueError(f"不支持的语言: {lang}")
        project.supported_languages = _dumps(supported_languages)
    await db.flush()
    return project


def get_supported_languages(project: Project) -> list[str]:
    langs = _loads(project.supported_languages)
    if project.default_language not in langs:
        langs.append(project.default_language)
    return langs


# ==================== 作品翻译 ====================

async def create_project_translation(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    language: str,
    title: str,
    description: str | None = None,
    status: str = "draft",
) -> ProjectTranslation:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"不支持的语言: {language}")
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise ValueError("作品不存在")
    if language not in get_supported_languages(project):
        raise ValueError(f"作品未启用语言: {language}")
    existing = (
        await db.execute(
            select(ProjectTranslation).where(
                ProjectTranslation.project_id == project_id,
                ProjectTranslation.language == language,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"该语言的作品翻译已存在: {language}")
    pt = ProjectTranslation(
        project_id=project_id,
        language=language,
        title=title,
        description=description,
        status=status,
    )
    db.add(pt)
    await db.flush()
    return pt


async def update_project_translation(
    db: AsyncSession,
    *,
    project_translation_id: uuid.UUID,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> ProjectTranslation:
    pt = (
        await db.execute(
            select(ProjectTranslation).where(
                ProjectTranslation.id == project_translation_id
            )
        )
    ).scalar_one_or_none()
    if pt is None:
        raise ValueError("作品翻译不存在")
    if title is not None:
        pt.title = title
    if description is not None:
        pt.description = description
    if status is not None:
        if status not in TRANSLATION_STATUSES:
            raise ValueError(f"非法翻译状态: {status}")
        pt.status = status
    pt.updated_at = datetime.utcnow()
    await db.flush()
    return pt


async def list_project_translations(
    db: AsyncSession, project_id: uuid.UUID
) -> list[ProjectTranslation]:
    result = await db.execute(
        select(ProjectTranslation)
        .where(ProjectTranslation.project_id == project_id)
        .order_by(ProjectTranslation.language.asc())
    )
    return list(result.scalars().all())


# ==================== 章节翻译 ====================

async def get_chapter(
    db: AsyncSession, chapter_id: uuid.UUID
) -> tuple[Outline | None, ChapterContent | None]:
    outline = (
        await db.execute(select(Outline).where(Outline.id == chapter_id))
    ).scalar_one_or_none()
    content = (
        await db.execute(
            select(ChapterContent).where(ChapterContent.outline_id == chapter_id)
        )
    ).scalar_one_or_none()
    return outline, content


async def create_chapter_translation(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    language: str,
    title: str,
    content: str = "",
    translation_status: str = "not_started",
) -> ChapterTranslation:
    """创建章节翻译；source_version_id 记录原文当前版本（ChapterContent.version）。"""
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"不支持的语言: {language}")
    if translation_status not in TRANSLATION_STATUSES:
        raise ValueError(f"非法翻译状态: {translation_status}")
    outline, content_model = await get_chapter(db, chapter_id)
    if outline is None:
        raise ValueError("章节不存在")
    source_version = content_model.version if content_model else 1
    existing = (
        await db.execute(
            select(ChapterTranslation).where(
                ChapterTranslation.chapter_id == chapter_id,
                ChapterTranslation.language == language,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"该语言的章节翻译已存在: {language}")
    ct = ChapterTranslation(
        chapter_id=chapter_id,
        language=language,
        title=title,
        content=content,
        translation_status=translation_status,
        source_version_id=(content_model.id if content_model else None),
        source_version=source_version,
    )
    db.add(ct)
    await db.flush()
    return ct


async def update_chapter_translation(
    db: AsyncSession,
    *,
    chapter_translation_id: uuid.UUID,
    title: str | None = None,
    content: str | None = None,
    translation_status: str | None = None,
) -> ChapterTranslation:
    ct = (
        await db.execute(
            select(ChapterTranslation).where(
                ChapterTranslation.id == chapter_translation_id
            )
        )
    ).scalar_one_or_none()
    if ct is None:
        raise ValueError("章节翻译不存在")
    if title is not None:
        ct.title = title
    if content is not None:
        ct.content = content
    if translation_status is not None:
        if translation_status not in TRANSLATION_STATUSES:
            raise ValueError(f"非法翻译状态: {translation_status}")
        if translation_status == "published":
            # 发布前校验与原文版本同步（不允许在 outdated 状态直接发布）
            _, content_model = await get_chapter(db, ct.chapter_id)
            current_version = content_model.version if content_model else 1
            if ct.source_version != current_version:
                ct.translation_status = "outdated"
                raise ValueError(
                    f"原文已更新（当前版本 {current_version}，译文基于 {ct.source_version}），请重新翻译后再发布"
                )
        ct.translation_status = translation_status
    ct.updated_at = datetime.utcnow()
    await db.flush()
    return ct


async def mark_chapter_translations_outdated(
    db: AsyncSession, chapter_id: uuid.UUID
) -> int:
    """原文更新后调用：把该章节所有 published 译文标记为 outdated，返回数量。"""
    result = await db.execute(
        select(ChapterTranslation).where(
            ChapterTranslation.chapter_id == chapter_id,
            ChapterTranslation.translation_status.in_(
                ("published", "reviewing", "machine_translated", "draft")
            ),
        )
    )
    items = list(result.scalars().all())
    now = datetime.utcnow()
    for ct in items:
        ct.translation_status = "outdated"
        ct.updated_at = now
    await db.flush()
    return len(items)


async def on_source_content_updated(
    db: AsyncSession, chapter_id: uuid.UUID
) -> int:
    """章节原文更新（服务端校验乐观锁并保存新版本）后：
    同步 source_version_id 并标记译文 outdated。返回被标记数量。"""
    _, content_model = await get_chapter(db, chapter_id)
    current_version = content_model.version if content_model else 1
    result = await db.execute(
        select(ChapterTranslation).where(
            ChapterTranslation.chapter_id == chapter_id
        )
    )
    items = list(result.scalars().all())
    now = datetime.utcnow()
    outdated_count = 0
    for ct in items:
        ct.source_version = current_version
        if ct.translation_status in ("published", "reviewing", "machine_translated", "draft"):
            ct.translation_status = "outdated"
            outdated_count += 1
        ct.updated_at = now
    await db.flush()
    return outdated_count


async def list_chapter_translations(
    db: AsyncSession,
    chapter_id: uuid.UUID,
    *,
    translation_status: str | None = None,
) -> list[ChapterTranslation]:
    stmt = select(ChapterTranslation).where(
        ChapterTranslation.chapter_id == chapter_id
    )
    if translation_status is not None:
        stmt = stmt.where(
            ChapterTranslation.translation_status == translation_status
        )
    result = await db.execute(stmt.order_by(ChapterTranslation.language.asc()))
    return list(result.scalars().all())


async def get_project_translation_for_chapter(
    db: AsyncSession, chapter_id: uuid.UUID
) -> ProjectTranslation | None:
    """按章节找到所属作品的指定语言翻译（章节 → 大纲 → 作品）。"""
    outline, _ = await get_chapter(db, chapter_id)
    if outline is None or outline.project_id is None:
        return None
    return None
