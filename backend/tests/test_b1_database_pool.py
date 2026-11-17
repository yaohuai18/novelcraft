# -*- coding: utf-8 -*-
"""B1 最小回归：``backend/app/core/database.py`` 的 SQLite 连接池分流。

覆盖点：
  1. 内存库（``:memory:``）仍使用 ``StaticPool``（单连接共享）；
  2. 文件库不使用 ``StaticPool``，走方言默认连接池，且 connect_args 带 ``timeout=30``；
  3. 文件库（``tmp_path`` 临时库）上同时取两个连接，实际拿到不同 DBAPI 连接；
  4. 不连接、不读取、不写入正式库 ``backend/novelcraft.db``（全程仅临时目录）。

实现方式：**不重载真实模块**（重载会用新的空 ``Base`` 替换模块级符号，污染后续
用例的 ``Base.metadata``）。改为把 ``app/core/database.py`` 的真实源码在独立命名
空间内 ``exec``，仅注入被 monkeypatch 改写的 ``settings.database_url``，从而原样
执行生产分支逻辑；测试结束后只 dispose 探针引擎，不改动任何模块对象。
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import StaticPool

import app.core.config as config_module
import app.core.database as database_module

SOURCE_PATH = pathlib.Path(database_module.__file__)
SOURCE_TEXT = SOURCE_PATH.read_text(encoding="utf-8")
ORIGINAL_URL = config_module.settings.database_url


def _build_namespace(monkeypatch: pytest.MonkeyPatch, url: str) -> dict:
    """在独立命名空间执行生产源码，返回其模块级符号字典。"""
    monkeypatch.setattr(config_module.settings, "database_url", url, raising=False)
    namespace: dict = {
        "__name__": "app.core.database_b1_probe",
        "__file__": str(SOURCE_PATH),
    }
    exec(compile(SOURCE_TEXT, str(SOURCE_PATH), "exec"), namespace)
    return namespace


@pytest.fixture()
def probe(monkeypatch: pytest.MonkeyPatch):
    created: list[AsyncEngine] = []

    def _factory(url: str) -> dict:
        namespace = _build_namespace(monkeypatch, url)
        created.append(namespace["engine"])
        return namespace

    yield _factory

    for engine in created:
        # dispose 不建立新连接，只释放池内资源（此时池为空）
        asyncio.run(engine.dispose())


def test_memory_sqlite_keeps_static_pool(probe) -> None:
    namespace = probe("sqlite+aiosqlite:///:memory:")

    assert namespace["_is_memory_sqlite"] is True
    assert namespace["_engine_kwargs"]["poolclass"] is StaticPool
    assert namespace["_engine_kwargs"]["connect_args"] == {"check_same_thread": False}
    assert isinstance(namespace["engine"].pool, StaticPool)


def test_memory_branch_matches_production_module_when_url_is_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一份源码在内存 URL 下的判定，与生产模块当前（文件 URL）判定必须不同。"""
    assert ORIGINAL_URL.startswith("sqlite"), "本用例前提：.env 为 SQLite 文件库"
    assert database_module._is_memory_sqlite is False

    namespace = _build_namespace(monkeypatch, "sqlite+aiosqlite:///:memory:")
    try:
        assert namespace["_is_memory_sqlite"] is True
        assert namespace["_engine_kwargs"]["poolclass"] is StaticPool
    finally:
        asyncio.run(namespace["engine"].dispose())


def test_current_settings_file_sqlite_uses_default_pool_with_timeout(probe) -> None:
    assert ORIGINAL_URL.startswith("sqlite"), "本用例前提：.env 为 SQLite 文件库"
    namespace = probe(ORIGINAL_URL)

    assert namespace["_is_memory_sqlite"] is False
    assert "poolclass" not in namespace["_engine_kwargs"], "文件库不得强制 StaticPool"
    assert isinstance(namespace["engine"].pool, StaticPool) is False
    assert namespace["_engine_kwargs"]["connect_args"]["check_same_thread"] is False
    assert namespace["_engine_kwargs"]["connect_args"]["timeout"] == 30

    # 与生产模块当前实际构造保持一致（只读比对，不建立连接）
    assert database_module._engine_kwargs["connect_args"]["timeout"] == 30
    assert isinstance(database_module.engine.pool, StaticPool) is False


def test_tmp_file_sqlite_gets_distinct_dbapi_connections(probe, tmp_path) -> None:
    db_path = tmp_path / "b1_probe.db"
    url = "sqlite+aiosqlite:///" + db_path.as_posix()
    namespace = probe(url)
    engine = namespace["engine"]

    assert namespace["_is_memory_sqlite"] is False
    assert isinstance(engine.pool, StaticPool) is False
    assert namespace["_engine_kwargs"]["connect_args"]["timeout"] == 30
    assert db_path.exists() is False, "构造引擎阶段不得创建数据库文件"

    async def _probe() -> tuple[int, int]:
        async with engine.connect() as first:
            async with engine.connect() as second:
                raw_first = await first.get_raw_connection()
                raw_second = await second.get_raw_connection()
                assert (await first.execute(text("SELECT 1"))).scalar() == 1
                assert (await second.execute(text("SELECT 1"))).scalar() == 1
                return (
                    id(raw_first.driver_connection),
                    id(raw_second.driver_connection),
                )

    first_id, second_id = asyncio.run(_probe())
    assert first_id != second_id, "文件库两个并发连接必须使用不同 DBAPI 连接"
    assert db_path.exists(), "临时库应创建在 tmp_path 内"
    assert "novelcraft.db" not in str(db_path).lower()
