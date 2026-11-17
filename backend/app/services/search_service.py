# 统一搜索服务（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# 项目内搜索：作品信息/大纲/章节标题与正文/人物/地点/势力/世界观/时间线/人物关系/标签/版本摘要；
# 全局搜索：用户有权访问的多个作品（本人私有 + 已公开），权限与软删除过滤；
# 初期实现关键词搜索 + 标题优先 + 正文包含匹配 + 结果高亮；SQLite ilike 兜底（FTS5 可选）。
# 匹配优先级：标题完全 > 标题部分 > 标签 > 人物/地点名称 > 摘要 > 正文。
import logging
import re
import time
import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outline import Outline
from app.models.project import Project
from app.models.search_document import SearchDocument
from app.models.search_history import SearchHistory
from app.models.tag import OutlineTag, Tag, TagRelation
from app.services import index_service

logger = logging.getLogger(__name__)

# type 参数允许的取值（outline/chapter/content/tag/project/character/world_setting，兼容第九批）
SEARCH_TYPES = {
    "outline", "chapter", "content", "tag", "project",
    "character", "world_setting", "location", "faction",
    "timeline_event", "version",
}
# outline_type 过滤允许的取值
OUTLINE_TYPES = {"summary", "volume", "chapter", "scene"}
# 索引 target_type -> 搜索结果 target_type 的映射
_TARGET_TYPE_MAP = {
    "outline": "outline",
    "chapter_content": "chapter",
    "tag": "tag",
    "character": "character",
    "world_setting": "world_setting",
    "project": "project",
}
_SNIPPET_LEN = 120
_HIGHLIGHT_TAG_OPEN = "<mark>"
_HIGHLIGHT_TAG_CLOSE = "</mark>"

# 搜索/建议缓存：键包含 user_id/project_id/query/filters/page，避免用户串读
_search_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 30.0


def cache_key(*parts) -> str:
    return "|".join(str(p) for p in parts)


def _cache_get(key: str):
    item = _search_cache.get(key)
    if item is None:
        return None
    ts, value = item
    if time.time() - ts > _CACHE_TTL:
        _search_cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value) -> None:
    _search_cache[key] = (time.time(), value)


def parse_query(q: str) -> tuple[list[str], list[str], list[str]]:
    """解析查询串：返回 (正向词, 排除词, 短语)。

    支持：多个关键词（空格分隔）、短语搜索（双引号）、排除关键词（-前缀）。
    示例：边境 夜袭；"异常印记"；边境 -港口
    """
    raw = (q or "").strip()
    if not raw:
        return [], [], []
    terms: list[str] = []
    excludes: list[str] = []
    phrases: list[str] = []
    for part in re.findall(r'"[^"]+"|-?\S+', raw):
        if part.startswith('"') and part.endswith('"') and len(part) > 2:
            phrases.append(part[1:-1])
        elif part.startswith("-") and len(part) > 1:
            excludes.append(part[1:])
        else:
            terms.append(part)
    terms = [t for t in terms if t]
    return terms, excludes, phrases


def _score(
    title_exact: bool, title_hit: bool, tag_hit: bool, name_hit: bool,
    summary_hits: int, text_hits: int, phrase_hits: int,
) -> float:
    """按规格四节匹配优先级打分：标题完全 > 标题部分 > 标签 > 名称 > 摘要 > 正文。"""
    score = 0.0
    if title_exact:
        score += 3.0
    elif title_hit:
        score += 2.0
    if tag_hit:
        score += 1.8
    if name_hit:
        score += 1.5
    score += 1.2 * min(summary_hits, 3)
    score += 0.5 * min(text_hits, 5)
    score += 0.8 * min(phrase_hits, 3)
    return round(min(score, 6.0), 2)


def make_snippet(plain_text: str, keyword: str) -> str:
    """纯文本匹配片段（第九批兼容）。"""
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    if not text:
        return ""
    keyword = (keyword or "").strip()
    if not keyword:
        return text[:_SNIPPET_LEN]
    pos = text.find(keyword)
    if pos >= 0:
        start = max(0, pos - 30)
        end = min(len(text), pos + len(keyword) + 60)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"
    return text[:_SNIPPET_LEN]


