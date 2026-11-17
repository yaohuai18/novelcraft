# 标签服务（第十四批「搜索、标签与发现系统」规格实现）
# 名称清洗（去空格/限长/禁重复/限特殊字符/统一大小写）、slug 唯一化、目标类型校验、
# 关系维护（TagRelation + OutlineTag）与 usage_count 同步、多标签 AND/OR 筛选。
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.models.tag import TAG_CATEGORIES, TAG_TARGET_TYPES, OutlineTag, Tag, TagRelation
from app.models.world_setting import WorldSetting

# 标签名允许的字符：中文/字母/数字/空白/_-。（去掉 / \ : * ? " < > | 等危险字符）
_ALLOWED_CHAR_RE = re.compile(r"[^\w\u4e00-\u9fff\-_\s]")
_MAX_TAG_NAME_LEN = 30


def clean_tag_name(name: str) -> str:
    """标签名称清洗：去首尾空格、压缩内部空白、去特殊字符、统一小写、限长。"""
    if name is None:
        return ""
    text = name.strip()
    text = re.sub(r"\s+", " ", text)
    text = _ALLOWED_CHAR_RE.sub("", text)
    text = text.strip("-_")
    if not text:
        return ""
    # 统一大小写规则：英文字母统一小写（中文不受影响）
    text = text.lower()
    return text[:_MAX_TAG_NAME_LEN]


def make_tag_slug(name: str) -> str:
    """由清洗后的名称生成 slug：非字母数字中文转连字符。"""
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", name.lower()).strip("-")
    return slug or "tag"


def normalize_category(category: str | None) -> str:
    """标签分类归一化，非法分类回落为「自定义」。"""
    if category and category in TAG_CATEGORIES:
        return category
    return "自定义"


async def ensure_unique_name(
    db: AsyncSession, project_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
) -> None:
    """项目内标签名称唯一（清洗后比较）。重复则抛 ValueError。"""
    stmt = select(Tag).where(Tag.project_id == project_id)
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    rows = (await db.execute(stmt)).scalars().all()
    for t in rows:
        if clean_tag_name(t.name) == name:
            raise ValueError(f"标签名称已存在: {t.name}")


