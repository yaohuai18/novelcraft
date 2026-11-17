# -*- coding: utf-8 -*-
"""B1 认证回归：未认证请求的统一 401 契约 + 离线护栏放行矩阵。

最终契约（必须同时满足）：
    HTTP 401
    detail = "未登录或登录已失效"
    WWW-Authenticate = "Bearer"

适用输入（全部归一到同一契约）：
    - 缺少 Authorization 头
    - 空 Bearer（``Bearer`` / ``Bearer ``）
    - 非 Bearer scheme（含 ``Basic ...``）
    - 无法解析的 Bearer 凭证

覆盖范围：
    - ``_HTTPBearer401`` 的未认证异常工厂 ``make_not_authenticated_error``；
    - ``_HTTPBearer401.__call__`` 兜底改写：只改写库默认两类 detail，
      应用自身产生的 403/401 必须原样保留；
    - 生产实际依赖注入路径：FastAPI 路由 ``Depends(bearer_scheme)`` 经进程内 ASGI 的真实响应；
    - 合法 Bearer 头的解析与路由到达行为不变；
    - ``get_current_user`` 的两条认证失败路径（无效/过期 token、用户不存在）
      同样返回 401 且携带 ``WWW-Authenticate: Bearer``，detail 与查询行为不变；
    - 反例：业务自身抛出的 401/403 原样返回且不得被附加认证头；
    - 离线护栏放行矩阵：放行 ASGITransport / TestClient 进程内 transport，
      仍拦截普通 HTTPTransport、默认 AsyncClient 与 MockTransport。

边界：
    - 不启动服务（无 uvicorn、无真实端口、不监听端口）；
    - 仅进程内 ASGI 调用；``get_current_user`` 的 DB 依赖通过 dependency_overrides
      替换为内存假会话，不建连接、不发 SQL、不接触任何真实数据库；
    - 不发起任何真实网络请求（护栏仍生效，socket 层拦截未改动）。

Run: cd backend && python -m pytest tests/test_b1_auth_401.py -q
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from jose import jwt
from starlette.requests import Request

from app.core.auth import (
    UNAUTHENTICATED_DETAIL,
    UNAUTHENTICATED_HEADERS,
    _HTTPBearer401,
    _LIBRARY_DETAILS,
    bearer_scheme,
    get_current_user,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.utils.security import create_access_token

EXPECTED_DETAIL = "未登录或登录已失效"
EXPECTED_SCHEME_HEADER = "Bearer"
EXPECTED_INVALID_TOKEN_DETAIL = "无效或已过期的登录凭证"
EXPECTED_USER_NOT_FOUND_DETAIL = "用户不存在"
OFFLINE_HINT = "阶段 1 测试必须离线"


def _request_with(headers: dict[str, str] | None = None) -> Request:
    """构造最小 ASGI scope 的 Request，不经过任何服务栈。"""
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
    )


def _probe_app() -> FastAPI:
    """只挂认证依赖的最小应用：不建库、不连库、不加载业务路由。"""
    app = FastAPI()

    @app.get("/probe/protected")
    async def protected(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> dict[str, str]:
        return {"scheme": credentials.scheme, "token": credentials.credentials}

    return app


# ---------------------------------------------------------------- 契约常量一致性


def test_contract_constants_are_single_source() -> None:
    assert UNAUTHENTICATED_DETAIL == EXPECTED_DETAIL
    assert UNAUTHENTICATED_HEADERS == {"WWW-Authenticate": EXPECTED_SCHEME_HEADER}


def test_library_detail_whitelist_is_explicit() -> None:
    """只允许改写 FastAPI/Starlette 的这两类库默认 detail，不得泛化。"""
    assert _LIBRARY_DETAILS == ("Not authenticated", "Invalid authentication credentials")


def test_bearer_scheme_is_the_401_variant() -> None:
    assert isinstance(bearer_scheme, _HTTPBearer401)
    assert bearer_scheme.auto_error is True


# ------------------------------------------------- 路径一：未认证异常工厂


def test_not_authenticated_error_factory_meets_contract() -> None:
    exc = bearer_scheme.make_not_authenticated_error()

    assert isinstance(exc, HTTPException)
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_DETAIL
    assert exc.headers is not None
    assert exc.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


def test_missing_authorization_header_returns_401() -> None:
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with()))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_DETAIL
    assert exc.headers is not None
    assert exc.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic dXNlcjpwYXNz",  # scheme 不是 Bearer（库 detail: Invalid authentication credentials）
        "Basic",  # 非 Bearer 且无凭证
        "Digest username=admin",  # 其他非 Bearer scheme
        "Bearer",  # 缺凭证
        "Bearer ",  # 空白凭证
    ],
)
def test_unparsable_authorization_returns_same_contract(authorization: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with({"Authorization": authorization})))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_DETAIL
    assert exc.headers is not None
    assert exc.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


# ------------------------------------------- 路径二：库默认 403 的兜底改写


def test_legacy_403_branch_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """库抛 403 + "Not authenticated" 时必须改写为 401 契约。"""

    async def fake_super_call(self, request):  # noqa: ANN001, ARG001
        raise HTTPException(status_code=403, detail="Not authenticated")

    monkeypatch.setattr(HTTPBearer, "__call__", fake_super_call)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with()))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_DETAIL
    assert exc.headers is not None
    assert exc.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


def test_legacy_invalid_credentials_403_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """库抛 403 + "Invalid authentication credentials"（非 Bearer scheme）同样必须改写。"""

    async def fake_super_call(self, request):  # noqa: ANN001, ARG001
        raise HTTPException(status_code=403, detail="Invalid authentication credentials")

    monkeypatch.setattr(HTTPBearer, "__call__", fake_super_call)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with()))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_DETAIL
    assert exc.headers is not None
    assert exc.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


def test_unrelated_http_exception_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """非库默认 detail 的异常必须原样抛出，避免掩盖其他认证语义。"""

    async def fake_super_call(self, request):  # noqa: ANN001, ARG001
        raise HTTPException(status_code=401, detail="令牌已吊销")

    monkeypatch.setattr(HTTPBearer, "__call__", fake_super_call)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with()))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == "令牌已吊销"


def test_application_403_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """应用自身产生的 403 必须原样保留（本批次只归一库默认 detail）。"""

    async def fake_super_call(self, request):  # noqa: ANN001, ARG001
        raise HTTPException(status_code=403, detail="无保存权限")

    monkeypatch.setattr(HTTPBearer, "__call__", fake_super_call)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(bearer_scheme(_request_with()))

    exc = excinfo.value
    assert exc.status_code == 403
    assert exc.detail == "无保存权限"


# ------------------------------------ 路径三：生产实际依赖注入链路（进程内 ASGI）


def _assert_unauthorized_response(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json() == {"detail": EXPECTED_DETAIL}
    assert response.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER


def test_injected_dependency_returns_401_over_asgi() -> None:
    """走 FastAPI 依赖注入 + 异常处理器，覆盖生产实际返回给前端的响应。"""
    client = TestClient(_probe_app())
    response = client.get("/probe/protected")

    _assert_unauthorized_response(response)


@pytest.mark.parametrize(
    "authorization",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Digest username=admin"],
)
def test_injected_dependency_rejects_unauthenticated_over_asgi(authorization: str) -> None:
    client = TestClient(_probe_app())
    response = client.get("/probe/protected", headers={"Authorization": authorization})

    _assert_unauthorized_response(response)


# -------------------------- 路径四：get_current_user 认证失败契约（进程内 ASGI）

VALID_USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


class _StubResult:
    """最小结果对象：只实现 get_current_user 用到的 scalar_one_or_none。"""

    def __init__(self, user: User | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> User | None:
        return self._user


class _StubSession:
    """内存假会话：不建连接、不发 SQL、不触库，仅统计查询次数。"""

    def __init__(self, user: User | None) -> None:
        self._user = user
        self.execute_calls = 0

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
        self.execute_calls += 1
        return _StubResult(self._user)


def _fake_user() -> User:
    return User(
        id=VALID_USER_ID,
        username="b1_probe",
        email="b1_probe@example.com",
        password_hash="x",
        role="user",
        status="active",
        failed_login_attempts=0,
    )


def _current_user_app(user: User | None) -> tuple[FastAPI, _StubSession]:
    """挂真实 get_current_user 的最小应用：DB 依赖被就地替换为假会话。"""
    app = FastAPI()
    session = _StubSession(user)

    async def _override_db():
        yield session

    app.dependency_overrides[get_db] = _override_db

    @app.get("/probe/current")
    async def current(current_user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"user_id": str(current_user.id)}

    return app, session


def _expired_token() -> str:
    payload = {
        "sub": str(VALID_USER_ID),
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt-at-all",  # 无法解码
        "a.b.c",  # 结构可见但签名非法
        _expired_token(),  # 已过期
        create_access_token("not-a-uuid"),  # sub 不是 UUID
    ],
)
def test_invalid_or_expired_token_meets_contract(token: str) -> None:
    """无效/过期/畸形 token：401 + 原 detail + WWW-Authenticate: Bearer。"""
    app, session = _current_user_app(_fake_user())
    response = TestClient(app).get(
        "/probe/current", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": EXPECTED_INVALID_TOKEN_DETAIL}
    assert response.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER
    assert session.execute_calls == 0  # 解码失败即返回，不查库


def test_user_not_found_meets_contract() -> None:
    """token 合法但用户不存在：401 + "用户不存在" + WWW-Authenticate: Bearer。"""
    app, session = _current_user_app(None)
    response = TestClient(app).get(
        "/probe/current",
        headers={"Authorization": f"Bearer {create_access_token(str(VALID_USER_ID))}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": EXPECTED_USER_NOT_FOUND_DETAIL}
    assert response.headers.get("WWW-Authenticate") == EXPECTED_SCHEME_HEADER
    assert session.execute_calls == 1


def test_existing_user_still_authenticated_unchanged() -> None:
    """用户存在时行为不变：200 + 原样返回用户标识。"""
    app, _ = _current_user_app(_fake_user())
    response = TestClient(app).get(
        "/probe/current",
        headers={"Authorization": f"Bearer {create_access_token(str(VALID_USER_ID))}"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(VALID_USER_ID)}


def test_get_current_user_exceptions_carry_scheme_header() -> None:
    """异常层校验：两类认证失败异常的 status/detail 不变，仅新增认证头。"""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_current_user(credentials=credentials, db=_StubSession(None)))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_INVALID_TOKEN_DETAIL
    assert exc.headers == {"WWW-Authenticate": EXPECTED_SCHEME_HEADER}

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=create_access_token(str(VALID_USER_ID))
    )
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_current_user(credentials=credentials, db=_StubSession(None)))

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == EXPECTED_USER_NOT_FOUND_DETAIL
    assert exc.headers == {"WWW-Authenticate": EXPECTED_SCHEME_HEADER}


# --------------------------- 反例：业务 401/403 不得被附加认证头（响应级）

def _business_error_app(status_code: int, detail: str) -> FastAPI:
    app = FastAPI()

    @app.get("/probe/business")
    async def business(  # noqa: ARG001
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> dict[str, str]:
        raise HTTPException(status_code=status_code, detail=detail)

    return app


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (401, "令牌已吊销"),
        (403, "无保存权限"),
    ],
)
def test_business_401_403_get_no_auth_challenge(status_code: int, detail: str) -> None:
    """业务自身抛出的 401/403 原样返回，且不得出现 WWW-Authenticate 头。"""
    response = TestClient(_business_error_app(status_code, detail)).get(
        "/probe/business", headers={"Authorization": "Bearer header.payload.sig"}
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "WWW-Authenticate" not in response.headers


# --------------------------------------------------- 合法 Bearer：行为不变


def test_bearer_token_still_parsed_without_touching_database() -> None:
    credentials = asyncio.run(
        bearer_scheme(_request_with({"Authorization": "Bearer header.payload.sig"}))
    )
    assert credentials is not None
    assert credentials.scheme == "Bearer"
    assert credentials.credentials == "header.payload.sig"


def test_valid_bearer_reaches_route_unchanged() -> None:
    client = TestClient(_probe_app())
    response = client.get(
        "/probe/protected",
        headers={"Authorization": "Bearer header.payload.sig"},
    )

    assert response.status_code == 200
    assert response.json() == {"scheme": "Bearer", "token": "header.payload.sig"}


# ---------------------------------------------- 离线护栏放行 / 拦截矩阵


def test_guard_allows_testclient_in_process_transport() -> None:
    """TestClient 的 transport 属于进程内 ASGI，护栏必须放行。"""
    client = TestClient(_probe_app())
    response = client.get("/probe/protected")

    assert response.status_code == 401  # 走到应用层，说明未被护栏拦下


def test_guard_allows_async_asgi_transport() -> None:
    """httpx.AsyncClient + ASGITransport 属进程内传输，护栏必须放行。"""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=_probe_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as async_client:
            return await async_client.get("/probe/protected")

    _assert_unauthorized_response(asyncio.run(call()))


def test_guard_blocks_plain_sync_http_transport() -> None:
    """普通 HTTPTransport 仍必须被 send 护栏拦截（不得按 URL 宽泛豁免）。"""
    with httpx.Client(transport=httpx.HTTPTransport()) as sync_client:
        with pytest.raises(AssertionError, match=OFFLINE_HINT):
            sync_client.get("http://testserver/probe/protected")


def test_guard_blocks_plain_sync_http_transport_even_for_local_url() -> None:
    """回环 URL 也不例外：拦截依据是 transport 类型，不是目标地址。"""
    with httpx.Client(transport=httpx.HTTPTransport()) as sync_client:
        with pytest.raises(AssertionError, match=OFFLINE_HINT):
            sync_client.get("http://127.0.0.1:8000/probe/protected")


def test_guard_blocks_default_async_client() -> None:
    """默认 AsyncClient（真实 HTTP transport）仍必须被拦截。"""

    async def call() -> None:
        async with httpx.AsyncClient() as async_client:
            await async_client.get("http://example.com/")

    with pytest.raises(AssertionError, match=OFFLINE_HINT):
        asyncio.run(call())


def test_guard_blocks_mock_transport() -> None:
    """白名单是精确类型匹配：MockTransport 等其他 transport 仍被拦截。"""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as sync_client:
        with pytest.raises(AssertionError, match=OFFLINE_HINT):
            sync_client.get("http://testserver/probe/protected")
