# 发现系统服务（第十四批「搜索、标签与发现系统」规格实现）
# 公开作品列表（最新发布/最近更新/热门）、热度公式 hot_score = score / pow(age_hours+2, 0.5)、
# 热门标签、相似作品推荐（共同标签数量）、公开统计字段（服务端更新，不信任客户端）。
import math
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.tag import Tag, TagRelation
from app.models.favorite import Favorite
from app.models.reader import ReadingProgress
from app.services import search_service

_CACHE_TTL = 30.0


def _now() -> datetime:
    # 模型时间戳均为 naive UTC（datetime.utcnow），保持一致避免 aware/naive 混算
    return datetime.utcnow()


def hot_score(project: Project) -> float:
    """热度计算（十四节规格）：
    score = view_count*0.2 + favorite_count*2 + completion_count*3 + recent_update_bonus
    hot_score = score / pow(age_hours + 2, 0.5)
    """
    view_bonus = project.view_count * 0.2
    fav_bonus = project.favorite_count * 2
    compl_bonus = project.completion_count * 3
    recent_bonus = 0.0
    if project.last_published_at is not None:
        hours = (_now() - project.last_published_at).total_seconds() / 3600.0
        if hours < 48:
            recent_bonus = 2.0 * (1 - hours / 48.0)
    score = view_bonus + fav_bonus + compl_bonus + recent_bonus

    base = project.last_published_at or project.published_at or project.updated_at or _now()
    age_hours = max(0.0, (_now() - base).total_seconds() / 3600.0)
    hot = score / math.pow(age_hours + 2, 0.5)
    return round(hot, 4)


def _project_to_public_item(project: Project, tags: list[str] | None = None) -> dict:
    return {
        "id": project.id,
        "title": project.title or "",
        "subtitle": getattr(project, "subtitle", None),
        "description": project.description,
        "cover_text": getattr(project, "cover_text", None),
        "project_slug": getattr(project, "project_slug", None),
        "genre": getattr(project, "genre", None),
        "publication_status": project.publication_status or "draft",
        "published_at": project.published_at,
        "updated_at": project.updated_at,
        "last_published_at": project.last_published_at,
        "view_count": project.view_count or 0,
        "unique_reader_count": project.unique_reader_count or 0,
        "favorite_count": project.favorite_count or 0,
        "chapter_count": project.chapter_count or 0,
        "word_count": project.word_count or 0,
        "completion_count": project.completion_count or 0,
        "hot_score": hot_score(project),
        "tags": tags or [],
    }


async def _published_projects(db: AsyncSession) -> list[Project]:
    stmt = select(Project).where(
        Project.publication_status == "published",
        Project.status == "active",
    )
    return list((await db.execute(stmt)).scalars().all())


