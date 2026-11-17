# 质量指标聚合接口（阶段B「人工处置与误报闭环」补充9）
#   GET /api/quality/report
#       返回正文质量指标聚合：生成截断率 / 校验阻断率 / 误报率 / 修复成功率，
#       以及误报反馈闭环摘要（false_positive 处置回灌）。
# 只读接口，不落库、不调 LLM；需 JWT 鉴权；project_id 可选（传则校验项目归属）。
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.fact_issue_disposition import FactIssueDisposition
from app.models.project import Project
from app.models.user import User
from app.schemas.quality import QualityReportResponse
from app.services import collaboration_service
from app.services.quality_service import aggregate_phase_b_metrics

router = APIRouter(prefix="/api/quality", tags=["质量指标"])


@router.get("/report", response_model=QualityReportResponse)
async def quality_report(
    project_id: str | None = Query(
        default=None, description="按项目过滤（可选，传则校验项目归属）"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回质量指标聚合报告（只读）：
    - generation_truncate_rate / validation_blocking_rate /
      false_positive_rate / repair_success_rate；
    - 误报反馈闭环：按 disposition 分组的处置统计与最近 false_positive 样本。
    """
    if project_id:
        try:
            pid = uuid.UUID(project_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="project_id 格式错误",
            )
        project = await db.get(Project, pid)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="项目不存在",
            )
        if project.user_id != current_user.id:
            role = await collaboration_service.get_role(
                db, project.id, current_user.id
            )
            if role is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="无权访问该项目",
                )

    metrics = await aggregate_phase_b_metrics(db, project_id=project_id)

    disp_q = select(FactIssueDisposition).order_by(
        FactIssueDisposition.set_at.desc()
    )
    if project_id:
        disp_q = disp_q.where(
            FactIssueDisposition.project_id == project_id
        )
    disp_rows = (
        (await db.execute(disp_q.limit(100))).scalars().all()
    )

    disp_summary: dict[str, int] = {}
    recent_fp: list[dict] = []
    for d in disp_rows:
        disp_summary[d.disposition] = disp_summary.get(d.disposition, 0) + 1
        if d.disposition == "false_positive" and len(recent_fp) < 10:
            recent_fp.append({
                "issue_key": d.issue_key,
                "rule": d.rule,
                "note": d.note,
                "set_by": d.set_by,
                "set_at": d.set_at.isoformat() if d.set_at else None,
            })

    from app.schemas.quality import QualityMetrics

    return QualityReportResponse(
        metrics=QualityMetrics(**metrics),
        disposition_summary=disp_summary,
        false_positive_feedback=recent_fp,
        generated_at=datetime.utcnow().isoformat(),
    )
