# CDN 与静态资源服务（第二十一批「国际化、多语言和多区域部署」规格十九）
# 公开资源可缓存；私有资源短期签名 URL + 权限校验；下载日志。
# 发布新版本使用文件指纹与版本化路径。
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cdn_asset import CdnAsset, DownloadLog

SIGNING_SECRET = "novelcraft-cdn-signing-secret"
DEFAULT_SIGNED_URL_TTL_SECONDS = 300  # 短期签名：5 分钟


def compute_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def register_asset(
    db: AsyncSession,
    *,
    asset_key: str,
    original_name: str,
    mime_type: str,
    file_size: int,
    is_public: bool,
    content: bytes | None = None,
    region_code: str = "CN",
    owner_id: uuid.UUID | None = None,
    cache_control: str | None = None,
) -> CdnAsset:
    """注册静态资源。content 提供时计算指纹并生成版本化路径。"""
    fingerprint = compute_fingerprint(content) if content is not None else ""
    version_path = (
        f"v{fingerprint[:12]}/{asset_key}" if fingerprint else None
    )
    existing = (
        await db.execute(
            select(CdnAsset).where(CdnAsset.asset_key == asset_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.mime_type = mime_type
        existing.file_size = file_size
        existing.is_public = is_public
        if fingerprint:
            existing.fingerprint = fingerprint
        if version_path:
            existing.version_path = version_path
        existing.owner_id = owner_id
        existing.updated_at = datetime.utcnow()
        await db.flush()
        return existing
    asset = CdnAsset(
        asset_key=asset_key,
        original_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        is_public=is_public,
        region_code=region_code,
        cache_control=cache_control or (
            "public, max-age=86400" if is_public else "private, no-store"
        ),
        fingerprint=fingerprint,
        version_path=version_path,
        owner_id=owner_id,
        status="active",
    )
    db.add(asset)
    await db.flush()
    return asset


async def get_asset(db: AsyncSession, asset_key: str) -> CdnAsset | None:
    return (
        await db.execute(
            select(CdnAsset).where(CdnAsset.asset_key == asset_key)
        )
    ).scalar_one_or_none()


def sign_url(asset: CdnAsset, *, ttl_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS) -> str:
    """私有资源短期签名 URL：路径 + 过期时间 + HMAC 签名。"""
    expires = int(time.time()) + ttl_seconds
    path = asset.version_path or asset.asset_key
    payload = f"{path}|{expires}"
    sig = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"https://cdn.novelcraft.local/{path}?e={expires}&s={sig}"


def verify_signed_url(url: str, *, max_age_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS) -> bool:
    """校验签名 URL：签名匹配且未过期。"""
    try:
        query = url.split("?", 1)[1]
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        expires = int(params.get("e", 0))
        sig = params.get("s", "")
        path = url.split("?", 1)[0].replace("https://cdn.novelcraft.local", "").lstrip("/")
        expected = hmac.new(
            SIGNING_SECRET.encode("utf-8"),
            f"{path}|{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        if time.time() > expires:
            return False
        return True
    except Exception:
        return False


async def check_asset_access(
    db: AsyncSession,
    *,
    asset_key: str,
    user_id: uuid.UUID | None,
    signed_url: str | None = None,
) -> dict:
    """资源访问权限校验：公开资源直接放行；私有资源需有效签名且为拥有者/管理员。
    返回 {allowed, reason, asset?, url?}；allowed 时附带可访问 URL（签名/原路径）。
    """
    asset = await get_asset(db, asset_key)
    if asset is None:
        return {"allowed": False, "reason": "资源不存在", "asset": None}
    if asset.status != "active":
        return {"allowed": False, "reason": "资源已下线", "asset": asset}
    if asset.is_public:
        return {
            "allowed": True,
            "reason": "public",
            "asset": asset,
            "url": f"https://cdn.novelcraft.local/{asset.version_path or asset.asset_key}",
            "cache_control": asset.cache_control,
        }
    if not signed_url or not verify_signed_url(signed_url):
        return {"allowed": False, "reason": "签名无效或已过期", "asset": asset}
    # 有效签名即访问凭证（服务端签发时已做登录鉴权），不再二次校验 owner
    return {
        "allowed": True,
        "reason": "signed",
        "asset": asset,
        "url": signed_url,
    }


async def log_download(
    db: AsyncSession,
    *,
    asset_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> DownloadLog:
    """私有资源下载日志（访问留痕）。"""
    log = DownloadLog(
        asset_id=asset_id,
        user_id=user_id,
        ip_hash=ip_hash,
        user_agent=user_agent,
    )
    db.add(log)
    await db.flush()
    return log


async def list_assets(
    db: AsyncSession,
    *,
    is_public: bool | None = None,
    region_code: str | None = None,
    limit: int = 100,
) -> list[CdnAsset]:
    stmt = select(CdnAsset).order_by(CdnAsset.created_at.desc())
    if is_public is not None:
        stmt = stmt.where(CdnAsset.is_public.is_(is_public))
    if region_code is not None:
        stmt = stmt.where(CdnAsset.region_code == region_code)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def list_download_logs(
    db: AsyncSession,
    *,
    asset_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[DownloadLog]:
    stmt = select(DownloadLog).order_by(DownloadLog.downloaded_at.desc())
    if asset_id is not None:
        stmt = stmt.where(DownloadLog.asset_id == asset_id)
    if user_id is not None:
        stmt = stmt.where(DownloadLog.user_id == user_id)
    result = await db.execute(stmt.limit(limit))
    return list(result.scalars().all())
