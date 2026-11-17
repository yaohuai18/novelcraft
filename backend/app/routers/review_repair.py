# -*- coding: utf-8 -*-
"""阶段I「独立 LLM 审稿 + 补丁闭环」路由。

- POST /api/projects/{project_id}/ai/consistency-review
      独立 LLM 连续性审稿（异步可取消，仅产出只读报告，不改正文）
- GET  /api/projects/{project_id}/ai/consistency-review
      审稿报告列表（只读报告查询）
- GET  /api/projects/{project_id}/ai/consistency-review/{review_id}
      审稿报告详情（只读）
- POST /api/projects/{project_id}/ai/repair-candidate
      修复补丁候选生成（只读，不落正文，受阶段D 限次守卫约束）
- POST /api/projects/{project_id}/ai/partial-recheck
      补丁应用后的局部复检（只读，仅重跑该问题关联事实域）

所有接口按 project_id + user_id 校验归属（协作者按角色），越权统一 404/403；
审稿与候选均为只读，不修改正文与事实库；应用补丁需人工确认后走既有正文修改链路。
"""
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.models.ai_task import AITask
from app.models.consistency_review import ConsistencyReview
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services import collaboration_service
from app.services.ai_task_service import (
    AUTO_REPAIR_MAX_PER_ISSUE,
    count_repair_attempts,
    run_ai_task,
)
from app.services.review_repair_service import (
    REVIEW_PROMPT_VERSION,
    run_partial_recheck,
)


router = APIRouter(
    prefix="/api/projects/{project_id}/ai",
    tags=["独立审稿与补丁闭环"],
)

# 触发审稿所需写权限（审稿本身只读，但会写只读报告与建议队列，限制为可写角色）
_WRITE_ROLES = {"owner", "editor"}
# 局部复检为纯只读校验
_READ_ROLES = {"owner", "editor", "viewer"}

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600


async def _ensure_project_readable(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Project:
    """项目存在且当前用户可读（作者或协作者），否则 404/403。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role not in _READ_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该项目",
            )
    return project


async def _ensure_project_writable(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Project:
    """项目存在且当前用户为 owner/editor，否则 404/403。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )
    if project.user_id != current_user.id:
        await collaboration_service.require_role(
            db, project.id, current_user.id, _WRITE_ROLES
        )
    return project


def _ensure_feature(enabled: bool, feature: str, flag: str) -> None:
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{feature}功能已关闭（{flag}=False）",
        )


def _serialize_review(review: ConsistencyReview, *, with_issues: bool = False) -> dict:
    issues = []
    if with_issues and review.issues:
        try:
            data = json.loads(review.issues)
            if isinstance(data, list):
                issues = data
        except Exception:  # noqa: BLE001
            issues = []
    counts: dict = {}
    if review.severity_counts:
        try:
            loaded = json.loads(review.severity_counts)
            if isinstance(loaded, dict):
                counts = loaded
        except Exception:  # noqa: BLE001
            counts = {}
    return {
        "id": str(review.id),
        "project_id": str(review.project_id),
        "chapter_id": str(review.chapter_id) if review.chapter_id else None,
        "chapter_title": review.chapter_title,
        "scope": review.scope,
        "status": review.status,
        "trigger": review.trigger,
        "score": review.score,
        "issue_count": review.issue_count or 0,
        "severity_counts": counts,
        "suggestion_count": review.suggestion_count or 0,
        "task_id": str(review.task_id) if review.task_id else None,
        "model_name": review.model_name,
        "prompt_version": review.prompt_version,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "completed_at": review.completed_at.isoformat() if review.completed_at else None,
        "report_only": True,
        "requires_confirmation": True,
        "issues": issues,
        "note": review.note,
    }


