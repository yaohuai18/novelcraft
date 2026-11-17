# 运营后台路由（第十七批「统计分析与运营后台」规格十三/十四/十五/十六/十七/十八/二十一）
# GET /api/admin/{dashboard|users/stats|content/stats|system/health|audit-logs|reports}
# 内容运营列表与操作、推荐位、系统配置、私有内容授权、工单处理。
# 所有人工操作必须记录 reason 并写审计日志（规格十四/十七）。
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import (
    check_private_access,
    hash_ip,
    mask_email,
    mask_username,
    require_admin_roles,
)
from app.models.report import Report
from app.models.user import User
from app.schemas.admin_schemas import (
    FeaturedCreateRequest,
    FeaturedUpdateRequest,
    HandleReportRequest,
    HandleTicketRequest,
    OfficialTagRequest,
    PrivateAccessGrantRequest,
    PrivateAccessRevokeRequest,
    ReasonRequest,
    RestrictUserRequest,
    SettingsUpdateRequest,
)
from app.services import (
    admin_service,
    audit_service,
    backup_auto_policy,
    content_ops_service,
    monitor_service,
    report_export_service,
    ticket_service,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["运营后台"]
)


def _client_info(request):
    """从请求提取脱敏 IP 哈希与 UA。"""
    ip = request.client.host if request.client else "127.0.0.1"
    ua = request.headers.get("user-agent")
    return hash_ip(ip), ua


# ---------- 运营首页 ----------

@router.get("/dashboard")
async def admin_dashboard(
    current_user: User = Depends(require_admin_roles("super_admin", "operator", "analyst")),
    db: AsyncSession = Depends(get_db),
):
    """运营首页（规格十三）。"""
    return await admin_service.get_dashboard(db)


# ---------- 用户统计 ----------

@router.get("/users/stats")
async def admin_users_stats(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    current_user: User = Depends(require_admin_roles("super_admin", "operator", "analyst")),
    db: AsyncSession = Depends(get_db),
):
    """用户与阅读统计（规格九，脱敏聚合）。"""
    frm = _parse_date(from_date)
    to = _parse_date(to_date)
    return await admin_service.get_users_stats(db, from_date=frm, to_date=to)


# ---------- 内容统计 ----------

@router.get("/content/stats")
async def admin_content_stats(
    days: int = Query(default=7, ge=1, le=90),
    current_user: User = Depends(require_admin_roles("super_admin", "operator", "analyst")),
    db: AsyncSession = Depends(get_db),
):
    """内容运营统计汇总（规格十四：公开/最新/热门/异常/举报/审核/隐藏）。"""
    public = await content_ops_service.list_public_projects(db, page=1, page_size=1)
    public_total = public["total"]
    latest = await content_ops_service.list_latest(db, days=days, limit=10)
    hot = await content_ops_service.list_hot(db, limit=10)
    abnormal = await content_ops_service.list_abnormal_growth(db, recent_days=days, limit=10)
    reported = await content_ops_service.list_reported(db, status="pending", page=1, page_size=1)
    reported_total = reported["total"]
    reviewing = await content_ops_service.list_reviewing(db, limit=10)
    hidden = await content_ops_service.list_hidden(db, limit=10)
    return {
        "days": days,
        "public_project_count": public_total,
        "latest": latest,
        "hot": hot,
        "abnormal_growth": abnormal,
        "pending_report_count": reported_total,
        "reviewing": reviewing,
        "hidden": hidden,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }


# ---------- 内容运营列表 ----------

