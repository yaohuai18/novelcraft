# 标签路由（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# GET/POST /api/projects/{project_id}/tags、PUT/DELETE /api/projects/{project_id}/tags/{tag_id}、
# POST/DELETE /api/projects/{project_id}/tags/{tag_id}/relations、批量添加、大纲打标签（第九批兼容）。
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.search_document import SearchDocument
from app.models.tag import OutlineTag, Tag, TagRelation
from app.models.user import User
from app.schemas.tag import (
    OutlineTagsUpdate,
    TagBatchAddRequest,
    TagBatchAddResponse,
    TagCreate,
    TagRelationCreate,
    TagResponse,
    TagUpdate,
)
from app.services import index_service, tag_service

router = APIRouter(
    prefix="/api/projects/{project_id}/tags",
    tags=["标签"]
)


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


async def get_owned_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="标签不存在"
        )
    return tag


@router.get("", response_model=list[TagResponse])
async def list_tags(
    project_id: uuid.UUID,
    category: str | None = Query(default=None, description="标签分类（题材/风格/角色/地点/剧情/状态/时间/自定义）"),
    keyword: str | None = Query(default=None, description="按名称模糊过滤"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目标签列表（按使用次数倒序），支持分类/关键词过滤。"""
    await check_project_access(project_id, current_user, db)
    tags, _total = await tag_service.list_project_tags(
        db, project_id, category=category, keyword=keyword,
        page=page, page_size=page_size,
    )
    return tags


@router.post("", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
async def create_tag(
    project_id: uuid.UUID,
    data: TagCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建标签：名称清洗（去空格/限长/禁重复/限特殊字符/统一小写），自动生成 slug。"""
    await check_project_access(project_id, current_user, db)
    name = tag_service.clean_tag_name(data.name)
    if not name:
        raise HTTPException(status_code=400, detail="标签名称不能为空")
    try:
        await tag_service.ensure_unique_name(db, project_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    slug = await tag_service.ensure_unique_slug(
        db, project_id, tag_service.make_tag_slug(name)
    )
    category = tag_service.normalize_category(data.category or data.tag_type)
    tag = Tag(
        user_id=current_user.id,
        project_id=project_id,
        name=name,
        slug=slug,
        color=data.color or "#4F6EF7",
        description=data.description,
        category=category,
        tag_type=data.tag_type,
        usage_count=0,
    )
    db.add(tag)
    await db.flush()
    # 索引同步失败不影响标签保存
    await index_service._safe_sync(
        db, "tag_create", project_id, "tag", tag.id,
        index_service.sync_tag_index(db, tag),
    )
    await db.commit()
    await db.refresh(tag)
    return tag


@router.put("/{tag_id}", response_model=TagResponse)
async def update_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    data: TagUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新标签：改名称会重新清洗并校验唯一、重新生成 slug。"""
    await check_project_access(project_id, current_user, db)
    tag = await get_owned_tag(project_id, tag_id, current_user, db)
    if data.name is not None:
        name = tag_service.clean_tag_name(data.name)
        if not name:
            raise HTTPException(status_code=400, detail="标签名称不能为空")
        try:
            await tag_service.ensure_unique_name(db, project_id, name, exclude_id=tag.id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        tag.name = name
        tag.slug = await tag_service.ensure_unique_slug(
            db, project_id, tag_service.make_tag_slug(name), exclude_id=tag.id
        )
    if data.color is not None:
        tag.color = data.color
    if data.tag_type is not None:
        tag.tag_type = data.tag_type
    if data.category is not None:
        tag.category = tag_service.normalize_category(data.category)
    if data.description is not None:
        tag.description = data.description
    await db.flush()
    await index_service._safe_sync(
        db, "tag_update", project_id, "tag", tag.id,
        index_service.sync_tag_index(db, tag),
    )
    await db.commit()
    await db.refresh(tag)
    return tag


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除标签：同时移除其所有关系并更新索引。"""
    await check_project_access(project_id, current_user, db)
    tag = await get_owned_tag(project_id, tag_id, current_user, db)
    # 收集受影响目标，删除关系
    rels = (await db.execute(
        select(TagRelation).where(TagRelation.tag_id == tag_id)
    )).scalars().all()
    affected: list[tuple[str, uuid.UUID]] = [
        (r.target_type, r.target_id) for r in rels
    ]
    for rel in rels:
        await db.delete(rel)
    ots = (await db.execute(
        select(OutlineTag).where(OutlineTag.tag_id == tag_id)
    )).scalars().all()
    for ot in ots:
        await db.delete(ot)
    await db.delete(tag)
    await db.flush()
    # 刷新受影响目标的标签文本索引（标签已删，重算后名称自然移除，避免残留误命中）
    seen: set[tuple[str, uuid.UUID]] = set()
    for target_type, target_id in affected:
        key = (target_type, target_id)
        if key in seen:
            continue
        seen.add(key)
        await index_service._safe_sync(
            db, "tag_delete_refresh", project_id, target_type, target_id,
            index_service.refresh_tags_text_for_target(
                db, project_id, target_type, target_id
            ),
        )
    await index_service._safe_sync(
        db, "tag_delete", project_id, "tag", tag_id,
        index_service.delete_index(db, project_id, "tag", tag_id),
    )
    await db.commit()


@router.post(
    "/{tag_id}/relations",
    response_model=TagRelationCreate,
    status_code=status.HTTP_201_CREATED,
)
async def add_relation(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    data: TagRelationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """给标签添加一个目标关系（作品/分卷/章节/人物/地点/势力/时间线事件/世界观规则）。"""
    await check_project_access(project_id, current_user, db)
    await get_owned_tag(project_id, tag_id, current_user, db)
    try:
        rel = await tag_service.add_relation(
            db, project_id, tag_id, data.target_type, data.target_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if rel is None:
        raise HTTPException(status_code=409, detail="关系已存在")
    await index_service._safe_sync(
        db, "tag_rel_add", project_id, data.target_type, data.target_id,
        index_service.refresh_tags_text_for_target(
            db, project_id, data.target_type, data.target_id
        ),
    )
    await db.commit()
    return TagRelationCreate(target_type=rel.target_type, target_id=rel.target_id)


@router.delete(
    "/{tag_id}/relations/{relation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_relation(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    relation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除一条标签关系。"""
    await check_project_access(project_id, current_user, db)
    await get_owned_tag(project_id, tag_id, current_user, db)
    rel = await db.get(TagRelation, relation_id)
    if rel is None or rel.tag_id != tag_id:
        raise HTTPException(status_code=404, detail="关系不存在")
    target_type, target_id = rel.target_type, rel.target_id
    await db.delete(rel)
    tag = await db.get(Tag, tag_id)
    if tag is not None and tag.usage_count > 0:
        tag.usage_count -= 1
    await db.flush()
    await index_service._safe_sync(
        db, "tag_rel_remove", project_id, target_type, target_id,
        index_service.refresh_tags_text_for_target(
            db, project_id, target_type, target_id
        ),
    )
    await db.commit()


@router.post("/batch", response_model=TagBatchAddResponse)
async def batch_add_tags(
    project_id: uuid.UUID,
    data: TagBatchAddRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量添加标签：target_type + target_ids + tag_ids。"""
    await check_project_access(project_id, current_user, db)
    try:
        created, skipped, relation_ids = await tag_service.add_batch_relations(
            db, project_id, data.target_type, data.target_ids, data.tag_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 刷新受影响目标的 tags_text（索引）
    for target_id in data.target_ids:
        await index_service._safe_sync(
            db, "tag_batch", project_id, data.target_type, target_id,
            index_service.refresh_tags_text_for_target(
                db, project_id, data.target_type, target_id
            ),
        )
    await db.commit()
    return TagBatchAddResponse(created=created, skipped=skipped, relation_ids=relation_ids)


# ============ 第九批兼容：大纲打标签 ============

@router.post("/{tag_id}/outlines/{outline_id}", status_code=status.HTTP_201_CREATED)
async def add_outline_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """给大纲条目打标签（第九批接口，保留兼容）。"""
    await check_project_access(project_id, current_user, db)
    await get_owned_tag(project_id, tag_id, current_user, db)
    try:
        created = await tag_service.add_outline_tag(db, project_id, tag_id, outline_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not created:
        raise HTTPException(status_code=409, detail="标签已存在")
    await index_service._safe_sync(
        db, "outline_tag_add", project_id, "outline", outline_id,
        index_service.refresh_tags_text_for_outline(db, outline_id),
    )
    await db.commit()
    return {"detail": "ok"}


@router.delete("/{tag_id}/outlines/{outline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_outline_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除大纲标签（第九批接口，保留兼容）。"""
    await check_project_access(project_id, current_user, db)
    await get_owned_tag(project_id, tag_id, current_user, db)
    removed = await tag_service.remove_outline_tag(db, project_id, tag_id, outline_id)
    if not removed:
        raise HTTPException(status_code=404, detail="标签关系不存在")
    await index_service._safe_sync(
        db, "outline_tag_remove", project_id, "outline", outline_id,
        index_service.refresh_tags_text_for_outline(db, outline_id),
    )
    await db.commit()


@router.put("/outlines/{outline_id}/tags", response_model=OutlineTagsUpdate)
async def update_outline_tags(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: OutlineTagsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整体替换大纲条目标签集合（第九批接口，保留兼容）。"""
    await check_project_access(project_id, current_user, db)
    old = (await db.execute(
        select(OutlineTag).where(OutlineTag.outline_id == outline_id)
    )).scalars().all()
    for ot in old:
        await db.delete(ot)
        tag = await db.get(Tag, ot.tag_id)
        if tag is not None and tag.usage_count > 0:
            tag.usage_count -= 1
    for tid in data.tag_ids:
        tag = await db.get(Tag, tid)
        if tag is None or tag.project_id != project_id:
            raise HTTPException(status_code=400, detail="标签不存在")
        exists = (await db.execute(
            select(OutlineTag).where(
                OutlineTag.outline_id == outline_id,
                OutlineTag.tag_id == tid,
            )
        )).scalar_one_or_none()
        if exists is None:
            db.add(OutlineTag(outline_id=outline_id, tag_id=tid))
            tag.usage_count += 1
    await index_service._safe_sync(
        db, "outline_tags_replace", project_id, "outline", outline_id,
        index_service.refresh_tags_text_for_outline(db, outline_id),
    )
    await db.commit()
    return data
