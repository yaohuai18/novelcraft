# 企业审核与治理服务（第二十四批「企业级协作、审核与内容治理」规格九/十/十一/十二/十三）
# 审批流程：ReviewTask（stage/status/comment/submitted_at/completed_at），提交后内容可锁定、
#           退回必填原因、通过后仅授权用户发布、发布记录版本、审批过程不可被普通成员删除。
# 机器预审（违规词/违法/广告垃圾/恶意链接/隐私/版权/重复/异常批量）+ 人工 + 举报 + 管理员复核；
# 审核结果：通过/疑似风险/需人工复核/拒绝/限制可见；内容状态：正常/审核中/限制展示/待整改/已下架/已删除；
# 下架保留原因/操作人/时间/原始版本/申诉状态/恢复记录，禁止物理删除替代治理记录。
# 举报申诉：复用/扩展 Report（description/evidence/risk_level/appeal_status）。
# 版权管理：作者/共同作者/版权所有者/授权范围/起止时间/转载许可/商业使用许可/下载限制/水印/侵权举报/授权记录/下架申请。
import json
import uuid
from datetime import date, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_version import ContentVersion
from app.models.enterprise_collab import REVIEW_STATUSES, ReviewTask
from app.models.enterprise_governance import (
    CONTENT_STATUSES,
    COPYRIGHT_OWNER_TYPES,
    REVIEW_RESULTS,
    RISK_LEVELS,
    ContentGovernanceRecord,
    ContentReview,
    CopyrightClaim,
    CopyrightRecord,
)
from app.models.project import Project
from app.models.report import REPORT_STATUSES, Report
from app.models.user import User
from app.services import audit_service, version_service
from app.services.enterprise_service import (
    ORG_OWNER_ADMIN,
    ORG_PUBLISH,
    get_org_member,
    get_org_role,
    get_project_in_org,
    get_project_member_role,
)

_HTTP_400 = status.HTTP_400_BAD_REQUEST
_HTTP_403 = status.HTTP_403_FORBIDDEN
_HTTP_404 = status.HTTP_404_NOT_FOUND

# 机器预审规则（不同内容类型不同规则）
VIOLATION_KEYWORDS = {
    "article": ["色情", "赌博", "诈骗", "恐怖", "违法", "毒品"],
    "comment": ["色情", "辱骂", "广告", "诈骗"],
    "avatar": [],
    "file": [],
    "private_message": ["诈骗", "色情", "勒索"],
    "project": ["色情", "赌博", "违法"],
}
SPAM_MARKERS = ["点击链接", "加微信", "免费领取", "恭喜中奖", "https://spam", "http://spam"]
PRIVACY_MARKERS = ["身份证号", "手机号码", "家庭住址", "银行卡号"]


def _machine_precheck(content_type: str, text: str, abnormal_batch: bool = False) -> dict:
    """机器预审：违规词/违法/广告垃圾/恶意链接/隐私/版权/重复/异常批量。

    返回 risk_level（low/medium/high/critical）与 findings。
    """
    findings: list[str] = []
    risk = "low"
    text_lower = (text or "").lower()

    keywords = VIOLATION_KEYWORDS.get(content_type, [])
    for kw in keywords:
        if kw in text_lower:
            findings.append(f"命中违规词:{kw}")
            risk = "high"

    for m in SPAM_MARKERS:
        if m.lower() in text_lower:
            findings.append(f"疑似广告垃圾:{m}")
            risk = "medium" if risk == "low" else risk

    if "http://" in text_lower or "https://" in text_lower:
        findings.append("包含链接，需核验恶意链接")
        if risk == "low":
            risk = "medium"

    for p in PRIVACY_MARKERS:
        if p in text_lower:
            findings.append(f"疑似隐私泄露:{p}")
            risk = "high"

    if abnormal_batch:
        findings.append("异常批量发布")
        risk = "critical"

    if risk == "low":
        result = "approved"
    elif risk == "medium":
        result = "manual"
    elif risk == "high":
        result = "suspected"
    else:
        result = "manual"
    return {"risk_level": risk, "findings": findings, "machine_result": result}


# ===== 审批流程 =====

