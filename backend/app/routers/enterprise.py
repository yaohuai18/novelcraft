# 第二十四批「企业级协作、审核与内容治理」- 组织/工作空间/成员/邀请/离职/项目状态/审计 路由
# 组织 → 工作空间 → 项目 → 文档 层级；一人可入多组织，组织间数据严格隔离；
# 后端从登录用户授权关系确认组织归属，不信任前端传参；服务端全量校验。
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.billing_org import ORGANIZATION_ROLES, Organization, OrganizationMember
from app.models.project import Project
from app.models.user import User
from app.services import audit_service
from app.services import enterprise_service as svc

router = APIRouter(prefix="/api/enterprise", tags=["batch24-enterprise"])

_HTTP_403 = status.HTTP_403_FORBIDDEN


async def _load_org(org_id: uuid.UUID, db: AsyncSession) -> Organization:
    return await svc.get_org_by_id(db, org_id)


async def _require_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID):
    return await svc.require_org_member(db, org_id, user_id)


# ---------- 组织 ----------
class OrgCreateIn(BaseModel):
    name: str
    plan: str | None = None


@router.post("/organizations")
async def create_organization(
    body: OrgCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建组织：创建者自动成为组织所有者。"""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="组织名称不能为空")
    org = Organization(
        name=body.name.strip(),
        owner_id=current_user.id,
        status="active",
        plan=body.plan or "free",
    )
    db.add(org)
    await db.flush()
    member = OrganizationMember(
        organization_id=org.id,
        user_id=current_user.id,
        role="owner",
        status="active",
        joined_at=datetime.utcnow(),
    )
    db.add(member)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=current_user.id,
        action="create_organization",
        target_type="organization",
        target_id=str(org.id),
        organization_id=org.id,
        after_value=body.name.strip(),
    )
    await db.commit()
    return {"id": str(org.id), "name": org.name, "owner_id": str(org.owner_id), "plan": org.plan}


@router.get("/organizations")
async def my_organizations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户加入的所有组织（一人可入多组织）。"""
    rows = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.user_id == current_user.id,
                OrganizationMember.status == "active",
            )
        )
    ).scalars().all()
    orgs = []
    for m in rows:
        org = await db.get(Organization, m.organization_id)
        if org is not None:
            orgs.append({"id": str(org.id), "name": org.name, "role": m.role})
    return {"organizations": orgs}


