"""请求可观测性：request_id 贯通、结构化请求日志、安全响应头。

第三批 a/b 项（低风险、可独立验证）：

- a 安全响应头：为所有 HTTP 响应补 `X-Content-Type-Options` / `X-Frame-Options` /
  `Referrer-Policy` / `Permissions-Policy` / `Content-Security-Policy`。默认策略对 API
  的 JSON 响应安全（`default-src 'none'`），可通过 `SECURITY_CSP_POLICY` 覆盖。
  该中间件**只加响应头**，不改变状态码、响应体或鉴权路径。
- b request_id：每个请求分配或沿用 `X-Request-Id`，写入 contextvar 并回写到响应头；
  日志通过 `RequestIdFilter` 自动带上 `request_id` 字段；审计记录可读同一 contextvar
  写进 `metadata["request_id"]`，从而把"一次请求"在日志与审计里对齐。

设计约束：
- 不引入新依赖（仅标准库 + starlette）。
- contextvar 在中间件内 set/reset，异常路径也 reset，避免串号。
- 客户端传来的 request id 默认接受（便于前端/网关串联），但长度与字符集受限；
  `TRUST_REQUEST_ID_HEADER=false` 时一律忽略并重新生成。
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"
_MAX_REQUEST_ID_LEN = 64
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{8,64}$")

_request_id_var: ContextVar[str] = ContextVar("novelcraft_request_id", default="-")


def new_request_id() -> str:
    """生成一个新的 request id（短、无敏感信息、可在日志里直接检索）。"""
    return f"req-{uuid.uuid4().hex[:16]}"


def current_request_id() -> str:
    """当前请求的 request id；不在请求上下文时返回 '-'。"""
    return _request_id_var.get()


def set_request_id(value: str):
    return _request_id_var.set(value)


def reset_request_id(token) -> None:
    try:
        _request_id_var.reset(token)
    except (ValueError, LookupError):  # 跨上下文 reset 时忽略，不阻断请求
        pass


def sanitize_request_id(raw: str | None, *, trust: bool) -> str:
    """沿用上游 id 前做白名单校验；不可信或不合法则重新生成。"""
    if trust and raw:
        candidate = raw.strip()
        if _SAFE_REQUEST_ID.match(candidate):
            return candidate
    return new_request_id()


class RequestIdFilter(logging.Filter):
    """把当前请求的 request_id 注入 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id()
        return True


def install_request_id_filter() -> None:
    """给 root logger 的现有 handler 挂 filter；没有 handler 时不强行 basicConfig。

    说明：uvicorn 会自行配置 handler，因此这里只做"挂 filter"，不改日志级别、
    不改格式、不接管输出。挂重复调用是幂等的（同一 filter 实例只挂一次）。
    """
    filt = RequestIdFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(filt)


async def request_context_middleware(request: Request, call_next) -> Response:
    """分配 request id、记录结构化请求行、回写响应头。"""
    import time as _time

    from app.core.config import settings

    rid = sanitize_request_id(
        request.headers.get(REQUEST_ID_HEADER),
        trust=getattr(settings, "trust_request_id_header", True),
    )
    token = set_request_id(rid)
    started = _time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = rid
        return response
    finally:
        elapsed_ms = (_time.perf_counter() - started) * 1000
        logging.getLogger("app.request").info(
            "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            status_code,
            elapsed_ms,
            rid,
        )
        reset_request_id(token)


def apply_security_headers(response: Response, *, csp_policy: str | None) -> None:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    for key, value in headers.items():
        response.headers.setdefault(key, value)
    if csp_policy:
        response.headers.setdefault("Content-Security-Policy", csp_policy)


async def security_headers_middleware(request: Request, call_next) -> Response:
    """给所有响应补安全头（可通过 SECURITY_HEADERS_ENABLED=false 关闭）。"""
    from app.core.config import settings

    response = await call_next(request)
    if getattr(settings, "security_headers", True):
        # Swagger/ReDoc 从 CDN 加载脚本样式，全局 CSP 会整页拦截，文档路径跳过 CSP（其余安全头保留）
        is_docs_path = (
            request.url.path in ("/docs", "/redoc", "/openapi.json")
            or request.url.path.startswith(("/docs/", "/redoc/"))
        )
        apply_security_headers(
            response,
            csp_policy=None if is_docs_path else getattr(settings, "security_csp_policy", None),
        )
    return response
