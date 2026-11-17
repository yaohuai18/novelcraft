# 协作服务（第十五批「通知、评论与协作反馈」规格实现）
# 项目成员与角色权限、项目邀请（令牌哈希）、活动日志。
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collaboration import (
    ACTIVITY_ACTIONS,
    INVITATION_STATUSES,
    MEMBER_STATUSES,
    PROJECT_ROLES,
    ActivityLog,
    ProjectInvitation,
    ProjectMember,
)
from app.models.project import Project
from app.models.user import User


def _dumps(payload: dict | None) -> str | None:
    return json.dumps(payload, ensure_ascii=False) if payload is not None else None


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def hash_invite_token(token: str) -> str:
    """邀请令牌哈希（sha256），令牌只存哈希不存明文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_invite_token() -> str:
    """生成 URL 安全随机令牌（明文仅创建时返回一次）。"""
    return secrets.token_urlsafe(32)


# ==================== 角色与权限 ====================

async def get_member(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ProjectMember | None:
    return (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.status == "active",
            )
        )
    ).scalar_one_or_none()


async def get_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> str | None:
    """返回用户在项目中的角色：项目创建者恒为 owner；否则取成员角色；无则 None。"""
    project = await db.get(Project, project_id)
    if project is not None and project.user_id == user_id:
        return "owner"
    member = await get_member(db, project_id, user_id)
    return member.role if member is not None else None


async def require_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    allowed: set[str],
) -> str:
    """校验用户角色在 allowed 集合内，否则返回 None。"""
    role = await get_role(db, project_id, user_id)
    if role is None or role not in allowed:
        return None
    return role


async def ensure_owner_member(db: AsyncSession, project: Project) -> ProjectMember | None:
    """项目创建者自动登记为 owner 成员（幂等）。"""
    member = await get_member(db, project.id, project.user_id)
    if member is not None:
        return member
    member = ProjectMember(
        project_id=project.id,
        user_id=project.user_id,
        role="owner",
        status="active",
        invited_by=None,
        joined_at=datetime.utcnow(),
    )
    db.add(member)
    await db.flush()
    return member


async def list_members(
    db: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[list[ProjectMember], int]:
    result = await db.execute(
        select(ProjectMember)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.status == "active",
        )
        .order_by(ProjectMember.joined_at.asc())
    )
    members = list(result.scalars().all())
    return members, len(members)


async def update_member_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    new_role: str,
) -> ProjectMember:
    member = await get_member(db, project_id, user_id)
    if member is None:
        raise ValueError("成员不存在")
    if new_role not in PROJECT_ROLES:
        raise ValueError("非法角色")
    if new_role == "owner":
        raise ValueError("不能通过成员角色变更转让 owner")
    member.role = new_role
    await db.flush()
    return member


async def remove_member(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    member = await get_member(db, project_id, user_id)
    if member is None or member.role == "owner":
        return False
    member.status = "removed"
    await db.flush()
    return True


# ==================== 邀请流程 ====================

async def create_invitation(
    db: AsyncSession,
    project: Project,
    inviter: User,
    invitee_email: str,
    role: str,
    expires_in_days: int = 7,
) -> tuple[ProjectInvitation, str]:
    """创建邀请：生成随机令牌，仅保存 sha256 哈希；返回 (邀请记录, 明文令牌)。"""
    if role not in PROJECT_ROLES or role == "owner":
        raise ValueError("非法邀请角色")

    token = generate_invite_token()
    now = datetime.utcnow()
    invitation = ProjectInvitation(
        project_id=project.id,
        inviter_id=inviter.id,
        invitee_email=invitee_email.strip().lower(),
        role=role,
        token_hash=hash_invite_token(token),
        status="pending",
        expires_at=now + timedelta(days=expires_in_days),
    )
    db.add(invitation)
    await db.flush()
    return invitation, token


async def get_pending_invitation_by_token(
    db: AsyncSession,
    token: str,
) -> ProjectInvitation | None:
    invitation = (
        await db.execute(
            select(ProjectInvitation).where(
                ProjectInvitation.token_hash == hash_invite_token(token),
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        return None
    # 过期自动落状态
    if invitation.status == "pending" and invitation.expires_at < datetime.utcnow():
        invitation.status = "expired"
        await db.flush()
    return invitation


async def accept_invitation(
    db: AsyncSession,
    invitation: ProjectInvitation,
    user: User,
) -> ProjectMember:
    """接收邀请：邮箱必须匹配、状态必须 pending、未过期；加入项目成员。"""
    if invitation.status != "pending":
        raise ValueError("邀请已处理，无法接受")
    if user.email.strip().lower() != invitation.invitee_email:
        raise ValueError("邀请邮箱与当前账号不匹配")
    if invitation.expires_at < datetime.utcnow():
        invitation.status = "expired"
        await db.flush()
        raise ValueError("邀请已过期")

    project = await db.get(Project, invitation.project_id)
    if project is None:
        raise ValueError("作品不存在")

    invitation.status = "accepted"
    invitation.accepted_at = datetime.utcnow()

    member = await get_member(db, project.id, user.id)
    if member is None:
        member = ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=invitation.role,
            status="active",
            invited_by=invitation.inviter_id,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
    else:
        if member.status != "active":
            member.status = "active"
            member.role = invitation.role
            member.joined_at = datetime.utcnow()
        member.role = invitation.role
        member.invited_by = invitation.inviter_id
    await db.flush()
    return member


async def decline_invitation(
    db: AsyncSession,
    invitation: ProjectInvitation,
    user: User,
) -> None:
    if invitation.status != "pending":
        raise ValueError("邀请已处理，无法拒绝")
    if user.email.strip().lower() != invitation.invitee_email:
        raise ValueError("邀请邮箱与当前账号不匹配")
    invitation.status = "declined"
    await db.flush()


async def revoke_invitation(
    db: AsyncSession,
    invitation: ProjectInvitation,
) -> None:
    if invitation.status != "pending":
        raise ValueError("仅待处理邀请可撤销")
    invitation.status = "revoked"
    await db.flush()


async def list_invitations(
    db: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
) -> list[ProjectInvitation]:
    filters = [ProjectInvitation.project_id == project_id]
    if status:
        filters.append(ProjectInvitation.status == status)
    result = await db.execute(
        select(ProjectInvitation)
        .where(*filters)
        .order_by(ProjectInvitation.created_at.desc())
    )
    return list(result.scalars().all())


async def check_expiring_invitations(
    db: AsyncSession,
    *,
    hours: int = 24,
) -> list[ProjectInvitation]:
    """第十五批：找出未来 hours 小时内即将过期的 pending 邀请（初期轮询用）。

    返回即将过期的邀请列表；调用方应针对每个邀请触发
    event_service.emit_invitation_expiring 生成「邀请即将过期」通知。
    """
    now = datetime.utcnow()
    deadline = now + timedelta(hours=hours)
    result = await db.execute(
        select(ProjectInvitation)
        .where(
            ProjectInvitation.status == "pending",
            ProjectInvitation.expires_at.isnot(None),
            ProjectInvitation.expires_at > now,
            ProjectInvitation.expires_at <= deadline,
        )
        .order_by(ProjectInvitation.expires_at.asc())
    )
    return list(result.scalars().all())


# ==================== 活动日志 ====================

async def add_activity(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
) -> ActivityLog:
    if action not in ACTIVITY_ACTIONS:
        raise ValueError(f"非法活动动作: {action}")
    log = ActivityLog(
        project_id=project_id,
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata_json=_dumps(metadata),
    )
    db.add(log)
    await db.flush()
    return log


async def list_activity(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ActivityLog], int]:
    filters = [ActivityLog.project_id == project_id]
    if action:
        filters.append(ActivityLog.action == action)
    if user_id is not None:
        filters.append(ActivityLog.user_id == user_id)
    total = (
        await db.execute(select(func.count(ActivityLog.id)).where(*filters))
    ).scalar_one()
    result = await db.execute(
        select(ActivityLog)
        .where(*filters)
        .order_by(ActivityLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total
