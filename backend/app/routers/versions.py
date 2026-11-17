# 版本管理与差异对比路由（第十二批规格实现）
# 挂载：main.py 中 include_router(versions.router)
# 所有接口先做 Project 归属校验（Project.id == project_id 且 Project.user_id == current_user.id），
# 目标内容再校验 target.project_id == project_id；created_by 一律由服务端生成。
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.content_version import TARGET_TYPES
from app.schemas.version import (
    AICandidateActionResponse,
    AICandidateCreate,
    AIApplyBlocksRequest,
    AIInsertRequest,
    DiffRequest,
    LockRequest,
    LockResponse,
    RestoreResponse,
    VersionCleanupRequest,
    VersionCleanupResponse,
    VersionCreate,
    VersionListResponse,
    VersionResponse,
)
from app.services import index_service
from app.services import version_service as vs
from app.services.project_access import check_project_access

router = APIRouter(prefix="/api/projects/{project_id}", tags=["版本管理"])

_HTTP_400 = status.HTTP_400_BAD_REQUEST


def _version_response(v) -> VersionResponse:
    return VersionResponse(
        id=v.id,
        project_id=v.project_id,
        target_type=v.target_type,
        target_id=v.target_id,
        version_number=v.version_number,
        title_snapshot=v.title_snapshot,
        content_snapshot=v.content_snapshot,
        metadata_snapshot=v.metadata_snapshot,
        change_type=v.change_type,
        change_summary=v.change_summary,
        source=v.source,
        status=v.status,
        created_by=v.created_by,
        created_at=v.created_at,
        generation_id=v.generation_id,
        parent_version_id=v.parent_version_id,
        candidate_content=v.candidate_content,
        accepted_at=v.accepted_at,
        rejected_at=v.rejected_at,
        locked=v.locked,
    )


def _check_target_type(target_type: str):
    if target_type not in TARGET_TYPES:
        raise HTTPException(status_code=_HTTP_400, detail="不支持的目标类型")


async def _resync_index_after_restore(
    db: AsyncSession, project_id: UUID, target_type: str, target_id: UUID,
) -> None:
    """恢复旧版本后重建搜索索引（规格六：恢复内容 → 重新生成索引，失败不影响恢复）。"""
    try:
        target = await vs.get_target(db, project_id, target_type, target_id)
    except HTTPException:
        return
    if target_type == "outline":
        await index_service._safe_sync(
            db, "sync_outline_index", project_id, "outline", target.id,
            index_service.sync_outline_index(db, target),
        )
    elif target_type == "chapter":
        cc, outline = target
        await index_service._safe_sync(
            db, "sync_chapter_content_index", project_id, "chapter_content", outline.id,
            index_service.sync_chapter_content_index(
                db, project_id, outline.id, outline.title or "", cc.content or "",
                outline.status, outline.visibility, volume_title=None,
            ),
        )
        await index_service._safe_sync(
            db, "sync_outline_index", project_id, "outline", outline.id,
            index_service.sync_outline_index(db, outline),
        )
    elif target_type == "knowledge_item":
        if hasattr(target, "content"):
            await index_service._safe_sync(
                db, "sync_world_setting_index", project_id, "world_setting", target.id,
                index_service.sync_world_setting_index(
                    db, project_id, target.id, target.name, target.content
                ),
            )
        else:
            await index_service._safe_sync(
                db, "sync_character_index", project_id, "character", target.id,
                index_service.sync_character_index(
                    db, project_id, target.id, target.name, target.description
                ),
            )


# ============ 版本列表（项目级） ============

