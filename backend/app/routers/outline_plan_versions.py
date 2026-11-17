# 大纲计划版本（OutlinePlanVersion）路由
# 「确认大纲」后端接口：用户确认时将当时完整目录与结构化章节计划持久化为
# status=confirmed 版本快照，记录 confirmedAt/confirmedBy；同一项目唯一 active
# confirmed，旧版置 superseded（不删除）；dry_run 预览影响面，不做静默替换。
# 权限：作者或 owner/editor 可同步草稿/确认；作者或项目成员可读版本列表。
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.outline_plan_version import (
    ConfirmOutlinePlanRequest,
    ConfirmOutlinePlanResponse,
    ConfirmPreviewItem,
    OutlinePlanStatusResponse,
    OutlinePlanVersionDetailResponse,
    OutlinePlanVersionResponse,
)
from app.services import collaboration_service
from app.services.outline_plan_version_service import (
    OutlinePlanError,
    confirm_outline_plan,
    get_active_confirmed_version,
    get_version_detail,
    list_versions,
    plan_status,
    snapshot_project,
    sync_draft_version,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/outline-plans",
    tags=["大纲版本化"],
)


async def _require_editor(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> None:
    """作者或 owner/editor 角色可同步草稿 / 确认大纲版本。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可确认大纲",
            )


async def _require_reader(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> None:
    """作者或项目成员可读取版本信息。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目成员可查看大纲版本",
            )


def _detail_dict(version) -> dict:
    try:
        content = json.loads(version.content_snapshot or "{}")
    except Exception:  # noqa: BLE001
        content = {}
    try:
        plans = json.loads(version.chapter_plans_snapshot or "{}")
    except Exception:  # noqa: BLE001
        plans = {}
    return {
        "id": str(version.id),
        "project_id": str(version.project_id),
        "version": version.version,
        "status": version.status,
        "confirmed_at": version.confirmed_at.isoformat() if version.confirmed_at else None,
        "confirmed_by": str(version.confirmed_by) if version.confirmed_by else None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
        "content_summary": {
            "projectTitle": content.get("projectTitle") or "",
            "volumeCount": len(content.get("volumes") or []),
            "chapterCount": len(plans or {}),
        },
        "content_snapshot": content,
        "chapter_plans_snapshot": plans,
    }


@router.get("/status", response_model=OutlinePlanStatusResponse)
async def get_plan_status(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OutlinePlanStatusResponse:
    """项目大纲版本化状态：active confirmed / 最新 draft / 是否有未确认修改。"""
    await _require_reader(project_id, current_user, db)
    result = await plan_status(db, project_id)
    return OutlinePlanStatusResponse(**result)


@router.get("", response_model=list[OutlinePlanVersionResponse])
async def get_versions(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OutlinePlanVersionResponse]:
    """历史版本列表（按版本号倒序）。"""
    await _require_reader(project_id, current_user, db)
    versions = await list_versions(db, project_id)
    out = []
    for v in versions:
        try:
            content = json.loads(v.content_snapshot or "{}")
        except Exception:  # noqa: BLE001
            content = {}
        try:
            plans = json.loads(v.chapter_plans_snapshot or "{}")
        except Exception:  # noqa: BLE001
            plans = {}
        out.append(
            OutlinePlanVersionResponse(
                id=v.id,
                project_id=v.project_id,
                version=v.version,
                status=v.status,
                confirmed_at=v.confirmed_at,
                confirmed_by=v.confirmed_by,
                created_at=v.created_at,
                updated_at=v.updated_at,
                content_summary={
                    "projectTitle": content.get("projectTitle") or "",
                    "volumeCount": len(content.get("volumes") or []),
                    "chapterCount": len(plans or {}),
                },
            )
        )
    return out


@router.get("/{version_id}", response_model=OutlinePlanVersionDetailResponse)
async def get_version(
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OutlinePlanVersionDetailResponse:
    """版本详情：完整目录快照 + 章节计划快照（历史版本查看）。"""
    await _require_reader(project_id, current_user, db)
    version = await get_version_detail(db, project_id, version_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="大纲版本不存在",
        )
    data = _detail_dict(version)
    return OutlinePlanVersionDetailResponse(
        **{
            k: v
            for k, v in data.items()
            if k in OutlinePlanVersionDetailResponse.model_fields
        }
    )


@router.post("/sync-draft", response_model=OutlinePlanStatusResponse)
async def sync_draft(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OutlinePlanStatusResponse:
    """同步/创建 draft 版本（AI 生成或人工编辑目录后调用，幂等）。"""
    await _require_editor(project_id, current_user, db)
    await sync_draft_version(db, project_id)
    result = await plan_status(db, project_id)
    return OutlinePlanStatusResponse(**result)


@router.post("/confirm", response_model=ConfirmOutlinePlanResponse)
async def confirm_plan(
    project_id: uuid.UUID,
    payload: ConfirmOutlinePlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConfirmOutlinePlanResponse:
    """确认大纲：dry_run=true 仅返回影响面预览；false 持久化 confirmed 版本。"""
    await _require_editor(project_id, current_user, db)
    try:
        result = await confirm_outline_plan(
            db,
            project_id,
            current_user.id,
            dry_run=payload.dry_run,
        )
    except OutlinePlanError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    version = result["version"]
    impact = [
        ConfirmPreviewItem(**item) for item in result.get("impact") or []
    ]
    return ConfirmOutlinePlanResponse(
        version=OutlinePlanVersionDetailResponse(
            **_detail_dict(version)
        ),
        superseded_version=result.get("superseded_version"),
        impact=impact,
        title_conflicts=[i for i in impact if i.title_conflict],
        written_chapters_affected=[
            i for i in impact if i.has_written_content and (
                i.title_changed or i.title_conflict
            )
        ],
        dry_run=bool(result.get("dry_run")),
        unchanged=bool(result.get("unchanged")),
    )
