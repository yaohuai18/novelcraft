# AI 多语言服务（第二十一批「国际化、多语言和多区域部署」规格七）
# AI 功能明确指定：输入语言 / 输出语言 / 地区表达习惯(variant) / 语气(tone) / 拼写规范 / 术语表。
# 术语表（TranslationGlossary）：角色名/专有名词/地名/组织名/技能名固定译法，
# 强制匹配（force_exact）的专有名词禁止 AI 随意翻译，替换后仍须保留原文校验。
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.translation_glossary import TranslationGlossary
from app.services.i18n_service import SUPPORTED_LANGUAGES

GLOSSARY_TYPES = ["character", "proper_noun", "place", "organization", "skill", "fixed_term"]


async def create_glossary_term(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    source_language: str,
    target_language: str,
    source_text: str,
    target_text: str,
    term_type: str = "proper_noun",
    force_exact: bool = True,
    scope: str = "project",
) -> TranslationGlossary:
    """新增术语表条目。force_exact=True 时 AI 翻译必须使用固定译法，禁止随意翻译。"""
    if source_language not in SUPPORTED_LANGUAGES or target_language not in SUPPORTED_LANGUAGES:
        raise ValueError("不支持的语言")
    if term_type not in GLOSSARY_TYPES:
        raise ValueError(f"非法术语类型: {term_type}")
    if project_id is not None:
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise ValueError("作品不存在")
    term = TranslationGlossary(
        project_id=project_id,
        source_language=source_language,
        target_language=target_language,
        source_text=source_text,
        target_text=target_text,
        term_type=term_type,
        force_exact=force_exact,
        scope=scope,
        status="active",
    )
    db.add(term)
    await db.flush()
    return term


async def update_glossary_term(
    db: AsyncSession,
    *,
    glossary_id: uuid.UUID,
    target_text: str | None = None,
    force_exact: bool | None = None,
    status: str | None = None,
) -> TranslationGlossary:
    term = (
        await db.execute(
            select(TranslationGlossary).where(
                TranslationGlossary.id == glossary_id
            )
        )
    ).scalar_one_or_none()
    if term is None:
        raise ValueError("术语表条目不存在")
    if target_text is not None:
        term.target_text = target_text
    if force_exact is not None:
        term.force_exact = force_exact
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValueError(f"非法状态: {status}")
        term.status = status
    term.updated_at = None  # SQLAlchemy onupdate 自动更新
    await db.flush()
    return term


async def list_glossary_terms(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
) -> list[TranslationGlossary]:
    stmt = select(TranslationGlossary).order_by(
        TranslationGlossary.term_type.asc(),
        TranslationGlossary.source_text.asc(),
    )
    if project_id is not None:
        stmt = stmt.where(TranslationGlossary.project_id == project_id)
    if source_language is not None:
        stmt = stmt.where(TranslationGlossary.source_language == source_language)
    if target_language is not None:
        stmt = stmt.where(TranslationGlossary.target_language == target_language)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_glossary_terms(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    source_language: str,
    target_language: str,
) -> list[TranslationGlossary]:
    """获取翻译任务使用的术语表（项目级 + 全局级，force_exact 优先）。"""
    stmt = select(TranslationGlossary).where(
        TranslationGlossary.source_language == source_language,
        TranslationGlossary.target_language == target_language,
        TranslationGlossary.status == "active",
    )
    if project_id is not None:
        # 项目级术语优先
        stmt = stmt.where(
            (TranslationGlossary.project_id == project_id)
            | (TranslationGlossary.project_id.is_(None))
        )
    else:
        stmt = stmt.where(TranslationGlossary.project_id.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


def build_ai_translation_context(
    *,
    input_language: str,
    output_language: str,
    variant: str | None,
    tone: str | None,
    spelling: str | None,
    glossary: list[TranslationGlossary],
) -> dict:
    """构造 AI 翻译/润色指令上下文（含术语表强制匹配规则）。"""
    force_terms = [
        {
            "source_text": t.source_text,
            "target_text": t.target_text,
            "term_type": t.term_type,
        }
        for t in glossary
        if t.force_exact
    ]
    return {
        "input_language": input_language,
        "output_language": output_language,
        "variant": variant,
        "tone": tone,
        "spelling": spelling,
        "glossary_force_exact": force_terms,
        "rule": "术语表中 force_exact 的专有名词必须使用固定译法，禁止随意翻译",
    }


def validate_translated_glossary(
    source_text: str,
    translated_text: str,
    glossary: list[TranslationGlossary],
) -> dict:
    """校验译文是否保留术语固定译法。
    对每个 force_exact 术语：原文中出现 source_text 且译文未出现 target_text 时视为违规。
    """
    violations = []
    for term in glossary:
        if not term.force_exact:
            continue
        if term.source_text in source_text and term.target_text not in translated_text:
            violations.append(
                {
                    "source_text": term.source_text,
                    "target_text": term.target_text,
                    "term_type": term.term_type,
                }
            )
    return {
        "ok": len(violations) == 0,
        "violations": violations,
    }


async def prepare_ai_request(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    input_language: str,
    output_language: str,
    variant: str | None = None,
    tone: str | None = None,
    spelling: str | None = None,
) -> dict:
    """组装一次 AI 多语言任务的完整上下文（含术语表）。"""
    glossary = await get_glossary_terms(
        db,
        project_id=project_id,
        source_language=input_language,
        target_language=output_language,
    )
    return build_ai_translation_context(
        input_language=input_language,
        output_language=output_language,
        variant=variant,
        tone=tone,
        spelling=spelling,
        glossary=glossary,
    )
