# 报表导出服务（第十七批「统计分析与运营后台」规格十八/二十一）
# 支持报表类型：users（用户增长）/ content（内容发布）/ reading（阅读数据）/
#              ai（AI 消耗）/ errors（错误统计）/ reports（举报处理）。
# 约束：时间范围限制、数据量限制（page_size 上限）、权限检查（super_admin/operator/analyst）、
#       用户信息脱敏（mask_username / mask_email）、导出操作审计。
import csv
import io
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import Report
from app.models.user import User
from app.services.audit_service import record as record_audit
from app.services.monitor_service import metrics
from app.utils.text import mask_email, mask_username

REPORT_TYPES = ["users", "content", "reading", "ai", "errors", "reports"]
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200

# 允许导出报表的角色
EXPORT_ALLOWED_ROLES = {"super_admin", "operator", "analyst"}


class ReportExportError(ValueError):
    pass


def _check_export_permission(actor: User) -> None:
    if actor.role not in EXPORT_ALLOWED_ROLES:
        raise ReportExportError("无权限导出报表（需要 super_admin/operator/analyst）")


def _mask_user_fields(rows: list[dict]) -> list[dict]:
    """用户信息脱敏（规格十八/二十）。"""
    masked = []
    for row in rows:
        row = dict(row)
        if "username" in row and row["username"]:
            row["username"] = mask_username(row["username"])
        if "email" in row and row["email"]:
            row["email"] = mask_email(row["email"])
        masked.append(row)
    return masked


def _build_csv(headers: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


async def export_report(
    db: AsyncSession,
    *,
    actor: User,
    report_type: str,
    from_date: date | None = None,
    to_date: date | None = None,
    fmt: str = "csv",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """导出统计报表（CSV）。"""
    _check_export_permission(actor)
    if report_type not in REPORT_TYPES:
        raise ReportExportError(f"非法报表类型: {report_type}")
    if fmt != "csv":
        raise ReportExportError("当前仅支持 CSV 格式导出")
    if page_size > MAX_PAGE_SIZE:
        raise ReportExportError(f"单次导出数据量不能超过 {MAX_PAGE_SIZE} 条")
    from_date = from_date or date.today() - timedelta(days=29)
    to_date = to_date or date.today()
    if from_date > to_date:
        raise ReportExportError("时间范围非法")

    headers, rows = await _collect_rows(
        db, report_type, from_date, to_date, page, page_size
    )
    rows = _mask_user_fields(rows)
    content = _build_csv(headers, rows)

    await record_audit(
        db,
        actor_id=actor.id,
        action="report.export",
        target_type="report",
        target_id=report_type,
        reason=f"导出 {report_type} 报表，范围 {from_date.isoformat()} ~ {to_date.isoformat()}，{len(rows)} 条",
        ip_hash=ip_hash,
        user_agent=user_agent,
    )
    return {
        "report_type": report_type,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "format": fmt,
        "rows": len(rows),
        "content": content,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }


async def _collect_rows(
    db: AsyncSession,
    report_type: str,
    from_date: date,
    to_date: date,
    page: int,
    page_size: int,
) -> tuple[list[str], list[dict]]:
    start = datetime.combine(from_date, datetime.min.time())
    end = datetime.combine(to_date, datetime.max.time())

    if report_type == "users":
        users = (
            await db.execute(
                select(User).where(User.created_at >= start, User.created_at <= end)
                .order_by(User.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return (
            ["user_id", "username", "email", "role", "created_at"],
            [{
                "user_id": str(u.id), "username": u.username, "email": u.email,
                "role": u.role, "created_at": u.created_at.isoformat() + "Z",
            } for u in users],
        )

    if report_type == "reports":
        reports = (
            await db.execute(
                select(Report).where(Report.created_at >= start, Report.created_at <= end)
                .order_by(Report.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return (
            ["report_id", "target_type", "target_id", "reason", "status", "created_at"],
            [{
                "report_id": str(r.id), "target_type": r.target_type,
                "target_id": r.target_id, "reason": r.reason,
                "status": r.status, "created_at": r.created_at.isoformat() + "Z",
            } for r in reports],
        )

    if report_type == "reading":
        from app.models.content_daily_stat import ContentDailyStat
        stats = (
            await db.execute(
                select(ContentDailyStat)
                .where(ContentDailyStat.stat_date >= from_date, ContentDailyStat.stat_date <= to_date)
                .order_by(ContentDailyStat.stat_date.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return (
            ["project_id", "target_type", "target_id", "stat_date", "view_count",
             "unique_reader_count", "favorite_count", "comment_count", "completion_count"],
            [{
                "project_id": str(s.project_id), "target_type": s.target_type,
                "target_id": s.target_id, "stat_date": s.stat_date.isoformat(),
                "view_count": s.view_count, "unique_reader_count": s.unique_reader_count,
                "favorite_count": s.favorite_count, "comment_count": s.comment_count,
                "completion_count": s.completion_count,
            } for s in stats],
        )

    if report_type == "ai":
        from app.models.ai_usage_daily_stat import AIUsageDailyStat
        rows = (
            await db.execute(
                select(AIUsageDailyStat)
                .where(AIUsageDailyStat.stat_date >= from_date, AIUsageDailyStat.stat_date <= to_date)
                .order_by(AIUsageDailyStat.stat_date.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return (
            ["stat_date", "project_id", "provider", "model", "task_type", "request_count",
             "success_count", "input_tokens", "output_tokens", "estimated_cost"],
            [{
                "stat_date": r.stat_date.isoformat(), "project_id": str(r.project_id),
                "provider": r.provider, "model": r.model, "task_type": r.task_type,
                "request_count": r.request_count, "success_count": r.success_count,
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "estimated_cost": r.estimated_cost,
            } for r in rows],
        )

    if report_type == "content":
        from app.models.project import Project
        projects = (
            await db.execute(
                select(Project).where(Project.created_at >= start, Project.created_at <= end)
                .order_by(Project.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return (
            ["project_id", "title", "publication_status", "word_count", "chapter_count",
             "view_count", "favorite_count", "created_at"],
            [{
                "project_id": str(p.id), "title": p.title,
                "publication_status": p.publication_status, "word_count": p.word_count,
                "chapter_count": p.chapter_count, "view_count": p.view_count,
                "favorite_count": p.favorite_count, "created_at": p.created_at.isoformat() + "Z",
            } for p in projects],
        )

    if report_type == "errors":
        proc = metrics.snapshot()
        return (
            ["metric", "value", "updated_at"],
            [{
                "metric": "request_total", "value": proc["request_total"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }, {
                "metric": "error_total", "value": proc["error_total"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }, {
                "metric": "error_rate", "value": proc["error_rate"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }, {
                "metric": "average_response_ms", "value": proc["average_response_ms"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }],
        )

    raise ReportExportError(f"未知报表类型: {report_type}")
