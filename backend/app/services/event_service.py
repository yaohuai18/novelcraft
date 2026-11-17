# 事件通知服务（第十五批「通知、评论与协作反馈」规格实现）
# 业务操作成功 → 发布领域事件 → 通知服务创建站内通知（按偏好渠道）；
# 同时写入活动日志。初期客户端轮询未读数（30-60 秒）消费站内通知。
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collaboration import ActivityLog, ProjectInvitation, ProjectMember
from app.models.comment import Comment
from app.models.notification import Notification
from app.models.project import Project
from app.models.user import User
from app.services import collaboration_service, notification_service


def _safe(fn):
    """通知/日志失败不阻断业务（事件机制：业务成功 → 尽力通知）。"""
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            return None
    return wrapper


async def _notify_project_owner(
    db: AsyncSession,
    project: Project,
    *,
    type: str,
    title: str,
    content: str | None = None,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict | None = None,
    exclude_user_id: uuid.UUID | None = None,
) -> Notification | None:
    """向项目作者（owner）发送通知；exclude 时跳过（如作者自己触发的评论）。"""
    if exclude_user_id is not None and project.user_id == exclude_user_id:
        return None
    return await notification_service.create_notification(
        db,
        user_id=project.user_id,
        type=type,
        title=title,
        content=content,
        project_id=project.id,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
    )


@_safe
async def emit_comment_created(
    db: AsyncSession,
    project: Project,
    comment: Comment,
    actor: User,
) -> Notification | None:
    """评论创建 → 通知项目作者（评论者非作者时）。"""
    title = f"{actor.username} 评论了作品《{project.title}》"
    return await _notify_project_owner(
        db, project,
        type="comment",
        title=title,
        content=comment.content[:200],
        actor_id=actor.id,
        target_type="comment",
        target_id=comment.id,
        payload={"comment_id": str(comment.id), "target_type": comment.target_type,
                 "target_id": comment.target_id},
        exclude_user_id=actor.id,
    )


@_safe
async def emit_comment_reply(
    db: AsyncSession,
    project: Project,
    reply: Comment,
    parent: Comment,
    actor: User,
) -> Notification | None:
    """评论回复 → 通知父评论作者（回复者非父作者时）。"""
    if parent.user_id == actor.id:
        return None
    return await notification_service.create_notification(
        db,
        user_id=parent.user_id,
        type="comment",
        title=f"{actor.username} 回复了你的评论",
        content=reply.content[:200],
        project_id=project.id,
        actor_id=actor.id,
        target_type="comment",
        target_id=parent.id,
        payload={"comment_id": str(reply.id), "parent_id": str(parent.id)},
    )


@_safe
async def emit_mentions(
    db: AsyncSession,
    project: Project,
    comment: Comment,
    actor: User,
    mentioned_user_ids: list[uuid.UUID],
) -> int:
    """用户被提及 → 被提及者收到通知（跳过无权限者已在解析阶段处理）。"""
    count = 0
    for uid in mentioned_user_ids:
        n = await notification_service.create_notification(
            db,
            user_id=uid,
            type="comment",
            title=f"{actor.username} 在评论中提到了你",
            content=comment.content[:200],
            project_id=project.id,
            actor_id=actor.id,
            target_type="comment",
            target_id=comment.id,
            payload={"comment_id": str(comment.id), "mentioned": True},
        )
        if n is not None:
            count += 1
    return count


@_safe
async def emit_comment_resolved(
    db: AsyncSession,
    project: Project,
    comment: Comment,
    actor: User,
) -> Notification | None:
    """评论被解决 → 通知评论创建者（非本人处理时）。"""
    if comment.user_id == actor.id:
        return None
    return await notification_service.create_notification(
        db,
        user_id=comment.user_id,
        type="comment",
        title=f"{actor.username} 解决了你的评论",
        content=comment.content[:200],
        project_id=project.id,
        actor_id=actor.id,
        target_type="comment",
        target_id=comment.id,
        payload={"comment_id": str(comment.id), "status": "resolved"},
    )


@_safe
async def emit_member_joined(
    db: AsyncSession,
    project: Project,
    member: ProjectMember,
    actor: User,
) -> Notification | None:
    """协作者加入项目 → 通知项目作者与邀请人。"""
    title = f"{actor.username} 加入了《{project.title}》（{member.role}）"
    target = (
        await _notify_project_owner(
            db, project,
            type="collaboration",
            title=title,
            actor_id=actor.id,
            target_type="project",
            target_id=project.id,
            payload={"member_id": str(member.id), "role": member.role},
            exclude_user_id=actor.id,
        )
    )
    return target


