# 企业后台与安全管理服务（第二十四批「企业级协作、审核与内容治理」规格十四/十六/二十/二十一/二十三）
# 审计查询（组织范围）：管理员查看审计日志本身记录；普通成员不可删日志。
# 企业文件管理：上传/替换/删除(回收站软删除+恢复期限+最终删除)/恢复/分享公开链接(关闭/过期/密码/限下载/限次数/访问日志)。
# 企业安全策略：邮箱域名限制/SSO(SAML/OAuth)/MFA/IP限制/设备管理/会话管理/强制退出/密码策略（配置+校验，SSO/MFA 占位）。
# 高风险操作二次验证：导出全部/删组织/改所有者/改安全策略/批量删/生成长期令牌 → HighRiskActionRecord。
# AI 与企业数据：企业内容是否用于模型改进、AI 读取私有项目权限、AI 输出保存与可见范围、AI 任务进审计、离职 AI 历史处理。
# 企业运营数据：成员活跃度/项目完成/审核时长/未解决评论/发布量/协作次数/导出分享记录/异常访问（最小化原则）。
import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.comment import Comment
from app.models.content_version import ContentVersion
from app.models.enterprise_collab import EditLock, ReviewTask
from app.models.enterprise_file import FILE_SHARE_STATUSES, FileAccessLog, FileShareLink
from app.models.enterprise_governance import HighRiskActionRecord
from app.models.enterprise_org import Workspace
from app.models.enterprise_security import AIEnterpriseConfig, EnterpriseSecurityPolicy
from app.models.billing_org import Organization, OrganizationMember
from app.models.collaboration import ProjectMember
from app.models.project import Project
from app.models.stored_file import StoredFile
from app.models.user import User
from app.core.config import settings
from app.core.observability import current_request_id
from app.services import audit_service
from app.utils.security import hash_password, verify_password
from app.services.enterprise_service import ORG_OWNER_ADMIN, get_org_member, get_org_role, get_project_in_org

_HTTP_400 = status.HTTP_400_BAD_REQUEST
_HTTP_403 = status.HTTP_403_FORBIDDEN
_HTTP_404 = status.HTTP_404_NOT_FOUND

HIGH_RISK_ACTIONS = [
    "export_all",
    "delete_organization",
    "change_owner",
    "change_security_policy",
    "batch_delete",
    "long_lived_token",
]


# ===== 审计日志（组织范围） =====

async def list_org_audit_logs(
    db: AsyncSession,
    org,
    actor: User,
    action: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> tuple[list[AuditLog], int]:
    """管理员查看组织审计日志；查看动作本身记入审计。"""
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅管理员可查看审计日志")
    filters = [AuditLog.organization_id == getattr(org, "id")]
    if action:
        filters.append(AuditLog.action == action)
    if from_dt:
        filters.append(AuditLog.created_at >= from_dt)
    if to_dt:
        filters.append(AuditLog.created_at <= to_dt)
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*filters))).scalar() or 0
    rows = (
        await db.execute(
            select(AuditLog).where(*filters).order_by(AuditLog.created_at.desc()).limit(100)
        )
    ).scalars().all()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="view_audit_logs",
        target_type="audit_log",
        organization_id=getattr(org, "id"),
    )
    return list(rows), total


# ===== 企业文件管理 =====

def _check_org_file(db_file: StoredFile, org) -> StoredFile:
    if db_file is None or db_file.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="文件不存在")
    return db_file


async def list_org_files(
    db: AsyncSession,
    org,
    include_deleted: bool = False,
) -> tuple[list[StoredFile], int]:
    filters = [StoredFile.organization_id == getattr(org, "id")]
    if not include_deleted:
        filters.append(StoredFile.deleted_at.is_(None))
    rows = (
        await db.execute(select(StoredFile).where(*filters).order_by(StoredFile.created_at.desc()).limit(100))
    ).scalars().all()
    return list(rows), len(rows)