@router.get("/versions", response_model=VersionListResponse)
async def list_all_versions(
    project_id: UUID,
    target_type: str | None = Query(None),
    target_id: UUID | None = Query(None),
    source: str | None = Query(None),
    created_by: UUID | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_drafts: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    rows, total = await vs.list_versions(
        db,
        project_id,
        target_type=target_type,
        target_id=target_id,
        source=source,
        created_by=created_by,
        from_dt=from_,
        to_dt=to,
        page=page,
        page_size=page_size,
        include_drafts=include_drafts,
        per_target=False,
    )
    return VersionListResponse(
        items=[_version_response(v) for v in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/versions/cleanup", response_model=VersionCleanupResponse)
async def cleanup_versions(
    project_id: UUID,
    body: VersionCleanupRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    result = await vs.cleanup_versions(
        db, project_id, body.keep_drafts, body.keep_ai_candidates
    )
    await db.commit()
    return VersionCleanupResponse(**result)


# ============ 目标级版本列表 / 创建 / 差异 / AI 候选 / 锁定 ============

@router.get("/{target_type}/{target_id}/versions", response_model=VersionListResponse)
async def list_target_versions(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    source: str | None = Query(None),
    created_by: UUID | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_drafts: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    await vs.get_target(db, project_id, target_type, target_id)  # 归属校验（404）
    rows, total = await vs.list_versions(
        db,
        project_id,
        target_type=target_type,
        target_id=target_id,
        source=source,
        created_by=created_by,
        from_dt=from_,
        to_dt=to,
        page=page,
        page_size=page_size,
        include_drafts=include_drafts,
        per_target=True,
    )
    return VersionListResponse(
        items=[_version_response(v) for v in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{target_type}/{target_id}/versions", response_model=VersionResponse)
async def create_version(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    body: VersionCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    target = await vs.get_target(db, project_id, target_type, target_id)

    if body.source == "restore":
        raise HTTPException(
            status_code=_HTTP_400, detail="恢复版本由恢复接口创建，请调用 restore"
        )
    vs._validate_source(body.source)

    latest = await vs.get_latest_version(db, project_id, target_type, target_id)
    if body.source == "import":
        change_type = "import"
    elif body.source == "ai":
        change_type = "ai_generate"
    else:
        change_type = "update" if latest is not None else "create"

    version = await vs.create_version_record(
        db,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target=target,
        change_type=change_type,
        source=body.source,
        change_summary=body.change_summary,
        created_by=current_user.id,
        is_draft=body.is_draft,
    )
    await db.commit()
    return _version_response(version)


@router.post("/{target_type}/{target_id}/versions/diff")
async def diff_versions(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    body: DiffRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    await vs.get_target(db, project_id, target_type, target_id)
    return await vs.diff_versions(
        db,
        project_id,
        target_type,
        target_id,
        body.base_version_id,
        body.compare_version_id,
    )


@router.post("/{target_type}/{target_id}/versions/ai-candidates", response_model=VersionResponse)
async def create_ai_candidate(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    body: AICandidateCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    version = await vs.create_ai_candidate(
        db, project_id, target_type, target_id, body, current_user
    )
    await db.commit()
    return _version_response(version)


@router.post("/{target_type}/{target_id}/lock", response_model=LockResponse)
async def set_lock(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    body: LockRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.set_locked(
        db, project_id, target_type, target_id, body.locked, current_user
    )
    await db.commit()
    return LockResponse(**result)


# ============ 版本详情 / 恢复 / AI 操作 ============

@router.get("/{target_type}/{target_id}/versions/{version_id}", response_model=VersionResponse)
async def get_version(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    version = await vs.get_version(db, project_id, target_type, target_id, version_id)
    return _version_response(version)


@router.post("/{target_type}/{target_id}/versions/{version_id}/restore", response_model=RestoreResponse)
async def restore_version(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.restore_version(
        db, project_id, target_type, target_id, version_id, current_user
    )
    # 恢复旧版本后重建搜索索引（规格六，失败不影响恢复）
    await _resync_index_after_restore(db, project_id, target_type, target_id)
    await db.commit()
    return RestoreResponse(**result)


@router.post("/{target_type}/{target_id}/versions/{version_id}/ai/accept", response_model=AICandidateActionResponse)
async def ai_accept(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.ai_accept(db, project_id, target_type, target_id, version_id, current_user)
    await db.commit()
    return AICandidateActionResponse(**result)


@router.post("/{target_type}/{target_id}/versions/{version_id}/ai/reject", response_model=AICandidateActionResponse)
async def ai_reject(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.ai_reject(db, project_id, target_type, target_id, version_id, current_user)
    await db.commit()
    return AICandidateActionResponse(**result)


@router.post("/{target_type}/{target_id}/versions/{version_id}/ai/replace", response_model=AICandidateActionResponse)
async def ai_replace(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.ai_replace(db, project_id, target_type, target_id, version_id, current_user)
    await db.commit()
    return AICandidateActionResponse(**result)


@router.post("/{target_type}/{target_id}/versions/{version_id}/ai/insert", response_model=AICandidateActionResponse)
async def ai_insert(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    body: AIInsertRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.ai_insert(
        db, project_id, target_type, target_id, version_id, body, current_user
    )
    await db.commit()
    return AICandidateActionResponse(**result)


@router.post("/{target_type}/{target_id}/versions/{version_id}/ai/apply-blocks", response_model=AICandidateActionResponse)
async def ai_apply_blocks(
    project_id: UUID,
    target_type: str,
    target_id: UUID,
    version_id: UUID,
    body: AIApplyBlocksRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await check_project_access(db, project_id, current_user.id)
    _check_target_type(target_type)
    result = await vs.ai_apply_blocks(
        db, project_id, target_type, target_id, version_id, body, current_user
    )
    await db.commit()
    return AICandidateActionResponse(**result)