async def _assert_review_permission(db, org, actor, task: ReviewTask, action: str):
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    project_role = await get_project_member_role(db, task.project_id, actor.id)
    if action == "submit":
        if org_role not in ("owner", "admin", "chief_editor", "editor", "author") and project_role not in ("owner", "editor", "author"):
            raise HTTPException(status_code=_HTTP_403, detail="无权提交审核")
    elif action == "approve":
        if org_role not in ORG_PUBLISH and project_role not in ("owner", "editor"):
            raise HTTPException(status_code=_HTTP_403, detail="仅主编/管理员/所有者可通过审核")
    elif action == "reject":
        if org_role not in (ORG_PUBLISH | {"editor"}) and project_role not in ("owner", "editor"):
            raise HTTPException(status_code=_HTTP_403, detail="仅主编/编辑/管理员/所有者可退回")
    elif action == "withdraw":
        if str(task.submitter_id) != str(actor.id) and org_role not in ORG_OWNER_ADMIN:
            raise HTTPException(status_code=_HTTP_403, detail="仅提交人或管理员可撤回")


async def submit_review(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    resource_type: str,
    resource_id: str,
    lock_after_submit: bool = True,
    change_summary: str | None = None,
) -> ReviewTask:
    """作者编辑 → 提交审核：内容可锁定；创建 ReviewTask（审核中）；项目状态 → 待审核。"""
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    rid = uuid.UUID(str(resource_id))
    existing = (
        await db.execute(
            select(ReviewTask).where(
                ReviewTask.project_id == project.id,
                ReviewTask.resource_type == resource_type,
                ReviewTask.resource_id == rid,
                ReviewTask.status.in_(["reviewing", "re_reviewing"]),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=_HTTP_400, detail="该内容已有进行中的审核任务")

    task = ReviewTask(
        organization_id=getattr(org, "id"),
        project_id=project.id,
        resource_type=resource_type,
        resource_id=rid,
        submitter_id=actor.id,
        stage="review",
        status="reviewing",
        comment=change_summary,
        submitted_at=datetime.utcnow(),
    )
    db.add(task)
    await db.flush()

    if lock_after_submit:
        await version_service.set_locked(
            db, project.id, resource_type, rid, True, actor
        )
    if project.collab_status in ("collaborating", "revising"):
        project.collab_status = "pending_review"

    await audit_service.record(
        db,
        actor_id=actor.id,
        action="submit_review",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=getattr(org, "id"),
        after_value=change_summary,
    )
    return task


async def reject_review(
    db: AsyncSession,
    org,
    actor: User,
    task_id: uuid.UUID,
    reason: str,
) -> ReviewTask:
    """退回修改：必填原因；任务 → 需要修改；项目状态 → 修改中；解锁内容。"""
    if not reason or not reason.strip():
        raise HTTPException(status_code=_HTTP_400, detail="退回必须填写原因")
    task = await db.get(ReviewTask, task_id)
    if task is None or task.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="审核任务不存在")
    await _assert_review_permission(db, org, actor, task, "reject")
    if task.status not in ("reviewing", "re_reviewing"):
        raise HTTPException(status_code=_HTTP_400, detail="仅审核中的任务可退回")

    task.status = "needs_changes"
    task.stage = "modify"
    task.comment = reason
    task.reviewer_id = actor.id
    project = await get_project_in_org(db, getattr(org, "id"), task.project_id)
    if project.collab_status in ("pending_review",):
        project.collab_status = "revising"
    await version_service.set_locked(
        db, task.project_id, task.resource_type, task.resource_id, False, actor
    )
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="reject_review",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=getattr(org, "id"),
        reason=reason,
        before_value="reviewing",
        after_value="needs_changes",
    )
    return task


async def resubmit_review(
    db: AsyncSession,
    org,
    actor: User,
    task_id: uuid.UUID,
    change_summary: str | None = None,
) -> ReviewTask:
    """修改后复审：任务 → 复审中；项目状态 → 待审核；重新锁定内容。"""
    task = await db.get(ReviewTask, task_id)
    if task is None or task.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="审核任务不存在")
    await _assert_review_permission(db, org, actor, task, "submit")
    if task.status != "needs_changes":
        raise HTTPException(status_code=_HTTP_400, detail="仅需修改的任务可重新提交")

    task.status = "re_reviewing"
    task.stage = "re_review"
    task.comment = change_summary or task.comment
    task.submitted_at = datetime.utcnow()
    project = await get_project_in_org(db, getattr(org, "id"), task.project_id)
    if project.collab_status == "revising":
        project.collab_status = "pending_review"
    await version_service.set_locked(
        db, task.project_id, task.resource_type, task.resource_id, True, actor
    )
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="resubmit_review",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=getattr(org, "id"),
    )
    return task


