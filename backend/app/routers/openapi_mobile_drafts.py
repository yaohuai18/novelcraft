# 开放 API v1 移动端本地草稿路由（第十九批「移动端适配与开放 API」规格五/六/七）
# 自动保存（upsert）与同步；冲突返回本地/服务器版本信息，不直接覆盖（默认保留冲突副本）。
# 冲突解决：keep_server / keep_local / save_as_new。
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.mobile_draft import MobileDraft
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import (
    MobileDraftSaveRequest,
    MobileDraftResolveRequest,
)
from app.services import mobile_draft_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/mobile/drafts", tags=["开放API-移动草稿", "实验"])

async def guard_write(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:write", kind="general", credentials=credentials, db=db)


async def guard_read(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:read", kind="general", credentials=credentials, db=db)


def _draft_dict(d: MobileDraft) -> dict:
    return {
        "id": str(d.id),
        "project_id": str(d.project_id),
        "chapter_id": str(d.chapter_id),
        "base_version": d.base_version,
        "content": d.content,
        "content_hash": d.content_hash,
        "device_info": d.device_info,
        "last_edited_at": d.last_edited_at.isoformat() if d.last_edited_at else None,
        "sync_status": d.sync_status,
        "sync_error": d.sync_error,
    }


@router.post("", status_code=201)
async def save_draft(
    request: Request,
    payload: MobileDraftSaveRequest,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    draft, conflict_info = await mobile_draft_service.upsert_and_sync(
        db,
        user=guard["principal"].user,
        project_id=payload.project_id,
        chapter_id=payload.chapter_id,
        content=payload.content,
        base_version=payload.base_version,
        device_info=payload.device_info,
    )
    status_code = 201 if conflict_info is None else 409
    await log_api_call(db, request, guard["principal"], status_code)
    return ok(
        request,
        {
            "draft": _draft_dict(draft),
            "conflict": conflict_info,
        },
        status_code=status_code,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("")
async def list_drafts(
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MobileDraft)
        .where(MobileDraft.user_id == guard["principal"].user.id)
        .order_by(MobileDraft.last_edited_at.desc())
        .limit(100)
    )
    drafts = list(result.scalars().all())
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"drafts": [_draft_dict(d) for d in drafts], "count": len(drafts)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{draft_id}")
async def get_draft(
    draft_id: UUID,
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(MobileDraft, draft_id)
    if draft is None or draft.user_id != guard["principal"].user.id:
        raise ApiError("not_found", "草稿不存在", status_code=404)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"draft": _draft_dict(draft)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/{draft_id}/sync")
async def sync_draft(
    draft_id: UUID,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(MobileDraft, draft_id)
    if draft is None or draft.user_id != guard["principal"].user.id:
        raise ApiError("not_found", "草稿不存在", status_code=404)
    draft, conflict_info = await mobile_draft_service.sync_draft(db, draft)
    status_code = 200 if conflict_info is None else 409
    await log_api_call(db, request, guard["principal"], status_code)
    return ok(
        request,
        {
            "draft": _draft_dict(draft),
            "conflict": conflict_info,
        },
        status_code=status_code,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/{draft_id}/conflict")
async def resolve_conflict(
    draft_id: UUID,
    payload: MobileDraftResolveRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(MobileDraft, draft_id)
    if draft is None or draft.user_id != guard["principal"].user.id:
        raise ApiError("not_found", "草稿不存在", status_code=404)
    draft, resolution = await mobile_draft_service.resolve_conflict(
        db, draft, payload.strategy
    )
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"draft": _draft_dict(draft), "resolution": resolution},
        rate_headers=build_rate_headers(guard["rate"]),
    )
