# 一致性检查路由（第四期「写错能发现」核心接口）
# POST /api/ai/check-consistency 对指定章节正文执行一致性检查（action_type=check 落 generation_records）
# GET /api/projects/{project_id}/consistency/checks 最近检查历史（重新检查面板可回看）
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.generation import GenerationRecord
from app.models.project import Project
from app.models.user import User
from app.schemas.consistency import (
    CheckConsistencyRequest,
    CheckConsistencyResponse,
    ConsistencyCheckRecord,
    DataConsistencyCheckRequest,
    DataConsistencyIssue,
    DataConsistencyRunResponse,
    DataConsistencySummary,
)
from app.services import collaboration_service, quick_consistency
from app.services.consistency_checker import run_static_consistency_check
from app.services.consistency_service import run_consistency_check

router = APIRouter(tags=["一致性检查"])


async def _get_project(
    db: AsyncSession, project_id: uuid.UUID, current_user: User
) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在或无权访问",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="作品不存在或无权访问",
            )
    return project


@router.post(
    "/api/ai/check-consistency", response_model=CheckConsistencyResponse
)
async def check_consistency(
    data: CheckConsistencyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """对指定章节正文执行一致性检查。

    检查依据为项目已确认设定（人物卡/快照/时间线/伏笔/章节记忆）。
    输出 score/issues[]/confirmed_updates[]/proposed_updates[]/reminders[]/
    outline 完成度/repetition/style。采纳 proposed_updates 请走第三期变更确认流程。
    """
    await _get_project(db, data.project_id, current_user)
    result = await run_consistency_check(
        db,
        project_id=data.project_id,
        chapter_id=data.chapter_id,
        chapter_title=data.chapter_title,
        content=data.content,
        outline_text=data.outline_text,
    )
    return CheckConsistencyResponse(
        mode=result.get("mode", "mock"),
        score=result.get("score", 0),
        issues=result.get("issues", []),
        confirmed_updates=result.get("confirmed_updates", []),
        proposed_updates=result.get("proposed_updates", []),
        reminders=result.get("reminders", []),
        outline=result.get("outline", {"completed": [], "uncovered": [], "score": 0}),
        repetition=result.get("repetition", {"internal": [], "against_previous_summary": []}),
        style=result.get("style", {"score": 0, "evaluation": ""}),
        checked_at=datetime.utcnow().isoformat(),
    )


@router.get(
    "/api/projects/{project_id}/consistency/checks",
    response_model=list[ConsistencyCheckRecord],
)
async def list_consistency_checks(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None = None,
    action_type: str = Query(default="all", pattern="^(all|check|consistency)$"),
    limit: int = Query(default=20, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一致性检查历史（action_type=check 的 AI 正文检查 + action_type=consistency 的静态数据层检查）。"""
    await _get_project(db, project_id, current_user)
    conds = [GenerationRecord.project_id == project_id]
    if action_type == "all":
        conds.append(GenerationRecord.action_type.in_(["check", "consistency"]))
    else:
        conds.append(GenerationRecord.action_type == action_type)
    stmt = (
        select(GenerationRecord)
        .where(*conds)
        .order_by(GenerationRecord.created_at.desc())
        .limit(limit)
    )
    if chapter_id is not None:
        conds.append(GenerationRecord.chapter_id == chapter_id)
        stmt = (
            select(GenerationRecord)
            .where(*conds)
            .order_by(GenerationRecord.created_at.desc())
            .limit(limit)
        )
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for r in rows:
        try:
            result = json.loads(r.response_content or "{}")
        except Exception:  # noqa: BLE001
            result = {}
        if not isinstance(result, dict):
            result = {}
        is_static = r.action_type == "consistency"
        out.append(
            ConsistencyCheckRecord(
                id=str(r.id),
                chapter_id=str(r.chapter_id) if r.chapter_id else "",
                chapter_title="",
                action_type=r.action_type or "check",
                status=r.status or "succeeded",
                result=result,
                created_at=r.created_at.isoformat() if r.created_at else "",
                dimension=result.get("dimension", "") if is_static else "",
                summary=result.get("summary", {}) if is_static else {},
            )
        )
    return out


@router.post(
    "/api/projects/{project_id}/consistency/check",
    response_model=DataConsistencyRunResponse,
)
async def run_data_consistency_check(
    project_id: uuid.UUID,
    data: DataConsistencyCheckRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发数据层一致性检查（全量或按维度），返回结构化问题并落库可查历史。"""
    await _get_project(db, project_id, current_user)
    dimension = (data.dimension if data and data.dimension else "all") or "all"
    if dimension not in (
        "all", "dangling_refs", "character_state",
        "relationships", "foreshadows", "appearance", "memory",
    ):
        # 非法维度兜底为全量检查（与 run_static_consistency_check 归一逻辑一致）
        dimension = "all"
    issues, summary = await run_static_consistency_check(
        db, project_id, dimension=dimension
    )
    payload = {
        "dimension": dimension,
        "summary": summary.model_dump(),
        "issues": [i.model_dump() for i in issues],
    }
    record = GenerationRecord(
        project_id=project_id,
        action_type="consistency",
        status="succeeded",
        system_prompt="静态一致性检查（数据层）",
        user_prompt=f"dimension={dimension}",
        response_content=json.dumps(payload, ensure_ascii=False),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return DataConsistencyRunResponse(
        check_id=str(record.id),
        project_id=str(project_id),
        dimension=dimension,
        status="succeeded",
        mode="static",
        summary=summary,
        issues=issues,
        created_at=record.created_at.isoformat()
        if record.created_at
        else datetime.utcnow().isoformat(),
    )


# ---------- 创作页轻量一致性校验（2026-11-17 批次 2） ----------

class QuickCheckRequest(BaseModel):
    """无库快速校验：创作页把内存中的规划与章节直接送来检查。"""

    plan: dict | None = None
    chapters: list[dict] = []
    rules: list[str] = []


class QuickCheckResponse(BaseModel):
    issues: list[dict]
    dimensions: int
    checked_at: str
    message: str


@router.post("/api/consistency/quick-check", response_model=QuickCheckResponse)
async def quick_check(
    data: QuickCheckRequest,
    current_user: User = Depends(get_current_user),
):
    """保存前的 4 维轻校验（伏笔顺序/时间线/人物状态/规则违反）。

    纯规则、零 AI 成本、不落库；返回 issues[] 由前端黄条展示，不阻断保存。
    """
    result = quick_consistency.quick_check(data.plan, data.chapters, data.rules)
    n = len(result["issues"])
    return QuickCheckResponse(
        issues=result["issues"],
        dimensions=result["dimensions"],
        checked_at=result["checked_at"],
        message=f"检查完成：{n} 个提示" if n else "检查完成：未发现矛盾",
    )


# ---------- 批次 4：追读力分析报告（只读、零 AI 成本） ----------

class ReadabilityReportResponse(BaseModel):
    project_id: str
    project_title: str
    chapters_total: int
    hook_coverage: dict
    thrill_density: dict
    foreshadow_debt: dict
    checked_at: str


@router.get(
    "/api/projects/{project_id}/readability-report",
    response_model=ReadabilityReportResponse,
)
async def readability_report(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """追读力摘要：钩子覆盖率 / 爽点密度 / 伏笔债务（未回收清单）。"""
    await _get_project(db, project_id, current_user)
    from app.services import readability as readability_service

    return await readability_service.compute_readability_report(db, project_id)
