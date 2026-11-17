# 协作路由（第十五批「通知、评论与协作反馈」规格实现）
# 成员与角色、项目邀请（创建/撤销/接受/拒绝）、活动日志；触发加入/接受通知与活动记录。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.collaboration import ProjectInvitation, ProjectMember
from app.models.project import Project
from app.models.user import User
from app.schemas.collaboration import (
    ActivityLogListResponse,
    ActivityLogResponse,
    InvitationAcceptResponse,
    InvitationCreate,
    InvitationResponse,
    InvitationTokenRequest,
    MemberRoleUpdate,
    ProjectMemberResponse,
)
from app.services import collaboration_service, event_service

router = APIRouter(
    prefix="/api",
    tags=["协作"]
)


async def get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品不存在")
    return project


async def _member_response(
    db: AsyncSession, member: ProjectMember,
) -> ProjectMemberResponse:
    user = await db.get(User, member.user_id)
    return ProjectMemberResponse(
        id=member.id,
        project_id=member.project_id,
        user_id=member.user_id,
        role=member.role,
        status=member.status,
        invited_by=member.invited_by,
        joined_at=member.joined_at,
        created_at=member.created_at,
        username=user.username if user else None,
    )


async def _invitation_response(
    invitation: ProjectInvitation,
    token: str | None = None,
) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        project_id=invitation.project_id,
        inviter_id=invitation.inviter_id,
        invitee_email=invitation.invitee_email,
        role=invitation.role,
        status=invitation.status,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        created_at=invitation.created_at,
        token=token,
    )


async def _activity_response(
    db: AsyncSession, log,
) -> ActivityLogResponse:
    username = None
    if log.user_id is not None:
        user = await db.get(User, log.user_id)
        username = user.username if user else None
    return ActivityLogResponse(
        id=log.id,
        project_id=log.project_id,
        user_id=log.user_id,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        metadata=collaboration_service._loads(log.metadata_json),
        created_at=log.created_at,
        username=username,
    )


# ==================== 成员与角色 ====================

@router.get("/projects/{project_id}/members", response_model=list[ProjectMemberResponse])
async def list_members(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """成员列表（项目成员可见）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="无权查看项目成员")
    members, _ = await collaboration_service.list_members(db, project.id)
    return [await _member_response(db, m) for m in members]


@router.put("/projects/{project_id}/members/{user_id}/role", response_model=ProjectMemberResponse)
async def update_member_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberRoleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """调整成员角色（仅 owner）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="仅项目作者可调整成员角色")
    try:
        member = await collaboration_service.update_member_role(
            db, project.id, user_id, data.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="member.role_changed", target_type="user", target_id=str(user_id),
        metadata={"role": data.role},
    )
    await db.commit()
    await db.refresh(member)
    return await _member_response(db, member)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除成员（仅 owner，不能移除自己）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="仅项目作者可移除成员")
    try:
        ok = await collaboration_service.remove_member(db, project.id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=400, detail="无法移除该项目作者")
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="member.removed", target_type="user", target_id=str(user_id),
    )
    await db.commit()


# ==================== 邀请流程 ====================

@router.post(
    "/projects/{project_id}/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    project_id: uuid.UUID,
    data: InvitationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建邀请（owner/editor）；返回一次性明文令牌，服务端只存哈希。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="无权邀请协作者")
    try:
        invitation, token = await collaboration_service.create_invitation(
            db, project, current_user,
            invitee_email=data.invitee_email,
            role=data.role,
            expires_in_days=data.expires_in_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="member.invite", target_type="user", target_id=data.invitee_email,
        metadata={"role": data.role, "invitation_id": str(invitation.id)},
    )
    await db.commit()
    await db.refresh(invitation)
    return await _invitation_response(invitation, token=token)


@router.get("/projects/{project_id}/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    project_id: uuid.UUID,
    invitation_status: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """邀请列表（owner/editor 可见；不返回令牌）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="无权查看邀请")
    invitations = await collaboration_service.list_invitations(
        db, project.id, status=invitation_status,
    )
    return [await _invitation_response(i) for i in invitations]


@router.post(
    "/projects/{project_id}/invitations/{invitation_id}/revoke",
    response_model=InvitationResponse,
)
async def revoke_invitation(
    project_id: uuid.UUID,
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """撤销邀请（仅 owner）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role != "owner":
        raise HTTPException(status_code=403, detail="仅项目作者可撤销邀请")
    invitation = await db.get(ProjectInvitation, invitation_id)
    if invitation is None or invitation.project_id != project.id:
        raise HTTPException(status_code=404, detail="邀请不存在")
    try:
        await collaboration_service.revoke_invitation(db, invitation)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="member.invitation_revoked", target_type="invitation", target_id=str(invitation.id),
    )
    await db.commit()
    await db.refresh(invitation)
    return await _invitation_response(invitation)


@router.post("/invitations/accept", response_model=InvitationAcceptResponse)
async def accept_invitation(
    data: InvitationTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """接收邀请：邮箱匹配 → 加入项目成员 → 记录活动并通知邀请人/作者。"""
    invitation = await collaboration_service.get_pending_invitation_by_token(
        db, data.token,
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请不存在或已被处理")
    project = await get_project_or_404(db, invitation.project_id)
    try:
        member = await collaboration_service.accept_invitation(
            db, invitation, current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await collaboration_service.ensure_owner_member(db, project)
    await collaboration_service.add_activity(
        db, project_id=project.id, user_id=current_user.id,
        action="member.join", target_type="invitation", target_id=str(invitation.id),
        metadata={"role": member.role},
    )
    await event_service.emit_invitation_accepted(db, project, invitation, current_user)
    await event_service.emit_member_joined(db, project, member, current_user)

    await db.commit()
    await db.refresh(member)
    resp = await _member_response(db, member)
    return InvitationAcceptResponse(
        invitation_id=invitation.id,
        member=resp,
        role=member.role,
    )


@router.post("/invitations/decline", response_model=InvitationResponse)
async def decline_invitation(
    data: InvitationTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """拒绝邀请。"""
    invitation = await collaboration_service.get_pending_invitation_by_token(
        db, data.token,
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请不存在或已被处理")
    try:
        await collaboration_service.decline_invitation(db, invitation, current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    await db.refresh(invitation)
    return await _invitation_response(invitation)


# ==================== 活动日志 ====================

@router.get("/projects/{project_id}/activity", response_model=ActivityLogListResponse)
async def list_activity(
    project_id: uuid.UUID,
    action: str | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """协作活动日志（项目成员可见）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="无权查看活动日志")
    logs, total = await collaboration_service.list_activity(
        db, project.id, action=action, user_id=user_id,
        page=page, page_size=page_size,
    )
    return ActivityLogListResponse(
        items=[await _activity_response(db, log) for log in logs],
        total=total,
        page=page,
        page_size=page_size,
    )
