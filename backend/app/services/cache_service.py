# 缓存服务（第十八批「性能优化、可靠性与安全加固」规格三/四）
# Cache Aside：读缓存 -> 未命中查库 -> 写缓存；数据修改时主动失效。
# 不建议缓存清单（私密正文/频繁变更协作内容/权限敏感/未完成AI结果/一次性安全令牌）
# 以白名单前缀校验，缓存键统一 key_prefix:... 便于按前缀批量失效。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.cache_entry import CacheEntry

# 不建议缓存的 key 前缀（黑名单校验）
NO_CACHE_PREFIXES = (
    "private_content:",
    "chapter:content:",          # 私密正文不缓存（公开章节由业务层决定是否显式放行）
    "permission:",
    "ai_result:pending:",
    "one_time_token:",
    "refresh_token:",
)

# 允许缓存的命名空间
ALLOWED_NAMESPACES = {
    "user", "project", "chapter", "stats", "tag", "config", "discover", "search",
}


def _validate_key(cache_key: str) -> str:
    """白名单校验：仅允许受控命名空间的缓存键，拒绝不建议缓存清单。"""
    if any(cache_key.startswith(prefix) for prefix in NO_CACHE_PREFIXES):
        raise ValueError(f"该缓存键不建议缓存: {cache_key}")
    ns = cache_key.split(":", 1)[0]
    if ns not in ALLOWED_NAMESPACES:
        raise ValueError(f"缓存命名空间不在白名单: {ns}")
    return cache_key


async def get_cached(db: AsyncSession, cache_key: str) -> tuple[dict | None, bool]:
    """读取缓存（命中返回 (value_dict, True)；未命中 (None, False)）。"""
    row = (
        await db.execute(
            select(CacheEntry).where(CacheEntry.cache_key == cache_key)
        )
    ).scalar_one_or_none()
    if row is None:
        return None, False
    if row.expires_at is not None and row.expires_at <= datetime.utcnow():
        await db.execute(
            delete(CacheEntry).where(CacheEntry.cache_key == cache_key)
        )
        await db.commit()
        return None, False
    try:
        return json.loads(row.value), True
    except Exception:
        return None, False


async def set_cached(
    db: AsyncSession,
    cache_key: str,
    value: dict,
    *,
    ttl_seconds: int | None = None,
    version: int = 1,
) -> CacheEntry:
    """写入缓存（upsert）；ttl_seconds=None 使用默认 TTL。"""
    _validate_key(cache_key)
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_default_ttl_seconds
    expires_at = datetime.utcnow() + timedelta(seconds=ttl)
    row = (
        await db.execute(
            select(CacheEntry).where(CacheEntry.cache_key == cache_key)
        )
    ).scalar_one_or_none()
    if row is None:
        row = CacheEntry(
            cache_key=cache_key,
            value=json.dumps(value, ensure_ascii=False),
            version=version,
            expires_at=expires_at,
        )
        db.add(row)
    else:
        row.value = json.dumps(value, ensure_ascii=False)
        row.version = version
        row.expires_at = expires_at
    await db.commit()
    await db.refresh(row)
    return row


async def invalidate(db: AsyncSession, cache_key: str) -> bool:
    """主动失效单条缓存；不存在返回 False。"""
    result = await db.execute(
        delete(CacheEntry).where(CacheEntry.cache_key == cache_key)
    )
    await db.commit()
    return result.rowcount > 0


async def invalidate_prefix(db: AsyncSession, key_prefix: str) -> int:
    """按前缀批量失效（如 project:project-01: -> 项目概览/章节/统计缓存）。"""
    result = await db.execute(
        delete(CacheEntry).where(CacheEntry.cache_key.like(f"{key_prefix}%"))
    )
    await db.commit()
    return result.rowcount


async def bump_version(db: AsyncSession, cache_key: str) -> CacheEntry | None:
    """版本化缓存键：对既有条目版本 +1 并延长 TTL（未命中则创建 version=1）。"""
    row = (
        await db.execute(
            select(CacheEntry).where(CacheEntry.cache_key == cache_key)
        )
    ).scalar_one_or_none()
    if row is None:
        return await set_cached(db, cache_key, {"_version": 1})
    row.version += 1
    row.expires_at = datetime.utcnow() + timedelta(
        seconds=settings.cache_default_ttl_seconds
    )
    await db.commit()
    await db.refresh(row)
    return row