async def upload_org_file(
    db: AsyncSession,
    org,
    actor: User,
    *,
    original_name: str,
    stored_name: str,
    file_size: int,
    mime_type: str,
    is_private: bool = True,
    project_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> StoredFile:
    if not original_name or not stored_name:
        raise HTTPException(status_code=_HTTP_400, detail="文件名称不能为空")
    db_file = StoredFile(
        original_name=original_name,
        stored_name=stored_name,
        file_size=file_size,
        mime_type=mime_type,
        is_private=is_private,
        created_by=actor.id,
        organization_id=getattr(org, "id"),
        workspace_id=workspace_id,
        project_id=project_id,
        deleted_at=None,
        restore_deadline=None,
    )
    db.add(db_file)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="upload_file",
        target_type="stored_file",
        target_id=str(db_file.id),
        organization_id=getattr(org, "id"),
        after_value=f"{original_name} ({file_size}B)",
    )
    log = FileAccessLog(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        actor_id=actor.id,
        action="upload",
    )
    db.add(log)
    await db.flush()
    return db_file


async def replace_org_file(
    db: AsyncSession,
    org,
    actor: User,
    file_id: uuid.UUID,
    *,
    new_stored_name: str,
    new_size: int,
    new_mime: str,
) -> StoredFile:
    db_file = _check_org_file(await db.get(StoredFile, file_id), org)
    if db_file.deleted_at is not None:
        raise HTTPException(status_code=_HTTP_400, detail="文件已在回收站，请先恢复")
    db_file.stored_name = new_stored_name
    db_file.file_size = new_size
    db_file.mime_type = new_mime
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="replace_file",
        target_type="stored_file",
        target_id=str(db_file.id),
        organization_id=getattr(org, "id"),
    )
    log = FileAccessLog(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        actor_id=actor.id,
        action="replace",
    )
    db.add(log)
    await db.flush()
    return db_file


async def delete_org_file(
    db: AsyncSession,
    org,
    actor: User,
    file_id: uuid.UUID,
    restore_deadline_days: int = 30,
) -> StoredFile:
    """删除走回收站软删除 + 恢复期限 + 最终删除；合同/稿件/审校资料不永久删除。"""
    db_file = _check_org_file(await db.get(StoredFile, file_id), org)
    if db_file.deleted_at is not None:
        raise HTTPException(status_code=_HTTP_400, detail="文件已在回收站")
    db_file.deleted_at = datetime.utcnow()
    db_file.deleted_by = actor.id
    db_file.restore_deadline = datetime.utcnow() + timedelta(days=restore_deadline_days)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="delete_file",
        target_type="stored_file",
        target_id=str(db_file.id),
        organization_id=getattr(org, "id"),
        reason=f"软删除，恢复期限 {restore_deadline_days} 天",
    )
    log = FileAccessLog(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        actor_id=actor.id,
        action="delete",
    )
    db.add(log)
    await db.flush()
    return db_file


async def restore_org_file(
    db: AsyncSession,
    org,
    actor: User,
    file_id: uuid.UUID,
) -> StoredFile:
    db_file = _check_org_file(await db.get(StoredFile, file_id), org)
    if db_file.deleted_at is None:
        raise HTTPException(status_code=_HTTP_400, detail="文件未在回收站")
    if db_file.restore_deadline and db_file.restore_deadline < datetime.utcnow():
        raise HTTPException(status_code=_HTTP_400, detail="已超过恢复期限，无法恢复")
    db_file.deleted_at = None
    db_file.deleted_by = None
    db_file.restore_deadline = None
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="restore_file",
        target_type="stored_file",
        target_id=str(db_file.id),
        organization_id=getattr(org, "id"),
    )
    log = FileAccessLog(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        actor_id=actor.id,
        action="restore",
    )
    db.add(log)
    await db.flush()
    return db_file


async def create_share_link(
    db: AsyncSession,
    org,
    actor: User,
    file_id: uuid.UUID,
    *,
    token: str,
    password_hash: str | None = None,
    expires_at: datetime | None = None,
    allow_download: bool = True,
    max_visits: int | None = None,
) -> FileShareLink:
    """创建公开链接：有效期/密码/限下载/限次数/访问日志。"""
    db_file = _check_org_file(await db.get(StoredFile, file_id), org)
    if db_file.deleted_at is not None:
        raise HTTPException(status_code=_HTTP_400, detail="文件已在回收站")
    link = FileShareLink(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        creator_id=actor.id,
        token=token,
        # 第三批 f：原实现把明文口令写进 password_hash 列（列名说哈希、实际存明文），
        # 既违背列语义，也让数据库/备份直接泄露可复用口令。这里统一 bcrypt 哈希。
        password_hash=hash_password(password_hash) if password_hash else None,
        status="active",
        expires_at=expires_at,
        allow_download=allow_download,
        max_visits=max_visits,
        visit_count=0,
    )
    db.add(link)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="share_file",
        target_type="file_share_link",
        target_id=str(link.id),
        organization_id=getattr(org, "id"),
    )
    log = FileAccessLog(
        organization_id=getattr(org, "id"),
        file_id=db_file.id,
        actor_id=actor.id,
        action="share",
    )
    db.add(log)
    await db.flush()
    return link


