# 刷新令牌服务（第十八批「性能优化、可靠性与安全加固」规格十八）
# 会话过期 + 刷新令牌轮换：RefreshToken 存哈希，使用后立即轮换（旧令牌失效、签发新令牌）。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security_utils import sha256_hex
from app.models.refresh_token import RefreshToken


async def issue(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_jti: str,
    device_info: str | None = None,
    ip_address: str | None = None,
) -> RefreshToken:
    """签发刷新令牌（服务端仅存令牌哈希）。"""
    row = RefreshToken(
        user_id=user_id,
        token_hash=sha256_hex(token_jti),
        device_info=device_info,
        ip_hash=sha256_hex(ip_address) if ip_address else None,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def rotate(
    db: AsyncSession,
    *,
    old_jti: str,
    new_jti: str,
) -> RefreshToken | None:
    """刷新令牌轮换：校验旧令牌哈希有效 -> 作废旧令牌 -> 签发新令牌（同会话）。"""
    old = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == sha256_hex(old_jti)
            )
        )
    ).scalar_one_or_none()
    if old is None or old.revoked_at is not None:
        return None
    if old.expires_at is not None and old.expires_at <= datetime.utcnow():
        return None
    old.revoked_at = datetime.utcnow()
    old.last_used_at = datetime.utcnow()
    old.used_count += 1
    new = RefreshToken(
        user_id=old.user_id,
        token_hash=sha256_hex(new_jti),
        device_info=old.device_info,
        ip_hash=old.ip_hash,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(new)
    await db.commit()
    await db.refresh(new)
    return new


async def revoke_all(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> int:
    """吊销某用户全部未过期刷新令牌（改密 / 注销场景）。"""
    rows = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.utcnow()
    for row in rows:
        row.revoked_at = now
    await db.commit()
    return len(rows)
