# 认证业务逻辑：注册（查重、哈希、落库）、登录（校验、失败锁定、签发 token）
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.security_event import SecurityEvent
from app.models.user import User
from app.schemas.user import TokenResponse, UserRegister
from app.utils.security import create_access_token, hash_password, verify_password


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """按用户名查询用户。"""
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def register_user(db: AsyncSession, data: UserRegister) -> User:
    """注册新用户：用户名或邮箱查重（重复返回 409），再哈希密码并落库。"""
    result = await db.execute(
        select(User).where(
            or_(
                User.username == data.username,
                User.email == data.email,
            )
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名或邮箱已经存在",
        )

    user = User(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    """校验用户名密码，通过则返回用户，否则返回 None。

    第十八批规格十八：登录失败计数，达到上限（login_max_attempts）后锁定账户
    login_lock_minutes 分钟；锁定期间直接拒绝；成功后重置计数并记录安全事件。
    """
    user = await get_user_by_username(db, username)
    if user is None:
        return None
    # 第十七批：受限/禁用账号（restricted / disabled）拒绝登录
    if user.status != "active":
        return None
    now = datetime.utcnow()
    if user.locked_until is not None and user.locked_until > now:
        return None
    if not verify_password(password, user.password_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= settings.login_max_attempts:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            db.add(SecurityEvent(
                event_type="account_locked",
                user_id=user.id,
                details="登录失败次数超限，账户临时锁定",
            ))
        else:
            db.add(SecurityEvent(
                event_type="login_failed",
                user_id=user.id,
                details="登录密码错误",
            ))
        await db.commit()
        return None
    user.failed_login_attempts = 0
    user.locked_until = None
    db.add(SecurityEvent(
        event_type="login_success",
        user_id=user.id,
        details="登录成功",
    ))
    await db.commit()
    return user


async def issue_token(user: User) -> TokenResponse:
    """为指定用户签发 JWT 访问令牌并组装响应。"""
    access_token = create_access_token(str(user.id))
    return TokenResponse(access_token=access_token, token_type="bearer")