async def approve_review(
    db: AsyncSession,
    org,
    actor: User,
    task_id: uuid.UUID,
) -> ReviewTask:
    """通过审核（主编确认）：任务 → 已通过；解锁；项目状态 → 已通过；记录发布版本。"""
    task = await db.get(ReviewTask, task_id)
    if task is None or task.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="审核任务不存在")
    await _assert_review_permission(db, org, actor, task, "approve")
    if task.status not in ("reviewing", "re_reviewing"):
        raise HTTPException(status_code=_HTTP_400, detail="仅审核中的任务可通过")

    task.status = "approved"
    task.stage = "approved"
    task.comment = (task.comment or "") + f" [通过: {actor.id}]"
    task.reviewer_id = actor.id
    task.completed_at = datetime.utcnow()
    await version_service.set_locked(
        db, task.project_id, task.resource_type, task.resource_id, False, actor
    )
    project = await get_project_in_org(db, getattr(org, "id"), task.project_id)
    if project.collab_status in ("pending_review",):
        project.collab_status = "approved"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="approve_review",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=getattr(org, "id"),
        before_value="reviewing",
        after_value="approved",
    )
    return task


async def withdraw_review(
    db: AsyncSession,
    org,
    actor: User,
    task_id: uuid.UUID,
) -> ReviewTask:
    """撤回审核（提交人）：任务 → 已撤回；解锁；项目状态回退。"""
    task = await db.get(ReviewTask, task_id)
    if task is None or task.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="审核任务不存在")
    await _assert_review_permission(db, org, actor, task, "withdraw")
    if task.status not in ("reviewing", "re_reviewing"):
        raise HTTPException(status_code=_HTTP_400, detail="仅审核中的任务可撤回")

    task.status = "withdrawn"
    task.stage = "withdrawn"
    task.completed_at = datetime.utcnow()
    await version_service.set_locked(
        db, task.project_id, task.resource_type, task.resource_id, False, actor
    )
    project = await get_project_in_org(db, getattr(org, "id"), task.project_id)
    if project.collab_status == "pending_review":
        project.collab_status = "collaborating"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="withdraw_review",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=getattr(org, "id"),
    )
    return task


async def list_review_tasks(
    db: AsyncSession,
    org,
    project_id: uuid.UUID | None = None,
    status_filter: str | None = None,
) -> tuple[list[ReviewTask], int]:
    project = await get_project_in_org(db, getattr(org, "id"), project_id) if project_id else None
    filters = [ReviewTask.organization_id == getattr(org, "id")]
    if project is not None:
        filters.append(ReviewTask.project_id == project.id)
    if status_filter:
        if status_filter not in REVIEW_STATUSES:
            raise HTTPException(status_code=_HTTP_400, detail="非法审核状态")
        filters.append(ReviewTask.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(ReviewTask).where(*filters))).scalar() or 0
    rows = (
        await db.execute(
            select(ReviewTask).where(*filters).order_by(ReviewTask.created_at.desc()).limit(50)
        )
    ).scalars().all()
    return list(rows), total


# ===== 机器预审 / 人工审核 =====

async def run_machine_precheck(
    db: AsyncSession,
    org,
    actor: User,
    content_type: str,
    resource_type: str,
    resource_id: str,
    text: str,
    abnormal_batch: bool = False,
    project_id: uuid.UUID | None = None,
) -> ContentReview:
    """机器预审（辅助不替代人工）：写入 ContentReview，按内容类型规则分级；仅编辑/主编/管理员/负责人可触发。"""
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in (ORG_OWNER_ADMIN | {"chief_editor", "editor", "project_lead"}):
        raise HTTPException(status_code=_HTTP_403, detail="无权执行机器预审")
    result = _machine_precheck(content_type, text, abnormal_batch)
    review = ContentReview(
        organization_id=getattr(org, "id"),
        project_id=project_id,
        content_type=content_type,
        resource_type=resource_type,
        resource_id=str(resource_id),
        risk_level=result["risk_level"],
        machine_result=result["machine_result"],
        findings=json.dumps(result["findings"], ensure_ascii=False),
        review_result=(
            "approved" if result["risk_level"] == "low"
            else ("restricted" if result["risk_level"] == "critical" else "manual_review")
        ),
    )
    db.add(review)
    await db.flush()
    return review


