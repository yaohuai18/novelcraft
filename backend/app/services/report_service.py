# 举报服务（第十五批「通知、评论与协作反馈」规格实现）
# 举报创建（同目标 pending 去重）、列表、处理（reviewing/resolved/rejected）。
import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import Comment
from app.models.project import Project
from app.models.report import REPORT_STATUSES, Report
from app.models.user import User


class ReportError(ValueError):
    pass


async def create_report(
    db: AsyncSession,
    reporter: User,
    target_type: str,
    target_id: str,
    reason: str,
) -> Report:
    """创建举报；同一举报人对同一目标已有 pending 举报时拒绝。"""
    existing = (
        await db.execute(
            select(Report).where(
                Report.reporter_id == reporter.id,
                Report.target_type == target_type,
                Report.target_id == target_id,
                Report.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ReportError("该内容已在审核中，请勿重复举报")

    report = Report(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        status="pending",
    )
    db.add(report)
    await db.flush()
    return report


async def list_reports(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    target_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Report], int]:
    """举报列表：可按目标项目过滤（通过 comment/project 目标反查 project_id）。"""
    filters: list = []
    if project_id is not None:
        comment_targets = (
            await db.execute(
                select(Comment).where(Comment.project_id == project_id)
            )
        ).scalars().all()
        comment_ids = {str(c.id) for c in comment_targets}
        conds = [
            (Report.target_type == "project")
            & (Report.target_id == str(project_id))
        ]
        if comment_ids:
            conds.append(
                (Report.target_type == "comment")
                & (Report.target_id.in_(comment_ids))
            )
        filters.append(or_(*conds))
    if status:
        filters.append(Report.status == status)
    if target_type:
        filters.append(Report.target_type == target_type)

    total = (
        await db.execute(select(func.count(Report.id)).where(*filters))
    ).scalar_one()
    result = await db.execute(
        select(Report)
        .where(*filters)
        .order_by(Report.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def handle_report(
    db: AsyncSession,
    report: Report,
    handler: User,
    new_status: str,
) -> Report:
    """处理举报：reviewing/resolved/rejected；记录处理人与时间。"""
    if new_status not in REPORT_STATUSES:
        raise ReportError(f"非法举报状态: {new_status}")
    if new_status == "pending":
        raise ReportError("不能回退到待处理")
    report.status = new_status
    report.handled_by = handler.id
    report.handled_at = datetime.utcnow()
    await db.flush()
    return report