async def ensure_unique_slug(
    db: AsyncSession, project_id: uuid.UUID, slug: str, exclude_id: uuid.UUID | None = None
) -> str:
    """项目内 slug 唯一；冲突时追加短随机后缀。"""
    candidate = slug
    while True:
        stmt = select(Tag).where(Tag.project_id == project_id, Tag.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(Tag.id != exclude_id)
        exists = (await db.execute(stmt)).scalar_one_or_none()
        if exists is None:
            return candidate
        candidate = f"{slug}-{uuid.uuid4().hex[:4]}"


def validate_target_type(target_type: str) -> None:
    """校验通用关系目标类型。"""
    if target_type not in TAG_TARGET_TYPES:
        raise ValueError(f"不支持的目标类型: {target_type}")


def _map_relation_target_to_model(target_type: str):
    """目标类型 -> 存在性校验模型（无独立模型的类型返回 None 表示跳过校验）。"""
    mapping = {
        "project": Project,
        "volume": Outline,
        "chapter": Outline,
        "scene": Outline,
        "character": Character,
        "worldview_rule": WorldSetting,
    }
    return mapping.get(target_type)


async def validate_target_exists(
    db: AsyncSession, project_id: uuid.UUID, target_type: str, target_id: uuid.UUID
) -> None:
    """校验目标存在且属于该项目；无独立模型的类型跳过。"""
    model = _map_relation_target_to_model(target_type)
    if model is None:
        return
    obj = await db.get(model, target_id)
    if obj is None:
        raise ValueError(f"目标不存在: {target_type}/{target_id}")
    # project 类型目标无 project_id 字段，直接比较主键；其余目标比较 project_id
    if target_type == "project":
        if obj.id != project_id:
            raise ValueError("目标不属于当前作品")
    elif getattr(obj, "project_id", None) != project_id:
        raise ValueError("目标不属于当前作品")


async def add_relation(
    db: AsyncSession, project_id: uuid.UUID, tag_id: uuid.UUID,
    target_type: str, target_id: uuid.UUID, commit_usage: bool = True,
) -> TagRelation | None:
    """创建单条通用关系（幂等：已存在则跳过）。返回新关系或 None。"""
    validate_target_type(target_type)
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.project_id != project_id:
        raise ValueError("标签不存在")
    await validate_target_exists(db, project_id, target_type, target_id)
    exists = (await db.execute(
        select(TagRelation).where(
            TagRelation.tag_id == tag_id,
            TagRelation.target_type == target_type,
            TagRelation.target_id == target_id,
        )
    )).scalar_one_or_none()
    if exists is not None:
        return None
    rel = TagRelation(tag_id=tag_id, target_type=target_type, target_id=target_id)
    db.add(rel)
    if commit_usage:
        tag.usage_count += 1
    await db.flush()
    return rel


async def add_batch_relations(
    db: AsyncSession, project_id: uuid.UUID,
    target_type: str, target_ids: list[uuid.UUID], tag_ids: list[uuid.UUID],
) -> tuple[int, int, list[uuid.UUID]]:
    """批量添加标签关系。返回 (created, skipped, relation_ids)。"""
    validate_target_type(target_type)
    created = 0
    skipped = 0
    relation_ids: list[uuid.UUID] = []
    # 预取项目内标签，校验归属
    tag_rows = (await db.execute(
        select(Tag).where(Tag.project_id == project_id, Tag.id.in_(tag_ids))
    )).scalars().all()
    valid_tags = {t.id: t for t in tag_rows}
    for tid in tag_ids:
        if tid not in valid_tags:
            skipped += 1
    for target_id in target_ids:
        await validate_target_exists(db, project_id, target_type, target_id)
        # 按请求 tag_ids 顺序迭代，保证 relation_ids 与 tag_ids 一一对应
        for tid in tag_ids:
            tag = valid_tags.get(tid)
            if tag is None:
                continue
            rel = await add_relation(
                db, project_id, tid, target_type, target_id, commit_usage=True
            )
            if rel is None:
                skipped += 1
            else:
                created += 1
                relation_ids.append(rel.id)
    await db.flush()
    return created, skipped, relation_ids


async def remove_relation(
    db: AsyncSession, project_id: uuid.UUID, tag_id: uuid.UUID,
    target_type: str, target_id: uuid.UUID,
) -> bool:
    """删除单条通用关系；返回是否删除。"""
    validate_target_type(target_type)
    rel = (await db.execute(
        select(TagRelation).where(
            TagRelation.tag_id == tag_id,
            TagRelation.target_type == target_type,
            TagRelation.target_id == target_id,
        )
    )).scalar_one_or_none()
    if rel is None:
        return False
    await db.delete(rel)
    tag = await db.get(Tag, tag_id)
    if tag is not None and tag.usage_count > 0:
        tag.usage_count -= 1
    await db.flush()
    return True


async def remove_relation_by_id(
    db: AsyncSession, project_id: uuid.UUID, relation_id: uuid.UUID,
) -> bool:
    """按关系主键删除（校验标签属于当前项目）。"""
    rel = await db.get(TagRelation, relation_id)
    if rel is None:
        return False
    tag = await db.get(Tag, rel.tag_id)
    if tag is None or tag.project_id != project_id:
        raise ValueError("标签不存在")
    await db.delete(rel)
    if tag.usage_count > 0:
        tag.usage_count -= 1
    await db.flush()
    return True


async def add_outline_tag(
    db: AsyncSession, project_id: uuid.UUID, tag_id: uuid.UUID, outline_id: uuid.UUID
) -> bool:
    """给大纲条目打标签（第九批接口，保留）；重复则跳过。"""
    outline = await db.get(Outline, outline_id)
    if outline is None or outline.project_id != project_id:
        raise ValueError("大纲不存在")
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.project_id != project_id:
        raise ValueError("标签不存在")
    exists = (await db.execute(
        select(OutlineTag).where(
            OutlineTag.outline_id == outline_id,
            OutlineTag.tag_id == tag_id,
        )
    )).scalar_one_or_none()
    if exists is not None:
        return False
    db.add(OutlineTag(outline_id=outline_id, tag_id=tag_id))
    tag.usage_count += 1
    await db.flush()
    return True


async def remove_outline_tag(
    db: AsyncSession, project_id: uuid.UUID, tag_id: uuid.UUID, outline_id: uuid.UUID
) -> bool:
    """移除大纲标签。"""
    ot = (await db.execute(
        select(OutlineTag).where(
            OutlineTag.outline_id == outline_id,
            OutlineTag.tag_id == tag_id,
        )
    )).scalar_one_or_none()
    if ot is None:
        return False
    await db.delete(ot)
    tag = await db.get(Tag, tag_id)
    if tag is not None and tag.usage_count > 0:
        tag.usage_count -= 1
    await db.flush()
    return True


async def compute_usage_count(db: AsyncSession, tag_id: uuid.UUID) -> int:
    """重算标签使用次数（TagRelation + OutlineTag）。"""
    rel_n = (await db.execute(
        select(func.count(TagRelation.id)).where(TagRelation.tag_id == tag_id)
    )).scalar_one() or 0
    ot_n = (await db.execute(
        select(func.count(OutlineTag.tag_id)).where(OutlineTag.tag_id == tag_id)
    )).scalar_one() or 0
    return int(rel_n) + int(ot_n)


async def recompute_all_usage_counts(db: AsyncSession, project_id: uuid.UUID) -> None:
    """项目内所有标签重算 usage_count（兜底修复）。"""
    tags = (await db.execute(
        select(Tag).where(Tag.project_id == project_id)
    )).scalars().all()
    for tag in tags:
        tag.usage_count = await compute_usage_count(db, tag.id)
    await db.flush()


async def list_project_tags(
    db: AsyncSession, project_id: uuid.UUID,
    category: str | None = None, keyword: str | None = None,
    page: int = 1, page_size: int = 100,
) -> tuple[list[Tag], int]:
    """项目标签列表，可按分类/关键词过滤，按 usage_count desc。"""
    stmt = select(Tag).where(Tag.project_id == project_id)
    if category:
        stmt = stmt.where(Tag.category == category)
    if keyword:
        stmt = stmt.where(Tag.name.ilike(f"%{keyword.strip()}%"))
    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one() or 0
    stmt = stmt.order_by(Tag.usage_count.desc(), Tag.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def filter_targets_by_tags(
    db: AsyncSession, project_id: uuid.UUID,
    tag_ids: list[uuid.UUID], match_mode: str = "AND",
    target_types: list[str] | None = None,
    limit: int = 200,
) -> list[tuple[str, uuid.UUID]]:
    """按多标签筛选目标（AND=必须同时包含全部标签；OR=包含任意一个）。

    返回 [(target_type, target_id), ...]，目标需属于本项目。
    """
    if not tag_ids:
        return []
    mode = (match_mode or "AND").upper()
    if mode == "OR":
        stmt = (
            select(TagRelation.target_type, TagRelation.target_id)
            .join(Tag, Tag.id == TagRelation.tag_id)
            .where(
                Tag.project_id == project_id,
                TagRelation.tag_id.in_(tag_ids),
            )
            .distinct()
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
        return [(r[0], r[1]) for r in rows]

    # AND：按目标分组统计命中标签数，等于 tag_ids 数量
    stmt = (
        select(TagRelation.target_type, TagRelation.target_id)
        .join(Tag, Tag.id == TagRelation.tag_id)
        .where(
            Tag.project_id == project_id,
            TagRelation.tag_id.in_(tag_ids),
        )
        .group_by(TagRelation.target_type, TagRelation.target_id)
        .having(func.count(TagRelation.tag_id.distinct()) >= len(set(tag_ids)))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [(r[0], r[1]) for r in rows]
