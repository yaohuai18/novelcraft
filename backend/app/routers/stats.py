# 项目统计路由（第十七批「统计分析与运营后台」规格二十一）
# GET /api/projects/{project_id}/stats/{overview|writing|chapters|readers|ai-usage|export}
# 附加：写作目标管理、每日快照上报、阅读事件上报、AI 用量上报。
# 私有内容访问：仅项目作者 / 成员 / 已授权临时访问者 / 后台角色可读（规格十二）。
import csv
import io
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import ADMIN_ROLES, hash_ip
from app.models.collaboration import ProjectMember
from app.models.outline import Outline
from app.models.private_access_grant import PrivateAccessGrant
from app.models.project import Project
from app.models.user import User
from app.models.writing_goal import WritingGoal
from app.schemas.stats_schemas import (
    AIUsageRecordCreate,
    ReadingEventCreate,
    WritingDailyStatCreate,
    WritingGoalCreate,
    WritingGoalUpdate,
)
from app.services import (
    admin_service,
    ai_stats_service,
    content_stats_service,
    writing_stats_service,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/stats",
    tags=["项目统计"]
)


async def _load_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    return project


async def _ensure_project_access(
    db: AsyncSession,
    project: Project,
    current_user: User,
    *,
    record_view: bool = False,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> None:
    """校验项目访问权：作者 / 成员 / 后台角色 / 临时授权。"""
    if project.user_id == current_user.id:
        return
    if current_user.role in ADMIN_ROLES:
        return
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == current_user.id,
                ProjectMember.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is not None:
        return
    grant = await admin_service.check_private_access(
        db, user_id=current_user.id, project_id=project.id
    )
    if grant is not None:
        if record_view:
            from app.services.audit_service import record as record_audit
            await record_audit(
                db,
                actor_id=current_user.id,
                action="private_access.view",
                target_type="project",
                target_id=str(project.id),
                reason=f"经临时授权查看受保护内容（授权ID: {grant.id}）",
                ip_hash=ip_hash,
                user_agent=user_agent,
            )
        return
    raise HTTPException(status_code=403, detail="无权访问该项目统计")


def _parse_date(value: str | None, name: str):
    if value is None:
        return None
    try:
        from datetime import date
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} 日期格式非法")


def _parse_dt(value: str | None, name: str):
    if value is None:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} 时间格式非法")


# ---------- 创作统计 ----------

