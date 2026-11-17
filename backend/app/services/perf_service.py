# 性能监控服务（第十八批「性能优化、可靠性与安全加固」规格二）
# 记录请求量 / P50 / P95 / P99 / 错误率 / 超时 / DB耗时 / 缓存命中率 / 队列等待 / AI与搜索耗时，
# 以及最慢接口与最慢 SQL；聚合快照持久化到 PerformanceMetric。
import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.performance_metric import PerformanceMetric


class _LiveStore:
    """进程内实时指标（内存采样），供聚合与健康检查使用。"""

    def __init__(self):
        self.durations: list[float] = []
        self.db_ms: list[float] = []
        self.ai_ms: list[float] = []
        self.search_ms: list[float] = []
        self.queue_wait_ms: list[float] = []
        self.cache_hits = 0
        self.cache_misses = 0
        self.errors = 0
        self.timeouts = 0
        self.total = 0
        self.slowest_endpoints: dict[str, float] = {}
        self.slowest_sql: dict[str, float] = {}
        self.start_ts = time.time()

    def record(
        self,
        *,
        duration_ms: float,
        db_ms: float = 0,
        ai_ms: float = 0,
        search_ms: float = 0,
        queue_wait_ms: float = 0,
        cache_hit: bool | None = None,
        is_error: bool = False,
        is_timeout: bool = False,
        slow_sql: str | None = None,
        endpoint: str = "",
        method: str = "",
    ):
        self.total += 1
        self.durations.append(duration_ms)
        self.db_ms.append(db_ms)
        self.ai_ms.append(ai_ms)
        self.search_ms.append(search_ms)
        self.queue_wait_ms.append(queue_wait_ms)
        if cache_hit is True:
            self.cache_hits += 1
        elif cache_hit is False:
            self.cache_misses += 1
        if is_error:
            self.errors += 1
        if is_timeout:
            self.timeouts += 1
        if endpoint:
            key = f"{method} {endpoint}" if method else endpoint
            cur = self.slowest_endpoints.get(key, 0)
            if duration_ms > cur:
                self.slowest_endpoints[key] = duration_ms
        if slow_sql:
            cur = self.slowest_sql.get(slow_sql, 0)
            if db_ms > cur:
                self.slowest_sql[slow_sql] = db_ms

    def snapshot(self) -> dict:
        def pct(arr, p):
            return round(statistics.quantiles(arr, n=100, method="inclusive")[p - 1], 2) if arr else 0.0

        n = self.total
        return {
            "request_total": n,
            "avg_response_ms": round(sum(self.durations) / n, 2) if n else 0.0,
            "p50_ms": pct(self.durations, 50),
            "p95_ms": pct(self.durations, 95),
            "p99_ms": pct(self.durations, 99),
            "error_rate": round(self.errors / n, 4) if n else 0.0,
            "timeout_count": self.timeouts,
            "db_avg_ms": round(sum(self.db_ms) / n, 2) if n else 0.0,
            "ai_avg_ms": round(sum(self.ai_ms) / n, 2) if n else 0.0,
            "search_avg_ms": round(sum(self.search_ms) / n, 2) if n else 0.0,
            "queue_wait_avg_ms": round(sum(self.queue_wait_ms) / n, 2) if n else 0.0,
            "cache_hit_rate": round(self.cache_hits / (self.cache_hits + self.cache_misses), 4)
            if (self.cache_hits + self.cache_misses)
            else 0.0,
            "slowest_endpoints": sorted(
                self.slowest_endpoints.items(), key=lambda kv: -kv[1]
            )[:10],
            "slowest_sql": sorted(self.slowest_sql.items(), key=lambda kv: -kv[1])[:10],
            "uptime_seconds": round(time.time() - self.start_ts, 1),
        }


live = _LiveStore()


async def persist_snapshot(db: AsyncSession) -> PerformanceMetric:
    """将当前进程内指标聚合成 PerformanceMetric 行落库（可定时调用）。"""
    snap = live.snapshot()
    row = PerformanceMetric(
        endpoint="__aggregate__",
        method="AGGREGATE",
        status_code=200,
        duration_ms=snap["p95_ms"],
        db_ms=snap["db_avg_ms"],
        ai_ms=snap["ai_avg_ms"],
        search_ms=snap["search_avg_ms"],
        queue_wait_ms=snap["queue_wait_avg_ms"],
        cache_hit=snap["cache_hit_rate"] >= 0.5,
        is_error=snap["error_rate"] > 0,
        is_timeout=snap["timeout_count"] > 0,
        slow_sql=json.dumps(snap["slowest_sql"], ensure_ascii=False),
        user_id=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def record_row(
    db: AsyncSession,
    *,
    endpoint: str,
    method: str,
    status_code: int,
    duration_ms: float,
    db_ms: float = 0,
    ai_ms: float = 0,
    search_ms: float = 0,
    queue_wait_ms: float = 0,
    cache_hit: bool = False,
    is_error: bool = False,
    is_timeout: bool = False,
    slow_sql: str | None = None,
    user_id: uuid.UUID | None = None,
) -> PerformanceMetric:
    """逐请求落库（可关闭以节省写入）。"""
    row = PerformanceMetric(
        endpoint=endpoint[:200],
        method=method[:10],
        status_code=status_code,
        duration_ms=duration_ms,
        db_ms=db_ms,
        ai_ms=ai_ms,
        search_ms=search_ms,
        queue_wait_ms=queue_wait_ms,
        cache_hit=cache_hit,
        is_error=is_error,
        is_timeout=is_timeout,
        slow_sql=(slow_sql or "")[:2000] or None,
        user_id=user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_metrics(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[PerformanceMetric], int]:
    from sqlalchemy import func

    total = (
        await db.execute(select(func.count(PerformanceMetric.id)))
    ).scalar_one()
    rows = (
        await db.execute(
            select(PerformanceMetric)
            .order_by(PerformanceMetric.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
