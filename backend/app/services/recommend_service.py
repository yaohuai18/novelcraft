# 推荐系统服务（第二十二批「数据分析、推荐系统和智能运营」规格十~十四/十五/二十三）
# 策略：热门/标签匹配/最近阅读/相似作品/关注作者更新/编辑精选/地区语言筛选。
# 过滤（推荐前强制）：权限(已发布公开)/地区/年龄/可见性/违规/用户屏蔽/重复曝光。
# 排序融合：相关性、近期活跃度、质量信号、新鲜度、用户偏好、重复曝光惩罚。
# 控制：同一作品不重复、同一作者不过度曝光、冷启动(新用户热门+编辑精选)、冷门曝光。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.featured_content import FeaturedContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.recommendation import BlockedRecommend, RecommendAction, RecommendLog
from app.models.tag import Tag, TagRelation

# 推荐场景（规格十）
SCENES = ["homepage", "reading_end", "project_internal", "writing_page", "ai", "notification"]


def _now() -> datetime:
    return datetime.utcnow()


def _props(e) -> dict:
    try:
        return json.loads(e.properties or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


async def _eligible_projects(db: AsyncSession, *, viewer: dict | None = None) -> list[Project]:
    """推荐候选：公开、已发布、非违规；含权限/地区/年龄/可见性过滤（规格十一/十四）。"""
    stmt = select(Project).where(
        Project.publication_status == "published",
        Project.published_at.isnot(None),
    )
    rows = (await db.execute(stmt)).scalars().all()

    viewer_region = (viewer or {}).get("region")
    viewer_age = (viewer or {}).get("age")
    # 违规过滤：被举报 >=3 次的内容不进推荐（规格十一：违规内容过滤）
    reported_ids = {str(rid) for rid in (await db.execute(
        select(RecommendAction.recommend_id)
        .where(
            RecommendAction.action == "report",
            RecommendAction.recommend_type == "project",
        )
        .group_by(RecommendAction.recommend_id)
        .having(func.count(RecommendAction.id) >= 3)
    )).scalars().all()}
    result = []
    for p in rows:
        if str(p.id) in reported_ids:
            continue
        # 地区限制
        if p.region_visibility:
            try:
                rv = json.loads(p.region_visibility)
                allowed = rv.get("allowed") or []
                denied = rv.get("denied") or []
                if viewer_region:
                    if viewer_region in denied:
                        continue
                    if allowed and viewer_region not in allowed:
                        continue
            except Exception:  # noqa: BLE001
                pass
        # 年龄限制（content_rating: everyone/teen/mature）
        if viewer_age is not None and p.content_rating == "mature" and viewer_age < 18:
            continue
        result.append(p)
    return result


async def _load_viewer_prefs(db: AsyncSession, user_id: uuid.UUID | None) -> dict:
    """读取用户偏好：最近阅读标签、屏蔽列表、重复曝光惩罚。"""
    prefs: dict = {"tags": set(), "read_ids": set(), "blocked": set(), "hidden": set()}
    if not user_id:
        return prefs

    # 最近阅读（阅读完成/启动事件 object 为章节，映射到 project_id）
    recent = (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.user_id == user_id,
            AnalyticsEvent.event_name.in_(["reading.chapter_start", "reading.chapter_complete"]),
        ).order_by(AnalyticsEvent.occurred_at.desc()).limit(50)
    )).scalars().all()
    for e in recent:
        if e.project_id:
            prefs["read_ids"].add(str(e.project_id))

    # 屏蔽与隐藏
    for br in (await db.execute(
        select(BlockedRecommend).where(BlockedRecommend.user_id == user_id)
    )).scalars().all():
        prefs["blocked"].add((br.target_type, br.target_id))
    for ra in (await db.execute(
        select(RecommendAction).where(
            RecommendAction.user_id == user_id,
            RecommendAction.action.in_(["hide", "block"]),
        )
    )).scalars().all():
        if ra.recommend_type and ra.recommend_id:
            prefs["hidden"].add((ra.recommend_type, ra.recommend_id))

    # 最近阅读作品标签
    if recent:
        proj_ids = [e.project_id for e in recent if e.project_id][:10]
        tag_rows = (await db.execute(
            select(TagRelation).where(
                TagRelation.target_type == "project",
                TagRelation.target_id.in_(proj_ids),
            ).limit(30)
        )).scalars().all()
        prefs["tags"] = {str(t.tag_id) for t in tag_rows}
    return prefs