def _looks_hashed(stored: str) -> bool:
    return stored.startswith(("$2a$", "$2b$", "$2y$", "$argon2", "$pbkdf2"))


def _verify_share_password(stored: str, provided: str | None) -> bool:
    """校验分享口令。

    兼容历史行：2026-11-12 之前创建的链接存的是**明文**（列名 password_hash 名不副实）。
    对这类行按常数时间比较明文，并在成功时由调用方就地升级为哈希。
    """
    if not provided:
        return False
    if _looks_hashed(stored):
        try:
            return verify_password(provided, stored)
        except Exception:  # noqa: BLE001 - 哈希格式损坏时按失败处理，不抛 500
            return False
    import hmac

    # compare_digest 不接受含非 ASCII 字符的 str（中文口令会直接 TypeError），
    # 统一按 UTF-8 字节比较；编码失败按“不匹配”处理。
    try:
        return hmac.compare_digest(provided.encode("utf-8"), stored.encode("utf-8"))
    except (TypeError, UnicodeEncodeError):  # noqa: BLE001 - 异常输入按失败处理
        return False


async def _recent_password_failures(db: AsyncSession, file_id: uuid.UUID) -> int:
    """时间窗内的口令失败次数（用访问日志计数，不新增表）。"""
    window_start = datetime.utcnow() - timedelta(
        minutes=int(getattr(settings, "share_access_window_minutes", 15))
    )
    stmt = select(func.count(FileAccessLog.id)).where(
        FileAccessLog.file_id == file_id,
        FileAccessLog.action == "access_denied",
        FileAccessLog.occurred_at >= window_start,
    )
    return int((await db.execute(stmt)).scalar() or 0)


def _audit_meta(**extra) -> dict:
    meta = {"request_id": current_request_id()}
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


