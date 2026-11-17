# 阶段J「审计补齐 + 备份调度」：备份自动调度策略（补充 10：数据备份）。
#
# 设计要点：
#   - 复用既有 backup_service（automatic 备份类型 + 保留裁剪 + TTL），不新造备份通道；
#   - 策略可关：settings.backup_auto_daily_enabled=False 时调度器一次也不创建备份；
#   - 磁盘占用可控：①每个作品每日最多 daily_limit 个；②按类型保留 retention_limit 个；
#     ③过期（expires_at < now）自动备份清理；④单轮调度最多处理 N 个作品；
#   - 幂等：同一时间窗内重复调用不会重复备份（按「上次自动备份时间 + 间隔」判定）；
#   - 只读评估与执行分离：evaluate_* 只读不写库，run_* 才创建备份，便于接口预览与测试。
#
# 回滚方式：把 backup_auto_daily_enabled 置 False 即完全停止调度（已生成的备份可手动删除）；
# 本模块只新增 automatic 备份记录，不改动任何正文/大纲数据。
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.project import Project
from app.models.project_backup import ProjectBackup
from app.services import backup_service

logger = logging.getLogger(__name__)

AUTO_BACKUP_TYPE = "automatic"
AUTO_BACKUP_NOTE_PREFIX = "每日自动备份"
SCHEDULER_SOURCE = "auto_schedule"

DEFAULT_CHECK_INTERVAL_SECONDS = 600
MIN_CHECK_INTERVAL_SECONDS = 60

# 进程内调度状态（仅用于避免重叠执行与失败退避，不落库）
_scheduler_task: asyncio.Task | None = None
_scheduler_stop = False
_run_lock: asyncio.Lock | None = None
_scheduler_state: dict = {
    "enabled": False,
    "started": False,
    "last_check_at": None,
    "last_run_at": None,
    "last_result": None,
    "consecutive_failures": 0,
    "next_check_in_seconds": None,
}


# ============ 配置（策略可关 + 磁盘占用可控） ============

def auto_policy_config() -> dict:
    """返回当前自动备份策略（只读，不查库）。"""
    limit = int(getattr(settings, "backup_auto_retention_limit", 7) or 7)
    limit = max(1, min(limit, backup_service.MAX_BACKUPS_PER_PROJECT))
    ttl = int(
        getattr(settings, "backup_auto_retention_ttl_days", backup_service.BACKUP_TTL_DAYS)
        or backup_service.BACKUP_TTL_DAYS
    )
    ttl = max(1, min(ttl, backup_service.BACKUP_TTL_DAYS))
    interval = int(getattr(settings, "backup_auto_interval_hours", 24) or 24)
    return {
        "enabled": bool(getattr(settings, "backup_auto_daily_enabled", True)),
        "hour": int(getattr(settings, "backup_auto_hour", 3) or 0),
        "minute": int(getattr(settings, "backup_auto_minute", 0) or 0),
        "interval_hours": max(1, interval),
        "daily_limit": max(1, int(getattr(settings, "backup_auto_daily_limit", 1) or 1)),
        "retention_limit": limit,
        "retention_ttl_days": ttl,
        "max_projects_per_run": max(
            1, int(getattr(settings, "backup_auto_max_projects_per_run", 3) or 3)
        ),
        "check_interval_seconds": max(
            MIN_CHECK_INTERVAL_SECONDS,
            int(getattr(settings, "backup_auto_check_interval_seconds", 600) or 600),
        ),
        "backup_type": AUTO_BACKUP_TYPE,
    }


def is_enabled() -> bool:
    return auto_policy_config()["enabled"]


# ============ 到期判定（只读） ============

def next_due_at(last_backup_at: datetime | None, now: datetime | None = None) -> datetime:
    """下一次应执行自动备份的时刻：上次自动备份 + 间隔；从未备份过则为「立即」。"""
    cfg = auto_policy_config()
    now = now or datetime.utcnow()
    if last_backup_at is None:
        return now
    return last_backup_at + timedelta(hours=cfg["interval_hours"])


def is_due(last_backup_at: datetime | None, now: datetime | None = None) -> bool:
    """是否到期：距上次自动备份已超过最小间隔（同一时间窗重复调用返回 False）。"""
    now = now or datetime.utcnow()
    return now >= next_due_at(last_backup_at, now)


