# 大纲路由：创建、列表（按类型筛选/排序）、详情、修改、删除、批量保存（全部需 JWT 鉴权 + 作品归属校验）
# 接口路径与行为严格按第六批规格实现
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.outline import ALLOWED_PARENT_TYPES, OUTLINE_TYPES, Outline
from app.models.project import Project
from app.models.user import User
from app.schemas.outline import (
    OutlineBatchCreate,
    OutlineCreate,
    OutlineResponse,
    OutlineUpdate
)
from app.services import index_service


router = APIRouter(
    prefix="/api/projects/{project_id}/outlines",
    tags=["大纲"]
)


async def check_project_access(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
):
    """校验作品存在且归属当前用户，非本人作品统一返回 404。"""
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id
        )
    )

    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问"
        )

    return project


async def get_outline(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
):
    """定位项目内大纲节点，作品无权访问或节点不存在均返回 404。"""
    await check_project_access(
        project_id,
        current_user,
        db
    )

    result = await db.execute(
        select(Outline).where(
            Outline.id == outline_id,
            Outline.project_id == project_id
        )
    )

    outline = result.scalar_one_or_none()

    if not outline:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="大纲不存在"
        )

    return outline


@router.post(
    "",
    response_model=OutlineResponse,
    status_code=status.HTTP_201_CREATED
)
async def create_outline(
    project_id: uuid.UUID,
    data: OutlineCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建大纲节点；校验父级同作品、父级类型符合层级约束。"""
    await check_project_access(
        project_id,
        current_user,
        db
    )

    if data.outline_type not in OUTLINE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不允许的大纲类型"
        )

    allowed_parents = ALLOWED_PARENT_TYPES[data.outline_type]

    if data.parent_id:
        parent_result = await db.execute(
            select(Outline).where(
                Outline.id == data.parent_id,
                Outline.project_id == project_id
            )
        )

        parent = parent_result.scalar_one_or_none()

        if not parent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="父级大纲不存在"
            )

        if parent.outline_type not in allowed_parents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="父级大纲类型不允许"
            )
    elif None not in allowed_parents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该大纲类型必须指定父级"
        )

    outline = Outline(
        project_id=project_id,
        **data.model_dump()
    )

    db.add(outline)
    await db.flush()

    # 大纲保存后同步搜索索引
    await index_service.sync_outline_index(db, outline)

    await db.commit()
    await db.refresh(outline)

    return outline


@router.post(
    "/batch",
    response_model=list[OutlineResponse],
    status_code=status.HTTP_201_CREATED
)
async def batch_create_outlines(
    project_id: uuid.UUID,
    data: OutlineBatchCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """批量保存大纲节点（1~100 条，第六批规格）。

    校验规则：
    - outline_type 必须属于允许类型；
    - parent_id 必须属于当前作品且存在；
    - 父级类型必须符合层级约束（summary 无父 / volume 父 summary /
      chapter 父 volume / scene 父 chapter）；
    - 新建节点尚未获得 id，无法被引用为其它条目的父级，
      自设父级与循环引用场景由 update 接口的父链追溯校验兜底；
    - 同一层级按 sort_order 排序即可正确还原大纲树。
    """
    await check_project_access(
        project_id,
        current_user,
        db
    )

    parent_ids = {
        item.parent_id
        for item in data.items
        if item.parent_id
    }

    parents: dict[uuid.UUID, Outline] = {}

    if parent_ids:
        parent_result = await db.execute(
            select(Outline).where(
                Outline.project_id == project_id,
                Outline.id.in_(parent_ids)
            )
        )

        for parent in parent_result.scalars().all():
            parents[parent.id] = parent

        missing = parent_ids - set(parents)

        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="父级大纲不存在或不属于当前作品"
            )

    for item in data.items:
        if item.outline_type not in OUTLINE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不允许的大纲类型"
            )

        allowed_parents = ALLOWED_PARENT_TYPES[item.outline_type]

        if item.parent_id:
            parent_type = parents[item.parent_id].outline_type

            if parent_type not in allowed_parents:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="父级大纲类型不允许"
                )
        elif None not in allowed_parents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="该大纲类型必须指定父级"
            )

    outlines: list[Outline] = []

    for item in data.items:
        outline = Outline(
            project_id=project_id,
            **item.model_dump()
        )

        db.add(outline)
        outlines.append(outline)

    await db.flush()

    # 批量保存后同步搜索索引
    for outline in outlines:
        await index_service.sync_outline_index(db, outline)

    await db.commit()

    for outline in outlines:
        await db.refresh(outline)

    return outlines


@router.get(
    "",
    response_model=list[OutlineResponse]
)
async def list_outlines(
    project_id: uuid.UUID,
    outline_type: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """大纲列表，支持 ?outline_type= 筛选，按 sort_order asc, created_at asc 排序。"""
    await check_project_access(
        project_id,
        current_user,
        db
    )

    query = select(Outline).where(
        Outline.project_id == project_id
    )

    if outline_type:
        query = query.where(
            Outline.outline_type == outline_type
        )

    query = query.order_by(
        Outline.sort_order.asc(),
        Outline.created_at.asc()
    )

    result = await db.execute(query)

    return list(result.scalars().all())


@router.get(
    "/{outline_id}",
    response_model=OutlineResponse
)
async def read_outline(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """大纲节点详情。"""
    return await get_outline(
        project_id,
        outline_id,
        current_user,
        db
    )


@router.put(
    "/{outline_id}",
    response_model=OutlineResponse
)
async def update_outline(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    data: OutlineUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改大纲节点；禁止自设为父级、禁止循环引用，父级必须同作品且类型合法。"""
    outline = await get_outline(
        project_id,
        outline_id,
        current_user,
        db
    )

    update_data = data.model_dump(exclude_unset=True)

    if "parent_id" in update_data:
        parent_id = update_data["parent_id"]

        if parent_id == outline.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="大纲不能将自己设置为父级"
            )

        allowed_parents = ALLOWED_PARENT_TYPES[outline.outline_type]

        if parent_id:
            result = await db.execute(
                select(Outline).where(
                    Outline.id == parent_id,
                    Outline.project_id == project_id
                )
            )

            parent = result.scalar_one_or_none()

            if not parent:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="父级大纲不存在"
                )

            if parent.outline_type not in allowed_parents:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="父级大纲类型不允许"
                )

            # 防循环引用：新父级不能是本节点的后代（沿父链上溯检测）
            cursor = parent.id
            visited = set()

            while cursor:
                if cursor == outline.id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="大纲不能形成循环引用"
                    )

                if cursor in visited:
                    break

                visited.add(cursor)

                cursor_result = await db.execute(
                    select(Outline.parent_id).where(
                        Outline.id == cursor
                    )
                )

                cursor = cursor_result.scalar_one_or_none()
        elif None not in allowed_parents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="该大纲类型必须指定父级"
            )

    for field, value in update_data.items():
        setattr(outline, field, value)

    # 大纲修改后同步搜索索引
    await index_service.sync_outline_index(db, outline)
    await db.commit()
    await db.refresh(outline)

    return outline


@router.delete(
    "/{outline_id}",
    status_code=status.HTTP_204_NO_CONTENT
)
async def delete_outline(
    project_id: uuid.UUID,
    outline_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除大纲节点（级联清理子节点）。"""
    outline = await get_outline(
        project_id,
        outline_id,
        current_user,
        db
    )

    # 第一阶段（E3 治理）：删除前先标记本大纲（若为章节）后续章节 needs_recheck，
    # 不静默假设依赖本节点的后续章节仍有效
    if outline.outline_type == "chapter":
        from app.services.chapter_lifecycle import mark_later_chapters_needs_recheck

        try:
            await mark_later_chapters_needs_recheck(db, outline)
        except Exception:  # noqa: BLE001
            pass

    await db.delete(outline)

    # 删除大纲后同步清理搜索索引（含正文索引）
    await index_service.delete_indexes_for_outline(
        db, outline.project_id, outline.id
    )

    await db.commit()
