# 开放 API v1 AI 异步任务路由（第十九批「移动端适配与开放 API」规格十七）
# POST /api/v1/ai/tasks 提交（立即返回 pending，后台执行）；GET 查询；POST cancel 取消。
# 限制：输入/输出长度、并发、每日额度、项目权限（由 openapi_ai_task_service 落地）。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import AsyncSessionLocal, get_db
from app.models.openapi_ai_task import OpenAPIAITask
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import AITaskCreateRequest
from app.services import openapi_ai_task_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/ai", tags=["开放API-AI", "实验"])

# AI 接口单独限流（规格二十）
async def guard_submit(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="ai:submit", kind="ai", credentials=credentials, db=db)


async def guard_read(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="ai:read", kind="ai", credentials=credentials, db=db)


def _task_dict(t: OpenAPIAITask) -> dict:
    options = {}
    try:
        options = json.loads(t.options or "{}")
    except (TypeError, ValueError):
        options = {}
    return {
        "id": str(t.id),
        "project_id": str(t.project_id),
        "chapter_id": str(t.chapter_id) if t.chapter_id else None,
        "task_type": t.task_type,
        "model": t.model,
        "status": t.status,
        "input": t.input_text,
        "options": options,
        "result": t.result,
        "error_message": t.error_message,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


@router.post("/tasks", status_code=201)
async def submit_ai_task(
    request: Request,
    payload: AITaskCreateRequest,
    guard: dict = Depends(guard_submit),
    db: AsyncSession = Depends(get_db),
):
    task = await openapi_ai_task_service.submit_task(
        db,
        user=guard["principal"].user,
        project_id=payload.project_id,
        chapter_id=payload.chapter_id,
        task_type=payload.task_type,
        model=payload.model,
        input_text=payload.input,
        options=payload.options or {},
    )
    # 任务保持 pending；首次 GET 查询时触发执行（避免提交请求阻塞在 AI 结果上）
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"task": _task_dict(task)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/tasks/{task_id}")
async def get_ai_task(
    task_id: UUID,
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    task = await openapi_ai_task_service.get_task(
        db, guard["principal"].user.id, task_id
    )
    # pending 任务首次查询时执行（独立会话运行本地 stub，不阻塞提交请求）
    if task.status == "pending":
        await openapi_ai_task_service.run_ai_task(task.id, AsyncSessionLocal)
        await db.refresh(task)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"task": _task_dict(task)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_ai_task(
    task_id: UUID,
    request: Request,
    guard: dict = Depends(guard_submit),
    db: AsyncSession = Depends(get_db),
):
    task = await openapi_ai_task_service.get_task(
        db, guard["principal"].user.id, task_id
    )
    task = await openapi_ai_task_service.cancel_task(db, task)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"task": _task_dict(task)},
        rate_headers=build_rate_headers(guard["rate"]),
    )
