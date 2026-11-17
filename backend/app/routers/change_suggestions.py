# 变更建议路由（第三期「变更确认流程」核心）
# 待确认建议列表 / 详情 / 确认（confirm | modified | ignore）；
# 确认后由 state_extraction_service 写入正式库（人物卡、快照、关系、时间线、伏笔）
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.change_suggestion import ChangeSuggestion
from app.models.character import Character
from app.models.project import Project
from app.models.user import User
from app.schemas.change_suggestion import (
    SUGGESTION_STATUS,
    SuggestionConfirmRequest,
    SuggestionCreateRequest,
    SuggestionListResponse,
)
from app.services.state_extraction_service import confirm_suggestion

router = APIRouter(
    prefix="/api/projects/{project_id}/suggestions",
    tags=["变更建议"],
)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
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


def _load_row(row: ChangeSuggestion) -> ChangeSuggestion:
    try:
        row.payload = json.loads(row.payload or "{}")
    except Exception:  # noqa: BLE001
        row.payload = {}
    return row


@router.get("", response_model=list[SuggestionListResponse])
async def list_suggestions(
    project_id: uuid.UUID,
    status_filter: str | None = None,
    category: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """变更建议列表，默认 pending；可按 status / category 过滤。"""
    await get_owned_project(project_id, current_user, db)
    stmt = (
        select(ChangeSuggestion)
        .where(ChangeSuggestion.project_id == project_id)
        .order_by(ChangeSuggestion.created_at.asc())
    )
    if status_filter:
        stmt = stmt.where(ChangeSuggestion.status == status_filter)
    else:
        stmt = stmt.where(ChangeSuggestion.status == "pending")
    if category:
        stmt = stmt.where(ChangeSuggestion.category == category)
    rows = (await db.execute(stmt)).scalars().all()
    return [_load_row(r) for r in rows]


@router.post("", response_model=SuggestionListResponse, status_code=201)
async def create_suggestion(
    project_id: uuid.UUID,
    data: SuggestionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新建变更建议（第四期「采纳建议」：把一致性检查 proposed_updates 转入第三期确认流程）。

    category 归一化：character_state -> character，其余沿用 relationship|timeline|foreshadow。
    """
    await get_owned_project(project_id, current_user, db)
    category = data.category
    if category == "character_state":
        category = "character"
    if category not in ("character", "relationship", "timeline", "foreshadow"):
        raise HTTPException(
            status_code=422,
            detail="category 必须为 character_state|relationship|timeline|foreshadow",
        )
    payload_dict = data.payload or {}

    # 解析人物引用：character/character_name → character_id；target_character* → target_character_id
    character_id = None
    target_character_id = None

    async def _find_char(name: str):
        if not name:
            return None
        row = (
            await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.name == name,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        candidates = (
            await db.execute(
                select(Character).where(Character.project_id == project_id)
            )
        ).scalars().all()
        for cand in candidates:
            try:
                aliases = json.loads(cand.aliases or "[]")
            except Exception:  # noqa: BLE001
                aliases = []
            if isinstance(aliases, list) and name in aliases:
                return cand
        return None

    if category in ("character", "relationship"):
        char_row = await _find_char(
            payload_dict.get("character") or payload_dict.get("character_name")
        )
        if char_row is not None:
            character_id = char_row.id
        if category == "relationship":
            tgt_row = await _find_char(
                payload_dict.get("target_character")
                or payload_dict.get("target_character_name")
            )
            if tgt_row is not None:
                target_character_id = tgt_row.id

    now = datetime.utcnow()
    row = ChangeSuggestion(
        id=uuid.uuid4(),
        project_id=project_id,
        chapter_id=data.chapter_id,
        category=category,
        character_id=character_id,
        target_character_id=target_character_id,
        title=data.title,
        description=data.description,
        payload=json.dumps(payload_dict, ensure_ascii=False),
        status="pending",
        created_at=now,
        confirmed_at=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _load_row(row)


@router.get("/{suggestion_id}", response_model=SuggestionListResponse)
async def get_suggestion(
    project_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """建议详情。"""
    await get_owned_project(project_id, current_user, db)
    row = (
        await db.execute(
            select(ChangeSuggestion).where(
                ChangeSuggestion.id == suggestion_id,
                ChangeSuggestion.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="建议不存在"
        )
    return _load_row(row)


@router.post("/{suggestion_id}/confirm", response_model=SuggestionListResponse)
async def confirm_one_suggestion(
    project_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    data: SuggestionConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """确认/修改确认/忽略建议。action=confirm|modified|ignore；modified 时 payload 覆盖。"""
    await get_owned_project(project_id, current_user, db)
    if data.action not in ("confirm", "modified", "ignore"):
        raise HTTPException(status_code=422, detail="action 必须为 confirm|modified|ignore")

    row = (
        await db.execute(
            select(ChangeSuggestion).where(
                ChangeSuggestion.id == suggestion_id,
                ChangeSuggestion.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="建议不存在"
        )
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"建议已处理（当前状态 {row.status}）")

    updated = await confirm_suggestion(
        db, suggestion_id, action=data.action, payload=data.payload
    )
    return _load_row(updated)