async def _last_auto_backup_at(db: AsyncSession, project_id: uuid.UUID) -> datetime | None:
    return (
        await db.execute(
            select(func.max(ProjectBackup.created_at)).where(
                ProjectBackup.project_id == project_id,
                ProjectBackup.backup_type == AUTO_BACKUP_TYPE,
            )
        )
    ).scalar_one_or_none()


async def _auto_backup_stats(db: AsyncSession, project_id: uuid.UUID, now: datetime) -> dict:
    """自动备份数量 / 24 小时内数量 / 已过期数量（只读）。"""
    rows = (
        await db.execute(
            select(
                ProjectBackup.created_at,
                ProjectBackup.expires_at,
            ).where(
                ProjectBackup.project_id == project_id,
                ProjectBackup.backup_type == AUTO_BACKUP_TYPE,
            )
        )
    ).all()
    total = len(rows)
    recent = 0
    expired = 0
    for created_at, expires_at in rows:
        if created_at is not None and created_at >= now - timedelta(hours=24):
            recent += 1
        if expires_at is not None and expires_at < now:
            expired += 1
    return {"total": total, "last_24h": recent, "expired": expired}


async def evaluate_project(
    db: AsyncSession,
    project: Project,
    *,
    now: datetime | None = None,
) -> dict:
    """只读评估单个作品是否需要自动备份（不写库，供接口预览与测试）。"""
    cfg = auto_policy_config()
    now = now or datetime.utcnow()
    last_at = await _last_auto_backup_at(db, project.id)
    stats = await _auto_backup_stats(db, project.id, now)

    reasons: list[str] = []
    if not cfg["enabled"]:
        reasons.append("自动备份调度已关闭")
    if not is_due(last_at, now):
        reasons.append("距上次自动备份未超过最小间隔")
    if stats["last_24h"] >= cfg["daily_limit"]:
        reasons.append("已达每日自动备份上限")

    should_backup = not reasons
    return {
        "project_id": str(project.id),
        "project_title": getattr(project, "title", None),
        "last_auto_backup_at": last_at.isoformat() if last_at else None,
        "next_due_at": next_due_at(last_at, now).isoformat(),
        "auto_backup_count": stats["total"],
        "auto_backup_last_24h": stats["last_24h"],
        "expired_count": stats["expired"],
        "over_retention_limit": stats["total"] > cfg["retention_limit"],
        "should_backup": should_backup,
        "skip_reasons": reasons,
    }