async def _project_tags(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not project_ids:
        return {}
    rows = (await db.execute(
        select(Tag.project_id, Tag.name)
        .join(TagRelation, TagRelation.tag_id == Tag.id)
        .where(
            Tag.project_id.in_(project_ids),
            TagRelation.target_type == "project",
        )
        .order_by(Tag.usage_count.desc())
    )).all()
    result: dict[uuid.UUID, list[str]] = {}
    for pid, name in rows:
        result.setdefault(pid, []).append(name)
    return result


async def list_latest(db: AsyncSession, page: int = 1, page_size: int = 10) -> dict:
    """最新发布：按 published_at desc。"""
    cache_k = search_service.cache_key("discover-latest", page, page_size)
    cached = search_service._cache_get(cache_k)
    if cached is not None:
        return cached
    projs = await _published_projects(db)
    projs.sort(key=lambda p: p.published_at or datetime.min, reverse=True)
    total = len(projs)
    paged = projs[(page - 1) * page_size: page * page_size]
    tags = await _project_tags(db, [p.id for p in paged])
    result = {
        "items": [_project_to_public_item(p, tags.get(p.id)) for p in paged],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    search_service._cache_set(cache_k, result)
    return result


async def list_recent_updated(db: AsyncSession, page: int = 1, page_size: int = 10) -> dict:
    """最近更新：按 updated_at desc。"""
    cache_k = search_service.cache_key("discover-updated", page, page_size)
    cached = search_service._cache_get(cache_k)
    if cached is not None:
        return cached
    projs = await _published_projects(db)
    projs.sort(key=lambda p: p.last_published_at or p.updated_at or datetime.min, reverse=True)
    total = len(projs)
    paged = projs[(page - 1) * page_size: page * page_size]
    tags = await _project_tags(db, [p.id for p in paged])
    result = {
        "items": [_project_to_public_item(p, tags.get(p.id)) for p in paged],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    search_service._cache_set(cache_k, result)
    return result


async def list_hot(db: AsyncSession, page: int = 1, page_size: int = 10) -> dict:
    """热门作品：按 hot_score desc（时间衰减热度公式）。"""
    cache_k = search_service.cache_key("discover-hot", page, page_size)
    cached = search_service._cache_get(cache_k)
    if cached is not None:
        return cached
    projs = await _published_projects(db)
    projs.sort(key=lambda p: hot_score(p), reverse=True)
    total = len(projs)
    paged = projs[(page - 1) * page_size: page * page_size]
    tags = await _project_tags(db, [p.id for p in paged])
    result = {
        "items": [_project_to_public_item(p, tags.get(p.id)) for p in paged],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    search_service._cache_set(cache_k, result)
    return result


async def list_hot_tags(db: AsyncSession, limit: int = 20) -> list[dict]:
    """热门标签：全站（公开作品所用）按 usage_count desc。"""
    cache_k = search_service.cache_key("discover-hot-tags", limit)
    cached = search_service._cache_get(cache_k)
    if cached is not None:
        return cached
    rows = (await db.execute(
        select(Tag)
        .order_by(Tag.usage_count.desc())
        .limit(min(limit, 100))
    )).scalars().all()
    result = [{
        "id": t.id,
        "name": t.name,
        "slug": t.slug,
        "color": t.color,
        "category": t.category,
        "usage_count": t.usage_count or 0,
    } for t in rows]
    search_service._cache_set(cache_k, result)
    return result


async def discover(db: AsyncSession, page: int = 1, page_size: int = 10) -> dict:
    """发现页聚合：最新发布 + 最近更新 + 热门 + 热门标签。"""
    latest, updated, hot = await _parallel3(
        list_latest(db, page, page_size),
        list_recent_updated(db, page, page_size),
        list_hot(db, page, page_size),
    )
    hot_tags = await list_hot_tags(db, 20)
    return {
        "latest": latest,
        "updated": updated,
        "hot": hot,
        "hot_tags": hot_tags,
    }


async def _parallel3(*coros):
    import asyncio
    return await asyncio.gather(*coros)


async def similar_projects(
    db: AsyncSession, project_id: uuid.UUID, limit: int = 10,
) -> list[dict]:
    """相似作品推荐：共同标签数量越多相似度越高（仅公开作品，排除自身）。"""
    project = await db.get(Project, project_id)
    if project is None:
        return []
    # 自身标签名集合（TagRelation project；跨项目按名称匹配共同标签）
    my_tag_names = set((await db.execute(
        select(Tag.name).join(TagRelation, TagRelation.tag_id == Tag.id).where(
            Tag.project_id == project_id,
            TagRelation.target_type == "project",
        )
    )).scalars().all())
    if not my_tag_names:
        return []

    cache_k = search_service.cache_key("similar", project_id, limit)
    cached = search_service._cache_get(cache_k)
    if cached is not None:
        return cached

    # 候选：拥有共同标签的公开作品
    rows = (await db.execute(
        select(Tag.project_id, func.count(Tag.id.distinct()).label("common"))
        .join(TagRelation, TagRelation.tag_id == Tag.id)
        .where(
            TagRelation.target_type == "project",
            Tag.name.in_(my_tag_names),
            Tag.project_id != project_id,
        )
        .group_by(Tag.project_id)
        .order_by(func.count(Tag.id.distinct()).desc())
        .limit(limit * 3)
    )).all()
    candidates: list[tuple[uuid.UUID, int]] = [(r[0], int(r[1])) for r in rows]
    if not candidates:
        search_service._cache_set(cache_k, [])
        return []

    proj_ids = [pid for pid, _ in candidates]
    projs = (await db.execute(
        select(Project).where(
            Project.id.in_(proj_ids),
            Project.publication_status == "published",
            Project.status == "active",
        )
    )).scalars().all()
    proj_map = {p.id: p for p in projs}
    tags = await _project_tags(db, proj_ids)
    result = []
    for pid, common in candidates:
        p = proj_map.get(pid)
        if p is None:
            continue
        similarity = round(common / max(len(my_tag_names), 1), 4)
        result.append({
            "project": _project_to_public_item(p, tags.get(pid)),
            "common_tag_count": common,
            "similarity": similarity,
        })
    result.sort(key=lambda x: (x["common_tag_count"], x["project"]["hot_score"]), reverse=True)
    result = result[:limit]
    search_service._cache_set(cache_k, result)
    return result


# ============ 公开统计字段（服务端更新） ============

async def count_published_chapters(db: AsyncSession, project_id: uuid.UUID) -> int:
    """统计已发布章节数。"""
    from app.models.outline import Outline
    row = (await db.execute(
        select(func.count(Outline.id)).where(
            Outline.project_id == project_id,
            Outline.outline_type.in_(["chapter", "scene"]),
            Outline.status == "published",
        )
    )).scalar_one()
    return int(row or 0)


async def count_project_words(db: AsyncSession, project_id: uuid.UUID) -> int:
    """统计正文字数（仅已发布章节；chapter_contents 无 status 列，须 JOIN outlines）。"""
    from app.models.outline import Outline
    from app.models.chapter_content import ChapterContent
    row = (await db.execute(
        select(func.coalesce(func.sum(ChapterContent.word_count), 0))
        .join(Outline, Outline.id == ChapterContent.outline_id)
        .where(
            Outline.project_id == project_id,
            Outline.status == "published",
        )
    )).scalar_one()
    return int(row or 0)


async def refresh_project_stats(db: AsyncSession, project_id: uuid.UUID) -> None:
    """服务端重算统计字段（发布/阅读/收藏变化后调用）。"""
    project = await db.get(Project, project_id)
    if project is None:
        return
    project.chapter_count = await count_published_chapters(db, project_id)
    project.word_count = await count_project_words(db, project_id)
    project.favorite_count = (await db.execute(
        select(func.count(Favorite.id)).where(
            Favorite.target_type == "project",
            Favorite.target_id == project_id,
        )
    )).scalar_one() or 0
    project.completion_count = (await db.execute(
        select(func.count(ReadingProgress.id)).where(
            ReadingProgress.project_id == project_id,
            ReadingProgress.completed.is_(True),
        )
    )).scalar_one() or 0
    await db.flush()


async def record_view(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """服务端记录阅读次数（view_count++；unique_reader_count 需按读者去重）。"""
    project = await db.get(Project, project_id)
    if project is None:
        return
    project.view_count = (project.view_count or 0) + 1
    if user_id is not None:
        # 简单去重：记录在 ReadingProgress 或 reader 访问里，这里只累加 view_count
        pass
    await db.flush()
