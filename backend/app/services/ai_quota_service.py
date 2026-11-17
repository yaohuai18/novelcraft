# AI 成本保护服务（第十八批「性能优化、可靠性与安全加固」规格十二）
# AI 额度：每日调用次数 / 项目预算 / 用户预算 / 并发限制。
# 提交 AI 任务前检查：用户权限、剩余额度、内容长度、模型可用性、项目状态。
# 额度扣减属于强一致场景：额度更新与业务任务创建在同一事务内提交。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ai_quota import AIQuota


async def _quota_row(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    period_date: str,
) -> AIQuota:
    row = (
        await db.execute(
            select(AIQuota).where(
                AIQuota.user_id == user_id,
                AIQuota.project_id == project_id,
                AIQuota.period_date == period_date,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIQuota(
            user_id=user_id,
            project_id=project_id,
            period_date=period_date,
            daily_limit=settings.ai_daily_default,
            project_budget=settings.ai_project_budget_default if project_id else None,
            user_budget=settings.ai_user_budget_default,
            concurrency_limit=settings.ai_concurrency_default,
        )
        db.add(row)
        await db.flush()
    return row


async def check_submission(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    content_length: int,
    period_date: str,
    user_role: str = "user",
    project_status: str | None = None,
    ai_model_available: bool = True,
) -> dict:
    """提交前检查：返回 {allowed, reason, quota}。
    检查项：内容长度、模型可用性、项目状态、每日额度、项目预算、用户预算、并发。
    会员用户可获得更高每日额度（role=member 时 daily_limit*2）。
    """
    if content_length > settings.ai_max_input_chars:
        return {"allowed": False, "reason": "content_too_long", "quota": None}
    if not ai_model_available:
        return {"allowed": False, "reason": "model_unavailable", "quota": None}
    if project_status is not None and project_status in ("archived", "deleted"):
        return {"allowed": False, "reason": "project_unavailable", "quota": None}

    quota = await _quota_row(db, user_id, project_id, period_date)
    daily_limit = quota.daily_limit * 2 if user_role == "member" else quota.daily_limit

    if quota.daily_used >= daily_limit:
        return {"allowed": False, "reason": "daily_limit_exceeded", "quota": quota}
    if quota.project_budget is not None and quota.project_used >= quota.project_budget:
        return {"allowed": False, "reason": "project_budget_exceeded", "quota": quota}
    if quota.user_budget is not None and quota.user_used >= quota.user_budget:
        return {"allowed": False, "reason": "user_budget_exceeded", "quota": quota}
    if quota.concurrency_active >= quota.concurrency_limit:
        return {"allowed": False, "reason": "concurrency_limit_reached", "quota": quota}

    return {"allowed": True, "reason": "ok", "quota": quota}


async def consume(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    period_date: str,
) -> AIQuota:
    """额度扣减（业务成功后在业务事务内调用；重复提交由幂等层拦截）。"""
    quota = await _quota_row(db, user_id, project_id, period_date)
    quota.daily_used += 1
    if project_id is not None:
        quota.project_used += 1
    quota.user_used += 1
    quota.updated_at = datetime.utcnow()
    await db.flush()
    return quota


async def mark_running(db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID | None, period_date: str, delta: int) -> None:
    """调整并发活跃数（任务开始 +1 / 结束 -1）。"""
    quota = await _quota_row(db, user_id, project_id, period_date)
    quota.concurrency_active = max(0, quota.concurrency_active + delta)
    quota.updated_at = datetime.utcnow()
    await db.flush()


async def get_quota(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    period_date: str,
) -> dict:
    """查询额度（供管理端与用户查看）。"""
    quota = await _quota_row(db, user_id, project_id, period_date)
    return {
        "user_id": str(quota.user_id),
        "project_id": str(quota.project_id) if quota.project_id else None,
        "period_date": quota.period_date,
        "daily_limit": quota.daily_limit,
        "daily_used": quota.daily_used,
        "project_budget": quota.project_budget,
        "project_used": quota.project_used,
        "user_budget": quota.user_budget,
        "user_used": quota.user_used,
        "concurrency_limit": quota.concurrency_limit,
        "concurrency_active": quota.concurrency_active,
    }