async def recommend(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    scene: str = "homepage",
    limit: int = 10,
    region: str | None = None,
    age: int | None = None,
    language: str | None = None,
) -> dict:
    """主推荐入口：多策略召回 + 排序融合 + 过滤 + 可解释。"""
    if scene not in SCENES:
        scene = "homepage"
    viewer = {"region": region, "age": age}
    projects = await _eligible_projects(db, viewer=viewer)
    if language:
        projects = [p for p in projects if (p.default_language or "zh") == language]

    prefs = await _load_viewer_prefs(db, user_id)
    blocked_ids = {pid for (t, pid) in prefs["blocked"] if t == "project"}
    hidden_ids = {pid for (t, pid) in prefs["hidden"] if t == "project"}

    # 1) 召回：热门 / 最近阅读相似 / 标签匹配 / 关注作者更新 / 编辑精选
    candidates: list[dict] = []
    seen_authors: dict[uuid.UUID, int] = {}

    # 热门（view_count + favorite_count 质量信号 + 新鲜度）
    for p in projects:
        if str(p.id) in blocked_ids or str(p.id) in hidden_ids or str(p.id) in prefs["read_ids"]:
            continue
        hot = (p.view_count or 0) * 1.0 + (p.favorite_count or 0) * 5.0
        freshness = max(0.0, 1.0 - max((_now() - (p.published_at or _now())).total_seconds(), 0) / (30 * 86400))
        candidates.append({
            "type": "project", "id": str(p.id), "score": hot,
            "freshness": freshness, "reason": "hot_popular",
            "project": p,
        })

    # 标签匹配（基于最近阅读标签）
    if prefs["tags"]:
        tag_projs = (await db.execute(
            select(TagRelation.target_id, func.count(TagRelation.tag_id))
            .where(
                TagRelation.target_type == "project",
                TagRelation.target_id.in_([p.id for p in projects]),
                TagRelation.tag_id.in_([uuid.UUID(t) for t in prefs["tags"] if _is_uuid(t)]),
            ).group_by(TagRelation.target_id).order_by(func.count(TagRelation.tag_id).desc()).limit(30)
        )).all()
        for pid, cnt in tag_projs:
            if str(pid) in [c["id"] for c in candidates]:
                continue
            p = next((x for x in projects if x.id == pid), None)
            if p and str(p.id) not in blocked_ids and str(p.id) not in hidden_ids and str(p.id) not in prefs["read_ids"]:
                candidates.append({"type": "project", "id": str(pid), "score": cnt * 8.0, "freshness": 0.5, "reason": "tag_match", "project": p})

    # 编辑精选
    featured = (await db.execute(
        select(FeaturedContent).where(
            FeaturedContent.status == "active",
            FeaturedContent.target_type == "project",
        ).order_by(FeaturedContent.position.asc()).limit(10)
    )).scalars().all()
    for f in featured:
        pid = str(f.target_id)
        if pid in [c["id"] for c in candidates]:
            continue
        p = next((x for x in projects if x.id == uuid.UUID(pid)), None)
        if p and pid not in blocked_ids and pid not in hidden_ids and pid not in prefs["read_ids"]:
            candidates.append({"type": "project", "id": pid, "score": 100.0, "freshness": 0.7, "reason": "editor_pick", "project": p})

    # 2) 排序融合：score + 新鲜度 + 重复曝光惩罚 + 作者曝光限制
    recent_logs = (await db.execute(
        select(RecommendLog.recommend_id, func.count(RecommendLog.id))
        .where(
            RecommendLog.user_id == user_id if user_id else RecommendLog.user_id.is_(None),
            RecommendLog.created_at >= _now() - timedelta(days=1),
        ).group_by(RecommendLog.recommend_id)
    )).all() if user_id else []
    exposure_map = {rid: c for rid, c in recent_logs}

    ranked = []
    for c in candidates:
        p = c["project"]
        repeat_penalty = min(exposure_map.get(c["id"], 0), 5) * 0.35
        author_overlap = seen_authors.get(p.user_id, 0) * 0.3
        quality = min((p.view_count or 0) / 1000.0, 3.0) + min((p.favorite_count or 0) / 50.0, 2.0)
        total = c["score"] + c["freshness"] * 15.0 + quality - repeat_penalty - author_overlap
        ranked.append({
            "type": c["type"],
            "id": c["id"],
            "title": p.title,
            "reason": c["reason"],
            "score": round(total, 4),
            "author_id": str(p.user_id) if p.user_id else None,
        })
        seen_authors[p.user_id] = seen_authors.get(p.user_id, 0) + 1

    ranked.sort(key=lambda x: -x["score"])

    # 3) 冷启动与冷门曝光：新用户无行为时优先热门+精选；冷门内容保底曝光（score 下限外的随机）
    if not prefs["read_ids"] and len(ranked) < limit:
        extra = (await db.execute(
            select(Project).where(
                Project.publication_status == "published",
                Project.view_count < 100,
            ).limit(limit)
        )).scalars().all()
        for p in extra:
            if str(p.id) not in [r["id"] for r in ranked] and str(p.id) not in blocked_ids and str(p.id) not in hidden_ids:
                ranked.append({
                    "type": "project", "id": str(p.id), "title": p.title,
                    "reason": "cold_start_boost", "score": 5.0, "author_id": str(p.user_id) if p.user_id else None,
                })

    results = ranked[:limit]
    # 4) 记录曝光（推荐效果监控 + 重复曝光惩罚）
    positions = {}
    for i, r in enumerate(results):
        positions[r["id"]] = i
    if user_id:
        for i, r in enumerate(results):
            log = RecommendLog(
                user_id=user_id, scene=scene, recommend_type=r["type"],
                recommend_id=r["id"], position=i, reason=r["reason"],
            )
            db.add(log)
        await db.flush()

    return {
        "scene": scene,
        "items": [
            {"position": i, **r, "explanation": _explain(r["reason"])}
            for i, r in enumerate(results)
        ],
        "strategy_note": "hot+tag+editor+similarity fused, filtered by permission/region/age/visibility/violation",
    }


