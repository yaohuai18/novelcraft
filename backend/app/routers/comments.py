# 评论路由（第十五批「通知、评论与协作反馈」规格实现）
# GET/POST /api/projects/{project_id}/comments、PUT/DELETE /api/comments/{comment_id}、
# POST /replies、/resolve、/reopen、/hide；提及解析与通知；权限校验。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.comment import Comment
from app.models.project import Project
from app.models.user import User
from app.schemas.comment import (
    CommentCreate,
    CommentListResponse,
    CommentResponse,
    CommentUpdate,
)
from app.services import (
    collaboration_service,
    comment_service,
    event_service,
)

project_comments_router = APIRouter(
    prefix="/api/projects/{project_id}/comments",
    tags=["评论"]
)
comments_router = APIRouter(
    prefix="/api/comments",
    tags=["评论"]
)


async def get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品不存在")
    return project


async def get_comment_or_404(db: AsyncSession, comment_id: uuid.UUID) -> Comment:
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评论不存在")
    return comment


async def _serialize_comment(db: AsyncSession, comment: Comment) -> CommentResponse:
    author = await db.get(User, comment.user_id)
    mentioned_ids = await comment_service.list_mentions(db, comment.id)
    reply_count = (
        await db.execute(
            select(func.count(Comment.id)).where(
                Comment.parent_id == comment.id,
                Comment.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return CommentResponse(
        id=comment.id,
        project_id=comment.project_id,
        user_id=comment.user_id,
        target_type=comment.target_type,
        target_id=comment.target_id,
        parent_id=comment.parent_id,
        content=comment.content,
        comment_type=comment.comment_type,
        status=comment.status,
        position_data=comment_service._loads(comment.position_data),
        orphaned=comment.orphaned,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        deleted_at=comment.deleted_at,
        author_username=author.username if author else None,
        mentioned_user_ids=mentioned_ids,
        reply_count=reply_count,
    )


def _handle_comment_error(exc: Exception) -> HTTPException:
    if isinstance(exc, comment_service.SensitiveContentError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, comment_service.RateLimitError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, comment_service.CommentError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@project_comments_router.get("", response_model=CommentListResponse)
async def list_comments(
    project_id: uuid.UUID,
    target_type: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    parent_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """评论列表（按类型可见范围过滤；默认不含回复，可通过 parent_id 查回复）。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role is None and project.publication_status != "published":
        raise HTTPException(status_code=403, detail="无权查看该作品评论")

    items, total = await comment_service.list_comments(
        db,
        project,
        current_user,
        target_type=target_type,
        target_id=target_id,
        status=status_filter,
        parent_id=parent_id,
        page=page,
        page_size=page_size,
    )
    return CommentListResponse(
        items=[await _serialize_comment(db, c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@project_comments_router.post("", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    request: Request,
    project_id: uuid.UUID,
    data: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建评论：审核（敏感词）、频率限制、提及解析；触发评论/提及通知。"""
    project = await get_project_or_404(db, project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="无权评论该作品")

    client_ip = request.client.host if request.client else None
    try:
        comment = await comment_service.create_comment(
            db, project, current_user, data, ip=client_ip,
        )
    except Exception as exc:
        raise _handle_comment_error(exc)

    mentioned_ids = await comment_service.resolve_mentions(db, comment, current_user)

    await collaboration_service.ensure_owner_member(db, project)
    await collaboration_service.add_activity(
        db,
        project_id=project.id,
        user_id=current_user.id,
        action="comment.create",
        target_type=comment.target_type,
        target_id=comment.target_id,
        metadata={"comment_id": str(comment.id), "comment_type": comment.comment_type},
    )
    await event_service.emit_comment_created(db, project, comment, current_user)
    await event_service.emit_mentions(db, project, comment, current_user, mentioned_ids)

    await db.commit()
    await db.refresh(comment)
    return await _serialize_comment(db, comment)


@comments_router.put("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: uuid.UUID,
    data: CommentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改评论：仅创建者本人。"""
    comment = await get_comment_or_404(db, comment_id)
    try:
        comment = await comment_service.update_comment(
            db, comment, current_user,
            content=data.content,
            position_data=data.position_data.model_dump() if data.position_data else None,
        )
    except Exception as exc:
        raise _handle_comment_error(exc)
    # 内容修改后重新解析提及（增量：新增 @）
    new_mentions = await comment_service.resolve_mentions(db, comment, current_user)
    project = await db.get(Project, comment.project_id)
    if project is not None:
        await event_service.emit_mentions(db, project, comment, current_user, new_mentions)
    await db.commit()
    await db.refresh(comment)
    return await _serialize_comment(db, comment)


@comments_router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除评论（软删）：创建者本人或项目作者（可删除违规）。"""
    comment = await get_comment_or_404(db, comment_id)
    project = await db.get(Project, comment.project_id)
    role = await collaboration_service.get_role(
        db, project.id, current_user.id
    ) if project is not None else None
    try:
        comment = await comment_service.soft_delete_comment(
            db, comment, current_user, role=role,
        )
    except comment_service.CommentError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if project is not None:
        await collaboration_service.add_activity(
            db, project_id=project.id, user_id=current_user.id,
            action="comment.delete", target_type="comment", target_id=comment.id,
        )
    await db.commit()


@comments_router.post("/{comment_id}/replies", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_reply(
    request: Request,
    comment_id: uuid.UUID,
    data: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回复评论：类型/目标继承父评论；触发回复通知。"""
    parent = await get_comment_or_404(db, comment_id)
    project = await get_project_or_404(db, parent.project_id)
    role = await collaboration_service.get_role(db, project.id, current_user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="无权评论该作品")

    client_ip = request.client.host if request.client else None
    try:
        reply = await comment_service.create_comment(
            db, project, current_user, data, ip=client_ip, parent=parent,
        )
    except Exception as exc:
        raise _handle_comment_error(exc)

    mentioned_ids = await comment_service.resolve_mentions(db, reply, current_user)

    await collaboration_service.add_activity(
        db,
        project_id=project.id,
        user_id=current_user.id,
        action="comment.reply",
        target_type="comment",
        target_id=str(reply.id),
        metadata={"parent_id": str(parent.id)},
    )
    await event_service.emit_comment_reply(db, project, reply, parent, current_user)
    await event_service.emit_mentions(db, project, reply, current_user, mentioned_ids)

    await db.commit()
    await db.refresh(reply)
    return await _serialize_comment(db, reply)


@comments_router.post("/{comment_id}/resolve", response_model=CommentResponse)
async def resolve_comment(
    comment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """标记评论已解决：创建者或 owner/editor/reviewer。"""
    comment = await get_comment_or_404(db, comment_id)
    project = await db.get(Project, comment.project_id)
    role = await collaboration_service.get_role(
        db, project.id, current_user.id
    ) if project is not None else None
    try:
        comment = await comment_service.resolve_comment(
            db, comment, current_user, role=role,
        )
    except comment_service.CommentError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if project is not None:
        await collaboration_service.add_activity(
            db, project_id=project.id, user_id=current_user.id,
            action="comment.resolve", target_type="comment", target_id=comment.id,
        )
        await event_service.emit_comment_resolved(db, project, comment, current_user)
    await db.commit()
    await db.refresh(comment)
    return await _serialize_comment(db, comment)


@comments_router.post("/{comment_id}/reopen", response_model=CommentResponse)
async def reopen_comment(
    comment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重新打开评论：创建者或项目成员。"""
    comment = await get_comment_or_404(db, comment_id)
    project = await db.get(Project, comment.project_id)
    role = await collaboration_service.get_role(
        db, project.id, current_user.id
    ) if project is not None else None
    try:
        comment = await comment_service.reopen_comment(
            db, comment, current_user, role=role,
        )
    except comment_service.CommentError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if project is not None:
        await collaboration_service.add_activity(
            db, project_id=project.id, user_id=current_user.id,
            action="comment.reopen", target_type="comment", target_id=comment.id,
        )
    await db.commit()
    await db.refresh(comment)
    return await _serialize_comment(db, comment)


@comments_router.post("/{comment_id}/hide", response_model=CommentResponse)
async def hide_comment(
    comment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """隐藏不当评论：仅项目作者（owner）可执行。"""
    comment = await get_comment_or_404(db, comment_id)
    project = await db.get(Project, comment.project_id)
    role = await collaboration_service.get_role(
        db, project.id, current_user.id
    ) if project is not None else None
    try:
        comment = await comment_service.hide_comment(db, comment, role=role)
    except comment_service.CommentError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if project is not None:
        await collaboration_service.add_activity(
            db, project_id=project.id, user_id=current_user.id,
            action="comment.hide", target_type="comment", target_id=comment.id,
        )
    await db.commit()
    await db.refresh(comment)
    return await _serialize_comment(db, comment)
