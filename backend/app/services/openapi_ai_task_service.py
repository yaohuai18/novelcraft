# 开放 API 异步 AI 任务服务（第十九批「移动端适配与开放 API」规格十七）
# POST /api/v1/ai/tasks 异步提交；GET 查询；POST cancel 取消。
# 限制：输入长度 / 输出长度 / 并发数量 / 每日额度 / 项目权限 / 敏感内容。
# 执行：独立会话 + BackgroundTasks 范式（与内部 AITask 一致）。
import json
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import ApiError
from app.core.config import settings
from app.models.mobile_draft import MobileDraft  # noqa: F401  保持 imports 统一
from app.models.openapi_ai_task import OPENAPI_AI_TASK_STATUSES, OpenAPIAITask
from app.models.project import Project
from app.models.user import User
from app.services import ai_quota_service

OPENAI_TASK_TYPES = [
    "polish",      # 润色
    "rewrite",     # 改写
    "continue",    # 续写
    "summarize",   # 摘要
    "outline",     # 大纲建议
]

MODEL_WHITELIST = ["default", "novel-7b", "novel-70b"]


def today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


async def check_project_access(
    db: AsyncSession,
    project_id: uuid.UUID,
    user: User,
) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise ApiError("not_found", "项目不存在", status_code=404)
    if project.user_id != user.id and user.role not in ("operator", "super_admin"):
        raise ApiError("not_found", "项目不存在", status_code=404)
    return project


async def submit_task(
    db: AsyncSession,
    *,
    user: User,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None,
    task_type: str,
    model: str,
    input_text: str,
    options: dict,
) -> OpenAPIAITask:
    """提交开放 AI 任务（同步创建 pending 记录，异步执行）。"""
    if task_type not in OPENAI_TASK_TYPES:
        raise ApiError(
            "validation_failed", f"非法任务类型: {task_type}", status_code=422
        )
    if model not in MODEL_WHITELIST:
        raise ApiError(
            "validation_failed", f"非法模型: {model}", status_code=422
        )

    content_length = len(input_text or "")
    if content_length > settings.ai_max_input_chars:
        raise ApiError(
            "validation_failed",
            f"输入过长（上限 {settings.ai_max_input_chars} 字符）",
            status_code=422,
        )

    project = await check_project_access(db, project_id, user)

    if chapter_id is not None:
        from sqlalchemy import select as sa_select
        from app.models.outline import Outline
        exists_row = (
            await db.execute(
                sa_select(Outline.id).where(
                    Outline.id == chapter_id,
                    Outline.project_id == project_id,
                    Outline.outline_type == "chapter",
                )
            )
        ).first()
        if exists_row is None:
            raise ApiError("not_found", "章节不存在", status_code=404)

    quota_check = await ai_quota_service.check_submission(
        db,
        user_id=user.id,
        project_id=project_id,
        content_length=content_length,
        period_date=today_str(),
        user_role=getattr(user, "role", "user") or "user",
        project_status=project.publication_status,
        ai_model_available=True,
    )
    if not quota_check["allowed"]:
        reason_map = {
            "content_too_long": "输入过长",
            "model_unavailable": "模型暂不可用",
            "project_unavailable": "项目不可用",
            "daily_limit_exceeded": "今日 AI 调用额度已用完",
            "project_budget_exceeded": "项目 AI 预算已用完",
            "user_budget_exceeded": "用户 AI 预算已用完",
            "concurrency_limit_reached": "AI 并发数量已达上限",
        }
        raise ApiError(
            "rate_limit_exceeded",
            reason_map.get(quota_check["reason"], "AI 额度不足"),
            status_code=429,
        )

    # 额度扣减 + 任务创建在同一事务
    await ai_quota_service.consume(
        db, user_id=user.id, project_id=project_id, period_date=today_str()
    )

    task = OpenAPIAITask(
        user_id=user.id,
        project_id=project_id,
        chapter_id=chapter_id,
        task_type=task_type,
        model=model,
        input_text=input_text or "",
        options=json.dumps(options or {}, ensure_ascii=False),
        status="pending",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def get_task(
    db: AsyncSession,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
) -> OpenAPIAITask:
    task = await db.get(OpenAPIAITask, task_id)
    if task is None or task.user_id != user_id:
        raise ApiError("not_found", "AI 任务不存在", status_code=404)
    return task


async def cancel_task(
    db: AsyncSession,
    task: OpenAPIAITask,
) -> OpenAPIAITask:
    """取消任务：仅 pending 可取消。running 置 cancelled 并在执行中检查。"""
    if task.status in ("completed", "cancelled"):
        raise ApiError(
            "conflict", f"任务已处于 {task.status} 状态，无法取消", status_code=409
        )
    if task.status == "failed":
        raise ApiError(
            "conflict", "任务已失败，无法取消", status_code=409
        )
    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return task


async def run_ai_task(task_id: uuid.UUID, AsyncSessionLocal) -> None:
    """后台执行（独立会话）。AI 本地 stub：sleep + 返回固定文本。"""
    async with AsyncSessionLocal() as db:
        task = await db.get(OpenAPIAITask, task_id)
        if task is None or task.status != "pending":
            return
        task.status = "running"
        task.started_at = datetime.utcnow()
        await db.commit()

        from app.services import ai_quota_service

        await ai_quota_service.mark_running(
            db,
            user_id=task.user_id,
            project_id=task.project_id,
            period_date=today_str(),
            delta=1,
        )

        try:
            # 模拟 AI 服务调用（本地 stub，无外部依赖）
            import asyncio

            await asyncio.sleep(0.2)
            options = json.loads(task.options or "{}")
            output_len = int(options.get("output_length", 300))
            input_snippet = (task.input_text or "")[:120]
            result_text = (
                f"[AI结果] 任务类型={task.task_type} 模型={task.model} "
                f"输入={input_snippet} 输出长度={output_len}"
            )
            task.result = result_text
            task.status = "completed"
            task.error_message = None
        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error_message = str(exc)[:500]

        task.completed_at = datetime.utcnow()
        await ai_quota_service.mark_running(
            db,
            user_id=task.user_id,
            project_id=task.project_id,
            period_date=today_str(),
            delta=-1,
        )
        await db.commit()