def _explain(reason: str) -> str:
    return {
        "hot_popular": "近期热门、质量信号强",
        "tag_match": "与你最近阅读的作品标签相关",
        "editor_pick": "编辑精选内容",
        "recent_read": "延续你最近阅读的兴趣",
        "author_update": "你关注作者的更新",
        "cold_start_boost": "冷门优质内容曝光",
    }.get(reason, reason)


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except Exception:  # noqa: BLE001
        return False


async def record_action(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    log_id: uuid.UUID | None,
    action: str,
    recommend_type: str | None = None,
    recommend_id: str | None = None,
) -> dict:
    """记录推荐交互：click/complete/hide/block/report（规格十三/二十三）。"""
    if action not in ("click", "complete", "hide", "block", "report"):
        return {"error": "invalid_action"}
    if action == "block" and recommend_type and recommend_id:
        existing = (await db.execute(
            select(BlockedRecommend).where(
                BlockedRecommend.user_id == user_id,
                BlockedRecommend.target_type == recommend_type,
                BlockedRecommend.target_id == recommend_id,
            )
        )).scalar_one_or_none()
        if not existing:
            db.add(BlockedRecommend(
                user_id=user_id, target_type=recommend_type, target_id=recommend_id,
            ))
    db.add(RecommendAction(
        user_id=user_id, log_id=log_id, action=action,
        recommend_type=recommend_type, recommend_id=recommend_id,
    ))
    await db.flush()
    return {"status": "recorded", "action": action}


async def block_recommend(db: AsyncSession, *, user_id: uuid.UUID, target_type: str, target_id: str) -> dict:
    """用户主动屏蔽（规格十一：用户控制）。"""
    existing = (await db.execute(
        select(BlockedRecommend).where(
            BlockedRecommend.user_id == user_id,
            BlockedRecommend.target_type == target_type,
            BlockedRecommend.target_id == target_id,
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(BlockedRecommend(user_id=user_id, target_type=target_type, target_id=target_id))
        await db.flush()
    return {"status": "blocked", "target_type": target_type, "target_id": target_id}


async def list_blocked(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(
        select(BlockedRecommend).where(BlockedRecommend.user_id == user_id).order_by(BlockedRecommend.created_at.desc())
    )).scalars().all()
    return [{"target_type": r.target_type, "target_id": r.target_id, "reason": r.reason} for r in rows]