@_safe
async def emit_invitation_accepted(
    db: AsyncSession,
    project: Project,
    invitation: ProjectInvitation,
    actor: User,
) -> Notification | None:
    """邀请被接受 → 通知邀请人。"""
    return await notification_service.create_notification(
        db,
        user_id=invitation.inviter_id,
        type="collaboration",
        title=f"{actor.username} 接受了《{project.title}》的协作邀请",
        actor_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        payload={"invitation_id": str(invitation.id), "role": invitation.role},
    )


@_safe
async def emit_content_updated(
    db: AsyncSession,
    project: Project,
    actor: User,
    *,
    target_type: str,
    target_id: str,
    payload: dict | None = None,
) -> Notification | None:
    """协作者修改内容 → 通知项目作者。"""
    return await _notify_project_owner(
        db, project,
        type="collaboration",
        title=f"{actor.username} 修改了作品《{project.title}》的内容",
        actor_id=actor.id,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        exclude_user_id=actor.id,
    )


@_safe
async def emit_ai_task_status(
    db: AsyncSession,
    project: Project | None,
    actor: User,
    *,
    status: str,
    task_title: str,
    target_id: str | None = None,
) -> Notification | None:
    """AI 任务完成/失败 → 通知任务创建者。"""
    ntype = "ai_task"
    if status in ("completed", "success"):
        title = f"AI 任务已完成：{task_title}"
    elif status in ("failed", "error"):
        title = f"AI 任务失败：{task_title}"
    else:
        title = f"AI 任务状态更新：{task_title}"
    return await notification_service.create_notification(
        db,
        user_id=actor.id,
        type=ntype,
        title=title,
        content=status,
        project_id=project.id if project else None,
        actor_id=actor.id,
        target_type="ai_task",
        target_id=target_id,
        payload={"task_status": status},
    )


@_safe
async def emit_chapter_published(
    db: AsyncSession,
    project: Project,
    actor: User,
    *,
    chapter_title: str,
    chapter_id: str,
) -> list[Notification]:
    """章节发布 → 通知项目协作者成员（除发布者外）。"""
    members_result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.status == "active",
            ProjectMember.user_id != actor.id,
        )
    )
    created: list[Notification] = []
    for member in members_result.scalars().all():
        n = await notification_service.create_notification(
            db,
            user_id=member.user_id,
            type="chapter",
            title=f"《{project.title}》发布了新章节：{chapter_title}",
            content="章节发布成功",
            project_id=project.id,
            actor_id=actor.id,
            target_type="chapter",
            target_id=chapter_id,
            payload={"published": True},
        )
        if n is not None:
            created.append(n)
    return created


@_safe
async def emit_version_created(
    db: AsyncSession,
    project: Project,
    actor: User,
    *,
    version: int,
    chapter_id: str,
) -> list[Notification]:
    """版本创建 → 通知项目协作者成员（除创建者外）。"""
    members_result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.status == "active",
            ProjectMember.user_id != actor.id,
        )
    )
    created: list[Notification] = []
    for member in members_result.scalars().all():
        n = await notification_service.create_notification(
            db,
            user_id=member.user_id,
            type="version",
            title=f"《{project.title}》创建了第 {version} 版",
            content="版本创建成功",
            project_id=project.id,
            actor_id=actor.id,
            target_type="chapter",
            target_id=chapter_id,
            payload={"version": version},
        )
        if n is not None:
            created.append(n)
    return created


@_safe
async def emit_invitation_expiring(
    db: AsyncSession,
    project: Project,
    invitation: ProjectInvitation,
) -> Notification | None:
    """邀请即将过期 → 通知项目作者。"""
    return await _notify_project_owner(
        db, project,
        type="collaboration",
        title=f"协作者邀请即将过期：{invitation.invitee_email}",
        content=f"邀请 {invitation.role} 角色，请及时处理",
        actor_id=invitation.inviter_id,
        target_type="invitation",
        target_id=invitation.id,
        payload={"invitation_id": str(invitation.id),
                 "expires_at": invitation.expires_at.isoformat() if invitation.expires_at else None},
        exclude_user_id=None,
    )
