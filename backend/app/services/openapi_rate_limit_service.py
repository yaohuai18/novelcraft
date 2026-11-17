# 开放 API 独立限流服务（第十九批「移动端适配与开放 API」规格二十）
# 分级限流：匿名（严格）/ 普通应用（按分钟）/ 可信应用（更高配额）/
#           AI 接口（单独限制）/ 导出接口（低并发限制）。
# 固定窗口计数（复用 RateLimitRecord，dimension="openapi"）；响应头
# X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset。
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rate_limit import RateLimitRecord

# 限流分级（每分钟请求数）
RATE_PLAN_DEFAULT = {
    "anonymous": 30,       # 匿名：严格限制
    "normal": 120,         # 普通应用 / 个人令牌
    "trusted": 600,        # 可信应用（已启用应用的应用令牌）
    "internal": 3000,      # 自家 JWT 客户端
}
AI_RATE_PLAN = {"anonymous": 5, "normal": 10, "trusted": 30, "internal": 60}
EXPORT_RATE_PLAN = {"anonymous": 1, "normal": 2, "trusted": 5, "internal": 10}


async def _bucket(
    db: AsyncSession,
    dimension_value: str,
    scope: str,
    period_start: datetime,
    period_end: datetime,
    limit: int,
) -> RateLimitRecord:
    row = (
        await db.execute(
            select(RateLimitRecord).where(
                RateLimitRecord.dimension == "openapi",
                RateLimitRecord.dimension_value == dimension_value,
                RateLimitRecord.scope == scope,
                RateLimitRecord.period_start == period_start,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = RateLimitRecord(
            dimension="openapi",
            dimension_value=dimension_value,
            scope=scope,
            limit=limit,
            count=1,
            period_start=period_start,
            period_end=period_end,
        )
        db.add(row)
        await db.flush()
    return row


def _window(timestamp: int, window_seconds: int) -> tuple[datetime, datetime]:
    start_ts = (timestamp // window_seconds) * window_seconds
    start = datetime.utcfromtimestamp(start_ts)
    end = start + timedelta(seconds=window_seconds)
    return start, end


async def check_and_bump(
    db: AsyncSession,
    *,
    client_key: str,
    plan: dict,
    timestamp: int,
    kind: str = "general",
    window_seconds: int = 60,
) -> dict:
    """固定窗口限流检查 + 计数。返回 {allowed, limit, count, remaining, retry_after, window_remaining}。"""
    limit = plan.get(client_key, plan.get("normal", 120))
    period_start, period_end = _window(timestamp, window_seconds)
    scope = f"{kind}:{window_seconds}s"

    bucket = await _bucket(
        db, client_key, scope, period_start, period_end, limit
    )

    allowed = bucket.count <= limit
    remaining = max(0, limit - bucket.count)
    bucket.count += 1
    await db.flush()

    return {
        "allowed": allowed,
        "limit": limit,
        "count": bucket.count,
        "remaining": remaining,
        "retry_after": max(0, int(period_end.timestamp()) - timestamp),
        "window_remaining": max(0, int(period_end.timestamp()) - timestamp),
    }


def build_rate_headers(result: dict) -> dict:
    """限流检查结果 → 响应头。"""
    return {
        "X-RateLimit-Limit": str(result.get("limit", 0)),
        "X-RateLimit-Remaining": str(result.get("remaining", 0)),
        "X-RateLimit-Reset": str(result.get("window_remaining", 0)),
    }
