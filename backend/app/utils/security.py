# 安全工具：密码哈希校验 + JWT 令牌生成与解码
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

# bcrypt 密码上下文（自动使用 bcrypt 算法）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希，返回可存储的哈希串。"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与已存储哈希是否匹配。"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user_id: str) -> str:
    """签发 JWT 访问令牌，user_id 为用户 id 字符串。"""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )

    payload = {
        "sub": user_id,
        "exp": expire,
    }

    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> str:
    """解码并校验 JWT 令牌，返回 payload 中的用户标识 sub。"""
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    user_id = payload.get("sub")

    if not user_id:
        raise ValueError("Token 中缺少用户标识")

    return user_id


def decode_token(token: str) -> dict:
    """兼容别名：旧接口返回完整 payload；新代码请使用 decode_access_token。"""
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