@router.get("/organizations/{org_id}")
async def organization_detail(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    return {"id": str(org.id), "name": org.name, "owner_id": str(org.owner_id), "plan": org.plan, "status": org.status}


# ---------- 组织内项目（协作载体） ----------
class OrgProjectCreateIn(BaseModel):
    title: str
    genre: str | None = None
    background: str | None = None
    world_setting: str | None = None
    main_conflict: str | None = None
    ending_direction: str | None = None
    narrative_perspective: str | None = None
    writing_style: str | None = None
    forbidden_settings: str | None = None


@router.post("/organizations/{org_id}/projects")
async def create_org_project(
    org_id: uuid.UUID,
    body: OrgProjectCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """在组织内创建协作项目：仅 owner/admin 可建；项目绑定 organization_id，
    供审核中心/协作权限页使用；创建行为记录审计日志。"""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    org = await _load_org(org_id, db)
    await svc.require_org_role(db, org_id, current_user.id, svc.ORG_OWNER_ADMIN)
    project = Project(
        user_id=current_user.id,
        organization_id=org.id,
        **body.model_dump(exclude={"title"}),
        title=body.title.strip(),
    )
    db.add(project)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=current_user.id,
        action="create_org_project",
        target_type="project",
        target_id=str(project.id),
        organization_id=org.id,
        after_value=body.title.strip(),
    )
    await db.commit()
    return {
        "id": str(project.id),
        "title": project.title,
        "organization_id": str(project.organization_id) if project.organization_id else None,
        "status": project.status,
    }


@router.get("/organizations/{org_id}/projects")
async def list_org_projects(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """组织内项目列表：组织成员可查看，供审核中心选择待审核项目。"""
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows = (
        await db.execute(
            select(Project)
            .where(Project.organization_id == org.id)
            .order_by(Project.created_at.desc())
        )
    ).scalars().all()
    return {"projects": [{"id": str(p.id), "title": p.title, "status": p.status} for p in rows]}


# ---------- 工作空间 ----------
class WorkspaceCreateIn(BaseModel):
    name: str


@router.post("/organizations/{org_id}/workspaces")
async def create_workspace(
    org_id: uuid.UUID,
    body: WorkspaceCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    ws = await svc.create_workspace(db, org, current_user, body.name)
    await db.commit()
    return {"id": str(ws.id), "name": ws.name, "organization_id": str(ws.organization_id)}


@router.get("/organizations/{org_id}/workspaces")
async def list_workspaces(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows = await svc.list_workspaces(db, org)
    return {"workspaces": [{"id": str(w.id), "name": w.name} for w in rows]}


# ---------- 邀请 ----------
class InviteIn(BaseModel):
    emails: list[str]
    role: str
    project_id: uuid.UUID | None = None
    expires_hours: int = 72


@router.post("/organizations/{org_id}/invitations")
async def invite_members(
    org_id: uuid.UUID,
    body: InviteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量邀请：域名限制 + 重复邀请处理（pending 去重）+ 过期时间。"""
    org = await _load_org(org_id, db)
    await svc.require_org_role(db, org_id, current_user.id, svc.ORG_OWNER_ADMIN)
    from app.services.enterprise_admin_service import get_or_create_security_policy

    policy = await get_or_create_security_policy(db, org)
    invs = await svc.create_invitations(
        db,
        org,
        current_user,
        body.emails,
        body.role,
        body.project_id,
        body.expires_hours,
        domain_policy=policy.email_domain_restriction,
    )
    await db.commit()
    return {
        "created": [
            {"id": str(i.id), "email": i.invitee_email, "role": i.role, "status": i.status}
            for i in invs
        ]
    }


class AcceptInviteIn(BaseModel):
    token: str


@router.post("/invitations/accept")
async def accept_invitation(
    body: AcceptInviteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inv = await svc.accept_invitation(db, current_user, body.token)
    await db.commit()
    return {"accepted": True, "organization_id": str(inv.organization_id), "role": inv.role}


@router.post("/organizations/{org_id}/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    org_id: uuid.UUID,
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    inv = await svc.revoke_invitation(db, org, current_user, invitation_id)
    await db.commit()
    return {"revoked": True, "id": str(inv.id)}


# ---------- 成员与角色 ----------
class RoleUpdateIn(BaseModel):
    new_role: str


@router.patch("/organizations/{org_id}/members/{member_id}/role")
async def update_member_role(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    body: RoleUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    member = await svc.update_member_role(db, org, current_user, member_id, body.new_role)
    await db.commit()
    return {"id": str(member.id), "user_id": str(member.user_id), "role": member.role}


@router.get("/organizations/{org_id}/members")
async def list_members(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    members = await svc.list_members_with_users(db, org)
    return {"members": members}


@router.get("/organizations/{org_id}/invitations")
async def list_invitations(
    org_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """组织邀请列表（默认全部，可按 status 筛选），仅 owner/admin 可查看。"""
    org = await _load_org(org_id, db)
    invs = await svc.list_invitations(db, org, current_user, status_filter)
    await db.commit()
    return {
        "invitations": [
            {
                "id": str(i.id),
                "email": i.invitee_email,
                "role": i.role,
                "status": i.status,
                "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "inviter_id": str(i.inviter_id) if i.inviter_id else None,
            }
            for i in invs
        ]
    }


class OffboardIn(BaseModel):
    reason: str | None = None


@router.post("/organizations/{org_id}/members/{user_id}/offboard")
async def offboard_member(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    body: OffboardIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    result = await svc.offboard_member(db, org, current_user, user_id, body.reason)
    await db.commit()
    return result


# ---------- 项目状态流转 ----------
class StatusChangeIn(BaseModel):
    new_status: str


@router.patch("/organizations/{org_id}/projects/{project_id}/status")
async def change_project_status(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: StatusChangeIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    project = await svc.get_project_in_org(db, org_id, project_id)
    updated = await svc.change_project_collab_status(db, org, current_user, project, body.new_status)
    await db.commit()
    return {"id": str(updated.id), "collab_status": updated.collab_status}


# ---------- 审计日志 ----------
@router.get("/organizations/{org_id}/audit-logs")
async def org_audit_logs(
    org_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: str | None = Query(None, description="按操作类型筛选"),
    actor_id: uuid.UUID | None = Query(None, description="按操作者筛选"),
    from_dt: datetime | None = Query(None, description="起始时间"),
    to_dt: datetime | None = Query(None, description="结束时间"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    rows, total = await svc.list_org_audit_logs(
        db, org, current_user,
        page=page, page_size=page_size,
        action=action, actor_id=actor_id, from_dt=from_dt, to_dt=to_dt,
    )
    await db.commit()
    # 批量补齐操作者用户名/邮箱
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    actor_map: dict[str, tuple[str, str]] = {}
    if actor_ids:
        users = (
            await db.execute(select(User).where(User.id.in_(actor_ids)))
        ).scalars().all()
        actor_map = {str(u.id): (u.username, u.email) for u in users}
    return {
        "total": total,
        "logs": [
            {
                "id": str(r.id),
                "action": r.action,
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "actor_username": actor_map.get(str(r.actor_id), ("", ""))[0] if r.actor_id else "",
                "actor_email": actor_map.get(str(r.actor_id), ("", ""))[1] if r.actor_id else "",
                "target_type": r.target_type,
                "target_id": r.target_id,
                "before_value": r.before_value,
                "after_value": r.after_value,
                "metadata_json": r.metadata_json,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