async def access_share_link(
    db: AsyncSession,
    link_id: uuid.UUID,
    *,
    password: str | None = None,
    ip_hash: str | None = None,
    want_download: bool = False,
) -> dict:
    """访问公开链接：关闭/过期/限流/密码/限次数/限下载校验 + 访问日志 + 审计。

    第三批 f 的三处修正：
    1. 口令校验前先做失败限流（默认 15 分钟内 10 次），避免匿名接口被爆破；
    2. 失败与成功都写审计（含 request_id），原实现只写文件访问日志、不写审计；
    3. `want_download` / 文件存在性在 `visit_count += 1` **之前**校验，
       避免一次注定失败的请求也消耗访问次数。
    """
    link = await db.get(FileShareLink, link_id)
    if link is None or link.status != "active":
        raise HTTPException(status_code=_HTTP_404, detail="分享链接不可用")
    if link.expires_at and link.expires_at < datetime.utcnow():
        link.status = "expired"
        await audit_service.record(
            db,
            actor_id=None,
            actor_type="anonymous",
            action="share_access_expired",
            target_type="file_share_link",
            target_id=str(link.id),
            organization_id=link.organization_id,
            ip_hash=ip_hash,
            metadata=_audit_meta(file_id=str(link.file_id)),
        )
        await db.commit()
        raise HTTPException(status_code=_HTTP_403, detail="分享链接已过期")

    if link.password_hash:
        failures = await _recent_password_failures(db, link.file_id)
        max_failures = int(getattr(settings, "share_access_max_failures", 10))
        if failures >= max_failures:
            await audit_service.record(
                db,
                actor_id=None,
                actor_type="anonymous",
                action="share_access_throttled",
                target_type="file_share_link",
                target_id=str(link.id),
                organization_id=link.organization_id,
                ip_hash=ip_hash,
                metadata=_audit_meta(file_id=str(link.file_id), failures=failures),
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="口令尝试次数过多，请稍后再试",
            )
        if not _verify_share_password(link.password_hash, password):
            db.add(
                FileAccessLog(
                    organization_id=link.organization_id,
                    file_id=link.file_id,
                    actor_id=None,
                    action="access_denied",
                    ip_hash=ip_hash,
                )
            )
            await audit_service.record(
                db,
                actor_id=None,
                actor_type="anonymous",
                action="share_access_denied",
                target_type="file_share_link",
                target_id=str(link.id),
                organization_id=link.organization_id,
                ip_hash=ip_hash,
                metadata=_audit_meta(file_id=str(link.file_id), reason="bad_password"),
            )
            await db.commit()
            raise HTTPException(status_code=_HTTP_403, detail="访问密码错误")
        if not _looks_hashed(link.password_hash):
            # 历史明文行：校验通过后立即升级为哈希，后续请求不再接触明文。
            link.password_hash = hash_password(password)

    if link.max_visits is not None and link.visit_count >= link.max_visits:
        raise HTTPException(status_code=_HTTP_403, detail="分享链接访问次数已达上限")
    if want_download and not link.allow_download:
        raise HTTPException(status_code=_HTTP_403, detail="该分享禁止下载")

    db_file = await db.get(StoredFile, link.file_id)
    if db_file is None or db_file.deleted_at is not None:
        raise HTTPException(status_code=_HTTP_404, detail="文件不存在或已删除")

    link.visit_count += 1
    db.add(
        FileAccessLog(
            organization_id=link.organization_id,
            file_id=link.file_id,
            actor_id=None,
            action="download" if want_download else "view",
            ip_hash=ip_hash,
        )
    )
    await audit_service.record(
        db,
        actor_id=None,
        actor_type="anonymous",
        action="share_access",
        target_type="file_share_link",
        target_id=str(link.id),
        organization_id=link.organization_id,
        ip_hash=ip_hash,
        metadata=_audit_meta(
            file_id=str(link.file_id),
            download=bool(want_download),
            visit_count=link.visit_count,
        ),
    )
    await db.flush()
    return {
        "file_id": str(link.file_id),
        "original_name": db_file.original_name,
        "mime_type": db_file.mime_type,
        "visit_count": link.visit_count,
        "allow_download": link.allow_download,
    }


async def close_share_link(
    db: AsyncSession,
    org,
    actor: User,
    link_id: uuid.UUID,
) -> FileShareLink:
    link = await db.get(FileShareLink, link_id)
    if link is None or link.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="分享链接不存在")
    link.status = "closed"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="close_share_link",
        target_type="file_share_link",
        target_id=str(link.id),
        organization_id=getattr(org, "id"),
    )
    return link


async def list_share_logs(
    db: AsyncSession,
    org,
) -> tuple[list[FileAccessLog], int]:
    filters = [FileAccessLog.organization_id == getattr(org, "id")]
    rows = (
        await db.execute(
            select(FileAccessLog).where(*filters).order_by(FileAccessLog.occurred_at.desc()).limit(100)
        )
    ).scalars().all()
    return list(rows), len(rows)


# ===== 企业安全策略 / AI 企业数据 =====

async def get_or_create_security_policy(db: AsyncSession, org) -> EnterpriseSecurityPolicy:
    policy = (
        await db.execute(
            select(EnterpriseSecurityPolicy).where(
                EnterpriseSecurityPolicy.organization_id == getattr(org, "id")
            )
        )
    ).scalar_one_or_none()
    if policy is None:
        policy = EnterpriseSecurityPolicy(
            organization_id=getattr(org, "id"),
            email_domain_restriction=None,
            sso_enabled=False,
            mfa_required=False,
        )
        db.add(policy)
        await db.flush()
    return policy


async def update_security_policy(
    db: AsyncSession,
    org,
    actor: User,
    *,
    email_domain_restriction: list[str] | None = None,
    sso_enabled: bool | None = None,
    sso_type: str | None = None,
    sso_config: dict | None = None,
    mfa_required: bool | None = None,
    ip_restrictions: list[str] | None = None,
    device_policy: dict | None = None,
    session_policy: dict | None = None,
    password_policy: dict | None = None,
    confirm: str | None = None,
    reason: str | None = None,
) -> EnterpriseSecurityPolicy:
    """修改安全策略：高风险操作，需要二次验证（confirm='confirm' + 原因）并记录。"""
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅管理员可修改安全策略")
    if confirm != "confirm":
        raise HTTPException(status_code=_HTTP_400, detail="修改安全策略为高风险操作，需要二次确认")
    if not reason or not reason.strip():
        raise HTTPException(status_code=_HTTP_400, detail="修改安全策略必须填写原因")

    policy = await get_or_create_security_policy(db, org)
    if email_domain_restriction is not None:
        policy.email_domain_restriction = json.dumps(email_domain_restriction, ensure_ascii=False)
    if sso_enabled is not None:
        policy.sso_enabled = sso_enabled
    if sso_type is not None:
        if sso_type not in ("saml", "oauth"):
            raise HTTPException(status_code=_HTTP_400, detail="sso_type 仅支持 saml / oauth")
        policy.sso_type = sso_type
    if sso_config is not None:
        policy.sso_config = json.dumps(sso_config, ensure_ascii=False)
    if mfa_required is not None:
        policy.mfa_required = mfa_required
    if ip_restrictions is not None:
        policy.ip_restrictions = json.dumps(ip_restrictions, ensure_ascii=False)
    if device_policy is not None:
        policy.device_policy = json.dumps(device_policy, ensure_ascii=False)
    if session_policy is not None:
        policy.session_policy = json.dumps(session_policy, ensure_ascii=False)
    if password_policy is not None:
        policy.password_policy = json.dumps(password_policy, ensure_ascii=False)
    policy.updated_by = actor.id
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="change_security_policy",
        target_type="security_policy",
        target_id=str(policy.id),
        organization_id=getattr(org, "id"),
        reason=reason,
    )
    await record_high_risk_action(
        db, org, actor,
        action="change_security_policy",
        reason=reason,
        impact_scope={"targets": ["security_policy"], "description": "企业安全策略已更新"},
        confirmed=True,
    )
    return policy


async def validate_email_domain(db: AsyncSession, org, email: str) -> bool:
    policy = await get_or_create_security_policy(db, org)
    if not policy.email_domain_restriction:
        return True
    domains = json.loads(policy.email_domain_restriction or "[]")
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return domain in [d.lower() for d in domains]


async def get_ai_config(db: AsyncSession, org) -> AIEnterpriseConfig:
    cfg = (
        await db.execute(
            select(AIEnterpriseConfig).where(AIEnterpriseConfig.organization_id == getattr(org, "id"))
        )
    ).scalar_one_or_none()
    if cfg is None:
        cfg = AIEnterpriseConfig(
            organization_id=getattr(org, "id"),
            allow_training=False,
            allow_read_private=False,
            allow_save_output=True,
            ai_output_visibility="organization",
            audit_ai_tasks=True,
            sensitive_hint=True,
        )
        db.add(cfg)
        await db.flush()
    return cfg


async def update_ai_config(
    db: AsyncSession,
    org,
    actor: User,
    *,
    allow_training: bool | None = None,
    allow_read_private: bool | None = None,
    allow_save_output: bool | None = None,
    ai_output_visibility: str | None = None,
    audit_ai_tasks: bool | None = None,
    sensitive_hint: bool | None = None,
) -> AIEnterpriseConfig:
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅管理员可修改 AI 企业配置")
    cfg = await get_ai_config(db, org)
    if allow_training is not None:
        cfg.allow_training = allow_training
    if allow_read_private is not None:
        cfg.allow_read_private = allow_read_private
    if allow_save_output is not None:
        cfg.allow_save_output = allow_save_output
    if ai_output_visibility is not None:
        if ai_output_visibility not in ("private", "project", "organization"):
            raise HTTPException(status_code=_HTTP_400, detail="ai_output_visibility 仅支持 private/project/organization")
        cfg.ai_output_visibility = ai_output_visibility
    if audit_ai_tasks is not None:
        cfg.audit_ai_tasks = audit_ai_tasks
    if sensitive_hint is not None:
        cfg.sensitive_hint = sensitive_hint
    cfg.updated_by = actor.id
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="update_ai_config",
        target_type="ai_enterprise_config",
        target_id=str(cfg.id),
        organization_id=getattr(org, "id"),
        after_value=json.dumps(
            {
                "allow_training": cfg.allow_training,
                "allow_read_private": cfg.allow_read_private,
                "allow_save_output": cfg.allow_save_output,
                "ai_output_visibility": cfg.ai_output_visibility,
                "audit_ai_tasks": cfg.audit_ai_tasks,
                "sensitive_hint": cfg.sensitive_hint,
            },
            ensure_ascii=False,
        ),
    )
    return cfg


