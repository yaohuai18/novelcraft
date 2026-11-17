# 失败样本管理（2026-11-16 第三档升级）：项目级「过去犯过的错」账本。
# 生成时自动注入 system prompt；此处提供列表 / 新增 / 删除 / 启停。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.failure_sample import FailureSample
from app.models.project import Project
from app.models.user import User
from app.services import collaboration_service

router = APIRouter(
    prefix="/api/projects/{project_id}/failure-samples",
    tags=["失败样本"],
)


class FailureSampleCreate(BaseModel):
    category: str = Field(..., max_length=30)
    bad_example: str = Field(..., min_length=1)
    correct_way: str = Field(..., min_length=1)
    source: str = Field(default="manual", max_length=32)


async def _require_editor(db: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    if project.user_id != user.id:
        role = await collaboration_service.get_role(db, project.id, user.id)
        if role not in ("owner", "editor"):
            raise HTTPException(status_code=403, detail="仅项目作者或编辑可管理失败样本")


@router.get("")
async def list_failure_samples(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目失败样本 + 全局样本。"""
    rows = (
        await db.execute(
            select(FailureSample)
            .where(
                or_(
                    FailureSample.project_id == project_id,
                    FailureSample.project_id.is_(None),
                )
            )
            .order_by(FailureSample.created_at.asc())
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id) if r.project_id else None,
            "category": r.category,
            "bad_example": r.bad_example,
            "correct_way": r.correct_way,
            "source": r.source,
            "enabled": r.enabled,
        }
        for r in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_failure_sample(
    project_id: uuid.UUID,
    data: FailureSampleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手工登记一条失败样本（下次生成即生效）。"""
    await _require_editor(db, project_id, current_user)
    sample = FailureSample(
        project_id=project_id,
        category=data.category,
        bad_example=data.bad_example,
        correct_way=data.correct_way,
        source=data.source,
    )
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    return {"id": str(sample.id)}


@router.delete("/{sample_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_failure_sample(
    project_id: uuid.UUID,
    sample_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_editor(db, project_id, current_user)
    sample = await db.get(FailureSample, sample_id)
    if sample is None or (
        sample.project_id is not None and sample.project_id != project_id
    ):
        raise HTTPException(status_code=404, detail="样本不存在")
    await db.delete(sample)
    await db.commit()
