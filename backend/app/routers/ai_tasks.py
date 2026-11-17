# AI 任务路由（第八批规格实现 + P0 任务系统补齐）
# 创建 / 查询 / 取消 / 恢复 / 重试 / 列表 AI 生成任务：
# - POST   /api/projects/{project_id}/ai/tasks            创建任务（后台执行，支持幂等键/超时/模型/快照/自动重试/成本预算）
# - GET    /api/projects/{project_id}/ai/tasks            任务列表（任务中心数据源）
# - GET    /api/projects/{project_id}/ai/tasks/{task_id}  查询任务状态
# - POST   /api/projects/{project_id}/ai/tasks/{task_id}/cancel  取消任务（仅 pending/running）
# - POST   /api/projects/{project_id}/ai/tasks/{task_id}/resume  恢复已取消任务（保留进度快照断点续跑）
# - POST   /api/projects/{project_id}/ai/tasks/{task_id}/retry   重试失败/超时任务（上限 max_retries，默认 3）
# 所有接口按 project_id + user_id 校验归属，越权统一 404。
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import AsyncSessionLocal, get_db
from app.models.ai_task import AITask
from app.models.project import Project
from app.models.user import User
from app.schemas.ai_task import (
    AITaskCreateResponse,
    AITaskListResponse,
    AITaskResponse
)
from app.services.ai_task_service import (
    AUTO_REPAIR_MAX_PER_ISSUE,
    count_repair_attempts,
    run_ai_task,
)
from app.services.token_usage_service import (
    UsageLedgerError,
    assert_recoverable,
)


router = APIRouter(
    prefix="/api/projects/{project_id}/ai/tasks",
    tags=["AI 任务"]
)

ALLOWED_TASK_TYPES = {
    "generate_summary",
    "generate_volumes",
    "generate_chapters",
    "continue",
    "rewrite",
    "polish",
    # P1：一致性检查 / 变更建议生成统一接入 ai_tasks
    "consistency_check",
    "generate_suggestions",
    # 阶段D：repair 类任务（自动修复次数上限守卫，默认 2 次/问题）
    "repair",
    # 阶段I：独立 LLM 审稿（只读报告）与修复补丁候选（只读候选，共用限次守卫）
    "consistency_review",
    "repair_candidate",
}

MAX_PROMPT_LENGTH = 100000
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
# 阶段D：自动重试上限允许范围（0 = 不自动重试，默认关闭）
MAX_AUTO_RETRIES = 5


@router.post(
    "",
    response_model=AITaskCreateResponse
)
async def create_ai_task(
    project_id: uuid.UUID,
    task_type: str,
    prompt: str,
    outline_id: uuid.UUID | None = None,
    chapter_id: uuid.UUID | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    idempotency_key: str | None = None,
    request_snapshot: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    # 阶段D「异步任务增强」：自动重试上限 / 成本预算（默认关闭，仅新任务生效）
    max_retries: int = 0,
    cost_budget: int | None = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id
        )
    )

    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )

    if task_type not in ALLOWED_TASK_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不支持的 AI 任务类型"
        )

    if len(prompt) > MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请求内容过长"
        )

    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="超时时间超出允许范围"
        )

    # 阶段D：max_retries 允许范围 0-5（0 = 不自动重试，默认关闭）
    if max_retries < 0 or max_retries > MAX_AUTO_RETRIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"max_retries 允许范围 0-{MAX_AUTO_RETRIES}"
        )
    # 阶段D：cost_budget 需为正整数（省略 = 不检查，默认关闭）
    if cost_budget is not None and cost_budget < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cost_budget 需为正整数或省略（不检查）"
        )

    # P0：幂等键查重——同一用户/作品下相同幂等键的任务直接返回原任务，不重复创建
    if idempotency_key:
        result = await db.execute(
            select(AITask).where(
                AITask.idempotency_key == idempotency_key,
                AITask.project_id == project_id,
                AITask.user_id == current_user.id
            ).order_by(AITask.created_at.desc()).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return AITaskCreateResponse(
                task_id=existing.id,
                status=existing.status,
                duplicated=True
            )

    # 阶段D：repair 类任务自动修复次数上限守卫（默认 2 次/问题）。
    # 同一作品下同一 issue_id 已发起 AUTO_REPAIR_MAX_PER_ISSUE 次 repair 任务则拒绝。
    # 阶段I：修复补丁候选（repair_candidate）与 repair 共用同一计数，纳入相同上限。
    if task_type in ("repair", "repair_candidate"):
        snap = {}
        if request_snapshot:
            try:
                snap = json.loads(request_snapshot)
            except Exception:
                snap = {}
        issue_id = snap.get("issue_id")
        if not issue_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="repair/repair_candidate 任务需要 request_snapshot.issue_id"
            )
        attempts = await count_repair_attempts(db, project_id, issue_id)
        if attempts >= AUTO_REPAIR_MAX_PER_ISSUE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"该问题修复/候选次数已达上限"
                    f"（{AUTO_REPAIR_MAX_PER_ISSUE} 次/问题），请人工处置"
                )
            )

    # P1：chapter_id 合并进 request_snapshot，供后台执行器定位章节正文
    if chapter_id is not None:
        if request_snapshot:
            try:
                snap = json.loads(request_snapshot)
            except Exception:
                snap = {}
            snap.setdefault("chapter_id", str(chapter_id))
            request_snapshot = json.dumps(snap, ensure_ascii=False)
        else:
            request_snapshot = json.dumps(
                {"chapter_id": str(chapter_id)},
                ensure_ascii=False,
            )

    task = AITask(
        user_id=current_user.id,
        project_id=project_id,
        outline_id=outline_id,
        task_type=task_type,
        status="pending",
        prompt=prompt,
        model_name=model_name,
        prompt_version=prompt_version,
        idempotency_key=idempotency_key,
        request_snapshot=request_snapshot,
        timeout_at=datetime.utcnow() + timedelta(seconds=timeout_seconds),
        # 阶段D：自动重试上限 / 成本预算（默认关闭，仅新任务生效）
        max_retries=max_retries,
        cost_budget=cost_budget,
    )

    db.add(task)
    await db.commit()
    await db.refresh(task)

    background_tasks.add_task(
        run_ai_task,
        task.id,
        AsyncSessionLocal
    )

    return AITaskCreateResponse(
        task_id=task.id,
        status=task.status
    )