@router.get("/overview")
async def project_stats_overview(
    project_id: uuid.UUID,
    period: str = Query(default="30d", pattern="^(today|7d|30d|year|custom)$"),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创作统计概览（规格二）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        result = await writing_stats_service.get_writing_stats(
            db, project,
            period=period,
            from_date=_parse_date(from_date, "from"),
            to_date=_parse_date(to_date, "to"),
        )
    except writing_stats_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/writing")
async def project_stats_writing(
    project_id: uuid.UUID,
    period: str = Query(default="30d", pattern="^(today|7d|30d|year|custom)$"),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创作统计明细（含每日序列与写作目标进度，规格二/三/四）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        return await writing_stats_service.get_writing_stats(
            db, project,
            period=period,
            from_date=_parse_date(from_date, "from"),
            to_date=_parse_date(to_date, "to"),
        )
    except writing_stats_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------- 内容统计 ----------

@router.get("/chapters")
async def project_stats_chapters(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None = Query(default=None),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """章节表现统计（规格五）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        return await content_stats_service.get_chapter_stats(
            db, project_id,
            chapter_id=chapter_id,
            from_date=_parse_date(from_date, "from"),
            to_date=_parse_date(to_date, "to"),
        )
    except content_stats_service.ReadingEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/readers")
async def project_stats_readers(
    project_id: uuid.UUID,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """读者与阅读漏斗统计（规格六/七）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    frm = _parse_date(from_date, "from")
    to = _parse_date(to_date, "to")
    summary = await content_stats_service.get_project_content_stats(
        db, project, from_date=frm, to_date=to
    )
    funnel = await content_stats_service.get_reading_funnel(
        db, project,
        from_dt=_parse_dt(from_date + "T00:00:00Z", "from") if from_date else None,
        to_dt=_parse_dt(to_date + "T23:59:59Z", "to") if to_date else None,
    )
    return {
        "project_id": str(project.id),
        "summary": summary,
        "funnel": funnel,
        "updated_at": summary["updated_at"],
        "is_realtime": True,
        "is_estimated": False,
    }


# ---------- AI 使用统计 ----------

@router.get("/ai-usage")
async def project_stats_ai_usage(
    project_id: uuid.UUID,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    task_type: str | None = Query(default=None),
    group_by: str = Query(default="day", pattern="^(day|model|task_type|provider)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI 使用量、成本与失败统计（规格十/十一）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    frm = _parse_date(from_date, "from")
    to = _parse_date(to_date, "to")
    try:
        usage = await ai_stats_service.get_ai_usage(
            db, project_id=project.id,
            from_date=frm, to_date=to, task_type=task_type, group_by=group_by,
        )
        failures = await ai_stats_service.get_failure_stats(
            db, project_id=project.id, from_date=frm, to_date=to
        )
    except ai_stats_service.AIStatsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**usage, "failures": failures}


# ---------- 项目统计导出 ----------

@router.get("/export")
async def project_stats_export(
    project_id: uuid.UUID,
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出项目统计（规格十八：限时间范围/限数据量/记录导出操作）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)

    from app.services.audit_service import record as record_audit
    await record_audit(
        db,
        actor_id=current_user.id,
        action="stats.export",
        target_type="project",
        target_id=str(project.id),
        reason="导出项目统计报表",
        ip_hash=hash_ip("127.0.0.1"),
    )
    await db.commit()

    frm = _parse_date(from_date, "from")
    to = _parse_date(to_date, "to")
    writing = await writing_stats_service.get_writing_stats(
        db, project, period="custom", from_date=frm, to_date=to
    )
    chapters = await content_stats_service.get_chapter_stats(
        db, project.id, from_date=frm, to_date=to
    )
    if fmt == "json":
        return {
            "writing": writing,
            "chapters": chapters,
            "updated_at": writing["updated_at"],
            "is_realtime": True,
            "is_estimated": False,
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["指标", "数值"])
    for k, v in writing.items():
        if isinstance(v, (dict, list)):
            continue
        writer.writerow([k, v])
    writer.writerow([])
    writer.writerow(["chapter_id", "title", "word_count", "view_count", "favorite_count",
                     "comment_count", "completion_rate", "average_read_seconds"])
    for c in chapters["chapter_stats"]:
        writer.writerow([c["chapter_id"], c["title"], c["word_count"], c["view_count"],
                         c["favorite_count"], c["comment_count"], c["completion_rate"],
                         c["average_read_seconds"]])
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=project_stats_{project.id}.csv"},
    )


# ---------- 写作目标 ----------

@router.get("/goals")
async def list_goals(
    project_id: uuid.UUID,
    goal_type: str | None = Query(default=None),
    goal_status: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出项目写作目标（规格三）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    goals = await writing_stats_service.list_goals(
        db, project.id, goal_type=goal_type, status=goal_status
    )
    return {
        "goals": [
            {
                "id": str(g.id),
                "project_id": str(g.project_id),
                "goal_type": g.goal_type,
                "target_value": g.target_value,
                "current_value": g.current_value,
                "start_date": g.start_date.isoformat() if g.start_date else None,
                "end_date": g.end_date.isoformat() if g.end_date else None,
                "status": g.status,
                "reminders": writing_stats_service.goal_reminders(g),
                "created_at": g.created_at.isoformat() + "Z",
                "updated_at": g.updated_at.isoformat() + "Z",
            }
            for g in goals
        ]
    }


@router.post("/goals", status_code=status.HTTP_201_CREATED)
async def create_goal(
    project_id: uuid.UUID,
    data: WritingGoalCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建写作目标。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        goal = await writing_stats_service.create_goal(
            db, current_user, project.id,
            goal_type=data.goal_type,
            target_value=data.target_value,
            start_date=data.start_date,
            end_date=data.end_date,
        )
    except writing_stats_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(goal)
    return {
        "id": str(goal.id),
        "goal_type": goal.goal_type,
        "target_value": goal.target_value,
        "current_value": goal.current_value,
        "status": goal.status,
        "reminders": writing_stats_service.goal_reminders(goal),
    }


@router.put("/goals/{goal_id}")
async def update_goal(
    project_id: uuid.UUID,
    goal_id: uuid.UUID,
    data: WritingGoalUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新写作目标（进度/目标值/状态）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    goal = (
        await db.execute(
            select(WritingGoal).where(
                WritingGoal.id == goal_id,
                WritingGoal.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if goal is None:
        raise HTTPException(status_code=404, detail="目标不存在")
    try:
        goal = await writing_stats_service.update_goal(
            db, goal,
            target_value=data.target_value,
            current_value=data.current_value,
            start_date=data.start_date,
            end_date=data.end_date,
            status=data.status,
        )
    except writing_stats_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(goal)
    return {
        "id": str(goal.id),
        "goal_type": goal.goal_type,
        "target_value": goal.target_value,
        "current_value": goal.current_value,
        "status": goal.status,
        "reminders": writing_stats_service.goal_reminders(goal),
    }


# ---------- 数据上报 ----------

@router.post("/writing-daily", status_code=status.HTTP_201_CREATED)
async def upsert_writing_daily(
    project_id: uuid.UUID,
    data: WritingDailyStatCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """保存每日写作快照（规格四：不每次扫描全文）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    stat = await writing_stats_service.upsert_daily_stat(
        db, current_user, project.id,
        stat_date=data.stat_date,
        word_count=data.word_count,
        new_word_count=data.new_word_count,
        edited_word_count=data.edited_word_count,
        deleted_word_count=data.deleted_word_count,
        chapter_count=data.chapter_count,
        active_minutes=data.active_minutes,
    )
    await db.commit()
    await db.refresh(stat)
    return {
        "id": str(stat.id),
        "stat_date": stat.stat_date.isoformat(),
        "word_count": stat.word_count,
        "new_word_count": stat.new_word_count,
        "edited_word_count": stat.edited_word_count,
        "deleted_word_count": stat.deleted_word_count,
        "chapter_count": stat.chapter_count,
        "active_minutes": stat.active_minutes,
    }


@router.post("/reading-events", status_code=status.HTTP_201_CREATED)
async def record_reading_event(
    project_id: uuid.UUID,
    data: ReadingEventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """记录阅读事件（规格八：防重复/防机器人/未公开章节不入公开统计）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        event = await content_stats_service.record_event(
            db,
            project_id=project.id,
            event_type=data.event_type,
            chapter_id=data.chapter_id,
            # 身份一律取当前登录用户：不接受请求体伪造他人阅读记录
            user_id=current_user.id,
            anonymous_id=data.anonymous_id,
            session_id=data.session_id or f"anon:{current_user.id}",
            occurred_at=data.occurred_at,
            metadata=data.metadata,
        )
    except content_stats_service.ReadingEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    if event is None:
        return {"recorded": False, "reason": "重复事件或已存在相同去重记录"}
    return {"recorded": True, "event_id": str(event.id)}


@router.post("/ai-usage", status_code=status.HTTP_201_CREATED)
async def record_ai_usage(
    project_id: uuid.UUID,
    data: AIUsageRecordCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """记录一次 AI 调用用量（规格十/十一，服务端聚合）。"""
    project = await _load_project(db, project_id)
    await _ensure_project_access(db, project, current_user)
    try:
        stat = await ai_stats_service.record_usage(
            db,
            # 身份一律取当前登录用户：不接受请求体伪造他人用量/成本
            user_id=current_user.id,
            project_id=project.id,
            provider=data.provider,
            model=data.model,
            task_type=data.task_type,
            success=data.success,
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            estimated_cost=data.estimated_cost,
            failure_reason=data.failure_reason,
            retry_count=data.retry_count,
        )
    except ai_stats_service.AIStatsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(stat)
    return {"recorded": True, "stat_id": str(stat.id)}
