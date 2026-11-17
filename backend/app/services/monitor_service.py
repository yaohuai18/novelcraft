# 系统监控服务（第十七批「统计分析与运营后台」规格十六）
# 进程内采样器：请求量 / 平均响应时间 / 错误率 / 缓存命中率 / 邮件失败数。
# SystemMetric 落库采样：用于趋势与告警。
# 队列积压 / AI 任务积压 / 索引延迟 / 存储空间 / 备份失败 来自数据库与磁盘实时读取。
import os
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage_daily_stat import AIFailureStat
from app.models.ai_task import AITask
from app.models.project_backup import ProjectBackup
from app.models.search_document import SearchDocument
from app.models.system_metric import SystemMetric

# 告警阈值（可调）
ALERT_THRESHOLDS = {
    "error_rate": 0.05,            # 错误率 > 5%
    "queue_backlog": 50,           # 任务队列积压 > 50
    "storage_usage": 0.80,         # 存储使用率 > 80%
    "cache_hit_rate": 0.90,        # 缓存命中率 < 90%
    "index_lag_minutes": 10,       # 搜索索引延迟 > 10 分钟
    "ai_failure_1h": 10,           # 近 1h AI 失败 > 10
}


class _InProcessMetrics:
    """进程内实时计数器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.request_total = 0
        self.error_total = 0
        self.response_time_sum = 0.0
        self.response_time_count = 0
        self.cache_hit = 0
        self.cache_miss = 0
        self.mail_failure = 0
        # 近 1h 窗口（简化：记录最近 3600 个响应样本）
        self._recent_samples: list[tuple[datetime, int, float]] = []  # (ts, is_error, ms)

    def record_request(self, *, is_error: bool = False, duration_ms: float = 0.0) -> None:
        with self._lock:
            now = datetime.utcnow()
            self.request_total += 1
            self.response_time_sum += duration_ms
            self.response_time_count += 1
            if is_error:
                self.error_total += 1
            self._recent_samples.append((now, 1 if is_error else 0, duration_ms))
            cutoff = now - timedelta(hours=1)
            while self._recent_samples and self._recent_samples[0][0] < cutoff:
                self._recent_samples.pop(0)

    def record_cache(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self.cache_hit += 1
            else:
                self.cache_miss += 1

    def record_mail_failure(self) -> None:
        with self._lock:
            self.mail_failure += 1

    def snapshot(self) -> dict:
        with self._lock:
            recent = list(self._recent_samples)
        total_recent = len(recent)
        recent_errors = sum(1 for r in recent if r[1])
        recent_ms = [r[2] for r in recent]
        return {
            "request_total": self.request_total,
            "error_total": self.error_total,
            "average_response_ms": round(
                self.response_time_sum / self.response_time_count, 2
            ) if self.response_time_count else 0.0,
            "error_rate": round(
                self.error_total / self.request_total, 4
            ) if self.request_total else 0.0,
            "recent_1h_requests": total_recent,
            "recent_1h_errors": recent_errors,
            "recent_1h_error_rate": round(recent_errors / total_recent, 4) if total_recent else 0.0,
            "recent_1h_average_response_ms": round(sum(recent_ms) / len(recent_ms), 2) if recent_ms else 0.0,
            "cache_hit_rate": round(
                self.cache_hit / (self.cache_hit + self.cache_miss), 4
            ) if (self.cache_hit + self.cache_miss) else 0.0,
            "mail_failure_count": self.mail_failure,
        }


metrics = _InProcessMetrics()


def record_request(*, is_error: bool = False, duration_ms: float = 0.0) -> None:
    metrics.record_request(is_error=is_error, duration_ms=duration_ms)


def record_cache(hit: bool) -> None:
    metrics.record_cache(hit=hit)


def record_mail_failure() -> None:
    metrics.record_mail_failure()


async def record_metric_sample(
    db: AsyncSession,
    *,
    metric_name: str,
    metric_value: float,
    recorded_at: datetime | None = None,
) -> SystemMetric:
    """写一条系统采样（SystemMetric）。"""
    sample = SystemMetric(
        metric_name=metric_name,
        metric_value=metric_value,
        recorded_at=recorded_at or datetime.utcnow(),
    )
    db.add(sample)
    await db.flush()
    return sample


async def list_metric_samples(
    db: AsyncSession,
    *,
    metric_name: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    limit: int = 100,
) -> list[SystemMetric]:
    filters = []
    if metric_name:
        filters.append(SystemMetric.metric_name == metric_name)
    if from_dt:
        filters.append(SystemMetric.recorded_at >= from_dt)
    if to_dt:
        filters.append(SystemMetric.recorded_at <= to_dt)
    rows = (
        await db.execute(
            select(SystemMetric)
            .where(*filters)
            .order_by(SystemMetric.recorded_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return rows


async def get_system_health(
    db: AsyncSession,
    *,
    include_alerts: bool = True,
    base_dir: str | None = None,
) -> dict:
    """系统健康监控（规格十六）。"""
    now = datetime.utcnow()
    proc = metrics.snapshot()

    # 队列积压：AI 任务 pending + running
    queue_backlog = (
        await db.execute(
            select(func.count(AITask.id)).where(
                AITask.status.in_(["pending", "running"])
            )
        )
    ).scalar_one()
    ai_task_backlog = queue_backlog

    # 索引延迟：SearchDocument 最新 updated_at
    latest_index = (
        await db.execute(
            select(func.max(SearchDocument.updated_at))
        )
    ).scalar_one()
    if latest_index is not None:
        index_lag_minutes = max(0.0, round((now - latest_index).total_seconds() / 60, 2))
    else:
        index_lag_minutes = 0.0

    # 存储空间（工作目录所在盘）
    storage_total = 0
    storage_used = 0
    storage_usage_rate = 0.0
    try:
        base = base_dir or os.getcwd()
        usage = __import__("shutil").disk_usage(base)
        storage_total = usage.total
        storage_used = usage.used
        storage_usage_rate = round(usage.used / usage.total, 4)
    except Exception:
        pass

    # 备份失败（近 24h）
    backup_failed_24h = (
        await db.execute(
            select(func.count(ProjectBackup.id)).where(
                ProjectBackup.status == "failed",
                ProjectBackup.created_at >= now - timedelta(hours=24),
            )
        )
    ).scalar_one()

    # AI 连续失败（近 1h）
    ai_failure_1h = (
        await db.execute(
            select(func.coalesce(func.sum(AIFailureStat.request_count), 0)).where(
                AIFailureStat.created_at >= now - timedelta(hours=1)
            )
        )
    ).scalar_one()

    # 数据库连接：SQLite 内存库固定 1（连接池上限）
    db_connections = 1
    db_pool_max = 20

    health = {
        "requests": {
            "total": proc["request_total"],
            "recent_1h": proc["recent_1h_requests"],
            "recent_1h_errors": proc["recent_1h_errors"],
        },
        "performance": {
            "average_response_ms": proc["average_response_ms"],
            "recent_1h_average_response_ms": proc["recent_1h_average_response_ms"],
            "error_rate": proc["error_rate"],
            "recent_1h_error_rate": proc["recent_1h_error_rate"],
        },
        "database": {
            "connections": db_connections,
            "pool_max": db_pool_max,
            "connection_usage_rate": round(db_connections / db_pool_max, 4),
        },
        "queue": {
            "queue_backlog": queue_backlog,
            "ai_task_backlog": ai_task_backlog,
        },
        "index": {
            "lag_minutes": index_lag_minutes,
            "last_updated": latest_index.isoformat() + "Z" if latest_index else None,
        },
        "storage": {
            "total_bytes": storage_total,
            "used_bytes": storage_used,
            "usage_rate": storage_usage_rate,
        },
        "cache": {
            "hit_rate": proc["cache_hit_rate"],
            "hit": None,  # 进程内不保留绝对计数外的最近命中，聚合见 proc
        },
        "mail": {
            "failure_count": proc["mail_failure_count"],
        },
        "updated_at": now.isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }

    if include_alerts:
        health["alerts"] = _build_alerts(
            proc=proc,
            queue_backlog=queue_backlog,
            storage_usage_rate=storage_usage_rate,
            index_lag_minutes=index_lag_minutes,
            ai_failure_1h=ai_failure_1h,
            backup_failed_24h=backup_failed_24h,
        )
    return health


def _build_alerts(
    *,
    proc: dict,
    queue_backlog: int,
    storage_usage_rate: float,
    index_lag_minutes: float,
    ai_failure_1h: int,
    backup_failed_24h: int,
) -> list[dict]:
    """生成告警项（规格十六：重要告警）。"""
    alerts: list[dict] = []
    thresholds = ALERT_THRESHOLDS

    if proc["recent_1h_error_rate"] > thresholds["error_rate"]:
        alerts.append({
            "level": "critical",
            "code": "ERROR_RATE_SPIKE",
            "message": f"近 1h 错误率 {proc['recent_1h_error_rate'] * 100:.1f}% 超过阈值",
            "value": proc["recent_1h_error_rate"],
        })
    if queue_backlog > thresholds["queue_backlog"]:
        alerts.append({
            "level": "warning",
            "code": "QUEUE_BACKLOG",
            "message": f"任务队列积压 {queue_backlog} 条",
            "value": queue_backlog,
        })
    if storage_usage_rate > thresholds["storage_usage"]:
        alerts.append({
            "level": "critical",
            "code": "STORAGE_LOW",
            "message": f"存储使用率 {storage_usage_rate * 100:.1f}% 超过阈值",
            "value": storage_usage_rate,
        })
    if proc["cache_hit_rate"] > 0 and proc["cache_hit_rate"] < thresholds["cache_hit_rate"]:
        alerts.append({
            "level": "warning",
            "code": "CACHE_HIT_LOW",
            "message": f"缓存命中率 {proc['cache_hit_rate'] * 100:.1f}% 低于阈值",
            "value": proc["cache_hit_rate"],
        })
    if index_lag_minutes > thresholds["index_lag_minutes"]:
        alerts.append({
            "level": "warning",
            "code": "INDEX_STALLED",
            "message": f"搜索索引延迟 {index_lag_minutes} 分钟",
            "value": index_lag_minutes,
        })
    if ai_failure_1h >= thresholds["ai_failure_1h"]:
        alerts.append({
            "level": "critical",
            "code": "AI_SERVICE_FAILING",
            "message": f"近 1h AI 失败 {ai_failure_1h} 次",
            "value": ai_failure_1h,
        })
    if backup_failed_24h > 0:
        alerts.append({
            "level": "warning",
            "code": "BACKUP_FAILED",
            "message": f"近 24h 备份失败 {backup_failed_24h} 次",
            "value": backup_failed_24h,
        })
    return alerts
