# 企业协作服务（第二十四批「企业级协作、审核与内容治理」规格六/七/八）
# 锁定编辑：开始编辑锁定章节 → 他人只读可评论 → 保存/退出释放；
#           异常退出/浏览器关闭/网络中断/长时未操作/多设备/锁定超时/管理员强制解除。
# 评论批注：复用/扩展 Comment（range_start/range_end/status/resolved_at），选中文字评论、回复、
#           提及、待处理/处理中/已解决/已忽略/已重新打开、筛选未解决、历史；评论不覆盖正文。
# 版本管理：查看/比较/恢复（复用 version_service）+ 标记重要（ContentVersion.is_important）。
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import COMMENT_STATUSES, Comment
from app.models.content_version import ContentVersion
from app.models.enterprise_collab import EDIT_LOCK_STATUSES, EditLock
from app.models.project import Project
from app.models.user import User
from app.services import audit_service
from app.services.enterprise_service import (
    can_comment,
    can_edit_target,
    get_org_member,
    get_project_in_org,
    get_project_member_role,
    get_org_role,
    ORG_OWNER_ADMIN,
)

_HTTP_400 = status.HTTP_400_BAD_REQUEST
_HTTP_403 = status.HTTP_403_FORBIDDEN
_HTTP_404 = status.HTTP_404_NOT_FOUND

DEFAULT_LOCK_TTL_MINUTES = 30
LOCK_HEARTBEAT_GRACE_MINUTES = 5


def _active_lock_expired(lock: EditLock, now: datetime) -> bool:
    if lock.status != "locked":
        return True
    if lock.expires_at is not None and lock.expires_at < now:
        return True
    # 长时未操作：心跳超过 TTL 视为超时
    if lock.last_heartbeat_at + timedelta(minutes=DEFAULT_LOCK_TTL_MINUTES) < now:
        return True
    return False


# ===== 编辑锁 =====

async def acquire_edit_lock(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    ttl_minutes: int = DEFAULT_LOCK_TTL_MINUTES,
    resource_creator_id: uuid.UUID | None = None,
) -> EditLock:
    """开始编辑锁定：校验编辑权限；同目标他人锁 → 拒绝；自己已有锁 → 续租（多设备）。

    超时/异常退出遗留锁 → 标记 expired 后重新获取；网络中断恢复通过心跳续期。
    """
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    org_member = await get_org_member(db, getattr(org, "id"), actor.id)
    if org_member is None:
        raise HTTPException(status_code=_HTTP_403, detail="无权访问该组织")
    org_role = org_member.role
    project_role = await get_project_member_role(db, project_id, actor.id)
    if not can_edit_target(org_role, project_role, resource_creator_id, actor.id):
        raise HTTPException(status_code=_HTTP_403, detail="当前角色无权编辑该内容")

    now = datetime.utcnow()
    # 清理该目标上已失效的锁
    stale = (
        await db.execute(
            select(EditLock).where(
                EditLock.project_id == project_id,
                EditLock.resource_type == resource_type,
                EditLock.resource_id == resource_id,
                EditLock.status == "locked",
            )
        )
    ).scalars().all()
    for lock in stale:
        if _active_lock_expired(lock, now):
            lock.status = "expired"
            lock.released_at = now

    existing = (
        await db.execute(
            select(EditLock).where(
                EditLock.project_id == project_id,
                EditLock.resource_type == resource_type,
                EditLock.resource_id == resource_id,
                EditLock.status == "locked",
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.user_id != actor.id:
        raise HTTPException(
            status_code=_HTTP_403,
            detail="该内容正被其他成员编辑，当前仅可查看或评论",
        )

    expires_at = now + timedelta(minutes=ttl_minutes)
    if existing is not None:
        existing.last_heartbeat_at = now
        existing.expires_at = expires_at
        lock = existing
    else:
        lock = EditLock(
            organization_id=getattr(org, "id"),
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=actor.id,
            status="locked",
            acquired_at=now,
            last_heartbeat_at=now,
            expires_at=expires_at,
        )
        db.add(lock)
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="acquire_edit_lock",
        target_type=resource_type,
        target_id=str(resource_id),
        organization_id=getattr(org, "id"),
    )
    return lock


async def heartbeat_edit_lock(
    db: AsyncSession,
    org,
    actor: User,
    lock_id: uuid.UUID,
) -> EditLock:
    """长时编辑心跳续期；网络中断恢复：锁未超时则延续，超时则拒绝并要求重新获取。"""
    lock = await db.get(EditLock, lock_id)
    if lock is None or lock.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="编辑锁不存在")
    now = datetime.utcnow()
    if lock.status != "locked" or _active_lock_expired(lock, now):
        lock.status = "expired"
        lock.released_at = now
        await db.flush()
        raise HTTPException(status_code=_HTTP_403, detail="编辑锁已超时，请重新获取")
    if lock.user_id != actor.id:
        raise HTTPException(status_code=_HTTP_403, detail="非锁主无权续期")
    lock.last_heartbeat_at = now
    lock.expires_at = now + timedelta(minutes=DEFAULT_LOCK_TTL_MINUTES)
    await db.flush()
    return lock


async def release_edit_lock(
    db: AsyncSession,
    org,
    actor: User,
    lock_id: uuid.UUID,
) -> EditLock:
    """保存/退出编辑释放锁（仅锁主或管理员）。"""
    lock = await db.get(EditLock, lock_id)
    if lock is None or lock.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="编辑锁不存在")
    if lock.user_id != actor.id:
        await _require_admin(db, org, actor)
    if lock.status == "locked":
        lock.status = "released"
        lock.released_at = datetime.utcnow()
        await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="release_edit_lock",
        target_type=lock.resource_type,
        target_id=str(lock.resource_id),
        organization_id=getattr(org, "id"),
    )
    return lock