def make_highlight(plain_text: str, terms: list[str], phrases: list[str]) -> str:
    """生成高亮片段：定位首个命中词，取上下文并给关键词加 <mark>。"""
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    if not text:
        return ""
    lower = text.lower()
    hits = [w for w in terms if w and w.lower() in lower] + \
           [p for p in phrases if p and p.lower() in lower]
    if not hits:
        return text[:_SNIPPET_LEN]
    hit = hits[0]
    pos = lower.find(hit.lower())
    start = max(0, pos - 30)
    end = min(len(text), pos + len(hit) + 60)
    fragment = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    for w in sorted(set(terms + phrases), key=len, reverse=True):
        if not w:
            continue
        fragment = re.sub(
            re.escape(w),
            f"{_HIGHLIGHT_TAG_OPEN}{w}{_HIGHLIGHT_TAG_CLOSE}",
            fragment,
            flags=re.IGNORECASE,
        )
    return f"{prefix}{fragment}{suffix}"


def _match_position(title_hit: bool, summary_hit: bool, text_hit: bool, tag_hit: bool) -> str:
    if title_hit:
        return "title"
    if tag_hit:
        return "tag"
    if summary_hit:
        return "summary"
    if text_hit:
        return "content"
    return ""


async def _load_outlines(
    db: AsyncSession, doc_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Outline]:
    if not doc_ids:
        return {}
    stmt = select(Outline).where(Outline.id.in_(doc_ids))
    return {o.id: o for o in (await db.execute(stmt)).scalars().all()}


def _doc_score_and_flags(
    doc: SearchDocument, terms: list[str], excludes: list[str], phrases: list[str],
    name_keywords: list[str] | None = None,
) -> tuple[float, bool, bool, bool, bool]:
    """计算文档得分与命中标记（title_exact/title/tag/name）。排除词命中返回 None。"""
    title_l = (doc.title or "").lower()
    text_l = (doc.plain_text or "").lower()
    summary_l = (doc.summary or "").lower()
    tags_l = (doc.tags_text or "").lower()
    name_kws = name_keywords or []

    for ex in excludes:
        if ex and (ex.lower() in title_l or ex.lower() in text_l):
            return 0.0, False, False, False, False

    title_exact = any(t and t.lower() == title_l for t in terms if t) or \
        (bool(phrases) and all(p.lower() in title_l for p in phrases))
    title_hit = any(t and t.lower() in title_l for t in terms) or \
        any(p and p.lower() in title_l for p in phrases)
    tag_hit = any(t and t.lower() in tags_l for t in terms) or \
        any(p and p.lower() in tags_l for p in phrases)
    name_hit = any(t and t.lower() in (n or "").lower() for t in terms for n in name_kws)
    summary_hits = sum(1 for t in terms if t and t.lower() in summary_l)
    text_hits = sum(text_l.count(t.lower()) for t in terms)
    phrase_hits = sum(text_l.count(p.lower()) + title_l.count(p.lower()) for p in phrases)

    score = _score(title_exact, title_hit, tag_hit, name_hit, summary_hits, text_hits, phrase_hits)
    return score, title_exact or title_hit, summary_hits > 0, text_hits > 0, tag_hit


def _build_item(
    doc: SearchDocument, outline: Outline | None, terms: list[str], phrases: list[str],
    score: float, match_pos: str, name_keywords: list[str] | None = None,
) -> dict:
    title = doc.title or ""
    highlight_text = doc.plain_text or doc.summary or title
    if match_pos == "title" and terms:
        highlight_text = title
    highlight = make_highlight(highlight_text, terms, phrases)
    snippet = make_snippet(doc.plain_text, terms[0] if terms else "")
    return {
        "target_type": _map_to_search_type(doc, outline),
        "target_id": doc.target_id,
        "title": title,
        "highlight": highlight,
        "snippet": snippet,
        "score": score,
        "updated_at": doc.updated_at,
        "status": doc.status,
        "visibility": doc.visibility,
        "volume_title": doc.volume_title,
        "match_position": match_pos,
        # 第九批兼容
        "type": _map_to_search_type(doc, outline),
        "id": doc.target_id,
        "outline_type": outline.outline_type if outline else None,
        "parent_id": outline.parent_id if outline else None,
    }


