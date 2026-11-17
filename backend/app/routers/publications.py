# 发布与阅读模式：作者发布/撤回/归档/恢复/预览/检查接口（第十三批规格实现）
# 全部接口需 JWT 鉴权，并校验作品归属 project.user_id == current_user.id（非本人 404）。
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.schemas.publication import (
    PreviewChapterResponse,
    PreviewProjectResponse,
    ProjectPublishRequest,
    ProjectPublishResponse,
    PublicationCreate,
    PublicationResponse,
    PublicationUpdate,
    PublishCheckResponse,
)
from app.services import (
    audit_service,
    event_service,
    publication_service,
    publish_orchestrator,
)
from app.services.chapter_lifecycle import LifecycleRejected

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["发布与阅读（作者）"],
)

logger = logging.getLogger(__name__)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Project:
    """校验作品存在且归属当前用户（project.user_id == current_user.id），否则 404。"""
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id,
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )
    return project


async def get_owned_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Outline:
    """校验项目内章节节点（outline_type=chapter），否则 404。"""
    await get_owned_project(project_id, current_user, db)
    result = await db.execute(
        select(Outline).where(
            Outline.id == chapter_id,
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
        )
    )
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在"
        )
    return chapter


async def _sync_after_publication_change(
    db: AsyncSession, project: Project, chapter: Outline | None = None,
    *, commit: bool = True,
) -> None:
    """发布/撤回/归档/恢复后联动（U2a 起由 publication_service 统一实现）。

    章节发布成功路径不再调用本包装（同步已并入编排器业务事务），
    撤回/归档/恢复/作品发布等旁路变更继续复用同一同步入口。
    """
    await publication_service.sync_publish_side_effects(
        db, project, chapter, commit=commit,
    )


@router.post("/publish", response_model=ProjectPublishResponse)
async def publish_project(
    project_id: uuid.UUID,
    data: ProjectPublishRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发布整个作品：至少一章已发布，且全部章节无高等级问题（force 忽略低/中等级）。"""
    project = await get_owned_project(project_id, current_user, db)
    result = await publication_service.publish_project(db, project, force=data.force)
    await _sync_after_publication_change(db, project)
    return result


@router.put("/publication", response_model=ProjectPublishResponse)
async def update_publication(
    project_id: uuid.UUID,
    data: PublicationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新作品公开信息：名称/副标题/简介/封面文字/发布状态/slug。"""
    project = await get_owned_project(project_id, current_user, db)
    result = await publication_service.update_publication_info(db, project, data)
    await _sync_after_publication_change(db, project)
    return result


@router.post("/restore", response_model=ProjectPublishResponse)
async def restore_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复作品：归档/撤回的作品恢复为草稿状态（可重新编辑发布）。"""
    project = await get_owned_project(project_id, current_user, db)
    project.publication_status = "draft"
    project.status = "active"
    await db.commit()
    await db.refresh(project)
    await _sync_after_publication_change(db, project)
    return ProjectPublishResponse(
        project_id=project.id,
        status=project.publication_status,
        project_slug=project.project_slug,
        published_at=project.published_at,
        message="作品已恢复为草稿",
    )


@router.get("/chapters/{chapter_id}/publication", response_model=PublicationResponse)
async def get_chapter_publication(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查看章节发布记录。"""
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    return await publication_service.get_publication(db, chapter)


@router.post("/chapters/{chapter_id}/publish", response_model=PublicationResponse)
async def publish_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    data: PublicationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """章节发布：执行发布前检查（高等级问题禁止，force 忽略低/中等级）→ 转换正文 → 生成发布快照。"""
    project = await get_owned_project(project_id, current_user, db)
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    # U2a：门禁、快照、发布状态写透、索引/统计同步与唯一一次业务提交
    # 全部收敛到 publish_orchestrator，路由不再直接调用 publication_service /
    # 生命周期发布服务旧入口，也不写任何发布状态列。
    try:
        outcome = await publish_orchestrator.publish_chapter(
            db,
            publish_orchestrator.PublishCommand(
                project=project,
                outline=chapter,
                payload=data,
                write_snapshot=True,
                gate_profile=publish_orchestrator.GATE_PROFILE_STATE_ONLY,
            ),
        )
    except LifecycleRejected as exc:
        # 审计行只在 savepoint 内 flush，拒绝响应前单独提交这一条。
        # 提交失败只降级为「审计未持久化」，不得把 409 变成 500。
        if exc.audit_pending:
            await audit_service.commit_pending_rejection_audit(db)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        # D-U2A-4：内容错误（无正文）保持既有 400 语义，不得升级为 5xx
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"can_publish": False, "issues": [str(exc)]},
        ) from exc
    result = outcome.publication
    # 第十五批事件通知：章节发布成功 → 通知协作者成员（发布通知）。
    # 业务已提交，通知失败只记日志并清理会话，不得回滚或污染已完成业务。
    try:
        await event_service.emit_chapter_published(
            db, project, current_user,
            chapter_id=str(chapter.id),
            chapter_title=chapter.title or "",
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("章节发布通知失败（业务发布已提交，忽略通知失败）")
        await db.rollback()
    return result


@router.post("/chapters/{chapter_id}/unpublish", response_model=PublicationResponse)
async def unpublish_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """撤回发布：章节与发布记录置为草稿，公开链接不可访问、目录隐藏，历史阅读记录保留。"""
    project = await get_owned_project(project_id, current_user, db)
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    result = await publication_service.unpublish_chapter(db, None, chapter)
    await _sync_after_publication_change(db, project, chapter)
    return result


@router.post("/chapters/{chapter_id}/archive", response_model=PublicationResponse)
async def archive_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """归档章节：不允许普通编辑，仍可在作者后台查看，可恢复为草稿。"""
    project = await get_owned_project(project_id, current_user, db)
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    result = await publication_service.archive_chapter(db, chapter)
    await _sync_after_publication_change(db, project, chapter)
    return result


@router.post("/chapters/{chapter_id}/restore", response_model=PublicationResponse)
async def restore_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复章节：归档/撤回的章节恢复为草稿。"""
    project = await get_owned_project(project_id, current_user, db)
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    result = await publication_service.restore_chapter(db, chapter)
    await _sync_after_publication_change(db, project, chapter)
    return result


@router.post("/chapters/{chapter_id}/publish-check", response_model=PublishCheckResponse)
async def publish_check(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发布前检查：返回 can_publish + issues（高等级问题禁止发布）。"""
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    return await publication_service.check_publish_ready(db, chapter)


@router.get("/preview", response_model=PreviewProjectResponse)
async def preview_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """作者预览作品首页/目录（读者视角，不改变发布状态）。"""
    project = await get_owned_project(project_id, current_user, db)
    return await publication_service.preview_project(db, project)


@router.get("/chapters/{chapter_id}/preview", response_model=PreviewChapterResponse)
async def preview_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """作者预览单章正文（基于当前编辑稿转换，不改变发布状态）。"""
    project = await get_owned_project(project_id, current_user, db)
    chapter = await get_owned_chapter(project_id, chapter_id, current_user, db)
    return await publication_service.preview_chapter(db, project, chapter)
