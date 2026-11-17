# 开放 API 统一错误结构（第十九批「移动端适配与开放 API」规格二十一）
# 所有 /api/v1 与 /api/mobile 接口出错时返回：
#   {"error": {"code": ..., "message": ..., "request_id": ..., "details": ...}}
# 错误码固定 9 种，禁止泄露数据库结构/内部路径/他人存在性/密钥/堆栈。
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

ERROR_CODES = {
    "invalid_request",
    "unauthorized",
    "permission_denied",
    "not_found",
    "conflict",
    "validation_failed",
    "rate_limit_exceeded",
    "task_failed",
    "service_unavailable",
}


class ApiError(Exception):
    """开放 API 统一业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
        request_id: str | None = None,
    ):
        if code not in ERROR_CODES:
            raise ValueError(f"未知错误码: {code}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.request_id = request_id
        super().__init__(message)


def _build_error_body(request: Request, exc: ApiError) -> dict:
    request_id = exc.request_id or str(uuid.uuid4())
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            "details": exc.details,
        }
    }


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_body(request, exc),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """将 FastAPI HTTPException 转为统一错误结构（仅开放 API 路径）。"""
    if request.url.path.startswith("/api/v1") or request.url.path.startswith("/api/mobile"):
        code_map = {
            400: "invalid_request",
            401: "unauthorized",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            429: "rate_limit_exceeded",
            500: "service_unavailable",
        }
        code = code_map.get(exc.status_code, "invalid_request")
        # 统一错误信息脱敏：不把 detail 原样返回给客户端
        safe_messages = {
            400: "请求参数不合法",
            401: "未认证或凭据无效",
            403: "没有访问权限",
            404: "资源不存在",
            409: "资源状态冲突",
            429: "请求过于频繁，请稍后再试",
            500: "服务器内部错误",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": safe_messages.get(
                        exc.status_code, "服务器内部错误"
                    ),
                    "request_id": str(uuid.uuid4()),
                    "details": {},
                }
            },
        )
    # 非开放 API 路径维持原结构
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )
