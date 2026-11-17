# 企业组织服务（第二十四批「企业级协作、审核与内容治理」规格二/三/四/五/十七）
# 组织 → 工作空间 → 项目 → 文档/章节 层级；一人可入多组织，组织间数据严格隔离；
# 权限=角色+资源范围，服务端全量校验（后端从登录用户授权关系确认组织归属，不信任前端传参）；
# 邀请（邮箱/组织或项目/角色/过期/撤回/重复处理/域名限制/批量）；离职处理（撤销登录/移交负责人/保留历史）。
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_org import (
    ORGANIZATION_ROLES,
    Organization,
    OrganizationMember,
)
from app.models.audit_log import AuditLog
from app.models.collaboration import ProjectMember
from app.models.enterprise_collab import ReviewTask
from app.models.enterprise_org import (
    ORG_INVITATION_STATUSES,
    WORKSPACE_STATUSES,
    OrgInvitation,
    Workspace,
)
from app.models.project import Project
from app.models.stored_file import StoredFile
from app.models.user import User
from app.services import audit_service

_HTTP_400 = status.HTTP_400_BAD_REQUEST
_HTTP_403 = status.HTTP_403_FORBIDDEN
_HTTP_404 = status.HTTP_404_NOT_FOUND

# ===== 组织角色职责常量 =====
ORG_OWNER_ADMIN = {"owner", "admin"}
ORG_MANAGERS = {"owner", "admin", "project_lead"}
ORG_EDIT_ALL = {"owner", "admin", "project_lead", "chief_editor", "editor"}
ORG_PUBLISH = {"owner", "admin", "chief_editor"}
ORG_COMMENT_ALLOWED = {"owner", "admin", "project_lead", "chief_editor", "editor", "author", "reviewer", "commenter"}

# 项目协作状态流转
COLLAB_STATUS_FLOW = {
    "draft": ["collaborating", "archived"],
    "collaborating": ["pending_review", "revising", "archived"],
    "pending_review": ["revising", "approved", "withdrawn"],
    "revising": ["pending_review", "collaborating", "archived"],
    "approved": ["published", "archived", "revising"],
    "published": ["archived", "revising"],
    "archived": [],
}

# ===== 工具函数 =====

def _loads(raw: str | None) -> list | dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _dumps(payload) -> str | None:
    return json.dumps(payload, ensure_ascii=False) if payload is not None else None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


async def get_org_member(
    db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMember | None:
    """从登录用户的授权关系确认组织归属（不信任前端传参）。"""
    return (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "active",
            )
        )
    ).scalar_one_or_none()


async def require_org_member(
    db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMember:
    member = await get_org_member(db, organization_id, user_id)
    if member is None:
        raise HTTPException(status_code=_HTTP_403, detail="无权访问该组织或不属于该组织")
    return member


async def get_org_role(
    db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> str | None:
    member = await get_org_member(db, organization_id, user_id)
    return member.role if member is not None else None


async def require_org_role(
    db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, allowed: set[str]
) -> OrganizationMember:
    member = await require_org_member(db, organization_id, user_id)
    if member.role not in allowed:
        raise HTTPException(status_code=_HTTP_403, detail=f"组织角色 {member.role} 无权执行该操作")
    return member


async def get_org_by_id(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    org = await db.get(Organization, organization_id)
    if org is None or org.status != "active":
        raise HTTPException(status_code=_HTTP_404, detail="组织不存在")
    return org


async def get_project_in_org(
    db: AsyncSession, organization_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    """项目必须属于该组织（跨组织隔离校验）。"""
    project = await db.get(Project, project_id)
    if project is None or project.organization_id is None or project.organization_id != organization_id:
        raise HTTPException(status_code=_HTTP_404, detail="项目不存在或不属于该组织")
    return project


def can_edit_target(org_role: str | None, project_member_role: str | None, creator_id, actor_id) -> bool:
    """编辑权限：项目成员独立权限优先，其次组织角色。

    作者仅自己章节；编辑全部；审稿人/评论员/只读成员无编辑；owner 全权。
    """
    if project_member_role == "owner":
        return True
    if project_member_role == "editor":
        return True
    if project_member_role == "author":
        return creator_id is not None and str(creator_id) == str(actor_id)
    if project_member_role in ("reviewer", "commenter", "readonly", "guest"):
        return False
    if org_role in ORG_EDIT_ALL:
        return True
    if org_role == "author":
        return creator_id is not None and str(creator_id) == str(actor_id)
    return False


def can_comment(org_role: str | None, project_member_role: str | None) -> bool:
    if org_role in ORG_COMMENT_ALLOWED:
        return True
    return project_member_role in ("owner", "editor", "author", "reviewer", "commenter")


def can_publish(org_role: str | None, project_member_role: str | None) -> bool:
    if org_role in ORG_PUBLISH:
        return True
    return project_member_role in ("owner", "editor")


async def get_project_member_role(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> str | None:
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is not None:
        return member.role
    project = await db.get(Project, project_id)
    if project is not None and project.user_id == user_id:
        return "owner"
    return None


# ===== 工作空间 =====

async def create_workspace(
    db: AsyncSession, org: Organization, owner: User, name: str
) -> Workspace:
    if not name.strip():
        raise HTTPException(status_code=_HTTP_400, detail="工作空间名称不能为空")
    membership = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == owner.id,
                OrganizationMember.status == "active",
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.role not in ORG_OWNER_ADMIN:
        raise HTTPException(
            status_code=_HTTP_403, detail="仅组织所有者/管理员可创建工作空间"
        )
    ws = Workspace(
        organization_id=org.id,
        name=name.strip(),
        owner_id=owner.id,
        status="active",
    )
    db.add(ws)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=owner.id,
        action="create_workspace",
        target_type="workspace",
        target_id=str(ws.id),
        organization_id=org.id,
    )
    return ws


async def list_workspaces(db: AsyncSession, org: Organization) -> list[Workspace]:
    return (
        await db.execute(
            select(Workspace)
            .where(
                Workspace.organization_id == org.id,
                Workspace.status == "active",
            )
            .order_by(Workspace.created_at.asc())
        )
    ).scalars().all()


# ===== 成员邀请（批量 / 域名限制 / 重复处理 / 过期 / 撤回） =====

async def _check_invite_domain(policy_domain_json: str | None, email: str):
    domains = _loads(policy_domain_json)
    if not domains:
        return
    email_domain = email.split("@")[-1].lower() if "@" in email else ""
    if email_domain not in [d.lower() for d in domains]:
        raise HTTPException(status_code=_HTTP_403, detail=f"邮箱域名 {email_domain} 不在允许范围内")


async def create_invitations(
    db: AsyncSession,
    org: Organization,
    inviter: User,
    emails: list[str],
    role: str,
    project_id: uuid.UUID | None,
    expires_hours: int = 72,
    domain_policy: str | None = None,
) -> list[OrgInvitation]:
    """批量邀请：域名限制 + 重复邀请处理（pending 去重）+ 过期时间。"""
    if role not in ORGANIZATION_ROLES:
        raise HTTPException(status_code=_HTTP_400, detail="非法组织角色")
    if expires_hours <= 0:
        raise HTTPException(status_code=_HTTP_400, detail="过期时间必须为正")
    if project_id is not None:
        await get_project_in_org(db, org.id, project_id)

    created: list[OrgInvitation] = []
    for email in emails:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=_HTTP_400, detail=f"非法邮箱: {email}")
        await _check_invite_domain(domain_policy, email)

        existing = (
            await db.execute(
                select(OrgInvitation).where(
                    OrgInvitation.organization_id == org.id,
                    OrgInvitation.invitee_email == email,
                    OrgInvitation.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            created.append(existing)
            continue

        token = generate_token()
        inv = OrgInvitation(
            organization_id=org.id,
            project_id=project_id,
            inviter_id=inviter.id,
            invitee_email=email,
            role=role,
            token_hash=hash_token(token),
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=expires_hours),
        )
        db.add(inv)
        created.append(inv)
        await audit_service.record(
            db,
            actor_id=inviter.id,
            action="invite_member",
            target_type="organization",
            target_id=str(org.id),
            organization_id=org.id,
            after_value=f"email={email} role={role}",
        )
    await db.flush()
    return created


async def accept_invitation(
    db: AsyncSession, user: User, raw_token: str
) -> OrgInvitation:
    inv = (
        await db.execute(
            select(OrgInvitation).where(
                OrgInvitation.token_hash == hash_token(raw_token)
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=_HTTP_404, detail="邀请不存在")
    if inv.status != "pending":
        raise HTTPException(status_code=_HTTP_400, detail="邀请已失效")
    if inv.expires_at < datetime.utcnow():
        inv.status = "expired"
        await db.flush()
        raise HTTPException(status_code=_HTTP_400, detail="邀请已过期")

    member = await get_org_member(db, inv.organization_id, user.id)
    if member is None:
        member = OrganizationMember(
            organization_id=inv.organization_id,
            user_id=user.id,
            role=inv.role,
            status="active",
            joined_at=datetime.utcnow(),
        )
        db.add(member)
    elif member.status == "left":
        member.status = "active"
        member.role = inv.role
        member.joined_at = datetime.utcnow()

    inv.status = "accepted"
    inv.accepted_at = datetime.utcnow()
    await db.flush()
    await audit_service.record(
        db,
        actor_id=user.id,
        action="accept_invitation",
        target_type="organization",
        target_id=str(inv.organization_id),
        organization_id=inv.organization_id,
    )
    return inv


async def revoke_invitation(
    db: AsyncSession, org: Organization, actor: User, invitation_id: uuid.UUID
) -> OrgInvitation:
    await require_org_role(db, org.id, actor.id, ORG_OWNER_ADMIN)
    inv = await db.get(OrgInvitation, invitation_id)
    if inv is None or inv.organization_id != org.id:
        raise HTTPException(status_code=_HTTP_404, detail="邀请不存在")
    if inv.status != "pending":
        raise HTTPException(status_code=_HTTP_400, detail="仅待处理邀请可撤回")
    inv.status = "revoked"
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="revoke_invitation",
        target_type="org_invitation",
        target_id=str(inv.id),
        organization_id=org.id,
    )
    return inv


async def update_member_role(
    db: AsyncSession,
    org: Organization,
    actor: User,
    member_id: uuid.UUID,
    new_role: str,
) -> OrganizationMember:
    """修改组织成员角色（管理员），记录变更前后值。"""
    await require_org_role(db, org.id, actor.id, ORG_OWNER_ADMIN)
    if new_role not in ORGANIZATION_ROLES:
        raise HTTPException(status_code=_HTTP_400, detail="非法组织角色")
    member = await db.get(OrganizationMember, member_id)
    if member is None or member.organization_id != org.id:
        raise HTTPException(status_code=_HTTP_404, detail="成员不存在")
    if member.role == "owner" and member.user_id == org.owner_id:
        # 组织所有者角色不可由普通管理员降级（仅所有者可改所有者）
        await require_org_role(db, org.id, actor.id, {"owner"})
    old = member.role
    member.role = new_role
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="change_member_role",
        target_type="organization_member",
        target_id=str(member.id),
        organization_id=org.id,
        before_value=old,
        after_value=new_role,
    )
    return member


async def list_members(db: AsyncSession, org: Organization) -> list[OrganizationMember]:
    return (
        await db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.status == "active",
            )
            .order_by(OrganizationMember.joined_at.asc())
        )
    ).scalars().all()


async def list_members_with_users(db: AsyncSession, org: Organization) -> list[dict]:
    """成员列表（含用户昵称/邮箱）：仅返回 active 成员，供前端协作权限页展示。"""
    rows = await db.execute(
        select(OrganizationMember, User.username, User.email)
        .join(User, User.id == OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.status == "active",
        )
        .order_by(OrganizationMember.joined_at.asc())
    )
    return [
        {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "username": username or "",
            "email": email or "",
            "role": m.role,
            "status": m.status,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        }
        for m, username, email in rows.all()
    ]


async def list_invitations(
    db: AsyncSession,
    org: Organization,
    actor: User,
    status: str | None = None,
) -> list[OrgInvitation]:
    """组织待处理邀请列表：仅 owner/admin 可查看。"""
    await require_org_role(db, org.id, actor.id, ORG_OWNER_ADMIN)
    filters = [OrgInvitation.organization_id == org.id]
    if status:
        if status not in ORG_INVITATION_STATUSES:
            raise HTTPException(status_code=_HTTP_400, detail="非法邀请状态")
        filters.append(OrgInvitation.status == status)
    return (
        await db.execute(
            select(OrgInvitation)
            .where(*filters)
            .order_by(OrgInvitation.created_at.desc())
            .limit(100)
        )
    ).scalars().all()


# ===== 离职处理 =====

async def offboard_member(
    db: AsyncSession,
    org: Organization,
    actor: User,
    user_id: uuid.UUID,
    reason: str | None = None,
) -> dict:
    """成员离职：撤销登录权限（成员置 left）+ 移交项目负责人 + 处理未完成审核任务 +
    处理私有文件标记；保留历史编辑记录与已发布归属，不删历史操作记录。"""
    await require_org_role(db, org.id, actor.id, ORG_OWNER_ADMIN)
    member = await get_org_member(db, org.id, user_id)
    if member is None:
        raise HTTPException(status_code=_HTTP_404, detail="成员不存在或已离职")

    # 1. 撤销组织内访问权限
    member.status = "left"

    # 2. 移交项目负责人权限：将该成员负责的项目移交给管理员/所有者
    lead_projects = (
        await db.execute(
            select(Project).where(
                Project.organization_id == org.id,
                Project.project_lead_id == user_id,
            )
        )
    ).scalars().all()
    successor = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.status == "active",
                OrganizationMember.role.in_(["owner", "admin"]),
                OrganizationMember.user_id != user_id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    successor_id = successor.user_id if successor is not None else org.owner_id
    for p in lead_projects:
        p.project_lead_id = successor_id

    # 3. 处理未完成审核任务（转交给继任负责人）
    open_tasks = (
        await db.execute(
            select(ReviewTask).where(
                ReviewTask.organization_id == org.id,
                ReviewTask.status.in_(["reviewing", "needs_changes", "re_reviewing"]),
                ReviewTask.submitter_id == user_id,
            )
        )
    ).scalars().all()
    for t in open_tasks:
        t.reviewer_id = successor_id
        t.comment = (t.comment or "") + f" [离职移交: {reason or '成员离职'}]"

    # 4. 处理私有文件：标记归属组织保留（不删除）
    private_files = (
        await db.execute(
            select(StoredFile).where(
                StoredFile.created_by == user_id,
                StoredFile.is_private.is_(True),
                StoredFile.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    file_count = len(private_files)

    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="offboard_member",
        target_type="organization_member",
        target_id=str(member.id),
        organization_id=org.id,
        reason=reason,
        before_value="active",
        after_value="left",
        metadata={
            "moved_projects": len(lead_projects),
            "open_review_tasks": len(open_tasks),
            "private_files_kept": file_count,
            "successor_id": str(successor_id),
        },
    )
    return {
        "offboarded": True,
        "user_id": str(user_id),
        "moved_projects": len(lead_projects),
        "handled_review_tasks": len(open_tasks),
        "private_files_kept": file_count,
        "successor_id": str(successor_id),
    }


# ===== 项目协作状态流转 =====

async def change_project_collab_status(
    db: AsyncSession,
    org: Organization,
    actor: User,
    project: Project,
    new_status: str,
) -> Project:
    """项目状态流转（草稿→协作中→待审核→修改中→已通过→已发布→已归档）。

    权限：提交审核=作者/编辑/主编；通过=主编/管理员/所有者；退回=主编/编辑；
    发布=主编/管理员/所有者；归档=管理员/所有者/项目负责人。
    """
    if new_status not in COLLAB_STATUS_FLOW:
        raise HTTPException(status_code=_HTTP_400, detail="非法项目协作状态")
    if new_status not in COLLAB_STATUS_FLOW.get(project.collab_status, []):
        raise HTTPException(
            status_code=_HTTP_400,
            detail=f"项目状态不可从 {project.collab_status} 流转到 {new_status}",
        )

    org_role = await get_org_role(db, org.id, actor.id)
    project_role = await get_project_member_role(db, project.id, actor.id)
    allowed_by_action = {
        "collaborating": ORG_MANAGERS,
        "pending_review": {"owner", "admin", "chief_editor", "editor", "author"},
        "revising": {"owner", "admin", "chief_editor", "editor", "author"},
        "approved": ORG_PUBLISH,
        "published": ORG_PUBLISH,
        "archived": ORG_OWNER_ADMIN | {"project_lead"},
    }
    if org_role not in allowed_by_action.get(new_status, set()) and project_role not in ("owner", "editor"):
        raise HTTPException(status_code=_HTTP_403, detail="无权流转项目状态")

    old = project.collab_status
    project.collab_status = new_status
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="project_status_change",
        target_type="project",
        target_id=str(project.id),
        organization_id=org.id,
        before_value=old,
        after_value=new_status,
    )
    return project


# ===== 审计日志查询（组织范围，管理员查日志本身被记录） =====

async def list_org_audit_logs(
    db: AsyncSession,
    org: Organization,
    actor: User,
    page: int = 1,
    page_size: int = 20,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> tuple[list, int]:
    """组织审计日志：管理员/所有者可查；查看行为本身记录审计。
    支持按操作类型 / 操作者 / 时间范围筛选。"""
    await require_org_role(db, org.id, actor.id, ORG_OWNER_ADMIN)
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="view_audit_logs",
        target_type="organization",
        target_id=str(org.id),
        organization_id=org.id,
    )
    filters = [AuditLog.organization_id == org.id]
    if action:
        filters.append(AuditLog.action == action)
    if actor_id is not None:
        filters.append(AuditLog.actor_id == actor_id)
    if from_dt is not None:
        filters.append(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        filters.append(AuditLog.created_at <= to_dt)
    total = (
        await db.execute(
            select(func.count()).select_from(AuditLog).where(*filters)
        )
    ).scalar() or 0
    rows = (
        await db.execute(
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