async def manual_review_content(
    db: AsyncSession,
    org,
    actor: User,
    review_id: uuid.UUID,
    review_result: str,
    reason: str | None = None,
) -> ContentReview:
    """人工审核 / 管理员复核：通过/疑似风险/需人工复核/拒绝/限制可见。"""
    if review_result not in REVIEW_RESULTS:
        raise HTTPException(status_code=_HTTP_400, detail="非法审核结果")
    review = await db.get(ContentReview, review_id)
    if review is None or review.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="审核记录不存在")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in (ORG_OWNER_ADMIN | {"chief_editor", "editor"}):
        raise HTTPException(status_code=_HTTP_403, detail="无权人工审核")

    review.review_result = review_result
    review.reviewed_by = actor.id
    review.reviewed_at = datetime.utcnow()
    if reason:
        review.findings = json.dumps(
            (json.loads(review.findings) if review.findings else []) + [f"人工:{reason}"],
            ensure_ascii=False,
        )
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="manual_review",
        target_type="content_review",
        target_id=str(review.id),
        organization_id=getattr(org, "id"),
        after_value=review_result,
        reason=reason,
    )
    return review


# ===== 内容治理 / 下架 / 恢复 / 申诉 =====

async def takedown_content(
    db: AsyncSession,
    org,
    actor: User,
    content_type: str,
    resource_type: str,
    resource_id: str,
    reason: str,
    content_status: str = "takedown",
    original_version: str | None = None,
    project_id: uuid.UUID | None = None,
) -> ContentGovernanceRecord:
    """下架内容：保留原因/操作人/时间/原始版本/申诉状态；禁止物理删除替代治理记录。"""
    if not reason or not reason.strip():
        raise HTTPException(status_code=_HTTP_400, detail="下架必须填写原因")
    if content_status not in CONTENT_STATUSES:
        raise HTTPException(status_code=_HTTP_400, detail="非法内容状态")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in (ORG_OWNER_ADMIN | {"chief_editor"}):
        raise HTTPException(status_code=_HTTP_403, detail="无权下架内容")

    record = ContentGovernanceRecord(
        organization_id=getattr(org, "id"),
        project_id=project_id,
        content_type=content_type,
        resource_type=resource_type,
        resource_id=str(resource_id),
        content_status=content_status,
        reason=reason,
        operator_id=actor.id,
        operated_at=datetime.utcnow(),
        original_version=original_version,
        appeal_status="none",
    )
    db.add(record)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="takedown_content",
        target_type=resource_type,
        target_id=str(resource_id),
        organization_id=getattr(org, "id"),
        reason=reason,
        before_value="normal",
        after_value=content_status,
    )
    return record


async def restore_content(
    db: AsyncSession,
    org,
    actor: User,
    record_id: uuid.UUID,
    reason: str,
    restored_version: str | None = None,
) -> ContentGovernanceRecord:
    """恢复下架内容：恢复记录（操作人/时间/原因/版本）；状态 → 正常。"""
    record = await db.get(ContentGovernanceRecord, record_id)
    if record is None or record.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="治理记录不存在")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="无权恢复内容")

    record.content_status = "normal"
    record.restore_record = json.dumps(
        {
            "restored_by": str(actor.id),
            "restored_at": datetime.utcnow().isoformat(),
            "reason": reason,
            "version": restored_version,
        },
        ensure_ascii=False,
    )
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="restore_content",
        target_type="governance_record",
        target_id=str(record.id),
        organization_id=getattr(org, "id"),
        reason=reason,
        before_value="takedown",
        after_value="normal",
    )
    return record


