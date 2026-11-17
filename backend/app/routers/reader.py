# 发布与阅读模式：公开阅读接口 + 阅读设置/阅读进度接口（第十三批规格实现）
# 公开阅读：无需登录，作品/章节按 slug 或 UUID 解析；隐藏草稿章节与未发布分卷。
# 阅读设置/进度：需登录，user_id 取自当前用户。
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.publication import (
    PublicChapterNavigationResponse,
    PublicChapterResponse,
    PublicProjectResponse,
    PublicVolumeItem,
    ReaderPreferenceResponse,
    ReaderPreferenceUpdate,
    ReadingProgressListResponse,
    ReadingProgressResponse,
    ReadingProgressUpdate,
)
from app.services import discover_service, publication_service

public_router = APIRouter(
    prefix="/api/public/projects/{project_ref}",
    tags=["发布与阅读（公开）"],
)

reader_router = APIRouter(
    prefix="/api/reader",
    tags=["发布与阅读（阅读设置/进度）"],
)


@public_router.get("", response_model=PublicProjectResponse)
async def get_public_project(
    project_ref: str,
    db: AsyncSession = Depends(get_db),
):
    """公开作品首页：作品公开信息 + 目录（隐藏草稿章节/未发布分卷/非 public 章节）。"""
    project = await publication_service.resolve_project_ref(db, project_ref)
    result = await publication_service.get_public_project(db, project)
    # 服务端阅读计数（规格十三：不信任客户端提交）
    await discover_service.record_view(db, project.id, None)
    await db.commit()
    return result


@public_router.get("/chapters", response_model=list[PublicVolumeItem])
async def get_public_chapters(
    project_ref: str,
    db: AsyncSession = Depends(get_db),
):
    """公开章节目录（按分卷分组，隐藏草稿章节与未发布分卷）。"""
    project = await publication_service.resolve_project_ref(db, project_ref)
    await publication_service.get_public_project(db, project)  # 触发可见性校验
    return await publication_service._build_toc(db, project, visible_only=True)


@public_router.get("/chapters/{chapter_ref}", response_model=PublicChapterResponse)
async def get_public_chapter(
    project_ref: str,
    chapter_ref: str,
    db: AsyncSession = Depends(get_db),
):
    """公开单章正文（含上一章/下一章信息）。"""
    project = await publication_service.resolve_project_ref(db, project_ref)
    chapter = await publication_service.resolve_chapter_ref(db, project, chapter_ref)
    result = await publication_service.get_public_chapter(db, project, chapter)
    # 服务端阅读计数（规格十三：不信任客户端提交）
    await discover_service.record_view(db, project.id, None)
    await db.commit()
    return result


@public_router.get(
    "/chapters/{chapter_ref}/navigation",
    response_model=PublicChapterNavigationResponse,
)
async def get_public_chapter_navigation(
    project_ref: str,
    chapter_ref: str,
    db: AsyncSession = Depends(get_db),
):
    """公开章节导航：当前章 + 上一章/下一章。"""
    project = await publication_service.resolve_project_ref(db, project_ref)
    chapter = await publication_service.resolve_chapter_ref(db, project, chapter_ref)
    full = await publication_service.get_public_chapter(db, project, chapter)
    return PublicChapterNavigationResponse(
        chapter_id=full.chapter_id,
        title=full.title,
        chapter_number=full.chapter_number,
        slug=full.slug,
        prev_chapter_id=full.prev_chapter_id,
        prev_chapter_slug=full.prev_chapter_slug,
        prev_chapter_title=full.prev_chapter_title,
        next_chapter_id=full.next_chapter_id,
        next_chapter_slug=full.next_chapter_slug,
        next_chapter_title=full.next_chapter_title,
    )


# ---------- 阅读设置 ----------

@reader_router.get("/preferences", response_model=ReaderPreferenceResponse)
async def get_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户阅读设置（未设置过时返回默认值）。"""
    return await publication_service.get_preference(db, current_user.id)


@reader_router.put("/preferences", response_model=ReaderPreferenceResponse)
async def update_preferences(
    data: ReaderPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """保存当前用户阅读设置（只影响本人，不修改作品正文）。"""
    return await publication_service.upsert_preference(db, current_user.id, data)


# ---------- 阅读进度 ----------

@reader_router.get("/progress/{project_id}", response_model=ReadingProgressListResponse)
async def list_progress(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户在某作品下的全部阅读进度（最近阅读优先）。"""
    return await publication_service.list_progress(db, current_user.id, project_id)


@reader_router.put(
    "/progress/{project_id}/{chapter_id}",
    response_model=ReadingProgressResponse,
)
async def upsert_progress(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    data: ReadingProgressUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """记录/更新阅读进度：章节内滚动位置、最后阅读时间、标记已读。"""
    return await publication_service.upsert_progress(
        db, current_user.id, project_id, chapter_id, data
    )


@reader_router.get(
    "/progress/{project_id}/{chapter_id}",
    response_model=ReadingProgressResponse,
)
async def get_progress(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取某章节阅读进度（用于继续阅读）。"""
    return await publication_service.get_progress(
        db, current_user.id, project_id, chapter_id
    )
