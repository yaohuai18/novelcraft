# 搜索路由（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# GET /api/search（全局搜索）、GET /api/projects/{project_id}/search（项目内搜索）、
# GET /api/projects/{project_id}/search/suggestions（搜索建议）、
# GET /api/search/history（最近搜索，全局范围）。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.search import SearchItem, SearchResponse
from app.schemas.search_history import (
    SearchHistoryItem,
    SearchHistoryListResponse,
    SearchSuggestionsResponse,
)
from app.services import search_service

router = APIRouter(
    prefix="/api",
    tags=["搜索"]
)

SEARCH_TYPES = search_service.SEARCH_TYPES
OUTLINE_TYPES = search_service.OUTLINE_TYPES


async def check_project_access(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id,
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )
    return project


def _to_item_dict(item: dict) -> dict:
    return item


@router.get("/projects/{project_id}/search", response_model=SearchResponse)
async def project_search(
    project_id: uuid.UUID,
    q: str = Query(default="", description="搜索关键词（支持多个词/短语/排除词）"),
    scope: str | None = Query(default=None, description="搜索范围：all/outline/chapter/character/world/tag/version 等"),
    target_type: str | None = Query(default=None, description="目标类型：chapter/outline/tag/character/world_setting/project"),
    type: str | None = Query(default=None, description="第九批兼容参数（type）"),
    tag_ids: str | None = Query(default=None, description="逗号分隔的标签 ID 列表，与 match_mode 配合筛选"),
    match_mode: str = Query(default="AND", pattern="^(AND|OR)$"),
    status: str | None = Query(default=None, description="发布状态过滤：published/draft/archived 等"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: str = Query(default="relevance", pattern="^(relevance|updated)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目内搜索：作品信息/大纲/章节标题与正文/人物/世界观/标签/版本摘要。"""
    await check_project_access(project_id, current_user, db)

    types = None
    if scope and scope != "all":
        types = _scope_to_types(scope)
    if type:
        types = (types or []) + [type]

    tag_id_list: list[uuid.UUID] | None = None
    if tag_ids:
        raw = [t.strip() for t in tag_ids.split(",") if t.strip()]
        try:
            tag_id_list = [uuid.UUID(t) for t in raw]
        except ValueError:
            raise HTTPException(status_code=400, detail="tag_ids 格式错误")

    items, total = await search_service.search(
        db,
        project_id=project_id,
        q=q,
        types=types,
        target_type=target_type,
        tag_ids=tag_id_list,
        match_mode=match_mode,
        status=status,
        page=page,
        page_size=page_size,
        sort=sort,
        user_id=current_user.id,
    )

    # 记录搜索历史（项目内）
    if q.strip():
        await search_service.record_search_history(
            db, current_user.id, q, project_id=project_id
        )
        await db.commit()

    return SearchResponse(
        query=q,
        items=[SearchItem(**it) for it in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/search", response_model=SearchResponse)
async def global_search(
    q: str = Query(default="", description="搜索关键词"),
    project_id: uuid.UUID | None = Query(default=None, description="限定作品（可选）"),
    scope: str | None = Query(default=None),
    target_type: str | None = Query(default=None, description="目标类型：project/chapter/character/tag"),
    tag_ids: str | None = Query(default=None, description="逗号分隔标签 ID"),
    match_mode: str = Query(default="AND", pattern="^(AND|OR)$"),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: str = Query(default="relevance", pattern="^(relevance|updated)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """全局搜索：当前用户有权访问的多个作品（本人私有 + 已公开），权限与软删除过滤。"""
    tag_id_list: list[uuid.UUID] | None = None
    if tag_ids:
        raw = [t.strip() for t in tag_ids.split(",") if t.strip()]
        try:
            tag_id_list = [uuid.UUID(t) for t in raw]
        except ValueError:
            raise HTTPException(status_code=400, detail="tag_ids 格式错误")

    if project_id is not None:
        # 限定到某项目：走项目内搜索（权限校验由项目内搜索完成）
        await check_project_access(project_id, current_user, db)
        items, total = await search_service.search(
            db,
            project_id=project_id,
            q=q,
            target_type=target_type,
            tag_ids=tag_id_list,
            match_mode=match_mode,
            status=status,
            page=page,
            page_size=page_size,
            sort=sort,
            user_id=current_user.id,
        )
    else:
        items, total = await search_service.search_global(
            db,
            user_id=current_user.id,
            q=q,
            target_type=target_type,
            tag_ids=tag_id_list,
            match_mode=match_mode,
            status=status,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    if q.strip():
        await search_service.record_search_history(db, current_user.id, q)
        await db.commit()

    return SearchResponse(
        query=q,
        items=[SearchItem(**it) for it in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/projects/{project_id}/search/suggestions", response_model=SearchSuggestionsResponse)
async def search_suggestions(
    project_id: uuid.UUID,
    q: str = Query(default="", description="输入前缀"),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """搜索建议：最近搜索词 / 高频标签 / 章节标题 / 人物名称 / 地点名称。"""
    await check_project_access(project_id, current_user, db)
    items = await search_service.suggestions(
        db, current_user.id, project_id, q, limit=limit
    )
    return SearchSuggestionsResponse(query=q, items=items)


@router.get("/search/history", response_model=SearchHistoryListResponse)
async def search_history(
    project_id: uuid.UUID | None = Query(default=None, description="限定作品（可选）"),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最近搜索记录（按创建时间倒序）。"""
    items = await search_service.list_search_history(
        db, current_user.id, project_id=project_id, limit=limit
    )
    return SearchHistoryListResponse(items=items, total=len(items))


@router.delete("/search/history/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_search_history_item(
    history_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清除单条搜索记录。"""
    removed = await search_service.delete_search_history(db, current_user.id, history_id)
    if not removed:
        raise HTTPException(status_code=404, detail="搜索记录不存在")
    await db.commit()


@router.delete("/search/history", status_code=status.HTTP_204_NO_CONTENT)
async def clear_search_history(
    project_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清空搜索历史（可限定作品）。"""
    await search_service.clear_search_history(
        db, current_user.id, project_id=project_id
    )
    await db.commit()


def _scope_to_types(scope: str) -> list[str]:
    """scope -> type 集合映射。"""
    mapping = {
        "all": list(SEARCH_TYPES),
        "outline": ["outline", "chapter"],
        "chapter": ["chapter", "content"],
        "content": ["content"],
        "character": ["character"],
        "world": ["world_setting"],
        "world_setting": ["world_setting"],
        "location": ["world_setting"],
        "faction": ["world_setting"],
        "timeline": ["outline"],
        "timeline_event": ["outline"],
        "relation": ["outline"],
        "tag": ["tag"],
        "project": ["project"],
        "version": ["outline"],
    }
    if scope in mapping:
        return mapping[scope]
    return [scope]
