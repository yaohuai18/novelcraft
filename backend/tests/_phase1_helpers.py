# -*- coding: utf-8 -*-
"""阶段 1 正式测试共用夹具（backend/tests/，完全离线）。

约定：
  - 只用 sqlite+aiosqlite 内存库（绝不触碰 backend/novelcraft.db）；
  - AI 调用一律 monkeypatch httpx.AsyncClient.post，注入假响应；
  - 不配置真实 API Key、不使用真实历史正文与真实响应样本。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

# 假密钥：仅用于通过「已配置 API Key / base_url」的非空校验，不会发出任何请求
FAKE_KEY = "sk-test-phase1-offline"
FAKE_BASE = "https://api.example.invalid/v1"


class FakeResponse:
    """最小 httpx 响应替身（不发网络）。"""

    def __init__(self, data: dict, status_code: int = 200, headers: dict | None = None):
        self._data = data
        self.status_code = status_code
        self.headers = (
            headers if headers is not None else {"x-request-id": "req-phase1-offline"}
        )

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def text(self) -> str:
        return json.dumps(self._data, ensure_ascii=False)

    def json(self) -> dict:
        return self._data


def completion_body(
    *,
    content: str = "阶段1测试正文",
    finish_reason: str = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 22,
    usage: bool = True,
    reasoning_content: str | None = None,
) -> dict:
    """构造 OpenAI 兼容的补全响应体；usage=False 用于模拟「无真实 usage」。"""
    message: dict = {"content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    body: dict = {
        "id": "chatcmpl-phase1-offline",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return body


def patch_provider(monkeypatch, *, body: dict | None = None, status_code: int = 200) -> dict:
    """注入假配置并拦截 httpx 下发；返回 captured 容器（url / headers / payload / calls）。"""
    import httpx

    from app.core.config import settings
    from app.services import ai_service

    monkeypatch.setattr(settings, "deepseek_api_key", FAKE_KEY, raising=False)
    monkeypatch.setattr(settings, "deepseek_base_url", FAKE_BASE, raising=False)
    monkeypatch.setattr(
        ai_service,
        "get_effective_ai_config",
        lambda: (FAKE_KEY, FAKE_BASE, "deepseek-v4-pro"),
    )

    captured: dict = {"calls": []}
    response_body = body if body is not None else completion_body()

    async def fake_post(self, url, headers=None, json=None, **kwargs):  # noqa: A002
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        captured["calls"].append({"url": url, "payload": json})
        return FakeResponse(response_body, status_code=status_code)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


def drain_ai_context() -> None:
    """清空 ai_service「最近一次调用」contextvar，避免用例间串味。"""
    from app.services import ai_service

    ai_service.take_last_ai_usage()
    ai_service.take_last_ai_meta()
    ai_service.take_last_ai_prompt()
    ai_service.take_last_ai_policy()


@asynccontextmanager
async def memory_db():
    """sqlite+aiosqlite 内存库（StaticPool 单连接），退出时自动释放。"""
    from app.core.database import Base

    # 一次性注册全部 ORM 表（app/models/__init__.py 已聚合导入）；
    # 只导入部分模型会因外键找不到目标表导致 create_all 报 NoReferencedTableError。
    import app.models  # noqa: F401

    # 既有缺口（阶段 1 只读发现，非本轮引入，不修改生产代码）：
    # app/models/chapter.py 未登记进 app/models/__init__.py，测试侧在此显式补注册（只读导入）。
    # 与 B-2 无关的 item.py、item_possession.py 不在本批导入/注册，避免将其模型与关系依赖并入本批。
    import app.models.chapter  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield engine, session
        finally:
            await session.close()
    finally:
        await engine.dispose()


def run(coro):
    """在同步用例中驱动协程（与既有 temp 回归脚本风格一致，无需 pytest-asyncio）。"""
    return asyncio.run(coro)