async def force_release_edit_lock(
    db: AsyncSession,
    org,
    actor: User,
    lock_id: uuid.UUID,
) -> EditLock:
    """管理员强制解除编辑锁。"""
    await _require_admin(db, org, actor)
    lock = await db.get(EditLock, lock_id)
    if lock is None or lock.organization_id != getattr(org, "id"):
        raise HTTPException(status_code=_HTTP_404, detail="编辑锁不存在")
    lock.status = "force_released"
    lock.released_at = datetime.utcnow()
    await db.flush()
    await audit_service.record(
        db,
        actor_id=actor.id,
        action="force_release_edit_lock",
        target_type=lock.resource_type,
        target_id=str(lock.resource_id),
        organization_id=getattr(org, "id"),
    )
    return lock


async def _require_admin(db: AsyncSession, org, actor: User):
    member = await get_org_member(db, getattr(org, "id"), actor.id)
    if member is None or member.role not in ORG_OWNER_ADMIN:
        raise HTTPException(status_code=_HTTP_403, detail="仅组织管理员可执行该操作")


async def list_edit_locks(
    db: AsyncSession, org, project_id: uuid.UUID
) -> list[EditLock]:
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    rows = (
        await db.execute(
            select(EditLock)
            .where(
                EditLock.project_id == project.id,
                EditLock.status == "locked",
            )
            .order_by(EditLock.acquired_at.asc())
        )
    ).scalars().all()
    return list(rows)


# ===== 评论批注 =====

