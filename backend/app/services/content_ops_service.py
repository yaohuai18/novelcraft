# 内容运营服务（第十七批「统计分析与运营后台」规格十四）
# 后台查看：公开作品列表/最新发布/热门/异常增长/被举报/审核中/被隐藏。
# 运营操作：隐藏/恢复/下架章节/处理举报/官方标签/推荐位/限制账号 —— 全部记录原因 + 审计。
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outline import Outline
from app.models.project import Project
from app.models.publication import Publication
from app.models.report import REPORT_STATUSES, Report
from app.models.user import User
from app.services.audit_service import record as record_audit


class ContentOpsError(ValueError):
    pass


def _project_brief(p: Project) -> dict:
    return {
        "id": str(p.id),
        "user_id": str(p.user_id),
        "title": p.title,
        "publication_status": p.publication_status,
        "status": p.status,
        "official_tag": p.official_tag,
        "view_count": p.view_count,
        "favorite_count": p.favorite_count,
        "word_count": p.word_count,
        "chapter_count": p.chapter_count,
        "created_at": p.created_at.isoformat() + "Z",
        "published_at": p.published_at.isoformat() + "Z" if p.published_at else None,
        "updated_at": p.updated_at.isoformat() + "Z",
    }


# ---------- 列表 ----------

async def list_public_projects(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    filters = [Project.publication_status == "published", Project.status != "hidden"]
    total = (await db.execute(select(func.count(Project.id)).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(Project).where(*filters)
            .order_by(Project.updated_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_project_brief(p) for p in rows]}


async def list_latest(
    db: AsyncSession,
    *,
    days: int = 7,
    limit: int = 20,
) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(Project).where(
                Project.publication_status == "published",
                Project.status != "hidden",
                Project.last_published_at >= since,
            ).order_by(Project.last_published_at.desc()).limit(limit)
        )
    ).scalars().all()
    return {"days": days, "items": [_project_brief(p) for p in rows]}


async def list_hot(
    db: AsyncSession,
    *,
    limit: int = 20,
) -> dict:
    rows = (
        await db.execute(
            select(Project).where(
                Project.publication_status == "published",
                Project.status != "hidden",
            ).order_by(Project.view_count.desc(), Project.favorite_count.desc()).limit(limit)
        )
    ).scalars().all()
    return {"items": [_project_brief(p) for p in rows]}


async def list_abnormal_growth(
    db: AsyncSession,
    *,
    recent_days: int = 7,
    limit: int = 20,
) -> dict:
    """异常增长：近 recent_days 天新增阅读量对比前一个同周期，增长率异常高或为负。"""
    now = datetime.utcnow()
    recent_start = now - timedelta(days=recent_days)
    prev_start = now - timedelta(days=recent_days * 2)

    recent_stats = (
        await db.execute(
            select(
                Project.id,
                func.sum(Project.view_count).label("views"),
            ).where(Project.updated_at >= recent_start).group_by(Project.id)
        )
    ).all()
    # 简化：基于 Project.view_count 无法按天切分，改用 ContentDailyStat 聚合
    from app.models.content_daily_stat import ContentDailyStat
    rows = (
        await db.execute(
            select(
                ContentDailyStat.project_id,
                func.sum(ContentDailyStat.view_count),
            ).where(
                ContentDailyStat.stat_date >= prev_start.date(),
                ContentDailyStat.stat_date <= now.date(),
            ).group_by(ContentDailyStat.project_id)
        )
    ).all()
    growth: dict[str, dict] = {}
    for pid, views in rows:
        growth.setdefault(str(pid), {"recent": 0, "previous": 0})
        # 按 stat_date 拆分
    daily = (
        await db.execute(
            select(
                ContentDailyStat.project_id,
                ContentDailyStat.stat_date,
                ContentDailyStat.view_count,
            ).where(
                ContentDailyStat.stat_date >= prev_start.date(),
                ContentDailyStat.stat_date <= now.date(),
            )
        )
    ).all()
    agg: dict[str, dict] = {}
    for pid, d, views in daily:
        entry = agg.setdefault(str(pid), {"recent": 0, "previous": 0})
        if d >= recent_start.date():
            entry["recent"] += views
        else:
            entry["previous"] += views

    abnormal = []
    for pid, entry in agg.items():
        rate = (entry["recent"] - entry["previous"]) / max(entry["previous"], 1)
        if rate >= 1.0 or (entry["previous"] > 0 and entry["recent"] == 0):
            project = (
                await db.execute(select(Project).where(Project.id == uuid.UUID(pid)))
            ).scalar_one_or_none()
            if project is None:
                continue
            abnormal.append({
                **_project_brief(project),
                "recent_views": entry["recent"],
                "previous_views": entry["previous"],
                "growth_rate": round(rate, 4),
            })
    abnormal.sort(key=lambda x: x["growth_rate"], reverse=True)
    return {"items": abnormal[:limit]}


