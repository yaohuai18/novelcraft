# 幂等处理服务（第十八批「性能优化、可靠性与安全加固」规格十三）
# Idempotency-Key 支持：创建备份 / 提交AI任务 / 发布章节 / 发送邀请 / 导入项目。
# 相同 key + endpoint 的重复请求直接返回首次结果（response_status / response_body）。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.idempotency_record import IdempotencyRecord

DEFAULT_TTL_HOURS = 24


async def find_replay(
    db: AsyncSession,
    *,
    idempotency_key: str,
    endpoint: str,
) -> IdempotencyRecord | None:
    """查找幂等记录：命中且未过期则返回（重复请求重放首次结果）。"""
    row = (
        await db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == idempotency_key,
                IdempotencyRecord.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.utcnow():
        return None
    return row


async def save_result(
    db: AsyncSession,
    *,
    idempotency_key: str,
    endpoint: str,
    user_id: uuid.UUID | None,
    response_status: int,
    response_body: dict,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> IdempotencyRecord:
    """保存首次处理结果（业务事务内调用，重复请求可直接返回）。"""
    row = (
        await db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == idempotency_key,
                IdempotencyRecord.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = IdempotencyRecord(
        idempotency_key=idempotency_key,
        endpoint=endpoint,
        user_id=user_id,
        response_status=response_status,
        response_body=json.dumps(response_body, ensure_ascii=False),
        expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(row)
    await db.flush()
    return row


async def list_records(
    db: AsyncSession,
    *,
    endpoint: str | None = None,
    user_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[IdempotencyRecord], int]:
    """幂等记录查询（管理端）。"""
    from sqlalchemy import func

    filters = []
    if endpoint:
        filters.append(IdempotencyRecord.endpoint == endpoint)
    if user_id is not None:
        filters.append(IdempotencyRecord.user_id == user_id)
    total = (
        await db.execute(select(func.count(IdempotencyRecord.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(IdempotencyRecord)
            .where(*filters)
            .order_by(IdempotencyRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
