# 限流服务（第十八批「性能优化、可靠性与安全加固」规格十一）
# 固定窗口计数：维度(dimension, dimension_value) + 作用域(scope) + 窗口(period_start/period_end)。
# 维度覆盖 user_id / ip / device / project_id / api_key / global；
# 作用域覆盖 login / register / password_reset / comment / ai / import_export / search / upload / email。
# 超限时返回 error=rate_limit_exceeded + retry_after + request_id。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.rate_limit import RateLimitRecord

SCOPES = {
    "login", "register", "password_reset", "comment", "ai",
    "import_export", "search", "upload", "email",
}
DIMENSIONS = {"user_id", "ip", "device", "project_id", "api_key", "global"}


def _window_bounds() -> tuple[datetime, datetime, datetime]:
    now = datetime.utcnow()
    ts = int(now.timestamp())
    window_seconds = settings.rate_limit_window_seconds
    start_ts = (ts // window_seconds) * window_seconds
    period_start = datetime.utcfromtimestamp(start_ts)
    period_end = datetime.utcfromtimestamp(start_ts + window_seconds)
    return period_start, period_end, now


async def check_and_bump(
    db: AsyncSession,
    *,
    scope: str,
    dimension: str,
    dimension_value: str,
    limit: int | None = None,
) -> dict:
    """对 (scope, dimension, dimension_value) 执行固定窗口计数。
    返回 {allowed, retry_after, request_id, count, limit}；allowed=False 表示超限。
    """
    if scope not in SCOPES:
        raise ValueError(f"未知限流作用域: {scope}")
    if dimension not in DIMENSIONS:
        raise ValueError(f"未知限流维度: {dimension}")

    period_start, period_end, now = _window_bounds()
    effective_limit = limit or settings.rate_limit_defaults.get(scope, settings.rate_limit_defaults["default"])
    request_id = str(uuid.uuid4())

    row = (
        await db.execute(
            select(RateLimitRecord).where(
                RateLimitRecord.dimension == dimension,
                RateLimitRecord.dimension_value == dimension_value,
                RateLimitRecord.scope == scope,
                RateLimitRecord.period_start == period_start,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        # 清理过期窗口（同维度+作用域的旧窗口）
        old = (
            await db.execute(
                select(RateLimitRecord).where(
                    RateLimitRecord.dimension == dimension,
                    RateLimitRecord.dimension_value == dimension_value,
                    RateLimitRecord.scope == scope,
                    RateLimitRecord.period_start < period_start,
                )
            )
        ).scalars().all()
        for o in old:
            await db.delete(o)
        row = RateLimitRecord(
            dimension=dimension,
            dimension_value=dimension_value[:128],
            scope=scope,
            limit=effective_limit,
            count=0,
            period_start=period_start,
            period_end=period_end,
        )
        db.add(row)
        await db.flush()

    if row.count >= row.limit:
        retry_after = max(
            1,
            int((period_end - now).total_seconds()),
        )
        await db.commit()
        return {
            "allowed": False,
            "retry_after": retry_after,
            "request_id": request_id,
            "count": row.count,
            "limit": row.limit,
        }

    row.count += 1
    row.period_end = period_end
    await db.commit()
    return {
        "allowed": True,
        "retry_after": 0,
        "request_id": request_id,
        "count": row.count,
        "limit": row.limit,
    }


async def list_limits(
    db: AsyncSession,
    *,
    scope: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[RateLimitRecord], int]:
    """限流记录查询（管理端）。"""
    from sqlalchemy import func

    filters = []
    if scope:
        if scope not in SCOPES:
            raise ValueError(f"未知限流作用域: {scope}")
        filters.append(RateLimitRecord.scope == scope)
    total = (
        await db.execute(select(func.count(RateLimitRecord.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(RateLimitRecord)
            .where(*filters)
            .order_by(RateLimitRecord.period_start.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
