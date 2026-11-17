# -*- coding: utf-8 -*-
"""backend/tests 公共配置（阶段 1 冻结期新增：关键测试正式化 + 离线护栏）。

背景：阶段 1 的关键回归测试原先散落在 backend/temp/ 下（被根 .gitignore 的
`temp/` 规则忽略，无 pytest 配置、无统一入口、不随提交追踪）。本轮把它们迁入
本目录，使其成为「正式可追踪」的回归资产。

硬约束（与阶段 1 冻结期任务书一致，只读校验、不产生业务副作用）：
  - 完全离线：不配置真实 API Key，不发起真实 DeepSeek 请求；
  - 不使用正式库 backend/novelcraft.db，一律 sqlite+aiosqlite 内存库；
  - 不使用真实历史正文、真实生成记录、真实线上响应样本；
  - 不执行数据库迁移、不写业务数据、不依赖外部环境变量。

离线护栏（autouse）：忘记 mock 时显式失败，而不是静默发出真实网络请求。
说明：仅放行本机回环（127.0.0.1 / ::1 / AF_UNIX）——Windows 事件循环自管道与
aiosqlite/线程同步依赖回环 socket，若一并拦截会使所有异步用例无法运行；任何
指向外部主机的连接一律失败。
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

import httpx

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

_OFFLINE_HINT = "阶段 1 测试必须离线：禁止真实外部网络（请 monkeypatch httpx.AsyncClient.post 注入假响应）"

# 进程内 ASGI transport 白名单：这些 transport 不建立 socket、不访问网络，
# 仅把请求直接交给 ASGI 应用处理，因此 send 护栏放行。
# 其他 transport（含 httpx.HTTPTransport / MockTransport 等）一律拦截。
_IN_PROCESS_TRANSPORT_TYPES: tuple[type, ...] = (httpx.ASGITransport,)
try:  # starlette TestClient 的内部 transport（私有，缺失时自动降级为只放行 ASGITransport）
    from starlette.testclient import _TestClientTransport as _StarletteTestClientTransport

    _IN_PROCESS_TRANSPORT_TYPES += (_StarletteTestClientTransport,)
except ImportError:  # pragma: no cover - 依赖版本差异兜底
    pass

_REAL_ASYNC_SEND = httpx.AsyncClient.send


def _is_in_process_transport(client) -> bool:
    """仅当客户端的 transport 属于已知进程内 ASGI transport 时返回 True。"""
    transport = getattr(client, "_transport", None)
    if transport is None:
        return False
    return isinstance(transport, _IN_PROCESS_TRANSPORT_TYPES)


def _is_local_address(address) -> bool:
    """判断连接目标是否为本机回环（放行）或外部主机（拦截）。"""
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX 路径域套接字
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("utf-8", "ignore")
        if not isinstance(host, str):
            return False
        if host in {"localhost", "::1", "0.0.0.0", "::", ""}:
            return True
        return host.startswith("127.")
    return False


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "offline: 离线用例（本目录用例必须全部离线，禁止真实网络）"
    )


@pytest.fixture(autouse=True)
def _offline_network_guard(monkeypatch):
    """拦截一切真实外部网络出口：用例必须 monkeypatch httpx.AsyncClient.post 注入假响应。"""
    import httpx

    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def _guarded_connect(self, address, *args, **kwargs):
        if _is_local_address(address):
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(f"{_OFFLINE_HINT}（目标：{address!r}）")

    def _guarded_create_connection(address, *args, **kwargs):
        # create_connection 会自行建立并连接 socket：仅放行本机回环目标
        if _is_local_address(address):
            return real_create_connection(address, *args, **kwargs)
        raise AssertionError(f"{_OFFLINE_HINT}（目标：{address!r}）")

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=False)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection, raising=False)

    real_sync_send = httpx.Client.send

    async def _blocked_async_send(self, request, *args, **kwargs):
        if _is_in_process_transport(self):
            return await _REAL_ASYNC_SEND(self, request, *args, **kwargs)
        raise AssertionError(
            "阶段 1 测试必须离线：禁止真实 HTTP 发送（请 monkeypatch httpx.AsyncClient.post）"
        )

    def _blocked_sync_send(self, request, *args, **kwargs):
        if _is_in_process_transport(self):
            return real_sync_send(self, request, *args, **kwargs)
        raise AssertionError(
            "阶段 1 测试必须离线：禁止真实 HTTP 发送（请 monkeypatch httpx.Client.post）"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked_async_send, raising=False)
    monkeypatch.setattr(httpx.Client, "send", _blocked_sync_send, raising=False)
    yield