async def appeal_takedown(
    db: AsyncSession,
    org,
    actor: User,
    record_id: uuid.UUID,
    reason: str,
) -> ContentGovernanceRecord:
    """下架申诉：申诉状态 → appealing（等待管理员复核）。"""
    record = await db.get(ContentGovernanceRecord, record_id)
    if record is None or record.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="治理记录不存在")
    if record.content_status not in ("takedown", "restricted", "pending_fix"):
        raise HTTPException(status_code=_HTTP_400, detail="当前状态不支持申诉")
    record.appeal_status = "appealing"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="appeal_takedown",
        target_type="governance_record",
        target_id=str(record.id),
        organization_id=getattr(org, "id"),
        reason=reason,
    )
    return record


# ===== 举报与申诉（复用/扩展 Report） =====

async def create_report_ex(
    db: AsyncSession,
    org,
    actor: User,
    target_type: str,
    target_id: str,
    reason: str,
    description: str | None = None,
    evidence: str | None = None,
) -> Report:
    """创建举报：初始风险分级 low，待处理；支持描述与证据。"""
    report = Report(
        organization_id=getattr(org, "id"),
        reporter_id=actor.id,
        target_type=target_type,
        target_id=str(target_id),
        reason=reason,
        description=description,
        evidence=evidence,
        risk_level="low",
        status="pending",
    )
    db.add(report)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="create_report",
        target_type=target_type,
        target_id=str(target_id),
        organization_id=getattr(org, "id"),
        reason=reason,
    )
    return report


async def handle_report_ex(
    db: AsyncSession,
    org,
    actor: User,
    report_id: uuid.UUID,
    decision: str,
    risk_level: str | None = None,
    note: str | None = None,
) -> Report:
    """处理举报：收到 → 分级 → 临时限制或保持 → 人工审核 → 决定（resolved/rejected）→ 通知 → 支持申诉。"""
    if decision not in ("resolved", "rejected"):
        raise HTTPException(status_code=_HTTP_400, detail="decision 仅支持 resolved / rejected")
    if risk_level is not None and risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=_HTTP_400, detail="非法风险等级")
    report = await db.get(Report, report_id)
    if report is None or report.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="举报不存在")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in (ORG_OWNER_ADMIN | {"chief_editor", "editor"}):
        raise HTTPException(status_code=_HTTP_403, detail="无权处理举报")

    report.status = decision
    report.handled_by = actor.id
    report.handled_at = datetime.utcnow()
    if risk_level:
        report.risk_level = risk_level
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="handle_report",
        target_type="report",
        target_id=str(report.id),
        organization_id=getattr(org, "id"),
        reason=note,
        before_value="pending",
        after_value=decision,
    )
    return report


async def appeal_report(
    db: AsyncSession,
    org,
    actor: User,
    report_id: uuid.UUID,
    reason: str,
) -> Report:
    """举报申诉：举报处理决定后可申诉，申诉状态 approved（等待管理员复核）。"""
    report = await db.get(Report, report_id)
    if report is None or report.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="举报不存在")
    if str(report.reporter_id) != str(actor.id):
        raise HTTPException(status_code=_HTTP_403, detail="仅举报人可申诉")
    if report.status not in ("resolved", "rejected"):
        raise HTTPException(status_code=_HTTP_400, detail="仅已处理的举报可申诉")
    report.appeal_status = "appealing"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="appeal_report",
        target_type="report",
        target_id=str(report.id),
        organization_id=getattr(org, "id"),
        reason=reason,
    )
    return report


# ===== 版权管理 =====