@router.post("/consistency-review")
async def create_consistency_review_task(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str | None = None,
    outline_text: str | None = None,
    trigger: str = Query(default="manual"),
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发起独立 LLM 连续性审稿（异步可取消；只产出报告，不修改正文）。

    审稿问题只入 change_suggestions 建议队列（pending），人工确认后才可应用。
    """
    _ensure_feature(
        bool(getattr(settings, "llm_consistency_review_enabled", True)),
        "独立 LLM 审稿",
        "llm_consistency_review_enabled",
    )
    await _ensure_project_writable(project_id, current_user, db)

    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"超时时间允许范围 1-{MAX_TIMEOUT_SECONDS} 秒",
        )

    outline = await db.get(Outline, chapter_id)
    if outline is None or outline.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在或不属于该项目",
        )

    snapshot = {
        "chapter_id": str(chapter_id),
        "chapter_title": chapter_title or outline.title or "",
        "outline_text": (outline_text or "")[:5000],
        "trigger": trigger,
        "report_only": True,
        "requires_confirmation": True,
    }
    task = AITask(
        user_id=current_user.id,
        project_id=project_id,
        outline_id=chapter_id,
        task_type="consistency_review",
        status="pending",
        prompt="独立 LLM 连续性审稿（只读报告）",
        prompt_version=REVIEW_PROMPT_VERSION,
        request_snapshot=json.dumps(snapshot, ensure_ascii=False),
        timeout_at=datetime.utcnow() + timedelta(seconds=timeout_seconds),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    background_tasks.add_task(run_ai_task, task.id, AsyncSessionLocal)

    return {
        "task_id": str(task.id),
        "status": task.status,
        "chapter_id": str(chapter_id),
        "report_only": True,
        "content_modified": False,
        "requires_confirmation": True,
        "note": "审稿任务已创建：仅产出只读报告，不修改正文；问题将进入建议队列待人工确认",
    }


@router.get("/consistency-review")
async def list_consistency_reviews(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """审稿报告列表（只读）。"""
    await _ensure_project_readable(project_id, current_user, db)

    conditions = [ConsistencyReview.project_id == project_id]
    if chapter_id is not None:
        conditions.append(ConsistencyReview.chapter_id == chapter_id)

    total = (await db.execute(
        select(func.count(ConsistencyReview.id)).where(*conditions)
    )).scalar_one()
    rows = (await db.execute(
        select(ConsistencyReview)
        .where(*conditions)
        .order_by(ConsistencyReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )).scalars().all()

    return {
        "total": total,
        "items": [_serialize_review(row) for row in rows],
    }


@router.get("/consistency-review/{review_id}")
async def get_consistency_review(
    project_id: uuid.UUID,
    review_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """审稿报告详情（只读报告，含问题清单）。"""
    await _ensure_project_readable(project_id, current_user, db)
    review = await db.get(ConsistencyReview, review_id)
    if review is None or review.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="审稿报告不存在",
        )
    return _serialize_review(review, with_issues=True)


@router.post("/repair-candidate")
async def create_repair_candidate_task(
    project_id: uuid.UUID,
    issue_id: str,
    issue: str,
    chapter_id: uuid.UUID | None = None,
    model_name: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成修复补丁候选（只读候选、不落正文，衔接阶段D 限次守卫）。

    - issue 为校验问题 JSON 字符串（含 rule/message/suggestion 等）；
    - 同一问题（issue_id）最多 AUTO_REPAIR_MAX_PER_ISSUE 次，超出拒绝；
    - 候选仅返回供人工审阅，应用需人工确认后走既有正文修改链路。
    """
    _ensure_feature(
        bool(getattr(settings, "repair_candidate_enabled", True)),
        "补丁候选生成",
        "repair_candidate_enabled",
    )
    await _ensure_project_writable(project_id, current_user, db)

    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"超时时间允许范围 1-{MAX_TIMEOUT_SECONDS} 秒",
        )

    try:
        issue_data = json.loads(issue) if isinstance(issue, str) else dict(issue)
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="issue 需为合法 JSON 对象字符串",
        )
    if not isinstance(issue_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="issue 需为 JSON 对象",
        )

    # 阶段D 限次守卫：同一问题已发起次数达上限则拒绝（repair 与候选共用计数）
    attempts = await count_repair_attempts(db, project_id, issue_id)
    if attempts >= AUTO_REPAIR_MAX_PER_ISSUE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"该问题修复候选/修复尝试已达上限"
                f"（{AUTO_REPAIR_MAX_PER_ISSUE} 次/问题），请人工处置"
            ),
        )

    snapshot = {
        "issue_id": issue_id,
        "issue": issue_data,
        "chapter_id": str(chapter_id) if chapter_id else issue_data.get("chapter_id"),
        "read_only": True,
        "requires_confirmation": True,
    }
    task = AITask(
        user_id=current_user.id,
        project_id=project_id,
        outline_id=chapter_id,
        task_type="repair_candidate",
        status="pending",
        prompt=f"修复补丁候选生成（只读）：{issue_id}",
        model_name=model_name,
        request_snapshot=json.dumps(snapshot, ensure_ascii=False),
        timeout_at=datetime.utcnow() + timedelta(seconds=timeout_seconds),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    background_tasks.add_task(run_ai_task, task.id, AsyncSessionLocal)

    return {
        "task_id": str(task.id),
        "status": task.status,
        "issue_id": issue_id,
        "attempts_used": attempts + 1,
        "attempts_limit": AUTO_REPAIR_MAX_PER_ISSUE,
        "read_only": True,
        "content_modified": False,
        "requires_confirmation": True,
        "note": "补丁候选任务已创建：仅只读生成候选，不落正文；应用需人工确认",
    }


@router.post("/partial-recheck")
async def partial_recheck(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    rule: str,
    issue_key: str | None = None,
    related_ids: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """补丁应用后的局部复检（只读）：仅重跑该问题关联事实域。

    不整库全量重校验、不触碰未关联章节与正文；命中同一 issue_key 视为仍存在，
    未命中视为已解决并建议处置 fixed（由人工经处置接口落库）。
    """
    await _ensure_project_readable(project_id, current_user, db)

    if not (rule or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rule 不能为空（局部复检按规则确定事实域范围）",
        )

    parsed_related: list[str] = []
    if related_ids:
        try:
            loaded = json.loads(related_ids)
            if isinstance(loaded, list):
                parsed_related = [str(x) for x in loaded if x]
            else:
                parsed_related = [item for item in str(related_ids).split(",") if item]
        except Exception:  # noqa: BLE001
            parsed_related = [item for item in str(related_ids).split(",") if item]

    return await run_partial_recheck(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        rule=rule,
        issue_key=issue_key,
        related_ids=parsed_related,
    )
