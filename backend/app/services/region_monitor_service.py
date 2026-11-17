# 区域化日志监控与容灾服务（第二十一批「国际化、多语言和多区域部署」规格二十三/二十四）
# RegionMetric：按区域记录请求量/响应时间/错误率/缓存命中率/AI 可用率。
# RegionStatus：区域状态检测与故障切换；涉及私人数据时遵守数据驻留规则
#   （payment_isolated / private_restricted 数据不允许任意切换到其它区域）。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.region_monitor import (
    REGION_HEALTH_STATUSES,
    RegionMetric,
    RegionStatus,
)
from app.services.region_service import (
    check_cross_region_allowed,
    get_region,
    update_region_status,
)

# 故障判定阈值
ERROR_RATE_THRESHOLD = 0.05   # 5% 错误率 → degraded
ERROR_RATE_FAILED = 0.25      # 25% 错误率 → failed
AVG_RESPONSE_MS_DEGRADED = 1000
AVG_RESPONSE_MS_FAILED = 3000

# 需要遵守数据驻留的数据类型（故障切换时检查）
RESIDENCE_SENSITIVE_TYPES = ["payment", "private_content", "audit"]


async def record_metric(
    db: AsyncSession,
    *,
    region_code: str,
    request_count: int = 0,
    error_count: int = 0,
    total_response_ms: int = 0,
    cache_hit_count: int = 0,
    cache_miss_count: int = 0,
    ai_available: bool = True,
    metric_at: datetime | None = None,
) -> RegionMetric:
    """记录区域指标窗口；自动计算错误率。"""
    total = request_count + cache_hit_count + cache_miss_count
    error_rate = round(error_count / request_count, 4) if request_count else 0.0
    metric = RegionMetric(
        region_code=region_code,
        metric_at=metric_at or datetime.utcnow(),
        request_count=request_count,
        error_count=error_count,
        total_response_ms=total_response_ms,
        error_rate=error_rate,
        cache_hit_count=cache_hit_count,
        cache_miss_count=cache_miss_count,
        ai_available=ai_available,
    )
    db.add(metric)
    await db.flush()
    return metric


async def list_metrics(
    db: AsyncSession,
    *,
    region_code: str | None = None,
    limit: int = 100,
) -> list[RegionMetric]:
    stmt = select(RegionMetric).order_by(RegionMetric.metric_at.desc())
    if region_code is not None:
        stmt = stmt.where(RegionMetric.region_code == region_code)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


def evaluate_region_health(
    *,
    request_count: int,
    error_count: int,
    total_response_ms: int,
    ai_available: bool,
) -> tuple[str, str]:
    """区域健康评估：healthy / degraded / failed。"""
    error_rate = error_count / request_count if request_count else 0.0
    avg_ms = total_response_ms / request_count if request_count else 0
    if error_rate >= ERROR_RATE_FAILED or avg_ms >= AVG_RESPONSE_MS_FAILED or not ai_available:
        return "failed", f"错误率 {error_rate:.2%}，平均响应 {avg_ms:.0f}ms，AI 可用={ai_available}"
    if error_rate >= ERROR_RATE_THRESHOLD or avg_ms >= AVG_RESPONSE_MS_DEGRADED:
        return "degraded", f"错误率 {error_rate:.2%}，平均响应 {avg_ms:.0f}ms"
    return "healthy", "指标正常"


async def get_region_status(db: AsyncSession, region_code: str) -> RegionStatus | None:
    return (
        await db.execute(
            select(RegionStatus).where(RegionStatus.region_code == region_code)
        )
    ).scalar_one_or_none()


async def set_region_status(
    db: AsyncSession,
    *,
    region_code: str,
    status: str,
    reason: str | None = None,
    failover_to: str | None = None,
    data_resident_blocked: bool = False,
) -> RegionStatus:
    if status not in REGION_HEALTH_STATUSES:
        raise ValueError(f"非法区域健康状态: {status}")
    st = await get_region_status(db, region_code)
    if st is None:
        st = RegionStatus(
            region_code=region_code,
            status=status,
            detected_at=datetime.utcnow(),
            reason=reason,
            failover_to=failover_to,
            data_resident_blocked=data_resident_blocked,
        )
        db.add(st)
        await db.flush()
        return st
    st.status = status
    st.reason = reason
    st.failover_to = failover_to
    st.data_resident_blocked = data_resident_blocked
    st.detected_at = datetime.utcnow()
    st.updated_at = datetime.utcnow()
    await db.flush()
    return st


async def detect_and_update_status(
    db: AsyncSession,
    *,
    region_code: str,
    request_count: int,
    error_count: int,
    total_response_ms: int,
    ai_available: bool = True,
) -> RegionStatus:
    """检测区域状态并更新 RegionStatus（自动容灾检测）。"""
    status, reason = evaluate_region_health(
        request_count=request_count,
        error_count=error_count,
        total_response_ms=total_response_ms,
        ai_available=ai_available,
    )
    return await set_region_status(
        db,
        region_code=region_code,
        status=status,
        reason=reason,
    )


async def failover_region(
    db: AsyncSession,
    *,
    region_code: str,
    target_region: str,
    operator_id: uuid.UUID | None = None,
    sensitive_data_types: list[str] | None = None,
) -> dict:
    """故障切换：检测故障区域 → 判断影响范围 → 停止写入 → 切换流量。
    涉及私人数据时检查数据驻留规则：支付/私人正文/审计不允许随意跨区切换。
    返回 {switched, reason, blocked?}。
    """
    source = await get_region(db, region_code)
    target = await get_region(db, target_region)
    if source is None:
        raise ValueError(f"源区域不存在: {region_code}")
    if target is None:
        raise ValueError(f"目标区域不存在: {target_region}")
    if target.status == "failed":
        raise ValueError(f"目标区域不可用: {target_region}")

    blocked_types = []
    if sensitive_data_types is None:
        sensitive_data_types = list(RESIDENCE_SENSITIVE_TYPES)
    for data_type in sensitive_data_types:
        check = check_cross_region_allowed(
            data_type=data_type,
            source_region=region_code,
            target_region=target_region,
        )
        if not check["allowed"]:
            blocked_types.append(data_type)

    if blocked_types:
        await set_region_status(
            db,
            region_code=region_code,
            status="failed",
            reason=f"故障切换被数据驻留规则阻止（{','.join(blocked_types)}）",
            failover_to=target_region,
            data_resident_blocked=True,
        )
        return {
            "switched": False,
            "reason": "数据驻留规则禁止跨区切换",
            "blocked_types": blocked_types,
            "failover_to": target_region,
        }

    await update_region_status(db, region_code=region_code, status="failed")
    await update_region_status(db, region_code=target_region, status="active")
    await set_region_status(
        db,
        region_code=region_code,
        status="failed",
        reason="区域故障，已切换流量",
        failover_to=target_region,
        data_resident_blocked=False,
    )
    await set_region_status(
        db,
        region_code=target_region,
        status="healthy",
        reason="接收故障区域流量",
        failover_to=None,
        data_resident_blocked=False,
    )
    return {
        "switched": True,
        "reason": "ok",
        "blocked_types": [],
        "failover_to": target_region,
    }