@router.get(
    "",
    response_model=AITaskListResponse
)
async def list_ai_tasks(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """任务列表（任务中心数据源）：按项目 + 当前用户过滤，可按状态筛选。"""
    project = await db.get(Project, project_id)
    if not project or project.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )

    conditions = [
        AITask.project_id == project_id,
        AITask.user_id == current_user.id,
    ]
    if status_filter:
        conditions.append(AITask.status == status_filter)

    total = (await db.execute(
        select(func.count(AITask.id)).where(*conditions)
    )).scalar_one()

    result = await db.execute(
        select(AITask).where(*conditions)
        .order_by(AITask.created_at.desc())
        .offset(offset).limit(limit)
    )
    items = result.scalars().all()

    return AITaskListResponse(items=items, total=total)


@router.get(
    "/{task_id}",
    response_model=AITaskResponse
)
async def get_ai_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AITask).where(
            AITask.id == task_id,
            AITask.project_id == project_id,
            AITask.user_id == current_user.id
        )
    )

    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    # P0：惰性超时——查询时发现超过 timeout_at 的 pending/running 任务标记 timeout
    if task.status in {"pending", "running"} and task.timeout_at is not None:
        if datetime.utcnow() >= task.timeout_at:
            task.status = "timeout"
            task.error_message = "任务执行超时"
            task.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(task)

    return task


@router.post(
    "/{task_id}/cancel",
    response_model=AITaskResponse
)
async def cancel_ai_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AITask).where(
            AITask.id == task_id,
            AITask.project_id == project_id,
            AITask.user_id == current_user.id
        )
    )

    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    if task.status not in {"pending", "running"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前任务无法取消"
        )

    task.status = "cancelled"
    await db.commit()
    await db.refresh(task)

    return task


@router.post(
    "/{task_id}/retry",
    response_model=AITaskCreateResponse
)
async def retry_ai_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AITask).where(
            AITask.id == task_id,
            AITask.project_id == project_id,
            AITask.user_id == current_user.id
        )
    )

    old_task = result.scalar_one_or_none()

    if not old_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    if old_task.status not in {"failed", "timeout"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有失败或超时任务可以重试"
        )

    # 阶段D：手动重试上限与自动重试保持一致（max_retries，未设置时沿用历史硬编码 3）
    retry_limit = (
        old_task.max_retries
        if old_task.max_retries and old_task.max_retries > 0
        else 3
    )
    if old_task.retry_count >= retry_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="重试次数已达上限"
        )

    # B-2：重试前置契约——源任务账目未结清（冲突）-> 409；已下发未结清（在途）-> 422
    try:
        await assert_recoverable(db, old_task, action="retry")
    except UsageLedgerError as error:
        raise HTTPException(
            status_code=error.http_status,
            detail=error.message
        ) from error

    task = AITask(
        user_id=current_user.id,
        project_id=project_id,
        outline_id=old_task.outline_id,
        task_type=old_task.task_type,
        status="pending",
        prompt=old_task.prompt,
        retry_count=old_task.retry_count + 1,
        model_name=old_task.model_name,
        prompt_version=old_task.prompt_version,
        idempotency_key=old_task.idempotency_key,
        request_snapshot=old_task.request_snapshot,
        timeout_at=datetime.utcnow() + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS),
        # 阶段D：手动重试的新任务继承自动重试上限与成本预算
        max_retries=old_task.max_retries or 0,
        cost_budget=old_task.cost_budget,
    )

    db.add(task)
    await db.commit()
    await db.refresh(task)

    background_tasks.add_task(
        run_ai_task,
        task.id,
        AsyncSessionLocal
    )

    return AITaskCreateResponse(
        task_id=task.id,
        status=task.status
    )


@router.post(
    "/{task_id}/resume",
    response_model=AITaskResponse
)
async def resume_ai_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """恢复已取消任务：仅 cancelled 且保留进度快照（resume_token）的任务可恢复，
    置回 pending 后台断点续跑，不计入重试次数。"""
    result = await db.execute(
        select(AITask).where(
            AITask.id == task_id,
            AITask.project_id == project_id,
            AITask.user_id == current_user.id
        )
    )

    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    if task.status != "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有已取消任务可以恢复"
        )

    if not task.resume_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="任务没有进度快照，无法恢复（取消时未保留断点）"
        )

    # B-2：恢复前置契约——账目未结清（冲突）-> 409；已下发未结清（在途）-> 422
    try:
        await assert_recoverable(db, task, action="resume")
    except UsageLedgerError as error:
        raise HTTPException(
            status_code=error.http_status,
            detail=error.message
        ) from error

    task.status = "pending"
    task.error_message = None
    task.completed_at = None
    await db.commit()
    await db.refresh(task)

    background_tasks.add_task(
        run_ai_task,
        task.id,
        AsyncSessionLocal
    )

    return task
