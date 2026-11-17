# 章节计划（ChapterPlan）管理路由
# 章节标题单一事实来源 = outlines.title（chapter 行）；
# 计划标题仅用于比对提示，只有 sync_title=True 时才同步到章节标题。
# 权限：与章节正文一致 —— 作者或项目活跃成员可读；作者/owner/editor 可编辑。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.routers.chapter_contents import get_chapter
from pydantic import BaseModel, Field

from app.schemas.chapter_plan import (
    ChapterPlanData,
    ChapterPlanResponse,
    ChapterPlanUpdateRequest,
)
from app.services import collaboration_service
from app.services.chapter_plan_service import (
    TitleConflictError,
    chapter_number_of,
    generate_chapter_plan_batch,
    load_chapter_plan,
    save_chapter_plan,
)


class BatchGenerateRequest(BaseModel):
    """批量生成章节计划请求：每批最多 5 章。"""

    chapter_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=5)

router = APIRouter(
    prefix="/api/projects/{project_id}/chapter-plans",
    tags=["章节计划"],
)


async def _require_editor(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> None:
    """作者或 owner/editor 角色可编辑章节计划。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可编辑章节计划",
            )


@router.post("/batch-generate")
async def batch_generate_chapter_plans(
    project_id: uuid.UUID,
    data: BatchGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量生成章节计划（场景合同）：每批最多 5 章，逐章 AI 生成并落库。

    生成结果写入 outlines.chapter_plan，并回填 chapter_goal/chapter_outcome；
    单章失败不影响同批其他章节。
    """
    await _require_editor(project_id, current_user, db)
    results = await generate_chapter_plan_batch(db, project_id, data.chapter_ids)
    return {"results": results}


@router.get("/{chapter_id}", response_model=ChapterPlanResponse)
async def get_chapter_plan(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChapterPlanResponse:
    """读取指定章节的章节计划（含旧数据 legacy 回退；无计划时返回 plan=None）。

    服务端按 chapterId 精确读取，绝不回退到项目第一章/最近一章/其他章节计划。
    """
    chapter = await get_chapter(project_id, chapter_id, current_user, db)
    plan, source = await load_chapter_plan(db, project_id, chapter_id)
    number = await chapter_number_of(db, project_id, chapter)

    title = chapter.title or (plan or {}).get("title") or ""
    plan_title = (plan or {}).get("title") or ""
    title_sync_required = bool(
        plan and source == "manual" and plan_title and title != plan_title
    )

    return ChapterPlanResponse(
        projectId=project_id,
        chapterId=chapter_id,
        chapterNumber=number,
        title=title,
        summary=chapter.summary,
        plan=ChapterPlanData(**plan) if plan else None,
        planSource=source,
        titleSyncRequired=title_sync_required,
    )


@router.put("/{chapter_id}", response_model=ChapterPlanResponse)
async def update_chapter_plan(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    payload: ChapterPlanUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChapterPlanResponse:
    """保存章节计划；sync_title=True 时同步计划标题到 outlines.title（显式用户确认后）。"""
    chapter = await get_chapter(project_id, chapter_id, current_user, db)
    await _require_editor(project_id, current_user, db)
    try:
        saved = await save_chapter_plan(
            db,
            project_id,
            chapter_id,
            payload.plan,
            sync_title=payload.syncTitle,
        )
    except TitleConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "TITLE_CONFLICT", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await db.refresh(chapter)
    number = await chapter_number_of(db, project_id, chapter)
    title = chapter.title or saved.get("title") or ""
    plan_title = saved.get("title") or ""
    return ChapterPlanResponse(
        projectId=project_id,
        chapterId=chapter_id,
        chapterNumber=number,
        title=title,
        summary=chapter.summary,
        plan=ChapterPlanData(**saved) if saved else None,
        planSource="manual",
        titleSyncRequired=bool(plan_title and title != plan_title),
    )


@router.post("/{chapter_id}/migrate-legacy", response_model=ChapterPlanResponse)
async def migrate_legacy_plan(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChapterPlanResponse:
    """旧数据迁移：把 outlines.title/summary/content 固化为结构化 ChapterPlan（legacy→manual）。

    幂等：已有 manual 计划时直接返回当前计划，不覆盖用户已编辑内容。
    """
    chapter = await get_chapter(project_id, chapter_id, current_user, db)
    await _require_editor(project_id, current_user, db)
    plan, source = await load_chapter_plan(db, project_id, chapter_id)
    if source == "legacy":
        await save_chapter_plan(
            db,
            project_id,
            chapter_id,
            ChapterPlanData(**plan),
            sync_title=False,
        )
        plan, source = await load_chapter_plan(db, project_id, chapter_id)

    await db.refresh(chapter)
    number = await chapter_number_of(db, project_id, chapter)
    title = chapter.title or (plan or {}).get("title") or ""
    plan_title = (plan or {}).get("title") or ""
    return ChapterPlanResponse(
        projectId=project_id,
        chapterId=chapter_id,
        chapterNumber=number,
        title=title,
        summary=chapter.summary,
        plan=ChapterPlanData(**plan) if plan else None,
        planSource="manual" if plan else "none",
        titleSyncRequired=bool(plan and plan_title and title != plan_title),
    )
