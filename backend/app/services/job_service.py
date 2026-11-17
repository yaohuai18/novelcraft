# 异步任务队列服务（第十八批「性能优化、可靠性与安全加固」规格九/十）
# Job 生命周期：pending -> running -> succeeded | failed -> retrying -> dead（死信）。
# 指数退避：第 1 次 10s / 第 2 次 30s / 第 3 次 2min / 第 4 次 10min，超过 max_attempts 进死信。
# 管理员可查看死信任务并重新入队（requeue）。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job

RETRY_BACKOFF_SECONDS = [10, 30, 120, 600]
JOB_STATUSES = {"pending", "running", "succeeded", "failed", "retrying", "cancelled", "dead"}
RETRYABLE_QUEUES = {"default", "ai", "index", "mail", "notification", "import", "export", "backup", "stats"}


def _validate_queue(queue: str) -> str:
    if queue not in RETRYABLE_QUEUES:
        raise ValueError(f"未知队列: {queue}")
    return queue


async def enqueue(
    db: AsyncSession,
    *,
    queue: str,
    task_type: str,
    payload: dict,
    priority: int = 0,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> Job:
    """入队任务。"""
    _validate_queue(queue)
    job = Job(
        queue=queue,
        task_type=task_type,
        payload=json.dumps(payload, ensure_ascii=False),
        priority=priority,
        status="pending",
        max_attempts=max_attempts,
        available_at=available_at or datetime.utcnow(),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def claim_next(
    db: AsyncSession,
    *,
    queue: str,
    worker_name: str = "worker",
) -> Job | None:
    """领取下一个可执行任务（pending/retrying 且 available_at <= now），标记 running。"""
    now = datetime.utcnow()
    job = (
        await db.execute(
            select(Job)
            .where(
                Job.queue == queue,
                Job.status.in_(["pending", "retrying"]),
                Job.available_at <= now,
            )
            .order_by(Job.priority.desc(), Job.available_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.started_at = now
    await db.commit()
    await db.refresh(job)
    return job


async def complete(db: AsyncSession, job: Job) -> Job:
    """标记任务成功。"""
    job.status = "succeeded"
    job.completed_at = datetime.utcnow()
    job.error_message = None
    await db.commit()
    await db.refresh(job)
    return job


async def fail(
    db: AsyncSession,
    job: Job,
    *,
    error_message: str,
    retryable: bool = True,
) -> Job:
    """任务失败：可重试则按指数退避进入 retrying，否则累计尝试，超限进死信。"""
    job.attempts += 1
    job.error_message = error_message
    if retryable and job.attempts < job.max_attempts:
        delay = RETRY_BACKOFF_SECONDS[min(job.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        job.status = "retrying"
        job.available_at = datetime.utcnow() + timedelta(seconds=delay)
    elif job.attempts >= job.max_attempts:
        job.status = "dead"
        job.available_at = datetime.utcnow()
    else:
        job.status = "failed"
        job.available_at = datetime.utcnow()
    job.completed_at = None
    await db.commit()
    await db.refresh(job)
    return job


async def cancel(db: AsyncSession, job: Job) -> Job:
    """取消任务（仅 pending/retrying 允许）。"""
    if job.status not in ("pending", "retrying"):
        raise ValueError(f"任务当前状态不可取消: {job.status}")
    job.status = "cancelled"
    await db.commit()
    await db.refresh(job)
    return job


async def get_by_id(
    db: AsyncSession,
    job_id: str | uuid.UUID,
) -> Job | None:
    """按主键查询任务。"""
    if isinstance(job_id, str):
        try:
            job_id = uuid.UUID(job_id)
        except ValueError:
            return None
    return (
        await db.execute(select(Job).where(Job.id == job_id))
    ).scalar_one_or_none()


async def requeue_dead(db: AsyncSession, job: Job, *, priority: int | None = None) -> Job:
    """管理员重跑死信任务：重置计数并重新入队。"""
    if job.status != "dead":
        raise ValueError("仅死信任务可重新入队")
    job.status = "pending"
    job.attempts = 0
    job.error_message = None
    job.completed_at = None
    job.started_at = None
    job.available_at = datetime.utcnow()
    if priority is not None:
        job.priority = priority
    await db.commit()
    await db.refresh(job)
    return job


async def list_jobs(
    db: AsyncSession,
    *,
    queue: str | None = None,
    status_filter: str | None = None,
    task_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_dead: bool = True,
) -> tuple[list[Job], int]:
    """任务列表（管理端查看，默认含死信）。"""
    filters = []
    if queue:
        filters.append(Job.queue == queue)
    if status_filter:
        if status_filter not in JOB_STATUSES:
            raise ValueError(f"未知任务状态: {status_filter}")
        filters.append(Job.status == status_filter)
    if task_type:
        filters.append(Job.task_type == task_type)
    if not include_dead:
        filters.append(Job.status != "dead")

    total = (
        await db.execute(select(func.count(Job.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Job)
            .where(*filters)
            .order_by(Job.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
