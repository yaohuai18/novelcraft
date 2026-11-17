# 第二十四批「企业级审核与内容治理」- 审批/机器预审/人工审核/治理下架/举报申诉/版权/发布 路由
# 审批：提交(锁定)→退回(必填原因)→复审→通过→发布记录版本；审批过程不可被普通成员删除；
# 治理：机器预审分级 + 人工复核 + 下架保留记录 + 恢复 + 申诉；禁止物理删除替代治理记录；
# 举报：收到→分级→处理→决定→申诉；版权：登记/侵权举报/下架申请/申诉。
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.services import enterprise_review_service as svc
from app.services import enterprise_service as esvc

router = APIRouter(prefix="/api/enterprise", tags=["batch24-review"])


async def _load_org(org_id: uuid.UUID, db: AsyncSession):
    return await esvc.get_org_by_id(db, org_id)


async def _require_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID):
    return await esvc.require_org_member(db, org_id, user_id)


# ---------- 审批流程 ----------
class ReviewSubmitIn(BaseModel):
    resource_type: str
    resource_id: str
    lock_after_submit: bool = True
    change_summary: str | None = None


@router.post("/organizations/{org_id}/projects/{project_id}/reviews")
async def submit_review(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: ReviewSubmitIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    task = await svc.submit_review(
        db, org, current_user, project_id, body.resource_type, body.resource_id,
        body.lock_after_submit, body.change_summary,
    )
    await db.commit()
    return {"id": str(task.id), "status": task.status, "stage": task.stage, "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None}


class ReviewRejectIn(BaseModel):
    reason: str


@router.post("/organizations/{org_id}/review-tasks/{task_id}/reject")
async def reject_review(
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    body: ReviewRejectIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    task = await svc.reject_review(db, org, current_user, task_id, body.reason)
    await db.commit()
    return {"id": str(task.id), "status": task.status, "comment": task.comment}


class ReviewResubmitIn(BaseModel):
    change_summary: str | None = None


@router.post("/organizations/{org_id}/review-tasks/{task_id}/resubmit")
async def resubmit_review(
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    body: ReviewResubmitIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    task = await svc.resubmit_review(db, org, current_user, task_id, body.change_summary)
    await db.commit()
    return {"id": str(task.id), "status": task.status, "stage": task.stage}


@router.post("/organizations/{org_id}/review-tasks/{task_id}/approve")
async def approve_review(
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    task = await svc.approve_review(db, org, current_user, task_id)
    await db.commit()
    return {"id": str(task.id), "status": task.status, "completed_at": task.completed_at.isoformat() if task.completed_at else None}


@router.post("/organizations/{org_id}/review-tasks/{task_id}/withdraw")
async def withdraw_review(
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    task = await svc.withdraw_review(db, org, current_user, task_id)
    await db.commit()
    return {"id": str(task.id), "status": task.status}


@router.get("/organizations/{org_id}/review-tasks")
async def list_review_tasks(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(None),
    status_filter: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows, total = await svc.list_review_tasks(db, org, project_id, status_filter)
    # 批量补齐提交人/审核人用户名与项目标题
    user_ids = {t.submitter_id for t in rows if t.submitter_id} | {t.reviewer_id for t in rows if t.reviewer_id}
    user_map: dict[str, str] = {}
    if user_ids:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        user_map = {str(u.id): u.username for u in users}
    project_ids = {t.project_id for t in rows if t.project_id}
    proj_map: dict[str, str] = {}
    if project_ids:
        projs = (await db.execute(select(Project).where(Project.id.in_(project_ids)))).scalars().all()
        proj_map = {str(p.id): p.title for p in projs}
    return {"total": total, "tasks": [
        {
            "id": str(t.id),
            "project_id": str(t.project_id) if t.project_id else None,
            "project_title": proj_map.get(str(t.project_id), "") if t.project_id else "",
            "resource_type": t.resource_type,
            "resource_id": t.resource_id,
            "status": t.status,
            "stage": t.stage,
            "comment": t.comment,
            "submitter_id": str(t.submitter_id) if t.submitter_id else None,
            "submitter_name": user_map.get(str(t.submitter_id), "") if t.submitter_id else "",
            "reviewer_id": str(t.reviewer_id) if t.reviewer_id else None,
            "reviewer_name": user_map.get(str(t.reviewer_id), "") if t.reviewer_id else "",
            "submitted_at": t.submitted_at.isoformat() if t.submitted_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]}


# ---------- 机器预审 / 人工审核 ----------
class MachinePrecheckIn(BaseModel):
    content_type: str
    resource_type: str
    resource_id: str
    text: str
    abnormal_batch: bool = False
    project_id: uuid.UUID | None = None


@router.post("/organizations/{org_id}/machine-precheck")
async def machine_precheck(
    org_id: uuid.UUID,
    body: MachinePrecheckIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    review = await svc.run_machine_precheck(
        db, org, current_user, body.content_type, body.resource_type, body.resource_id, body.text, body.abnormal_batch, body.project_id,
    )
    await db.commit()
    return {
        "id": str(review.id),
        "risk_level": review.risk_level,
        "machine_result": review.machine_result,
        "review_result": review.review_result,
        "findings": json.loads(review.findings or "[]"),
    }


class ManualReviewIn(BaseModel):
    review_result: str
    reason: str | None = None


@router.post("/organizations/{org_id}/content-reviews/{review_id}/manual")
async def manual_review(
    org_id: uuid.UUID,
    review_id: uuid.UUID,
    body: ManualReviewIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    review = await svc.manual_review_content(db, org, current_user, review_id, body.review_result, body.reason)
    await db.commit()
    return {"id": str(review.id), "review_result": review.review_result, "reviewed_by": str(review.reviewed_by) if review.reviewed_by else None}


# ---------- 内容治理 / 下架 / 恢复 / 申诉 ----------
class TakedownIn(BaseModel):
    content_type: str
    resource_type: str
    resource_id: str
    reason: str
    content_status: str = "takedown"
    original_version: str | None = None
    project_id: uuid.UUID | None = None


@router.post("/organizations/{org_id}/governance/takedown")
async def takedown_content(
    org_id: uuid.UUID,
    body: TakedownIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    record = await svc.takedown_content(
        db, org, current_user, body.content_type, body.resource_type, body.resource_id,
        body.reason, body.content_status, body.original_version, body.project_id,
    )
    await db.commit()
    return {"id": str(record.id), "content_status": record.content_status, "appeal_status": record.appeal_status}


class RestoreIn(BaseModel):
    reason: str
    restored_version: str | None = None


@router.post("/organizations/{org_id}/governance/{record_id}/restore")
async def restore_content(
    org_id: uuid.UUID,
    record_id: uuid.UUID,
    body: RestoreIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    record = await svc.restore_content(db, org, current_user, record_id, body.reason, body.restored_version)
    await db.commit()
    return {"id": str(record.id), "content_status": record.content_status, "restore_record": record.restore_record}


class AppealIn(BaseModel):
    reason: str


@router.post("/organizations/{org_id}/governance/{record_id}/appeal")
async def appeal_takedown(
    org_id: uuid.UUID,
    record_id: uuid.UUID,
    body: AppealIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    record = await svc.appeal_takedown(db, org, current_user, record_id, body.reason)
    await db.commit()
    return {"id": str(record.id), "appeal_status": record.appeal_status}


# ---------- 举报与申诉 ----------
class ReportCreateIn(BaseModel):
    target_type: str
    target_id: str
    reason: str
    description: str | None = None
    evidence: str | None = None


@router.post("/organizations/{org_id}/reports")
async def create_report(
    org_id: uuid.UUID,
    body: ReportCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    report = await svc.create_report_ex(db, org, current_user, body.target_type, body.target_id, body.reason, body.description, body.evidence)
    await db.commit()
    return {"id": str(report.id), "status": report.status, "risk_level": report.risk_level}


class ReportHandleIn(BaseModel):
    decision: str
    risk_level: str | None = None
    note: str | None = None


@router.post("/organizations/{org_id}/reports/{report_id}/handle")
async def handle_report(
    org_id: uuid.UUID,
    report_id: uuid.UUID,
    body: ReportHandleIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    report = await svc.handle_report_ex(db, org, current_user, report_id, body.decision, body.risk_level, body.note)
    await db.commit()
    return {"id": str(report.id), "status": report.status, "risk_level": report.risk_level, "handled_by": str(report.handled_by) if report.handled_by else None}


@router.post("/organizations/{org_id}/reports/{report_id}/appeal")
async def appeal_report(
    org_id: uuid.UUID,
    report_id: uuid.UUID,
    body: AppealIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    report = await svc.appeal_report(db, org, current_user, report_id, body.reason)
    await db.commit()
    return {"id": str(report.id), "appeal_status": report.appeal_status}


# ---------- 版权管理 ----------
class CopyrightRegisterIn(BaseModel):
    owner_type: str
    copyright_owner: str
    co_authors: list | None = None
    authorization_scope: dict | str | None = None
    start_at: str | None = None
    end_at: str | None = None
    repost_permission: bool = False
    commercial_permission: bool = False
    download_restricted: bool = True
    watermark: bool = False


@router.post("/organizations/{org_id}/projects/{project_id}/copyright")
async def register_copyright(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: CopyrightRegisterIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    record = await svc.register_copyright(
        db, org, current_user, project_id, body.owner_type, body.copyright_owner,
        body.co_authors, body.authorization_scope, body.start_at, body.end_at,
        body.repost_permission, body.commercial_permission, body.download_restricted, body.watermark,
    )
    await db.commit()
    return {"id": str(record.id), "owner_type": record.owner_type, "status": record.status}


class CopyrightClaimIn(BaseModel):
    claim_type: str
    description: str


@router.post("/organizations/{org_id}/projects/{project_id}/copyright-claims")
async def create_copyright_claim(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: CopyrightClaimIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    claim = await svc.create_copyright_claim(db, org, current_user, project_id, body.claim_type, body.description)
    await db.commit()
    return {"id": str(claim.id), "claim_type": claim.claim_type, "status": claim.status}


class CopyrightClaimHandleIn(BaseModel):
    decision: str
    note: str | None = None


@router.post("/organizations/{org_id}/copyright-claims/{claim_id}/handle")
async def handle_copyright_claim(
    org_id: uuid.UUID,
    claim_id: uuid.UUID,
    body: CopyrightClaimHandleIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    claim = await svc.handle_copyright_claim(db, org, current_user, claim_id, body.decision, body.note)
    await db.commit()
    return {"id": str(claim.id), "status": claim.status, "handled_by": str(claim.handled_by) if claim.handled_by else None}


# ---------- 发布 ----------
class PublishIn(BaseModel):
    published_to: str = "internal"
    require_second_review: bool = False
    allow_edit_published: bool = False


@router.post("/organizations/{org_id}/projects/{project_id}/publish")
async def publish_project(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: PublishIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    project = await svc.publish_project(db, org, current_user, project_id, body.published_to, body.require_second_review, body.allow_edit_published)
    await db.commit()
    return {"id": str(project.id), "collab_status": project.collab_status}
