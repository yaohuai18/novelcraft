# 多区域与数据驻留服务（第二十一批「国际化、多语言和多区域部署」规格十三/十四/十五/十六）
# DataRegion：region_code / storage_endpoint / database_cluster / ai_provider / status。
# 跨区域数据策略分级：公开内容可全球缓存 / 普通配置可跨区同步 /
#   私人正文限制跨区复制 / 支付数据按地区隔离 / 审计日志按合规保存。
# 区域化访问：用户请求 → 就近接入区域 → 区域应用服务 → 主数据服务。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_region import REGION_STATUSES, DataRegion, RegionAccessLog
from app.models.project import Project
from app.models.user_locale import UserLocale

# 数据策略分级
DATA_POLICY_LEVELS = [
    "public_cacheable",      # 公开内容：可全球缓存
    "config_syncable",       # 普通配置：可跨区同步
    "private_restricted",    # 私人正文：限制跨区复制
    "payment_isolated",      # 支付数据：按地区隔离
    "audit_compliant",       # 审计日志：按合规要求保存
    "backup_pinned",         # 备份：使用指定区域存储
]


async def create_region(
    db: AsyncSession,
    *,
    region_code: str,
    name: str,
    storage_endpoint: str,
    database_cluster: str,
    ai_provider: str,
    status: str = "active",
    is_main: bool = False,
) -> DataRegion:
    if status not in REGION_STATUSES:
        raise ValueError(f"非法区域状态: {status}")
    existing = (
        await db.execute(
            select(DataRegion).where(DataRegion.region_code == region_code)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"区域已存在: {region_code}")
    region = DataRegion(
        region_code=region_code,
        name=name,
        storage_endpoint=storage_endpoint,
        database_cluster=database_cluster,
        ai_provider=ai_provider,
        status=status,
        is_main=is_main,
    )
    db.add(region)
    await db.flush()
    return region


async def list_regions(db: AsyncSession) -> list[DataRegion]:
    result = await db.execute(
        select(DataRegion).order_by(DataRegion.is_main.desc(), DataRegion.region_code.asc())
    )
    return list(result.scalars().all())


async def get_region(db: AsyncSession, region_code: str) -> DataRegion | None:
    return (
        await db.execute(
            select(DataRegion).where(DataRegion.region_code == region_code)
        )
    ).scalar_one_or_none()


async def get_main_region(db: AsyncSession) -> DataRegion | None:
    return (
        await db.execute(
            select(DataRegion).where(DataRegion.is_main.is_(True), DataRegion.status == "active")
        )
    ).scalars().first()


async def update_region_status(
    db: AsyncSession,
    *,
    region_code: str,
    status: str,
    operator_id: uuid.UUID | None = None,
) -> DataRegion:
    region = await get_region(db, region_code)
    if region is None:
        raise ValueError(f"区域不存在: {region_code}")
    if status not in REGION_STATUSES:
        raise ValueError(f"非法区域状态: {status}")
    region.status = status
    region.updated_at = datetime.utcnow()
    await db.flush()
    return region


# ==================== 数据策略 ====================

DATA_POLICY_RULES = {
    "public": "public_cacheable",
    "config": "config_syncable",
    "private_content": "private_restricted",
    "payment": "payment_isolated",
    "audit": "audit_compliant",
    "backup": "backup_pinned",
}

DATA_TYPES = list(DATA_POLICY_RULES.keys())


def data_policy_level(data_type: str) -> str:
    """数据类型的策略等级。"""
    return DATA_POLICY_RULES.get(data_type, "config_syncable")


def check_cross_region_allowed(
    *,
    data_type: str,
    source_region: str,
    target_region: str,
) -> dict:
    """跨区域传输检查：按策略等级判断是否允许从 source → target 传输。"""
    level = data_policy_level(data_type)
    allowed = True
    reason = "ok"
    if level == "payment_isolated" and source_region != target_region:
        allowed = False
        reason = "支付数据按地区隔离，禁止跨区域传输"
    elif level == "private_restricted" and source_region != target_region:
        allowed = False
        reason = "私人正文限制跨区复制"
    elif level == "backup_pinned":
        # 备份固定区域：仅允许写入指定备份区域
        allowed = True
        reason = "备份数据使用指定区域存储"
    return {
        "data_type": data_type,
        "policy_level": level,
        "source_region": source_region,
        "target_region": target_region,
        "allowed": allowed,
        "reason": reason,
    }


# ==================== 区域化访问 ====================

def resolve_access_region(
    *,
    preferred_region: str | None,
    request_ip_region: str | None,
    available_regions: list[DataRegion],
) -> str:
    """就近接入区域解析：优先用户偏好区域，其次 IP 区域，再回退主区域。"""
    active_codes = {r.region_code for r in available_regions if r.status == "active"}
    for candidate in (preferred_region, request_ip_region):
        if candidate and candidate in active_codes:
            return candidate
    main = next((r for r in available_regions if r.is_main), None)
    if main and main.region_code in active_codes:
        return main.region_code
    return available_regions[0].region_code if available_regions else "CN"


async def log_region_access(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    region_code: str,
    entry_route: str,
    request_ip_hash: str | None = None,
    user_agent: str | None = None,
) -> RegionAccessLog:
    log = RegionAccessLog(
        user_id=user_id,
        region_code=region_code,
        entry_route=entry_route,
        request_ip_hash=request_ip_hash,
        user_agent=user_agent,
    )
    db.add(log)
    await db.flush()
    return log


async def get_user_region(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    loc = (
        await db.execute(
            select(UserLocale).where(UserLocale.user_id == user_id)
        )
    ).scalar_one_or_none()
    return loc.region if loc and loc.region else None


def get_project_data_region(project: Project | None) -> str:
    """作品默认数据区域（暂按 CN 主区域，可扩展 Project.region_code）。"""
    return "CN"


# ==================== 区域化内容可见性 ====================

def check_content_visibility(
    project: Project,
    *,
    user_region: str,
    user_age: int | None = None,
) -> dict:
    """服务端执行地区与年龄权限判断（规格二十二）。
    region_visibility JSON：{"include": ["CN"], "exclude": ["KR"]} 或 ["CN"]。
    空 include 表示全部可见；exclude 优先级高于 include。
    """
    import json as _json

    raw = project.region_visibility
    include: list[str] = []
    exclude: list[str] = []
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                include = parsed.get("include") or []
                exclude = parsed.get("exclude") or []
            elif isinstance(parsed, list):
                include = parsed
        except (TypeError, ValueError):
            pass

    visible = True
    reason = "ok"
    if exclude and user_region in exclude:
        visible = False
        reason = f"作品在 {user_region} 地区不可见"
    if visible and include and user_region not in include:
        visible = False
        reason = f"作品仅对 {include} 地区可见"

    # 年龄分级：C（全年龄）/ T（青少年）/ M（成人）。仅成人内容做硬校验。
    rating = project.content_rating or "C"
    if visible and rating == "M" and user_age is not None and user_age < 18:
        visible = False
        reason = "作品为成人内容，未满 18 岁不可访问"

    return {
        "visible": visible,
        "reason": reason,
        "rating": rating,
        "region": user_region,
        "sensitive_tags": project.sensitive_tags,
        "review_status": project.review_status,
    }
