# 开放 API 统一响应包装（第十九批「移动端适配与开放 API」规格十二/二十）
# 响应统一结构：{"api_version": "v1", "data": ..., "request_id": "req-xxx"}
# 并附带限流响应头：X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset。
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


def get_request_id(request: Request) -> str:
    """从请求头或新生成 request_id，并缓存到 request.state。"""
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    rid = request.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"
    request.state.request_id = rid
    return rid


def ok(
    request: Request,
    data: dict,
    *,
    status_code: int = 200,
    rate_headers: dict | None = None,
) -> JSONResponse:
    """统一成功响应。"""
    headers = dict(rate_headers or {})
    headers.setdefault("X-Request-Id", get_request_id(request))
    return JSONResponse(
        status_code=status_code,
        content={
            "api_version": "v1",
            "data": data,
            "request_id": get_request_id(request),
        },
        headers=headers,
    )


def build_rate_headers(result: dict) -> dict:
    """将限流检查结果转为响应头。"""
    limit = result.get("limit", 0)
    count = result.get("count", 0)
    remaining = max(0, limit - count)
    reset = result.get("retry_after", 0)
    if result.get("allowed") and result.get("window_remaining"):
        reset = result["window_remaining"]
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(0, int(reset))),
    }
