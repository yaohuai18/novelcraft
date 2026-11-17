# 章节记忆路由（第二期「章节能被记住」规格实现）
# 提供：查询 / 重新生成 / 编辑保存（保存后 status=confirmed, source=manual）/ 确认状态。
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.chapter_memory import ChapterMemory
from app.models.chapter_content import ChapterContent
from app.models.project import Project
from app.models.user import User
from app.services import collaboration_service
from app.routers.chapter_contents import get_chapter
from app.schemas.chapter_memory import (
    ChapterMemoryConfirmResponse,
    ChapterMemoryResponse,
    ChapterMemoryUpdate,
)
from app.services.chapter_memory_service import (
    summarize_chapter,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/chapters/{outline_id}/memory",
    tags=["章节记忆"],
)


async def _require_editor(
    db: AsyncSession,
    project_id: uuid.UUID,
    current_user: User,
) -> None:
    """写操作限 owner/editor：低权限协作者（reviewer/commenter/viewer）只能读。"""
    project = await db.get(Project, project_id)
    if project is not None and project.user_id != current_user.id:
        role = await collaboration_service.get_role(db, project.id, current_user.id)
        if role not in ("owner", "editor"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="仅项目作者或编辑可修改章节记忆",
            )


async def _load_memory(
    db: AsyncSession, project_id: uuid.UUID, outline_id: uuid.UUID
) -> ChapterMemory | None:
    return (
        await db.execute(
            select(ChapterMemory).where(ChapterMemory.chapter_id == outline_id)
        )
    ).scalar_one_or_none()


async def _latest_content(
    db: AsyncSession, outline_id: uuid.UUID
) -> str:
    row = (
        await db.execute(
            select(ChapterContent)
            .where(ChapterContent.outline_id == outline_id)
            .order_by(ChapterContent.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (row.content if row and row.content else "").strip()


@router.get("", response_model=ChapterMemoryResponse)
async def get_chapter_memory(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询章节记忆；未生成时返回 404。"""
    await get_chapter(project_id, outline_id, current_user, db)
    memory = await _load_memory(db, project_id, outline_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该章节暂无记忆，请先保存正文并开启自动摘要，或手动重新生成",
        )
    return memory


@router.post("/generate", response_model=ChapterMemoryResponse)
async def regenerate_chapter_memory(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动触发本章摘要重新生成（忽略 auto_summarize 开关，覆盖现有 draft/confirmed 记忆）。"""
    chapter = await get_chapter(project_id, outline_id, current_user, db)
    await _require_editor(db, project_id, current_user)
    content = await _latest_content(db, outline_id)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="章节暂无正文，无法生成记忆",
        )
    memory = await summarize_chapter(
        db,
        project_id=project_id,
        chapter_id=outline_id,
        chapter_title=chapter.title or "",
        content=content,
    )
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="摘要生成失败（真实模型调用或解析失败），已记录 failed 生成记录，请稍后重试",
        )
    return memory


@router.put("", response_model=ChapterMemoryResponse)
async def update_chapter_memory(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: ChapterMemoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """编辑章节记忆：保存后 status=confirmed、source=manual（人工确认/修正）。"""
    await get_chapter(project_id, outline_id, current_user, db)
    await _require_editor(db, project_id, current_user)
    memory = await _load_memory(db, project_id, outline_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该章节暂无记忆，无法编辑；请先生成记忆",
        )
    memory.summary = data.summary or ""
    memory.memory_json = json.dumps(data.memory or {}, ensure_ascii=False)
    memory.source = "manual"
    memory.status = "confirmed"
    await db.commit()
    await db.refresh(memory)
    return memory


@router.post("/confirm", response_model=ChapterMemoryConfirmResponse)
async def confirm_chapter_memory(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """确认章节记忆（不改内容，仅将 status 置为 confirmed）。"""
    await get_chapter(project_id, outline_id, current_user, db)
    await _require_editor(db, project_id, current_user)
    memory = await _load_memory(db, project_id, outline_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该章节暂无记忆，无法确认",
        )
    memory.status = "confirmed"
    await db.commit()
    await db.refresh(memory)
    return memory
