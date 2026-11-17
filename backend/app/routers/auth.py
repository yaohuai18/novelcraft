# 认证路由：注册、登录、获取当前用户
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.security_schemas import RefreshTokenRotateRequest, RefreshTokenRotateResponse
from app.schemas.user import TokenResponse, UserLogin, UserRegister, UserResponse
from app.services import auth_service, refresh_token_service
from app.core.security_utils import generate_secure_token

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    """用户注册：用户名或邮箱已存在时返回 409。"""
    return await auth_service.register_user(db, data)


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    """用户登录：校验通过后返回访问令牌，凭证错误返回 401。"""
    user = await auth_service.authenticate_user(db, data.username, data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    return await auth_service.issue_token(user)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息（需携带 Bearer Token）。"""
    return current_user


@router.post("/refresh", response_model=RefreshTokenRotateResponse)
async def refresh_token_rotate(
    data: RefreshTokenRotateRequest,
    db: AsyncSession = Depends(get_db),
):
    """刷新令牌轮换（规格十八）：旧刷新令牌作废并签发新令牌。"""
    new_plain = generate_secure_token(48)
    new_row = await refresh_token_service.rotate(
        db,
        old_jti=data.refresh_token,
        new_jti=new_plain,
    )
    if new_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌无效或已过期",
        )
    return RefreshTokenRotateResponse(
        ok=True,
        new_token=new_plain,
        expires_at=new_row.expires_at,
    )
