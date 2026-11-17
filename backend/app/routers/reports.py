# 举报路由（第十五批「通知、评论与协作反馈」规格实现）
# 举报创建（重复 pending 去重）、列表与处理（reviewing/resolved/rejected）。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.outline import Outline
from app.models.project import Project
from app.models.report import Report
from app.models.user import User
from app.schemas.report import (
    ReportCreate,
    ReportListResponse,
    ReportResponse,
    ReportUpdate,
)
from app.services import collaboration_service, report_service

router = APIRouter(
    prefix="/api",
    tags=["举报", "实验"]
)


@router.post("/reports", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    data: ReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """举报评论/内容：同一举报人对同一目标 pending 去重。"""
    try:
        report = await report_service.create_report(
            db, current_user,
            target_type=data.target_type,
            target_id=data.target_id,
            reason=data.reason,
        )
    except report_service.ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(report)
    return ReportResponse.model_validate(report)


@router.get("/projects/{project_id}/reports", response_model=ReportListResponse)
async def list_reports(
    project_id: uuid.UUID,
    report_status: str | None = Query(default=None, alias="status"),
    target_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """举报列表（仅项目作者可查看）。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="仅项目作者可查看举报")
    items, total = await report_service.list_reports(
        db,
        project_id=project.id,
        status=report_status,
        target_type=target_type,
        page=page,
        page_size=page_size,
    )
    return ReportListResponse(
        items=[ReportResponse.model_validate(r) for r in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.put("/projects/{project_id}/reports/{report_id}", response_model=ReportResponse)
async def handle_report(
    project_id: uuid.UUID,
    report_id: uuid.UUID,
    data: ReportUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """处理举报（仅项目作者）：reviewing / resolved / rejected。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="仅项目作者可处理举报")
    report = await db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="举报不存在")
    # 对象级归属校验：Report 无 project_id 列，经 target_type/target_id 反查，
    # 防止任意项目作者跨项目处理他人举报（IDOR）
    if report.target_type == "project":
        if str(report.target_id) != str(project_id):
            raise HTTPException(status_code=404, detail="举报不存在")
    elif report.target_type in ("chapter", "outline"):
        try:
            target_uuid = uuid.UUID(str(report.target_id))
        except (ValueError, TypeError):
            target_uuid = None
        outline = await db.get(Outline, target_uuid) if target_uuid else None
        if outline is None or outline.project_id != project_id:
            raise HTTPException(status_code=404, detail="举报不存在")
    try:
        report = await report_service.handle_report(
            db, report, current_user, data.status,
        )
    except report_service.ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="report.handle", target_type="report", target_id=str(report.id),
        metadata={"status": data.status},
    )
    await db.commit()
    await db.refresh(report)
    return ReportResponse.model_validate(report)
