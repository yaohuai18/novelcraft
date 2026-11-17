# 第二十四批「企业级安全与管理后台」- 审计/文件/分享/安全策略/AI配置/高风险复核/运营数据 路由
# 审计日志：管理员查看本身记入日志，普通成员不可删日志；
# 文件：查看/下载/上传/替换/删除(回收站软删除+恢复期限)/恢复/分享公开链接(关闭/过期/密码/限下载/限次数/访问日志)；
# 安全策略：域名限制/SSO/SAML/OAuth/MFA/IP/设备/会话/密码策略，修改需二次确认+原因；
# AI 企业数据：训练开关/私有读取权限/输出可见范围/审计；高风险操作二次验证；运营数据最小化展示。
import uuid

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services import enterprise_admin_service as svc
from app.services import enterprise_service as esvc

router = APIRouter(prefix="/api/enterprise", tags=["batch24-admin"])


async def _load_org(org_id: uuid.UUID, db: AsyncSession):
    return await esvc.get_org_by_id(db, org_id)


async def _require_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID):
    return await esvc.require_org_member(db, org_id, user_id)


# ---------- 审计日志 ----------
@router.get("/organizations/{org_id}/audit-logs/query")
async def list_audit_logs(
    org_id: uuid.UUID,
    action: str | None = Query(None),
    from_dt: str | None = Query(None),
    to_dt: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    from datetime import datetime
    rows, total = await svc.list_org_audit_logs(
        db, org, current_user,
        action,
        datetime.fromisoformat(from_dt) if from_dt else None,
        datetime.fromisoformat(to_dt) if to_dt else None,
    )
    await db.commit()
    return {"total": total, "logs": [
        {"id": str(l.id), "actor_id": str(l.actor_id) if l.actor_id else None, "action": l.action,
         "resource_type": l.target_type, "resource_id": str(l.target_id) if l.target_id else None,
         "created_at": l.created_at.isoformat() if l.created_at else None}
        for l in rows
    ]}


# ---------- 企业文件管理 ----------
@router.get("/organizations/{org_id}/files")
async def list_files(
    org_id: uuid.UUID,
    include_deleted: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows, total = await svc.list_org_files(db, org, include_deleted)
    return {"total": total, "files": [
        {"id": str(f.id), "original_name": f.original_name, "file_size": f.file_size,
         "mime_type": f.mime_type, "is_private": f.is_private, "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None}
        for f in rows
    ]}


class FileUploadIn(BaseModel):
    original_name: str
    stored_name: str
    file_size: int
    mime_type: str = "application/octet-stream"
    is_private: bool = True
    project_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None


@router.post("/organizations/{org_id}/files")
async def upload_file(
    org_id: uuid.UUID,
    body: FileUploadIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    f = await svc.upload_org_file(
        db, org, current_user,
        original_name=body.original_name, stored_name=body.stored_name,
        file_size=body.file_size, mime_type=body.mime_type, is_private=body.is_private,
        project_id=body.project_id, workspace_id=body.workspace_id,
    )
    await db.commit()
    return {"id": str(f.id), "original_name": f.original_name}


class FileReplaceIn(BaseModel):
    new_stored_name: str
    new_size: int
    new_mime: str = "application/octet-stream"


@router.post("/organizations/{org_id}/files/{file_id}/replace")
async def replace_file(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    body: FileReplaceIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    f = await svc.replace_org_file(db, org, current_user, file_id, new_stored_name=body.new_stored_name, new_size=body.new_size, new_mime=body.new_mime)
    await db.commit()
    return {"id": str(f.id), "stored_name": f.stored_name}


class FileDeleteIn(BaseModel):
    restore_deadline_days: int = 30


@router.post("/organizations/{org_id}/files/{file_id}/delete")
async def delete_file(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    body: FileDeleteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    f = await svc.delete_org_file(db, org, current_user, file_id, body.restore_deadline_days)
    await db.commit()
    return {"id": str(f.id), "deleted_at": f.deleted_at.isoformat(), "restore_deadline": f.restore_deadline.isoformat()}


@router.post("/organizations/{org_id}/files/{file_id}/restore")
async def restore_file(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    f = await svc.restore_org_file(db, org, current_user, file_id)
    await db.commit()
    return {"id": str(f.id), "deleted_at": None}


class ShareLinkCreateIn(BaseModel):
    token: str
    password: str | None = None
    expires_at: str | None = None
    allow_download: bool = True
    max_visits: int | None = None


@router.post("/organizations/{org_id}/files/{file_id}/share-links")
async def create_share_link(
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    body: ShareLinkCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    from datetime import datetime
    link = await svc.create_share_link(
        db, org, current_user, file_id,
        token=body.token, password_hash=body.password,
        expires_at=datetime.fromisoformat(body.expires_at) if body.expires_at else None,
        allow_download=body.allow_download, max_visits=body.max_visits,
    )
    await db.commit()
    return {"id": str(link.id), "token": link.token, "status": link.status}


class ShareAccessIn(BaseModel):
    password: str | None = None
    ip_hash: str | None = None
    want_download: bool = False


@router.post("/share-links/{link_id}/access")
async def access_share_link(
    link_id: uuid.UUID,
    body: ShareAccessIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """匿名访问公开分享链接。

    第三批 f：`ip_hash` 不再取请求体（客户端可随意伪造，等于没有记录）。
    改为由服务端按来源地址取哈希；请求体里的 `ip_hash` 仅为兼容旧客户端保留，
    一律忽略。
    """
    client_host = request.client.host if request.client else ""
    ip_hash = None
    if client_host:
        ip_hash = hashlib.sha256(
            f"novelcraft-share:{client_host}".encode("utf-8")
        ).hexdigest()[:32]
    result = await svc.access_share_link(
        db,
        link_id,
        password=body.password,
        ip_hash=ip_hash,
        want_download=body.want_download,
    )
    await db.commit()
    return result


@router.post("/organizations/{org_id}/share-links/{link_id}/close")
async def close_share_link(
    org_id: uuid.UUID,
    link_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    link = await svc.close_share_link(db, org, current_user, link_id)
    await db.commit()
    return {"id": str(link.id), "status": link.status}


@router.get("/organizations/{org_id}/share-logs")
async def list_share_logs(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows, total = await svc.list_share_logs(db, org)
    return {"total": total, "logs": [
        {"id": str(l.id), "file_id": str(l.file_id) if l.file_id else None, "action": l.action,
         "actor_id": str(l.actor_id) if l.actor_id else None,
         "occurred_at": l.occurred_at.isoformat() if l.occurred_at else None}
        for l in rows
    ]}


# ---------- 企业安全策略 ----------
class SecurityPolicyIn(BaseModel):
    email_domain_restriction: list[str] | None = None
    sso_enabled: bool | None = None
    sso_type: str | None = None
    sso_config: dict | None = None
    mfa_required: bool | None = None
    ip_restrictions: list[str] | None = None
    device_policy: dict | None = None
    session_policy: dict | None = None
    password_policy: dict | None = None
    confirm: str | None = None
    reason: str | None = None


@router.get("/organizations/{org_id}/security-policy")
async def get_security_policy(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    policy = await svc.get_or_create_security_policy(db, org)
    await db.commit()
    return {
        "id": str(policy.id),
        "email_domain_restriction": policy.email_domain_restriction,
        "sso_enabled": policy.sso_enabled,
        "sso_type": policy.sso_type,
        "mfa_required": policy.mfa_required,
        "ip_restrictions": policy.ip_restrictions,
        "updated_by": str(policy.updated_by) if policy.updated_by else None,
    }


@router.put("/organizations/{org_id}/security-policy")
async def update_security_policy(
    org_id: uuid.UUID,
    body: SecurityPolicyIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    policy = await svc.update_security_policy(
        db, org, current_user,
        email_domain_restriction=body.email_domain_restriction,
        sso_enabled=body.sso_enabled, sso_type=body.sso_type, sso_config=body.sso_config,
        mfa_required=body.mfa_required, ip_restrictions=body.ip_restrictions,
        device_policy=body.device_policy, session_policy=body.session_policy,
        password_policy=body.password_policy, confirm=body.confirm, reason=body.reason,
    )
    await db.commit()
    return {"id": str(policy.id), "sso_enabled": policy.sso_enabled, "mfa_required": policy.mfa_required}


# ---------- AI 与企业数据 ----------
class AIConfigIn(BaseModel):
    allow_training: bool | None = None
    allow_read_private: bool | None = None
    allow_save_output: bool | None = None
    ai_output_visibility: str | None = None
    audit_ai_tasks: bool | None = None
    sensitive_hint: bool | None = None


@router.get("/organizations/{org_id}/ai-config")
async def get_ai_config(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    cfg = await svc.get_ai_config(db, org)
    await db.commit()
    return {
        "id": str(cfg.id),
        "allow_training": cfg.allow_training,
        "allow_read_private": cfg.allow_read_private,
        "allow_save_output": cfg.allow_save_output,
        "ai_output_visibility": cfg.ai_output_visibility,
        "audit_ai_tasks": cfg.audit_ai_tasks,
        "sensitive_hint": cfg.sensitive_hint,
    }


@router.put("/organizations/{org_id}/ai-config")
async def update_ai_config(
    org_id: uuid.UUID,
    body: AIConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    cfg = await svc.update_ai_config(
        db, org, current_user,
        allow_training=body.allow_training, allow_read_private=body.allow_read_private,
        allow_save_output=body.allow_save_output, ai_output_visibility=body.ai_output_visibility,
        audit_ai_tasks=body.audit_ai_tasks, sensitive_hint=body.sensitive_hint,
    )
    await db.commit()
    return {"id": str(cfg.id), "allow_training": cfg.allow_training, "ai_output_visibility": cfg.ai_output_visibility}


# ---------- 高风险操作二次验证 ----------
class HighRiskIn(BaseModel):
    action: str
    reason: str
    impact_scope: dict | str | None = None
    confirmed: bool = False


@router.post("/organizations/{org_id}/high-risk-actions")
async def record_high_risk_action(
    org_id: uuid.UUID,
    body: HighRiskIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    record = await svc.record_high_risk_action(
        db, org, current_user,
        action=body.action, reason=body.reason,
        impact_scope=body.impact_scope, confirmed=body.confirmed,
    )
    await db.commit()
    return {"id": str(record.id), "action": record.action, "status": record.status}


# ---------- 企业运营数据（最小化） ----------
@router.get("/organizations/{org_id}/operations-stats")
async def org_operations_stats(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    stats = await svc.org_operations_stats(db, org, current_user)
    return stats