async def evaluate_policy(
    db: AsyncSession,
    *,
    project_ids: list[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> dict:
    """只读评估自动备份策略（含策略参数与每个作品的到期情况）。"""
    now = now or datetime.utcnow()
    stmt = select(Project)
    if project_ids:
        stmt = stmt.where(Project.id.in_(project_ids))
    projects = (await db.execute(stmt.order_by(Project.created_at.asc()))).scalars().all()
    items = [await evaluate_project(db, p, now=now) for p in projects]
    return {
        "policy": auto_policy_config(),
        "evaluated_at": now.isoformat(),
        "project_count": len(items),
        "due_count": sum(1 for i in items if i["should_backup"]),
        "projects": items,
        "scheduler": dict(_scheduler_state),
    }


# ============ 执行（创建 automatic 备份 + 保留裁剪 + 过期清理） ============

async def purge_expired_backups(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict:
    """清理过期自动备份 + 按保留上限裁剪（磁盘占用可控）。"""
    cfg = auto_policy_config()
    now = now or datetime.utcnow()
    count_stmt = select(func.count(ProjectBackup.id)).where(
        ProjectBackup.backup_type == AUTO_BACKUP_TYPE,
        ProjectBackup.expires_at.isnot(None),
        ProjectBackup.expires_at < now,
    )
    if project_id is not None:
        count_stmt = count_stmt.where(ProjectBackup.project_id == project_id)
    expired = (await db.execute(count_stmt)).scalar_one() or 0

    del_stmt = delete(ProjectBackup).where(
        ProjectBackup.backup_type == AUTO_BACKUP_TYPE,
        ProjectBackup.expires_at.isnot(None),
        ProjectBackup.expires_at < now,
    )
    if project_id is not None:
        del_stmt = del_stmt.where(ProjectBackup.project_id == project_id)
    if expired:
        await db.execute(del_stmt)

    # 按类型裁剪到保留上限（复用 backup_service 的裁剪实现）
    trimmed = 0
    project_ids = [project_id] if project_id is not None else [
        row for row in (
            await db.execute(
                select(ProjectBackup.project_id)
                .where(ProjectBackup.backup_type == AUTO_BACKUP_TYPE)
                .distinct()
            )
        ).scalars().all()
    ]
    for pid in project_ids:
        trimmed += await backup_service._trim_by_type(
            db, pid, AUTO_BACKUP_TYPE, cfg["retention_limit"]
        )
    return {"expired_deleted": expired, "over_limit_deleted": trimmed}


async def run_auto_backup(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    force: bool = False,
    now: datetime | None = None,
    actor_id: uuid.UUID | None = None,
    max_projects: int | None = None,
    commit: bool = True,
) -> dict:
    """执行自动备份（幂等：同一时间窗内重复调用不会重复创建）。

    force=True 时忽略「是否到期」判定（仍受每日上限与保留上限约束），
    仅用于接口显式触发；调度器一律 force=False。
    """
    cfg = auto_policy_config()
    now = now or datetime.utcnow()
    if not cfg["enabled"] and not force:
        return {
            "status": "disabled",
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "policy": cfg,
            "results": [],
            "message": "自动备份调度已关闭",
        }

    stmt = select(Project)
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    projects = (await db.execute(stmt.order_by(Project.created_at.asc()))).scalars().all()
    budget = max_projects if max_projects is not None else cfg["max_projects_per_run"]
    budget = max(1, int(budget))

    results: list[dict] = []
    created = skipped = failed = 0
    for project in projects:
        if created >= budget:
            results.append({
                "project_id": str(project.id),
                "status": "deferred",
                "reason": "单轮处理上限（backup_auto_max_projects_per_run）",
            })
            skipped += 1
            continue

        evaluation = await evaluate_project(db, project, now=now)
        if not force and not evaluation["should_backup"]:
            results.append({
                "project_id": str(project.id),
                "status": "not_due",
                "reason": "；".join(evaluation["skip_reasons"]) or "未到期",
            })
            skipped += 1
            continue
        if force and evaluation["auto_backup_last_24h"] >= cfg["daily_limit"]:
            results.append({
                "project_id": str(project.id),
                "status": "not_due",
                "reason": "已达每日自动备份上限",
            })
            skipped += 1
            continue

        note = f"{AUTO_BACKUP_NOTE_PREFIX} {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        try:
            backup = await backup_service.create_backup(
                db,
                project,
                AUTO_BACKUP_TYPE,
                user_id=actor_id or project.user_id,
                note=note,
            )
            cleanup = await purge_expired_backups(db, project_id=project.id, now=now)
            created += 1
            results.append({
                "project_id": str(project.id),
                "status": "created",
                "backup_id": str(backup.id),
                "size": backup.size,
                "expires_at": backup.expires_at.isoformat() if backup.expires_at else None,
                "cleanup": cleanup,
            })
        except Exception as exc:  # noqa: BLE001 — 单个作品失败不影响其余作品
            logger.warning("auto backup failed for project %s: %s", project.id, exc)
            failed += 1
            results.append({
                "project_id": str(project.id),
                "status": "failed",
                "error": str(exc),
            })

    if commit:
        await db.commit()

    status = "ok" if failed == 0 else ("partial" if created else "failed")
    return {
        "status": status,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "policy": cfg,
        "results": results,
        "ran_at": now.isoformat(),
    }


# ============ 后台调度器 ============

def _get_lock() -> asyncio.Lock:
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    return _run_lock


def seconds_until_daily_window(now: datetime | None = None) -> float:
    """距离下一个配置时刻（hour:minute）的秒数。"""
    cfg = auto_policy_config()
    now = now or datetime.utcnow()
    target = now.replace(
        hour=cfg["hour"], minute=cfg["minute"], second=0, microsecond=0
    )
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def run_scheduled_backups(now: datetime | None = None) -> dict:
    """调度入口：自查库连接执行一轮自动备份（幂等，可重复调用）。"""
    from app.core.database import AsyncSessionLocal

    cfg = auto_policy_config()
    if not cfg["enabled"]:
        return {"status": "disabled", "created": 0, "skipped": 0, "failed": 0}

    lock = _get_lock()
    if lock.locked():
        return {"status": "busy", "created": 0, "skipped": 0, "failed": 0}

    async with lock:
        try:
            async with AsyncSessionLocal() as db:
                result = await run_auto_backup(
                    db,
                    force=False,
                    now=now or datetime.utcnow(),
                    max_projects=cfg["max_projects_per_run"],
                )
            _scheduler_state["last_run_at"] = datetime.utcnow().isoformat()
            _scheduler_state["last_result"] = {
                k: result.get(k) for k in ("status", "created", "skipped", "failed")
            }
            _scheduler_state["consecutive_failures"] = (
                0 if result.get("failed", 0) == 0 else _scheduler_state["consecutive_failures"] + 1
            )
            return result
        except Exception as exc:  # noqa: BLE001 — 调度失败不影响主服务
            logger.warning("backup scheduler run failed: %s", exc)
            _scheduler_state["consecutive_failures"] += 1
            _scheduler_state["last_result"] = {"status": "exception", "error": str(exc)}
            return {"status": "exception", "error": str(exc), "created": 0, "skipped": 0, "failed": 1}


async def scheduler_loop(check_interval_seconds: int | None = None) -> None:
    """后台调度循环：周期检查是否到期。

    首次等待到配置时刻（默认 03:00），此后每 check_interval 检查一次；
    服务在配置时刻未运行时会由「到期判定」自动补偿（catch-up）。
    """
    cfg = auto_policy_config()
    interval = max(
        MIN_CHECK_INTERVAL_SECONDS,
        int(check_interval_seconds or cfg["check_interval_seconds"]),
    )
    _scheduler_state["enabled"] = True
    _scheduler_state["started"] = True

    first_wait = min(seconds_until_daily_window(), interval)
    try:
        while not _scheduler_stop:
            _scheduler_state["next_check_in_seconds"] = first_wait
            await asyncio.sleep(first_wait)
            first_wait = interval
            if _scheduler_stop:
                break
            _scheduler_state["last_check_at"] = datetime.utcnow().isoformat()
            failures = _scheduler_state["consecutive_failures"]
            if failures >= 3:
                # 连续失败退避：最多等待 interval * 2^min(failures,4)
                backoff = min(interval * (2 ** min(failures, 4)), 3600)
                _scheduler_state["next_check_in_seconds"] = backoff
                await asyncio.sleep(backoff)
                first_wait = 0
                continue
            result = await run_scheduled_backups()
            _scheduler_state["last_check_at"] = datetime.utcnow().isoformat()
            _scheduler_state["last_run_at"] = _scheduler_state["last_run_at"] or datetime.utcnow().isoformat()
            if isinstance(result, dict) and result.get("status") == "disabled":
                break
    except asyncio.CancelledError:  # 服务关闭
        raise
    finally:
        _scheduler_state["started"] = False
        _scheduler_state["next_check_in_seconds"] = None


def start_scheduler(check_interval_seconds: int | None = None) -> dict:
    """启动后台调度（幂等；策略关闭时不启动）。由 main.py 在 startup 调用。"""
    global _scheduler_task, _scheduler_stop
    if not is_enabled():
        _scheduler_state["enabled"] = False
        return {"started": False, "reason": "自动备份调度已关闭"}
    if _scheduler_task is not None and not _scheduler_task.done():
        return {"started": True, "reason": "调度器已在运行"}
    _scheduler_stop = False
    _scheduler_state["enabled"] = True
    _scheduler_task = asyncio.create_task(scheduler_loop(check_interval_seconds))
    return {"started": True, "reason": "已启动", "interval_seconds": auto_policy_config()["check_interval_seconds"]}


async def stop_scheduler() -> dict:
    """停止后台调度（服务关闭时调用）。"""
    global _scheduler_task, _scheduler_stop
    _scheduler_stop = True
    task, _scheduler_task = _scheduler_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _scheduler_state["started"] = False
    return {"stopped": True}


def scheduler_state() -> dict:
    return dict(_scheduler_state)