def _map_to_search_type(doc: SearchDocument, outline: Outline | None) -> str:
    if doc.target_type == "outline":
        if outline and outline.outline_type in ("chapter", "scene"):
            return "chapter"
        return "outline"
    if doc.target_type == "chapter_content":
        return "content"
    return doc.target_type


async def search(
    db: AsyncSession,
    project_id: uuid.UUID,
    q: str,
    types: list[str] | None = None,
    tag_id: uuid.UUID | None = None,
    outline_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
    # 第十四批扩展参数
    target_type: str | None = None,
    tag_ids: list[uuid.UUID] | None = None,
    match_mode: str = "AND",
    status: str | None = None,
    sort: str = "relevance",
    user_id: uuid.UUID | None = None,
) -> tuple[list[dict], int]:
    """统一项目内搜索。返回 (items, total)。items 元素为符合 SearchItem 的 dict。

    参数均带默认值，第九批既有调用保持不变。
    """
    terms, excludes, phrases = parse_query(q)
    keyword = " ".join(terms)
    if not keyword and not phrases:
        return [], 0

    cache_k = cache_key(
        "project-search", user_id, project_id, q, target_type,
        ",".join(str(t) for t in (tag_ids or [])), match_mode, status, sort, page, page_size
    )
    cached = _cache_get(cache_k)
    if cached is not None:
        return cached

    types = types or list(SEARCH_TYPES)
    type_set = {t for t in types if t in SEARCH_TYPES}
    # 兼容：target_type 新参数映射进 type_set
    if target_type:
        mapped = {
            "chapter": {"chapter"},
            "content": {"content"},
            "outline": {"outline", "chapter"},
            "tag": {"tag"},
            "project": {"project"},
            "character": {"character"},
            "world_setting": {"world_setting"},
        }.get(target_type, {target_type})
        type_set &= mapped
    kw = f"%{keyword}%"

    items: list[dict] = []

    # 1) 索引表检索（大纲/正文/标签/人物/设定/作品）
    if type_set & {"outline", "chapter", "content", "tag", "character", "world_setting", "project", "location", "faction", "timeline_event", "version"}:
        stmt = select(SearchDocument).where(
            SearchDocument.project_id == project_id,
            SearchDocument.deleted_at.is_(None),
        )
        if tag_id is not None:
            tag_ids = (tag_ids or []) + [tag_id]
        if tag_ids:
            tagged = await tag_service_filter_targets(db, project_id, tag_ids, match_mode)
            if not tagged:
                items, total = [], 0
                _cache_set(cache_k, (items, total))
                return items, total
            types_ok = {t for t, _ in tagged}
            ids_ok = {t: set() for t, _ in tagged}
            for t, i in tagged:
                ids_ok.setdefault(t, set()).add(i)
            from sqlalchemy import and_
            conds = []
            for t, ids in ids_ok.items():
                mapped_doc_type = {
                    "project": "project", "chapter": "chapter_content",
                    "volume": "outline", "scene": "outline",
                    "character": "character", "worldview_rule": "world_setting",
                }.get(t)
                if mapped_doc_type and ids:
                    conds.append(and_(
                        SearchDocument.target_type == mapped_doc_type,
                        SearchDocument.target_id.in_(ids),
                    ))
            if not conds:
                items, total = [], 0
                _cache_set(cache_k, (items, total))
                return items, total
            stmt = stmt.where(or_(*conds))
        else:
            # 多关键词：任一关键词命中即召回（AND 过滤由 _doc_score_and_flags 打分/排除处理）
            conds = []
            for t in terms:
                if not t:
                    continue
                tk = f"%{t}%"
                conds.append(or_(
                    SearchDocument.title.ilike(tk),
                    SearchDocument.plain_text.ilike(tk),
                    SearchDocument.summary.ilike(tk),
                    SearchDocument.tags_text.ilike(tk),
                ))
            for p in phrases:
                if not p:
                    continue
                pk = f"%{p}%"
                conds.append(or_(
                    SearchDocument.title.ilike(pk),
                    SearchDocument.plain_text.ilike(pk),
                    SearchDocument.summary.ilike(pk),
                    SearchDocument.tags_text.ilike(pk),
                ))
            if conds:
                stmt = stmt.where(or_(*conds))
        if status:
            stmt = stmt.where(SearchDocument.status == status)
        docs = (await db.execute(stmt)).scalars().all()
        items.extend(await _build_doc_items(db, docs, terms, phrases, excludes, type_set, tag_ids, outline_type, status))

    # 2) 作品名称/简介命中（Project 直查，保证未建索引也命中）
    if "project" in type_set and not tag_ids:
        proj = await db.get(Project, project_id)
        if proj is not None:
            proj_title_hit = proj.title and keyword.lower() in proj.title.lower()
            bg = proj.description or ""
            proj_bg_hit = keyword.lower() in bg.lower()
            if proj_title_hit or proj_bg_hit:
                title = proj.title or ""
                snippet = make_snippet(bg if proj_bg_hit else title, keyword) or make_snippet(bg, keyword)
                items.append({
                    "target_type": "project",
                    "target_id": proj.id,
                    "title": title,
                    "highlight": make_highlight(title if proj_title_hit else bg, terms, phrases),
                    "snippet": snippet,
                    "score": _score(proj_title_hit and keyword.lower() == (title or "").lower(), proj_title_hit, False, False, 1 if proj_bg_hit else 0, 1 if proj_bg_hit else 0, 0),
                    "updated_at": proj.updated_at,
                    "status": proj.status,
                    "visibility": "public" if proj.publication_status == "published" else "private",
                    "volume_title": None,
                    "match_position": "title" if proj_title_hit else "summary",
                    "type": "project",
                    "id": proj.id,
                    "outline_type": None,
                    "parent_id": None,
                })

    if sort == "updated":
        items.sort(key=lambda it: it.get("updated_at") or datetime.min, reverse=True)
    else:
        items.sort(key=lambda it: it["score"], reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    paged = items[start:start + page_size]
    result = (paged, total)
    _cache_set(cache_k, result)
    return result


async def tag_service_filter_targets(
    db: AsyncSession, project_id: uuid.UUID,
    tag_ids: list[uuid.UUID], match_mode: str,
) -> list[tuple[str, uuid.UUID]]:
    """标签筛选目标（AND/OR），延迟导入避免循环依赖。"""
    from app.services import tag_service
    return await tag_service.filter_targets_by_tags(
        db, project_id, tag_ids, match_mode=match_mode
    )


async def _build_doc_items(
    db: AsyncSession,
    docs: list[SearchDocument],
    terms: list[str], phrases: list[str], excludes: list[str],
    type_set: set[str], tag_ids: list[uuid.UUID] | None,
    outline_type: str | None, status: str | None,
) -> list[dict]:
    if not docs:
        return []

    outline_ids = {
        d.target_id for d in docs if d.target_type in ("outline", "chapter_content")
    }
    outlines: dict[uuid.UUID, Outline] = await _load_outlines(db, outline_ids)

    # 排除：deleted_at 已在 SQL 过滤；排除词在 _doc_score_and_flags 处理

    items: list[dict] = []
    for doc in docs:
        score, title_hit, summary_hit, text_hit, tag_hit = _doc_score_and_flags(
            doc, terms, excludes, phrases
        )
        if score <= 0 and not (title_hit or summary_hit or text_hit or tag_hit):
            continue

        if doc.target_type == "outline":
            outline = outlines.get(doc.target_id)
            if outline is None:
                continue
            search_type = _map_to_search_type(doc, outline)
            if search_type not in type_set and "outline" not in type_set and "chapter" not in type_set:
                continue
            if outline_type and outline.outline_type != outline_type:
                continue
            match_pos = _match_position(title_hit, summary_hit, text_hit, tag_hit)
            item = _build_item(doc, outline, terms, phrases, score, match_pos)
            items.append(item)
        elif doc.target_type == "chapter_content":
            outline = outlines.get(doc.target_id)
            if outline is None:
                continue
            if "content" not in type_set:
                continue
            if outline_type and outline.outline_type != outline_type:
                continue
            match_pos = _match_position(title_hit, summary_hit, text_hit, tag_hit)
            item = _build_item(doc, outline, terms, phrases, score, match_pos)
            items.append(item)
        elif doc.target_type == "tag":
            if "tag" not in type_set:
                continue
            match_pos = "title" if title_hit else "content"
            items.append(_build_item(doc, None, terms, phrases, score, match_pos))
        elif doc.target_type in ("character", "world_setting"):
            search_type = _TARGET_TYPE_MAP[doc.target_type]
            if search_type not in type_set:
                continue
            match_pos = "title" if title_hit else "content"
            items.append(_build_item(doc, None, terms, phrases, score, match_pos))
        elif doc.target_type == "project":
            if "project" not in type_set:
                continue
            match_pos = "title" if title_hit else "summary" if summary_hit else "content"
            items.append(_build_item(doc, None, terms, phrases, score, match_pos))
    return items


async def search_global(
    db: AsyncSession,
    user_id: uuid.UUID,
    q: str,
    target_type: str | None = None,
    tag_ids: list[uuid.UUID] | None = None,
    match_mode: str = "AND",
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "relevance",
) -> tuple[list[dict], int]:
    """全局搜索：用户有权访问的多个作品（本人私有项目 + 已公开作品）。

    权限规则：私有项目仅本人可搜其全部非删除内容；公开作品仅返回公开章节/作品/人物名称等
    公开可见内容，不泄露大纲草稿/撤回章节/私有正文。
    """
    terms, excludes, phrases = parse_query(q)
    if not terms and not phrases:
        return [], 0

    cache_k = cache_key(
        "global-search", user_id, q, target_type,
        ",".join(str(t) for t in (tag_ids or [])), match_mode, status, page, page_size
    )
    cached = _cache_get(cache_k)
    if cached is not None:
        return cached

    # 有权限项目：本人拥有（active）或 公开（publication_status=published 且 status=active）
    owned_stmt = select(Project).where(
        Project.user_id == user_id,
        Project.status == "active",
    )
    owned = {p.id: p for p in (await db.execute(owned_stmt)).scalars().all()}
    public_stmt = select(Project).where(
        Project.publication_status == "published",
        Project.status == "active",
    )
    public = {p.id: p for p in (await db.execute(public_stmt)).scalars().all()}
    # 合并：本人公开项目在 owned 中优先按私有权限处理（可看到全部）
    public_only = {pid: p for pid, p in public.items() if pid not in owned}

    items: list[dict] = []
    kw = f"%{' '.join(terms)}%"

    # 1) 索引文档
    allowed_owned = list(owned.keys())
    allowed_public = list(public_only.keys())
    if allowed_owned or allowed_public:
        stmt = select(SearchDocument).where(
            SearchDocument.deleted_at.is_(None),
            or_(
                SearchDocument.title.ilike(kw),
                SearchDocument.plain_text.ilike(kw),
                SearchDocument.summary.ilike(kw),
                SearchDocument.tags_text.ilike(kw),
            ),
        )
        if allowed_owned and allowed_public:
            # 私有项目：全部类型；公开项目：仅公开可见类型（chapter_content 已发布 public / project / character / tag）
            from sqlalchemy import and_
            stmt = stmt.where(or_(
                and_(SearchDocument.project_id.in_(allowed_owned)),
                and_(
                    SearchDocument.project_id.in_(allowed_public),
                    or_(
                        SearchDocument.target_type.in_(["project", "character", "tag"]),
                        and_(
                            SearchDocument.target_type == "chapter_content",
                            SearchDocument.status == "published",
                            SearchDocument.visibility.in_(["public", "link_only"]),
                        ),
                    ),
                ),
            ))
        elif allowed_owned:
            stmt = stmt.where(SearchDocument.project_id.in_(allowed_owned))
        else:
            stmt = stmt.where(
                SearchDocument.project_id.in_(allowed_public),
                or_(
                    SearchDocument.target_type.in_(["project", "character", "tag"]),
                    SearchDocument.target_type == "chapter_content",
                ),
            )
            stmt = stmt.where(
                or_(
                    SearchDocument.target_type.in_(["project", "character", "tag"]),
                    SearchDocument.status == "published",
                )
            )
        if target_type:
            mapped_doc = {
                "project": "project", "chapter": "chapter_content",
                "character": "character", "tag": "tag",
            }.get(target_type)
            if mapped_doc:
                stmt = stmt.where(SearchDocument.target_type == mapped_doc)
            elif target_type in ("outline", "world_setting", "location", "faction", "timeline_event", "version"):
                # 这些类型不在全局公开范围，但私有项目可搜
                mapped_doc2 = {
                    "outline": "outline", "world_setting": "world_setting",
                }.get(target_type)
                if mapped_doc2:
                    stmt = stmt.where(SearchDocument.target_type == mapped_doc2)
                else:
                    stmt = stmt.where(SearchDocument.target_type == "__none__")
        if status:
            stmt = stmt.where(SearchDocument.status == status)
        docs = (await db.execute(stmt)).scalars().all()
        items.extend(await _build_global_doc_items(db, docs, terms, phrases, excludes))

    # 2) 作品名称/简介
    proj_stmt = select(Project).where(
        or_(Project.id.in_(list(owned.keys())), Project.id.in_(list(public.keys()))),
        Project.status == "active",
        or_(
            Project.title.ilike(kw),
            Project.description.ilike(kw),
            Project.background.ilike(kw),
        ),
    )
    projs = (await db.execute(proj_stmt)).scalars().all()
    for proj in projs:
        title_hit = proj.title and " ".join(terms).lower() in proj.title.lower()
        bg = proj.description or proj.background or ""
        items.append({
            "target_type": "project",
            "target_id": proj.id,
            "title": proj.title or "",
            "highlight": make_highlight(proj.title if title_hit else bg, terms, phrases),
            "snippet": make_snippet(bg, " ".join(terms)),
            "score": _score(title_hit and " ".join(terms).lower() == (proj.title or "").lower(), title_hit, False, False, 1 if not title_hit else 0, 1 if not title_hit else 0, 0),
            "updated_at": proj.updated_at,
            "status": proj.status,
            "visibility": "public" if proj.publication_status == "published" else "private",
            "volume_title": None,
            "match_position": "title" if title_hit else "summary",
            "type": "project",
            "id": proj.id,
            "outline_type": None,
            "parent_id": None,
        })

    if sort == "updated":
        items.sort(key=lambda it: it.get("updated_at") or datetime.min, reverse=True)
    else:
        items.sort(key=lambda it: it["score"], reverse=True)
    # 去重（同 target 同 type 只保留一条）
    seen: set[tuple[str, uuid.UUID]] = set()
    unique_items = []
    for it in items:
        key = (it["target_type"], it["target_id"])
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(it)
    total = len(unique_items)
    start = (page - 1) * page_size
    result = (unique_items[start:start + page_size], total)
    _cache_set(cache_k, result)
    return result


async def _build_global_doc_items(
    db: AsyncSession, docs: list[SearchDocument], terms, phrases, excludes,
) -> list[dict]:
    if not docs:
        return []
    outline_ids = {
        d.target_id for d in docs if d.target_type in ("outline", "chapter_content")
    }
    outlines = await _load_outlines(db, outline_ids)
    items = []
    for doc in docs:
        score, title_hit, summary_hit, text_hit, tag_hit = _doc_score_and_flags(
            doc, terms, excludes, phrases
        )
        if score <= 0 and not (title_hit or summary_hit or text_hit or tag_hit):
            continue
        outline = outlines.get(doc.target_id)
        match_pos = _match_position(title_hit, summary_hit, text_hit, tag_hit)
        items.append(_build_item(doc, outline, terms, phrases, score, match_pos))
    return items


# ============ 搜索历史 ============

async def record_search_history(
    db: AsyncSession, user_id: uuid.UUID, query: str,
    project_id: uuid.UUID | None = None,
) -> SearchHistory | None:
    """记录最近搜索（空查询不记录；同词同范围去重更新）。"""
    q = (query or "").strip()
    if not q or len(q) > 200:
        return None
    existing = (await db.execute(
        select(SearchHistory).where(
            SearchHistory.user_id == user_id,
            SearchHistory.project_id == project_id,
            SearchHistory.query == q,
        )
    )).scalars().all()
    for e in existing:
        await db.delete(e)
    hist = SearchHistory(user_id=user_id, project_id=project_id, query=q)
    db.add(hist)
    await db.flush()
    return hist


async def list_search_history(
    db: AsyncSession, user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[SearchHistory]:
    stmt = select(SearchHistory).where(SearchHistory.user_id == user_id)
    if project_id is not None:
        stmt = stmt.where(SearchHistory.project_id == project_id)
    stmt = stmt.order_by(SearchHistory.created_at.desc()).limit(min(limit, 100))
    return list((await db.execute(stmt)).scalars().all())


async def delete_search_history(
    db: AsyncSession, user_id: uuid.UUID, history_id: uuid.UUID
) -> bool:
    hist = await db.get(SearchHistory, history_id)
    if hist is None or hist.user_id != user_id:
        return False
    await db.delete(hist)
    await db.flush()
    return True


async def clear_search_history(
    db: AsyncSession, user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> int:
    stmt = select(SearchHistory).where(SearchHistory.user_id == user_id)
    if project_id is not None:
        stmt = stmt.where(SearchHistory.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    n = len(rows)
    for r in rows:
        await db.delete(r)
    await db.flush()
    return n


# ============ 搜索建议 ============

async def suggestions(
    db: AsyncSession, user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    q: str, limit: int = 10,
) -> list[str]:
    """轻量搜索建议：最近搜索词 / 高频标签 / 章节标题 / 人物名称 / 地点名称。

    输入为空或过短时仍返回最近搜索，不做重量级全文搜索。
    """
    q = (q or "").strip()
    cache_k = cache_key("suggest", user_id, project_id, q, limit)
    cached = _cache_get(cache_k)
    if cached is not None:
        return cached

    result: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        t = (text or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)

    kw = f"%{q}%"
    # 1) 最近搜索词
    hist_stmt = select(SearchHistory).where(SearchHistory.user_id == user_id)
    if project_id is not None:
        hist_stmt = hist_stmt.where(SearchHistory.project_id == project_id)
    if q:
        hist_stmt = hist_stmt.where(SearchHistory.query.ilike(kw))
    hist_stmt = hist_stmt.order_by(SearchHistory.created_at.desc()).limit(limit)
    for h in (await db.execute(hist_stmt)).scalars().all():
        add(h.query)

    # 2) 高频标签（项目内）
    if project_id is not None:
        tag_stmt = (
            select(Tag).where(Tag.project_id == project_id)
        )
        if q:
            tag_stmt = tag_stmt.where(Tag.name.ilike(kw))
        tag_stmt = tag_stmt.order_by(Tag.usage_count.desc()).limit(limit)
        for t in (await db.execute(tag_stmt)).scalars().all():
            add(t.name)

    # 3) 章节标题（项目内）
    if project_id is not None and q:
        out_stmt = (
            select(Outline.title)
            .where(
                Outline.project_id == project_id,
                Outline.outline_type.in_(["chapter", "scene"]),
                Outline.title.ilike(kw),
            )
            .limit(limit)
        )
        for title in (await db.execute(out_stmt)).scalars().all():
            add(title)

    # 4) 人物名称（项目内）
    if project_id is not None and q:
        from app.models.character import Character
        char_stmt = (
            select(Character.name)
            .where(Character.project_id == project_id, Character.name.ilike(kw))
            .limit(limit)
        )
        for name in (await db.execute(char_stmt)).scalars().all():
            add(name)

    # 5) 地点/势力名称：无独立模型，用人物/设定兜底；公开作品则从索引文档标题取
    if len(result) < limit and q:
        extra_stmt = (
            select(SearchDocument.title)
            .where(
                SearchDocument.project_id == project_id if project_id else SearchDocument.project_id.is_not(None),
                SearchDocument.title.ilike(kw),
                SearchDocument.deleted_at.is_(None),
                SearchDocument.target_type.in_(["character", "world_setting", "tag"]),
            )
            .limit(limit)
        )
        for title in (await db.execute(extra_stmt)).scalars().all():
            add(title)

    result = result[:limit]
    _cache_set(cache_k, result)
    return result