async def create_comment_ex(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    resource_type: str,
    resource_id: str,
    content: str,
    parent_id: uuid.UUID | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    mentions: list[str] | None = None,
) -> Comment:
    """选中文字发表评论 / 回复 / 提及成员；状态初始待处理；评论不覆盖正文。"""
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    org_member = await get_org_member(db, getattr(org, "id"), actor.id)
    if org_member is None:
        raise HTTPException(status_code=_HTTP_403, detail="无权访问该组织")
    project_role = await get_project_member_role(db, project.id, actor.id)
    if not can_comment(org_member.role, project_role):
        raise HTTPException(status_code=_HTTP_403, detail="当前角色无权发表评论")
    if not content.strip():
        raise HTTPException(status_code=_HTTP_400, detail="评论内容不能为空")

    # 锚点校验（选中文字批注）
    if range_start is not None or range_end is not None:
        if range_start is None or range_end is None or range_end < range_start:
            raise HTTPException(status_code=_HTTP_400, detail="评论锚点区间非法")

    mention_text = ""
    if mentions:
        mention_text = " @" + " @".join(mentions) + " "

    comment = Comment(
        project_id=project.id,
        organization_id=project.organization_id,
        target_type=resource_type,
        target_id=resource_id,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=actor.id,
        parent_id=parent_id,
        content=content + mention_text,
        status="pending",
        range_start=range_start,
        range_end=range_end,
    )
    db.add(comment)
    await db.flush()
    return comment


async def update_comment_status(
    db: AsyncSession,
    org,
    actor: User,
    comment_id: uuid.UUID,
    new_status: str,
) -> Comment:
    """待处理/处理中/已解决/已忽略/已重新打开；解决时记录 resolved_at。"""
    if new_status not in COMMENT_STATUSES:
        raise HTTPException(status_code=_HTTP_400, detail="非法评论状态")
    comment = await db.get(Comment, comment_id)
    if comment is None or comment.project_id is None:
        raise HTTPException(status_code=_HTTP_404, detail="评论不存在")
    project = await get_project_in_org(db, getattr(org, "id"), comment.project_id)
    org_member = await get_org_member(db, getattr(org, "id"), actor.id)
    if org_member is None:
        raise HTTPException(status_code=_HTTP_403, detail="无权访问该组织")
    allowed = comment.user_id == actor.id or org_member.role in (
        ORG_OWNER_ADMIN | {"editor", "chief_editor", "project_lead"}
    )
    if not allowed:
        project_role = await get_project_member_role(db, project.id, actor.id)
        allowed = project_role in ("owner", "editor")
    if not allowed:
        raise HTTPException(status_code=_HTTP_403, detail="无权处理该评论")
    comment.status = new_status
    comment.resolved_at = datetime.utcnow() if new_status == "resolved" else None
    await db.flush()
    return comment


async def list_comments_ex(
    db: AsyncSession,
    org,
    project_id: uuid.UUID,
    resource_type: str | None = None,
    resource_id: str | None = None,
    unresolved_only: bool = False,
) -> list[Comment]:
    """评论历史 / 筛选未解决评论（pending / processing）。"""
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    filters = [Comment.project_id == project.id]
    if resource_type:
        filters.append(Comment.resource_type == resource_type)
    if resource_id:
        filters.append(Comment.resource_id == resource_id)
    if unresolved_only:
        filters.append(Comment.status.in_(["pending", "processing"]))
    rows = (
        await db.execute(
            select(Comment).where(*filters).order_by(Comment.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


# ===== 版本管理（复用 version_service + 标记重要） =====

async def mark_version_important(
    db: AsyncSession,
    org,
    actor: User,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    important: bool,
) -> ContentVersion:
    """标记重要版本（主编/管理员/所有者/项目负责人）。"""
    project = await get_project_in_org(db, getattr(org, "id"), project_id)
    version = (
        await db.execute(
            select(ContentVersion).where(
                ContentVersion.id == version_id,
                ContentVersion.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=_HTTP_404, detail="版本不存在")
    org_role = await get_org_role(db, getattr(org, "id"), actor.id)
    if org_role not in (ORG_OWNER_ADMIN | {"project_lead", "chief_editor", "editor"}):
        raise HTTPException(status_code=_HTTP_403, detail="无权标记版本")
    version.is_important = important
    await db.flush()
    return version