# ===== 高风险操作二次验证 =====

async def record_high_risk_action(
    db: AsyncSession,
    org,
    actor: User,
    *,
    action: str,
    reason: str,
    impact_scope: dict | str | None = None,
    confirmed: bool = False,
) -> HighRiskActionRecord:
    """高风险操作（导出全部/删组织/改所有者/改安全策略/批量删/生成长期令牌）二次验证记录，仅管理员可登记。"""
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅管理员可登记高风险操作")
    if action not in HIGH_RISK_ACTIONS:
        raise HTTPException(status_code=_HTTP_400, detail="非法高风险动作")
    if not reason or not reason.strip():
        raise HTTPException(status_code=_HTTP_400, detail="高风险操作必须填写原因")
    record = HighRiskActionRecord(
        organization_id=getattr(org, "id"),
        actor_id=actor.id,
        action=action,
        reason=reason,
        status="confirmed" if confirmed else "pending",
        confirmed_at=datetime.utcnow() if confirmed else None,
        impact_scope=json.dumps(impact_scope, ensure_ascii=False) if impact_scope else None,
    )
    db.add(record)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action=f"high_risk:{action}",
        target_type="high_risk_action",
        target_id=str(record.id),
        organization_id=getattr(org, "id"),
        reason=reason,
        after_value="confirmed" if confirmed else "pending",
    )
    return record


# ===== 企业运营数据（最小化原则） =====

async def org_operations_stats(db: AsyncSession, org, actor: User) -> dict:
    """管理员后台运营数据：成员活跃度/项目完成/审核时长/未解决评论/发布量/协作次数/导出分享记录/异常访问。"""
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅管理员可查看运营数据")
    org_id = getattr(org, "id")
    total = {}
    total["organization_id"] = str(org_id)

    members = (
        await db.execute(
            select(func.count()).select_from(OrganizationMember).where(OrganizationMember.organization_id == org_id)
        )
    ).scalar() or 0
    total["member_count"] = members

    total["project_count"] = (
        await db.execute(
            select(func.count()).select_from(Project).where(Project.organization_id == org_id)
        )
    ).scalar() or 0
    total["published_projects"] = (
        await db.execute(
            select(func.count()).select_from(Project).where(
                Project.organization_id == org_id, Project.collab_status == "published"
            )
        )
    ).scalar() or 0
    total["project_completed"] = (
        await db.execute(
            select(func.count()).select_from(Project).where(
                Project.organization_id == org_id,
                Project.collab_status.in_(["published", "archived"]),
            )
        )
    ).scalar() or 0

    total["active_locks"] = (
        await db.execute(
            select(func.count()).select_from(EditLock).where(
                EditLock.organization_id == org_id, EditLock.status == "locked"
            )
        )
    ).scalar() or 0

    total["unresolved_comments"] = (
        await db.execute(
            select(func.count()).select_from(Comment).where(
                Comment.organization_id == org_id,
                Comment.status.in_(["pending", "processing", "reopened"]),
            )
        )
    ).scalar() or 0

    total["pending_review_tasks"] = (
        await db.execute(
            select(func.count()).select_from(ReviewTask).where(
                ReviewTask.organization_id == org_id,
                ReviewTask.status.in_(["reviewing", "re_reviewing"]),
            )
        )
    ).scalar() or 0

    total["share_logs"] = (
        await db.execute(
            select(func.count()).select_from(FileAccessLog).where(
                FileAccessLog.organization_id == org_id,
                FileAccessLog.action.in_(["share", "download", "view"]),
            )
        )
    ).scalar() or 0

    total["export_events"] = (
        await db.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.organization_id == org_id,
                AuditLog.action.like("%export%"),
            )
        )
    ).scalar() or 0

    return total
