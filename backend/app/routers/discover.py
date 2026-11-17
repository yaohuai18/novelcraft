# 发现系统路由（第十四批「搜索、标签与发现系统」规格实现）
# 公开作品列表：最新发布 / 最近更新 / 热门（热度时间衰减公式）；热门标签；相似作品推荐。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.discover import (
    DiscoverResponse,
    PublicProjectListResponse,
    SimilarProjectItem,
)
from app.services import discover_service

router = APIRouter(
    prefix="/api",
    tags=["发现"]
)


@router.get("/discover", response_model=DiscoverResponse)
async def discover(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发现页：最新发布 + 最近更新 + 热门作品 + 热门标签（仅公开内容）。"""
    return await discover_service.discover(db, page=page, page_size=page_size)


@router.get("/discover/latest", response_model=PublicProjectListResponse)
async def discover_latest(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最新发布作品列表。"""
    return await discover_service.list_latest(db, page=page, page_size=page_size)


@router.get("/discover/updated", response_model=PublicProjectListResponse)
async def discover_updated(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最近更新作品列表。"""
    return await discover_service.list_recent_updated(db, page=page, page_size=page_size)


@router.get("/discover/hot", response_model=PublicProjectListResponse)
async def discover_hot(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """热门作品列表（按热度公式排序）。"""
    return await discover_service.list_hot(db, page=page, page_size=page_size)


@router.get("/projects/{project_id}/similar", response_model=list[SimilarProjectItem])
async def similar_projects(
    project_id: uuid.UUID,
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """相似作品推荐：共同标签数量越多相似度越高（仅公开作品，排除自身）。"""
    result = await discover_service.similar_projects(db, project_id, limit=limit)
    if not result:
        # 允许为空（没有公开相似作品）
        pass
    return result
