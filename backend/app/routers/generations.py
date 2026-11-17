# 生成记录查询路由（第一期「生成不丢失」规格实现）
# 挂载：main.py 中 include_router(generations.router)
# 提供按 project 过滤的分页列表（可选 action_type 筛选）与单条详情（含 context_snapshot 解析）。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.generation import (
    GenerationRecordDetailResponse,
    GenerationRecordListResponse,
    GenerationRecordResponse,
)
from app.services import generation_service as gs
from app.services.project_access import check_project_access

router = APIRouter(prefix="/api/projects/{project_id}", tags=["生成记录"])


@router.get("/generations", response_model=GenerationRecordListResponse)
async def list_generations(
    project_id: UUID,
    action_type: str | None = Query(default=None, description="按动作类型筛选"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按 project 过滤的生成记录分页列表，按创建时间倒序。"""
    await check_project_access(db, project_id, current_user.id)
    rows, total = await gs.list_generations(
        db,
        project_id=project_id,
        action_type=action_type,
        page=page,
        page_size=page_size,
    )
    return GenerationRecordListResponse(
        items=[GenerationRecordResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/generations/{record_id}", response_model=GenerationRecordDetailResponse)
async def get_generation_detail(
    project_id: UUID,
    record_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """单条生成记录详情：附 context_snapshot 解析后的 JSON。"""
    await check_project_access(db, project_id, current_user.id)
    record = await gs.get_generation(db, project_id, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    context = None
    if record.context_snapshot:
        try:
            context = json.loads(record.context_snapshot)
        except json.JSONDecodeError:
            context = None
    response = GenerationRecordDetailResponse.model_validate(record)
    response.context = context
    return response
