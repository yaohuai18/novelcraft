# 开放 API v1 项目资源路由（第十九批「移动端适配与开放 API」规格十五）
# GET/POST /api/v1/projects，GET/PATCH /api/v1/projects/{id}。
# 越权防护：仅作者本人或管理员可见/修改；匿名严格限流。
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.project import Project
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import ProjectCreateRequest, ProjectUpdateRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/projects", tags=["开放API-项目", "实验"])

# 依赖：projects:read / projects:write + 通用限流
async def guard_read(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="projects:read", kind="general", credentials=credentials, db=db)


async def guard_write(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="projects:write", kind="general", credentials=credentials, db=db)


def _project_dict(p: Project) -> dict:
    return {
        "id": str(p.id),
        "title": p.title,
        "description": p.description,
        "genre": p.genre,
        "publication_status": p.publication_status,
        "status": p.status,
        "chapter_count": p.chapter_count,
        "word_count": p.word_count,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def _can_access(db: AsyncSession, project: Project, user) -> bool:
    return project.user_id == user.id or user.role in ("operator", "super_admin")


async def _get_owned_project(db: AsyncSession, project_id: UUID, user) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", "项目不存在", status_code=404)
    if not await _can_access(db, project, user):
        # 不泄露他人项目存在性
        raise ApiError("not_found", "项目不存在", status_code=404)
    return project


@router.get("")
async def list_projects(
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    user = guard["principal"].user
    if user.role in ("operator", "super_admin"):
        result = await db.execute(
            select(Project).order_by(Project.updated_at.desc()).limit(100)
        )
        projects = list(result.scalars().all())
    else:
        result = await db.execute(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.updated_at.desc())
            .limit(100)
        )
        projects = list(result.scalars().all())
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"projects": [_project_dict(p) for p in projects], "count": len(projects)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("", status_code=201)
async def create_project(
    request: Request,
    payload: ProjectCreateRequest,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    project = Project(
        user_id=guard["principal"].user.id,
        title=payload.title.strip(),
        description=payload.description,
        genre=payload.genre,
        status="active",
        publication_status="draft",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"project": _project_dict(project)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/{project_id}")
async def get_project(
    project_id: UUID,
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(db, project_id, guard["principal"].user)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"project": _project_dict(project)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.patch("/{project_id}")
async def update_project(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(db, project_id, guard["principal"].user)
    if payload.title is not None:
        project.title = payload.title.strip()
    if payload.description is not None:
        project.description = payload.description
    if payload.genre is not None:
        project.genre = payload.genre
    await db.commit()
    await db.refresh(project)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"project": _project_dict(project)},
        rate_headers=build_rate_headers(guard["rate"]),
    )