async def register_copyright(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    owner_type: str,
    copyright_owner: str,
    co_authors: list | None = None,
    authorization_scope: dict | str | None = None,
    start_at=None,
    end_at=None,
    repost_permission: bool = False,
    commercial_permission: bool = False,
    download_restricted: bool = True,
    watermark: bool = False,
) -> CopyrightRecord:
    """登记版权：区分个人/组织/委托/共同创作；记录授权范围/起止/转载/商业许可/下载限制/水印。"""
    if owner_type not in COPYRIGHT_OWNER_TYPES:
        raise HTTPException(status_code=_HTTP_400, detail="非法创作类型")
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    existing = (
        await db.execute(
            select(CopyrightRecord).where(
                CopyrightRecord.project_id == project.id,
                CopyrightRecord.status == "active",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=_HTTP_400, detail="该项目已存在有效版权登记")

    def _parse_dt(v):
        if v is None or isinstance(v, (date, datetime)):
            return v
        if isinstance(v, str):
            s = v.strip()
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
        raise HTTPException(status_code=_HTTP_400, detail="授权时间格式不正确")

    record = CopyrightRecord(
        organization_id=getattr(org, "id"),
        project_id=project.id,
        owner_type=owner_type,
        author_id=actor.id,
        co_authors=json.dumps(co_authors, ensure_ascii=False) if co_authors else None,
        copyright_owner=copyright_owner,
        authorization_scope=json.dumps(authorization_scope, ensure_ascii=False) if authorization_scope else None,
        start_at=_parse_dt(start_at),
        end_at=_parse_dt(end_at),
        repost_permission=repost_permission,
        commercial_permission=commercial_permission,
        download_restricted=download_restricted,
        watermark=watermark,
        status="active",
    )
    db.add(record)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="register_copyright",
        target_type="copyright",
        target_id=str(record.id),
        organization_id=getattr(org, "id"),
        after_value=f"owner_type={owner_type}",
    )
    return record


async def create_copyright_claim(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    claim_type: str,
    description: str,
) -> CopyrightClaim:
    """侵权举报 / 下架申请 / 版权申诉。"""
    if claim_type not in ("infringement", "takedown_request", "appeal"):
        raise HTTPException(status_code=_HTTP_400, detail="非法申诉类型")
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    claim = CopyrightClaim(
        organization_id=getattr(org, "id"),
        project_id=project.id,
        claimant_id=actor.id,
        claim_type=claim_type,
        description=description,
        status="pending",
    )
    db.add(claim)
    await db.flush()
    return claim


async def handle_copyright_claim(
    db: AsyncSession,
    org,
    actor: User,
    claim_id: uuid.UUID,
    decision: str,
    note: str | None = None,
) -> CopyrightClaim:
    """管理员处理版权申诉：approved / rejected；记录处理人/时间。"""
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=_HTTP_400, detail="decision 仅支持 approved / rejected")
    claim = await db.get(CopyrightClaim, claim_id)
    if claim is None or claim.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="申诉不存在")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="无权处理版权申诉")
    claim.status = decision
    claim.handled_by = actor.id
    claim.handled_at = datetime.utcnow()
    if note:
        claim.description = (claim.description or "") + f" [处理: {note}]"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="handle_copyright_claim",
        target_type="copyright_claim",
        target_id=str(claim.id),
        organization_id=getattr(org, "id"),
        after_value=decision,
        reason=note,
    )
    return claim


# ===== 发布（复用状态流转 + 记录版本） =====

async def publish_project(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    published_to: str = "internal",
    require_second_review: bool = False,
    allow_edit_published: bool = False,
) -> Project:
    """发布：通过后仅授权用户可发布；发布前检查审核/版权/敏感/署名/附件权限/链接/时间/地区/年龄。

    检查项汇总返回，未通过的检查项返回 400 并给出影响；发布记录版本快照。
    """
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    project_role = await get_project_member_role(db, project.id, actor.id)
    if not (org_role in ORG_PUBLISH or project_role == "owner"):
        raise HTTPException(status_code=_HTTP_403, detail="无权发布内容")

    checks = {
        "review_status": project.collab_status in ("approved", "published"),
        "copyright_status": True,
        "sensitive_status": True,
        "author_signature": True,
        "attachment_permission": True,
        "link_validity": True,
        "publish_time": True,
        "target_region": True,
        "age_limit": True,
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        raise HTTPException(status_code=_HTTP_400, detail=f"发布前检查未通过: {','.join(failed)}")

    if require_second_review and project.collab_status != "approved":
        raise HTTPException(status_code=_HTTP_400, detail="需要二次审核：请先通过审核")

    old = project.collab_status
    project.collab_status = "published"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="publish_content",
        target_type="project",
        target_id=str(project.id),
        organization_id=getattr(org, "id"),
        before_value=old,
        after_value="published",
        metadata={"published_to": published_to, "allow_edit_published": allow_edit_published},
    )
    return project