async def list_reported(
    db: AsyncSession,
    *,
    status: str = "pending",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    if status not in REPORT_STATUSES:
        raise ContentOpsError(f"非法状态: {status}")
    filters = [Report.status == status]
    total = (await db.execute(select(func.count(Report.id)).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(Report).where(*filters)
            .order_by(Report.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(r.id),
                "reporter_id": str(r.reporter_id),
                "target_type": r.target_type,
                "target_id": r.target_id,
                "reason": r.reason,
                "status": r.status,
                "handled_by": str(r.handled_by) if r.handled_by else None,
                "handled_at": r.handled_at.isoformat() + "Z" if r.handled_at else None,
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in rows
        ],
    }


async def list_reviewing(db: AsyncSession, *, limit: int = 20) -> dict:
    """审核中的内容：project.review + chapter.status=review。"""
    projects = (
        await db.execute(
            select(Project).where(Project.publication_status == "review")
            .order_by(Project.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    return {"items": [_project_brief(p) for p in projects]}


async def list_hidden(db: AsyncSession, *, limit: int = 20) -> dict:
    rows = (
        await db.execute(
            select(Project).where(Project.status == "hidden")
            .order_by(Project.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    return {"items": [_project_brief(p) for p in rows]}


# ---------- 运营操作 ----------

async def hide_project(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> Project:
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise ContentOpsError("作品不存在")
    project.status = "hidden"
    project.publication_status = "unlisted"
    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.hide",
        target_type="project", target_id=str(project_id),
        reason=reason, ip_hash=ip_hash, user_agent=user_agent,
    )
    return project


async def restore_project(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> Project:
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise ContentOpsError("作品不存在")
    project.status = "active"
    project.publication_status = "published"
    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.restore",
        target_type="project", target_id=str(project_id),
        reason=reason, ip_hash=ip_hash, user_agent=user_agent,
    )
    return project


async def unpublish_chapter(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> Outline:
    """运营下架章节：章节与发布记录同事务置为不可见。

    B3-prep 契约：
        - 资源状态复用 chapter_lifecycle.RESOURCE_STATUS_TAKEN_DOWN，不新增常量；
        - outline 与 Publication 的 status/visibility 两轴同步为 taken_down/private；
        - 保留 published_at（首次发布时间不再被下架清空）；
        - 不改写 lifecycle_status（下架是资源状态，不是生命周期迁移）；
        - 不在此处写索引同步与生命周期审计，避免与既有入口重复；
        - 不提交事务，由调用方（admin 路由）统一收口。
    """
    from app.services.chapter_lifecycle import RESOURCE_STATUS_TAKEN_DOWN

    chapter = (await db.execute(select(Outline).where(Outline.id == chapter_id))).scalar_one_or_none()
    if chapter is None or chapter.outline_type != "chapter":
        raise ContentOpsError("章节不存在")

    publication = (
        await db.execute(select(Publication).where(Publication.chapter_id == chapter.id))
    ).scalar_one_or_none()

    chapter.status = RESOURCE_STATUS_TAKEN_DOWN
    chapter.visibility = "private"
    if publication is not None:
        publication.status = RESOURCE_STATUS_TAKEN_DOWN
        publication.visibility = "private"

    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.unpublish_chapter",
        target_type="chapter", target_id=str(chapter_id),
        reason=reason, ip_hash=ip_hash, user_agent=user_agent,
    )
    return chapter


async def handle_report(
    db: AsyncSession,
    *,
    report_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: str,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> Report:
    if status not in REPORT_STATUSES:
        raise ContentOpsError(f"非法状态: {status}")
    report = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if report is None:
        raise ContentOpsError("举报不存在")
    report.status = status
    report.handled_by = actor_id
    report.handled_at = datetime.utcnow()
    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.handle_report",
        target_type="report", target_id=str(report_id),
        reason=reason, ip_hash=ip_hash, user_agent=user_agent,
    )
    return report


async def add_official_tag(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag: str,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> Project:
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise ContentOpsError("作品不存在")
    project.official_tag = tag.strip() or None
    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.official_tag",
        target_type="project", target_id=str(project_id),
        reason=f"{reason}；标签: {tag}", ip_hash=ip_hash, user_agent=user_agent,
    )
    return project


async def restrict_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: str,
    reason: str,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> User:
    if status not in ("active", "restricted", "disabled"):
        raise ContentOpsError(f"非法账号状态: {status}")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise ContentOpsError("用户不存在")
    user.status = status
    await db.flush()
    await record_audit(
        db, actor_id=actor_id, action="content.restrict_user",
        target_type="user", target_id=str(user_id),
        reason=f"{reason}；状态: {status}", ip_hash=ip_hash, user_agent=user_agent,
    )
    return user
