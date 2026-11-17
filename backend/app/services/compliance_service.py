# 地区合规服务（第二十一批「国际化、多语言和多区域部署」规格二十一）
# PolicyVersion：隐私政策/服务条款/Cookie 政策/数据处理说明版本管理（按地区）。
# ConsentRecord：用户同意记录（同意内容版本/时间/地区/IP/撤回时间/政策更新时间）。
# DataRequest：数据导出/账号删除/数据访问申请/数据更正。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance import (
    CONSENT_STATUSES,
    DATA_REQUEST_STATUSES,
    DATA_REQUEST_TYPES,
    POLICY_TYPES,
    ConsentRecord,
    DataRequest,
    PolicyVersion,
)


async def publish_policy(
    db: AsyncSession,
    *,
    policy_type: str,
    region: str,
    title: str,
    content: str,
    effective_at: datetime | None = None,
) -> PolicyVersion:
    """发布新版本政策（版本号自增，历史版本不可修改）。"""
    if policy_type not in POLICY_TYPES:
        raise ValueError(f"非法政策类型: {policy_type}")
    latest = (
        await db.execute(
            select(PolicyVersion)
            .where(
                PolicyVersion.policy_type == policy_type,
                PolicyVersion.region == region,
            )
            .order_by(PolicyVersion.version.desc())
        )
    ).scalars().first()
    version = (latest.version + 1) if latest else 1
    policy = PolicyVersion(
        policy_type=policy_type,
        region=region,
        version=version,
        title=title,
        content=content,
        effective_at=effective_at or datetime.utcnow(),
        published_at=datetime.utcnow(),
        status="published",
    )
    db.add(policy)
    await db.flush()
    return policy


async def get_policy(
    db: AsyncSession,
    *,
    policy_type: str,
    region: str = "CN",
    version: int | None = None,
) -> PolicyVersion | None:
    stmt = select(PolicyVersion).where(
        PolicyVersion.policy_type == policy_type,
        PolicyVersion.region == region,
    )
    if version is not None:
        stmt = stmt.where(PolicyVersion.version == version)
    else:
        stmt = stmt.order_by(PolicyVersion.version.desc())
    result = await db.execute(stmt)
    return result.scalars().first()


async def list_policies(
    db: AsyncSession,
    *,
    policy_type: str | None = None,
    region: str | None = None,
) -> list[PolicyVersion]:
    stmt = select(PolicyVersion).order_by(
        PolicyVersion.policy_type.asc(),
        PolicyVersion.region.asc(),
        PolicyVersion.version.desc(),
    )
    if policy_type is not None:
        stmt = stmt.where(PolicyVersion.policy_type == policy_type)
    if region is not None:
        stmt = stmt.where(PolicyVersion.region == region)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def record_consent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    policy_type: str,
    region: str = "CN",
    ip_hash: str | None = None,
    policy_version: int | None = None,
) -> ConsentRecord:
    """记录用户明确同意（同意内容版本/时间/地区/IP）。"""
    policy = await get_policy(
        db,
        policy_type=policy_type,
        region=region,
        version=policy_version,
    )
    if policy is None:
        raise ValueError("政策版本不存在")
    # 撤回旧的 active 同意
    old = (
        await db.execute(
            select(ConsentRecord).where(
                ConsentRecord.user_id == user_id,
                ConsentRecord.policy_type == policy_type,
                ConsentRecord.status == "active",
            )
        )
    ).scalars().all()
    for rec in old:
        rec.status = "withdrawn"
        rec.withdrawn_at = datetime.utcnow()

    consent = ConsentRecord(
        user_id=user_id,
        policy_id=policy.id,
        policy_type=policy_type,
        policy_version=policy.version,
        granted_at=datetime.utcnow(),
        region=region,
        ip_hash=ip_hash,
        status="active",
        policy_updated_at=policy.published_at,
    )
    db.add(consent)
    await db.flush()
    return consent


async def withdraw_consent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    policy_type: str,
) -> ConsentRecord | None:
    """撤回同意（记录撤回时间）。"""
    rec = (
        await db.execute(
            select(ConsentRecord).where(
                ConsentRecord.user_id == user_id,
                ConsentRecord.policy_type == policy_type,
                ConsentRecord.status == "active",
            ).order_by(ConsentRecord.granted_at.desc())
        )
    ).scalars().first()
    if rec is None:
        return None
    rec.status = "withdrawn"
    rec.withdrawn_at = datetime.utcnow()
    await db.flush()
    return rec


async def get_consent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    policy_type: str,
) -> ConsentRecord | None:
    return (
        await db.execute(
            select(ConsentRecord).where(
                ConsentRecord.user_id == user_id,
                ConsentRecord.policy_type == policy_type,
                ConsentRecord.status == "active",
            ).order_by(ConsentRecord.granted_at.desc())
        )
    ).scalars().first()


async def list_consents(db: AsyncSession, user_id: uuid.UUID) -> list[ConsentRecord]:
    result = await db.execute(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == user_id)
        .order_by(ConsentRecord.granted_at.desc())
    )
    return list(result.scalars().all())


async def create_data_request(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    request_type: str,
    region: str,
) -> DataRequest:
    """数据导出/账号删除/数据访问申请（数据更正）。"""
    if request_type not in DATA_REQUEST_TYPES:
        raise ValueError(f"非法申请类型: {request_type}")
    req = DataRequest(
        user_id=user_id,
        request_type=request_type,
        status="pending",
        region=region,
        requested_at=datetime.utcnow(),
    )
    db.add(req)
    await db.flush()
    return req


async def update_data_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    status: str,
    result: str | None = None,
) -> DataRequest | None:
    if status not in DATA_REQUEST_STATUSES:
        raise ValueError(f"非法申请状态: {status}")
    req = (
        await db.execute(
            select(DataRequest).where(DataRequest.id == request_id)
        )
    ).scalar_one_or_none()
    if req is None:
        return None
    req.status = status
    if result is not None:
        req.result = result
    if status == "completed":
        req.completed_at = datetime.utcnow()
    await db.flush()
    return req


async def list_data_requests(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    request_type: str | None = None,
) -> list[DataRequest]:
    stmt = select(DataRequest).order_by(DataRequest.requested_at.desc())
    if user_id is not None:
        stmt = stmt.where(DataRequest.user_id == user_id)
    if request_type is not None:
        stmt = stmt.where(DataRequest.request_type == request_type)
    result = await db.execute(stmt)
    return list(result.scalars().all())
