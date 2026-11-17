# 认证依赖：从 Authorization: Bearer 解析并校验 JWT，取出当前用户
# 入口与规格一致（第二批详细规格要求 get_current_user 位于 core/auth.py）
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.utils.security import decode_access_token


# 未认证统一契约（生产实际返回给前端的内容）
UNAUTHENTICATED_DETAIL = "未登录或登录已失效"
UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}

# FastAPI/Starlette 内部「未认证」的默认文案，用于识别后改写。
# 两类都必须覆盖（fastapi 0.115 的 HTTPBearer.__call__ 会分别抛出）：
#   - "Not authenticated"              —— 缺 Authorization 头、空 Bearer、不可解析凭证
#   - "Invalid authentication credentials" —— scheme 不是 Bearer（如 Basic）
# 只改写上述库默认文案；应用自身产生的 403 必须原样保留。
_LIBRARY_DETAILS = (
    "Not authenticated",
    "Invalid authentication credentials",
)


class _HTTPBearer401(HTTPBearer):
    """未携带 Authorization 头时统一返回 401（未登录/登录失效），而非 HTTPBearer 默认 403。

    语义对齐：未认证 = 401，未授权 = 403。前端据此区分「未登录」与「无保存权限」。

    契约（两条路径都必须满足）：
      - HTTP 401
      - detail = "未登录或登录已失效"
      - WWW-Authenticate: Bearer

    版本差异说明：
      - 部分 FastAPI 版本在 ``__call__`` 内部调用 ``make_not_authenticated_error()``
        直接抛 401 + "Not authenticated"（本钩子即为其覆写点，被调用时直接生效）；
      - fastapi 0.115.0 等版本不调用该钩子，由 ``__call__`` 直接抛 403：
        detail="Not authenticated"（缺头/不可解析）或
        detail="Invalid authentication credentials"（scheme 非 Bearer）。
    因此同时覆写钩子并在 ``__call__`` 兜底改写，两条路径归一。
    """

    def make_not_authenticated_error(self) -> HTTPException:
        """新版 FastAPI 的未认证异常工厂：直接返回中文 401 契约。"""
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED_DETAIL,
            headers=dict(UNAUTHENTICATED_HEADERS),
        )

    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        try:
            return await super().__call__(request)
        except HTTPException as exc:
            # 库默认的两类未认证文案（缺头/不可解析、非 Bearer scheme）统一改写；
            # 其他 detail（含应用自身 403）必须原样抛出，不得吞改。
            if (
                exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
                and exc.detail in _LIBRARY_DETAILS
            ):
                raise self.make_not_authenticated_error() from None
            raise


bearer_scheme = _HTTPBearer401()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从请求头解析 Bearer Token，解码校验后返回当前登录用户。"""
    token = credentials.credentials

    try:
        user_id = decode_access_token(token)
        user_uuid = uuid.UUID(user_id)
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的登录凭证",
            headers=dict(UNAUTHENTICATED_HEADERS),
        )

    result = await db.execute(
        select(User).where(User.id == user_uuid)
    )

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers=dict(UNAUTHENTICATED_HEADERS),
        )

    # 第十七批：受限/禁用账号的既有令牌同样拒绝（禁用立即生效，无需等 token 过期）
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被限制或禁用",
            headers=dict(UNAUTHENTICATED_HEADERS),
        )

    return user
