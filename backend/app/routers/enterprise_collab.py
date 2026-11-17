# 第二十四批「企业级协作」- 编辑锁 / 评论批注 / 版本标记 路由
# 锁定编辑模式：获取锁→他人只读可评论→保存/退出释放；处理超时/异常退出/强制解除；
# 评论：选中文字/回复/提及/状态流转/筛选未解决/历史；评论不覆盖正文。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services import enterprise_collab_service as svc
from app.services import enterprise_service as esvc

router = APIRouter(prefix="/api/enterprise", tags=["batch24-collab"])


async def _load_org(org_id: uuid.UUID, db: AsyncSession):
    return await esvc.get_org_by_id(db, org_id)


async def _require_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID):
    return await esvc.require_org_member(db, org_id, user_id)


# ---------- 编辑锁 ----------
class LockAcquireIn(BaseModel):
    resource_type: str
    resource_id: uuid.UUID
    ttl_minutes: int | None = None
    resource_creator_id: uuid.UUID | None = None


@router.post("/organizations/{org_id}/projects/{project_id}/edit-locks")
async def acquire_lock(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: LockAcquireIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    lock = await svc.acquire_edit_lock(
        db,
        org,
        current_user,
        project_id,
        body.resource_type,
        body.resource_id,
        body.ttl_minutes or 30,
        body.resource_creator_id,
    )
    await db.commit()
    return {"id": str(lock.id), "status": lock.status, "expires_at": lock.expires_at.isoformat() if lock.expires_at else None}


@router.post("/organizations/{org_id}/edit-locks/{lock_id}/heartbeat")
async def heartbeat_lock(
    org_id: uuid.UUID,
    lock_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    lock = await svc.heartbeat_edit_lock(db, org, current_user, lock_id)
    await db.commit()
    return {"id": str(lock.id), "status": lock.status, "expires_at": lock.expires_at.isoformat() if lock.expires_at else None}


@router.post("/organizations/{org_id}/edit-locks/{lock_id}/release")
async def release_lock(
    org_id: uuid.UUID,
    lock_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    lock = await svc.release_edit_lock(db, org, current_user, lock_id)
    await db.commit()
    return {"id": str(lock.id), "status": lock.status}


@router.post("/organizations/{org_id}/edit-locks/{lock_id}/force-release")
async def force_release_lock(
    org_id: uuid.UUID,
    lock_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    lock = await svc.force_release_edit_lock(db, org, current_user, lock_id)
    await db.commit()
    return {"id": str(lock.id), "status": lock.status}


@router.get("/organizations/{org_id}/projects/{project_id}/edit-locks")
async def list_locks(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    locks = await svc.list_edit_locks(db, org, project_id)
    return {"locks": [{"id": str(l.id), "user_id": str(l.user_id), "resource_type": l.resource_type, "resource_id": str(l.resource_id), "expires_at": l.expires_at.isoformat() if l.expires_at else None} for l in locks]}


# ---------- 评论批注 ----------
class CommentCreateIn(BaseModel):
    resource_type: str
    resource_id: str
    content: str
    parent_id: uuid.UUID | None = None
    range_start: int | None = None
    range_end: int | None = None
    mentions: list[str] | None = None


@router.post("/organizations/{org_id}/projects/{project_id}/comments")
async def create_comment(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    body: CommentCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    comment = await svc.create_comment_ex(
        db,
        org,
        current_user,
        project_id,
        body.resource_type,
        body.resource_id,
        body.content,
        body.parent_id,
        body.range_start,
        body.range_end,
        body.mentions,
    )
    await db.commit()
    return {"id": str(comment.id), "status": comment.status, "content": comment.content}


class CommentStatusIn(BaseModel):
    new_status: str


@router.patch("/organizations/{org_id}/comments/{comment_id}/status")
async def update_comment_status(
    org_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentStatusIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    comment = await svc.update_comment_status(db, org, current_user, comment_id, body.new_status)
    await db.commit()
    return {"id": str(comment.id), "status": comment.status, "resolved_at": comment.resolved_at.isoformat() if comment.resolved_at else None}


@router.get("/organizations/{org_id}/projects/{project_id}/comments")
async def list_comments(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    unresolved_only: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    rows = await svc.list_comments_ex(db, org, project_id, resource_type, resource_id, unresolved_only)
    return {"comments": [
        {
            "id": str(c.id),
            "user_id": str(c.user_id),
            "resource_type": c.resource_type,
            "resource_id": str(c.resource_id),
            "parent_id": str(c.parent_id) if c.parent_id else None,
            "content": c.content,
            "status": c.status,
            "range_start": c.range_start,
            "range_end": c.range_end,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in rows
    ]}


# ---------- 版本标记重要 ----------
class VersionImportantIn(BaseModel):
    important: bool = True


@router.post("/organizations/{org_id}/projects/{project_id}/versions/{version_id}/important")
async def mark_version_important(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    body: VersionImportantIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org = await _load_org(org_id, db)
    await _require_member(db, org_id, current_user.id)
    version = await svc.mark_version_important(db, org, current_user, project_id, version_id, body.important)
    await db.commit()
    return {"id": str(version.id), "is_important": version.is_important}