@router.get("/content/{list_type}")
async def admin_content_list(
    list_type: str = Path(..., pattern="^(public|latest|hot|abnormal|reported|reviewing|hidden)$"),
    days: int = Query(default=7, ge=1, le=90),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """内容运营列表（规格十四）。"""
    if list_type == "public":
        pub = await content_ops_service.list_public_projects(db, page=page, page_size=page_size)
        return {"list_type": list_type, "total": pub["total"], "items": pub["items"]}
    if list_type == "latest":
        latest = await content_ops_service.list_latest(db, days=days, limit=page_size)
        return {"list_type": list_type, **latest}
    if list_type == "hot":
        hot = await content_ops_service.list_hot(db, limit=page_size)
        return {"list_type": list_type, **hot}
    if list_type == "abnormal":
        abn = await content_ops_service.list_abnormal_growth(db, recent_days=days, limit=page_size)
        return {"list_type": list_type, **abn}
    if list_type == "reported":
        rep = await content_ops_service.list_reported(db, status="pending", page=page, page_size=page_size)
        return {"list_type": list_type, "total": rep["total"], "items": rep["items"]}
    if list_type == "reviewing":
        rev = await content_ops_service.list_reviewing(db, limit=page_size)
        return {"list_type": list_type, **rev}
    hidden = await content_ops_service.list_hidden(db, limit=page_size)
    return {"list_type": list_type, **hidden}


# ---------- 内容运营操作 ----------

@router.post("/content/projects/{project_id}/hide")
async def admin_hide_project(
    project_id: uuid.UUID,
    data: ReasonRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """隐藏作品（记录原因+审计）。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.hide_project(
            db, project_id=project_id, actor_id=current_user.id,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "hide_project", "project_id": str(project_id)}


@router.post("/content/projects/{project_id}/restore")
async def admin_restore_project(
    project_id: uuid.UUID,
    data: ReasonRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """恢复作品。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.restore_project(
            db, project_id=project_id, actor_id=current_user.id,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "restore_project", "project_id": str(project_id)}


@router.post("/content/chapters/{chapter_id}/unpublish")
async def admin_unpublish_chapter(
    chapter_id: uuid.UUID,
    data: ReasonRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """下架章节。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.unpublish_chapter(
            db, chapter_id=chapter_id, actor_id=current_user.id,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "unpublish_chapter", "chapter_id": str(chapter_id)}


@router.post("/content/reports/{report_id}/handle")
async def admin_handle_report(
    report_id: uuid.UUID,
    data: HandleReportRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """处理举报。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.handle_report(
            db, report_id=report_id, actor_id=current_user.id,
            status=data.status, reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "handle_report", "report_id": str(report_id)}


@router.post("/content/projects/{project_id}/tag")
async def admin_add_official_tag(
    project_id: uuid.UUID,
    data: OfficialTagRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """添加官方标签。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.add_official_tag(
            db, project_id=project_id, actor_id=current_user.id,
            tag=data.tag, reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "add_official_tag", "project_id": str(project_id)}


@router.post("/content/users/{user_id}/restrict")
async def admin_restrict_user(
    user_id: uuid.UUID,
    data: RestrictUserRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """限制/恢复账号功能。"""
    ip_hash, ua = _client_info(request)
    try:
        await content_ops_service.restrict_user(
            db, user_id=user_id, actor_id=current_user.id,
            status=data.status, reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except content_ops_service.ContentOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "restrict_user", "user_id": str(user_id)}


# ---------- 私有内容临时授权 ----------

@router.post("/private-access")
async def admin_grant_private_access(
    data: PrivateAccessGrantRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """授予私有内容临时访问权（明确授权+理由+自动失效）。"""
    ip_hash, ua = _client_info(request)
    try:
        grant = await admin_service.grant_private_access(
            db,
            granted_to=data.granted_to,
            granted_by=current_user.id,
            project_id=data.project_id,
            access_reason=data.access_reason,
            expires_at=data.expires_at,
            ip_hash=ip_hash,
            user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(grant)
    return {
        "ok": True,
        "grant_id": str(grant.id),
        "expires_at": grant.expires_at.isoformat() + "Z",
        "status": grant.status,
    }


@router.post("/private-access/{grant_id}/revoke")
async def admin_revoke_private_access(
    grant_id: uuid.UUID,
    data: PrivateAccessRevokeRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """撤销私有内容临时授权。"""
    ip_hash, ua = _client_info(request)
    try:
        await admin_service.revoke_private_access(
            db, grant_id=grant_id, actor_id=current_user.id,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "revoke_private_access"}


# ---------- 推荐位 ----------

@router.get("/featured")
async def admin_list_featured(
    position: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """推荐位列表（含有效性检查，规格十五）。"""
    try:
        items = await admin_service.list_featured(db, position=position, status=status_filter)
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"featured": items}


@router.post("/featured", status_code=status.HTTP_201_CREATED)
async def admin_create_featured(
    data: FeaturedCreateRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """创建推荐位。"""
    ip_hash, ua = _client_info(request)
    try:
        item = await admin_service.create_featured(
            db,
            target_type=data.target_type,
            target_id=data.target_id,
            position=data.position,
            sort_order=data.sort_order,
            start_at=data.start_at,
            end_at=data.end_at,
            created_by=current_user.id,
            reason=data.reason,
            ip_hash=ip_hash,
            user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(item)
    return {"ok": True, "featured_id": str(item.id)}


@router.put("/featured/{featured_id}")
async def admin_update_featured(
    featured_id: uuid.UUID,
    data: FeaturedUpdateRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """更新推荐位。"""
    ip_hash, ua = _client_info(request)
    try:
        await admin_service.update_featured(
            db, featured_id=featured_id, actor_id=current_user.id,
            reason=data.reason, sort_order=data.sort_order,
            start_at=data.start_at, end_at=data.end_at, status=data.status,
            ip_hash=ip_hash, user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "update_featured", "featured_id": str(featured_id)}


@router.delete("/featured/{featured_id}")
async def admin_delete_featured(
    featured_id: uuid.UUID,
    data: ReasonRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """删除推荐位。"""
    ip_hash, ua = _client_info(request)
    try:
        await admin_service.delete_featured(
            db, featured_id=featured_id, actor_id=current_user.id,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "delete_featured"}


# ---------- 系统健康与监控 ----------

@router.get("/system/health")
async def admin_system_health(
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """系统监控与告警（规格十六）。"""
    return await monitor_service.get_system_health(db, include_alerts=True)


# ---------- 系统配置 ----------

@router.get("/settings")
async def admin_get_settings(
    current_user: User = Depends(require_admin_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    return await admin_service.get_settings(db)


@router.put("/settings")
async def admin_update_settings(
    data: SettingsUpdateRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """修改系统配置（写审计，规格十七）。"""
    ip_hash, ua = _client_info(request)
    try:
        settings = await admin_service.update_settings(
            db, actor_id=current_user.id, updates=data.updates,
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except admin_service.AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return settings


# ---------- 审计日志 ----------

@router.get("/audit-logs")
async def admin_audit_logs(
    actor_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    from_dt: str | None = Query(default=None, alias="from"),
    to_dt: str | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_admin_roles("super_admin", "auditor")),
    db: AsyncSession = Depends(get_db),
):
    """审计日志查询（规格十七）。"""
    items, total = await audit_service.list_logs(
        db,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        from_dt=_parse_dt(from_dt),
        to_dt=_parse_dt(to_dt),
        page=page,
        page_size=page_size,
    )
    return {
        "total": total,
        "logs": [
            {
                "id": str(x.id),
                "actor_id": str(x.actor_id) if x.actor_id else None,
                "actor_type": x.actor_type,
                "action": x.action,
                "target_type": x.target_type,
                "target_id": x.target_id,
                "reason": x.reason,
                "ip_hash": x.ip_hash,
                "user_agent": x.user_agent,
                "metadata": x.metadata_json,
                "created_at": x.created_at.isoformat() + "Z",
            }
            for x in items
        ],
    }


# ---------- 内容级审计追溯 与 自动备份调度（阶段J） ----------

@router.get("/content-audit-trail")
async def admin_content_audit_trail(
    project_id: uuid.UUID | None = Query(default=None),
    outline_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(
        default=None, pattern="^(content_save|content_restore|ai_apply|backfill)$"
    ),
    actor_id: uuid.UUID | None = Query(default=None),
    from_dt: str | None = Query(default=None, alias="from"),
    to_dt: str | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_admin_roles("super_admin", "auditor")),
    db: AsyncSession = Depends(get_db),
):
    """内容级审计追溯（阶段J / 补充10 / R13）：正文变更的字段级 diff 明细。

    只读接口：按作品 / 章节 / action（content_save|content_restore|ai_apply|backfill）
    / 操作人 / 时间范围查询「谁在哪个版本改了哪些字段」。正文全文不入审计表，
    只返回哈希、长度与截断摘要。
    """
    items, total = await audit_service.list_content_logs(
        db,
        project_id=project_id,
        outline_id=outline_id,
        action=action,
        actor_id=actor_id,
        from_dt=_parse_dt(from_dt),
        to_dt=_parse_dt(to_dt),
        page=page,
        page_size=page_size,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "available_actions": list(audit_service.CONTENT_AUDIT_ACTIONS),
        "trail": [audit_service.content_audit_trail(x) for x in items],
    }


@router.get("/backup-auto-policy")
async def admin_backup_auto_policy(
    project_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(
        require_admin_roles("super_admin", "operator", "analyst")
    ),
    db: AsyncSession = Depends(get_db),
):
    """自动备份调度策略预览（阶段J）：只读评估策略参数与各作品到期情况。"""
    return await backup_auto_policy.evaluate_policy(
        db, project_ids=[project_id] if project_id else None
    )


@router.post("/backup-auto-run")
async def admin_backup_auto_run(
    request: Request,
    project_id: uuid.UUID | None = Query(default=None),
    force: bool = Query(default=False),
    current_user: User = Depends(require_admin_roles("super_admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """手动触发一轮自动备份（阶段J）。

    默认 force=False：只对「已到期且未达每日上限」的作品创建 automatic 备份；
    force=True 时忽略到期判定，但仍受每日上限 / 保留上限 / TTL 约束。
    只新增 automatic 备份记录，不改动任何正文数据；策略关闭时直接拒绝。
    """
    if not backup_auto_policy.is_enabled() and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="自动备份调度已关闭（backup_auto_daily_enabled=False）",
        )
    result = await backup_auto_policy.run_auto_backup(
        db,
        project_id=project_id,
        force=force,
        actor_id=current_user.id,
    )
    ip_hash, ua = _client_info(request)
    try:
        await audit_service.record(
            db,
            actor_id=current_user.id,
            action="backup_auto_run",
            actor_type="user",
            target_type="project_backup",
            target_id=str(project_id) if project_id else None,
            reason=f"手动触发自动备份（force={force}）：创建 {result.get('created', 0)} 个",
            ip_hash=ip_hash,
            user_agent=ua,
            metadata={
                "status": result.get("status"),
                "created": result.get("created"),
                "skipped": result.get("skipped"),
                "failed": result.get("failed"),
                "force": force,
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — 审计写入失败不影响备份结果返回
        pass
    return result


# ---------- 报表导出 ----------

@router.get("/reports")
async def admin_export_report(
    request: Request,
    report_type: str = Query(..., pattern="^(users|content|reading|ai|errors|reports)$"),
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=500, ge=1, le=5000),
    current_user: User = Depends(require_admin_roles("super_admin", "operator", "analyst")),
    db: AsyncSession = Depends(get_db),
):
    """报表导出（规格十八：限范围/限量/权限/脱敏/记录导出）。"""
    ip_hash, ua = _client_info(request)
    try:
        result = await report_export_service.export_report(
            db,
            actor=current_user,
            report_type=report_type,
            from_date=_parse_date(from_date),
            to_date=_parse_date(to_date),
            fmt=fmt,
            page=page,
            page_size=page_size,
            ip_hash=ip_hash,
            user_agent=ua,
        )
    except report_export_service.ReportExportError as exc:
        raise HTTPException(status_code=403 if "权限" in str(exc) else 400, detail=str(exc))
    await db.commit()
    return result


# ---------- 工单 ----------

@router.get("/tickets")
async def admin_list_tickets(
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_admin_roles("super_admin", "support", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """工单列表（support 角色）。"""
    try:
        return await ticket_service.list_tickets(
            db, status=status_filter, page=page, page_size=page_size
        )
    except ticket_service.TicketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tickets/{ticket_id}/handle")
async def admin_handle_ticket(
    ticket_id: uuid.UUID,
    data: HandleTicketRequest,
    request: Request,
    current_user: User = Depends(require_admin_roles("super_admin", "support", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """处理工单（规格十四：人工操作记录 reason 并写审计）。"""
    ip_hash, ua = _client_info(request)
    try:
        await ticket_service.handle_ticket(
            db, ticket_id=ticket_id, actor_id=current_user.id,
            status=data.status, note=data.note,
        )
        await audit_service.record(
            db, actor_id=current_user.id, action="ticket.handle",
            target_type="ticket", target_id=str(ticket_id),
            reason=data.reason, ip_hash=ip_hash, user_agent=ua,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"ok": True, "action": "handle_ticket", "ticket_id": str(ticket_id)}


# ---------- 工具函数 ----------

def _parse_date(value: str | None):
    if value is None:
        return None
    try:
        from datetime import date
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式非法")


def _parse_dt(value: str | None):
    if value is None:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法")
