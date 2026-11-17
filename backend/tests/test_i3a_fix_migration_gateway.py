"""I-3a-fix 迁移事务网关 / 签名校验 / 备份 API / v2 回归 专项测试。

约束（对应 D1–D11 裁定）：
- 全部用 tmp_path 下的隔离 SQLite 文件库，不触碰正式库 novelcraft.db；
- 不修改 v2 迁移语义，只验证新网关不破坏 v2；
- v5 只补 consistency_review 索引，不得重建表（表由 create_all 建立）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import sqlite3
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import migrate
from app.core.database import Base
from app.core.db_migrations import (
    PHASE19_MIGRATION_STATEMENTS,
    PHASE22_MIGRATION_STATEMENTS,
    ensure_phase19_schema,
)
import app.models  # noqa: F401  必须导入，否则 Base.metadata 为空、create_all 建不出任何表

pytestmark = pytest.mark.asyncio

V4_STATEMENTS = migrate.MIGRATION_STEPS[4][1]
V2_STATEMENTS = migrate.MIGRATION_STEPS[2][1]
CFD_TABLE = "chapter_fact_dependencies"
CFD_INDEXES = [
    stmt.split("ON", 1)[0].strip().split()[-1]
    for stmt in PHASE19_MIGRATION_STATEMENTS
    if "CREATE INDEX" in stmt.upper()
]
# 与 v4 目标签名冲突的伪表（缺列），用于验证「有对象但结构不符 → 失败关闭」
MISMATCH_TABLE_SQL = (
    "CREATE TABLE chapter_fact_dependencies ("
    "id CHAR(32) PRIMARY KEY, chapter_id CHAR(32) NOT NULL)"
)

# 正式库只读基线：分「迁移前」与「v4 执行后」两段记录，不得混用。
# - 迁移前基线只作取证留档，不是当前通过条件；
# - 当前通过条件 = v4 执行后的 SHA-256 + size + schema_migrations 版本集合；
# - mtime 只做「不得早于 v4 执行完成时刻」的下界校验：备份/只读打开会合法刷新
#   mtime，固定成常量会在任何一次正常运维后误报。
FORMAL_DB = pathlib.Path(__file__).resolve().parents[1] / "novelcraft.db"
BASELINE_SIZE = 154058752
BASELINE_SHA256 = "DF2BFE5915E4D37489BF2C3EC40745C6465F139EBEB518DA1234595E1DE2C2B6"
BASELINE_MTIME = "2026-10-18 18:25:32"

FORMAL_SIZE = 154058752
FORMAL_SHA256 = "71CDB5A0968FA67DA81E649D6FAB20B2328D561F08CAECDC9340A8CF259A7D7C"
FORMAL_MTIME_NOT_BEFORE = "2026-10-24 11:44:57"
FORMAL_SCHEMA_VERSIONS = (1, 2, 3, 4)  # 正式库当前实际状态；v5/v6/v7 均未应用


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _query(db_file: str, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _query_readonly(db_file: pathlib.Path, sql: str, params: tuple = ()) -> list[tuple]:
    """只读 URI 打开：用于正式库，保证查询不产生 -wal/-shm 或任何写入。"""
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _object_exists(db_file: str, kind: str, name: str) -> bool:
    rows = _query(
        db_file, "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    )
    return bool(rows)


def _applied_versions(db_file: str) -> list[int]:
    return [row[0] for row in _query(db_file, "SELECT version FROM schema_migrations ORDER BY version")]


async def _make_engine(tmp_path: pathlib.Path, name: str = "t.db"):
    db_file = tmp_path / f"{name}-{uuid.uuid4().hex[:8]}"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine = create_async_engine(url)
    return engine, str(db_file), url


async def _prepare_stock_db(engine, *, create_cfd: bool = False, versions=(1, 2, 3)) -> None:
    """构造「v1 基线 + 指定已应用版本」的存量库，模拟正式库形态。"""
    async with engine.connect() as conn:
        await migrate.ensure_migration_table(conn)
        async with migrate.ExplicitTransaction(conn) as tx:
            skip = {CFD_TABLE}
            tables = [t for n, t in Base.metadata.tables.items() if n not in skip]
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
            if create_cfd:
                for statement in PHASE19_MIGRATION_STATEMENTS:
                    await conn.exec_driver_sql(statement)
            for version in versions:
                await migrate.record_version(conn, version, f"v{version}")
            await tx.commit()


def _inject_statement_failure(monkeypatch, *, fail_after: int):
    """在第 fail_after 条语句（0-based）执行完成后抛错。"""
    original = migrate._execute_migration_statement
    state = {"count": 0}

    async def wrapper(conn, statement):
        if state["count"] == fail_after:
            raise RuntimeError(f"injected failure before statement #{fail_after}")
        await original(conn, statement)
        state["count"] += 1

    monkeypatch.setattr(migrate, "_execute_migration_statement", wrapper)
    return state


# ---------------------------------------------------------------------------
# A. 显式事务网关
# ---------------------------------------------------------------------------
async def test_probe_reports_transaction_state(tmp_path):
    engine, db_file, _ = await _make_engine(tmp_path, "probe")
    try:
        async with engine.connect() as conn:
            assert await migrate.probe_in_transaction(conn) is False
            async with migrate.ExplicitTransaction(conn) as tx:
                assert await migrate.probe_in_transaction(conn) is True
                await tx.commit()
            assert await migrate.probe_in_transaction(conn) is False
    finally:
        await engine.dispose()


async def test_nested_gateway_is_rejected(tmp_path):
    engine, _, _ = await _make_engine(tmp_path, "nested")
    try:
        async with engine.connect() as conn:
            async with migrate.ExplicitTransaction(conn) as tx:
                with pytest.raises(migrate.MigrationTransactionStateError):
                    async with migrate.ExplicitTransaction(conn):
                        pass
                await tx.commit()
    finally:
        await engine.dispose()


async def test_commit_without_begin_is_rejected(tmp_path):
    engine, _, _ = await _make_engine(tmp_path, "cwb")
    try:
        async with engine.connect() as conn:
            with pytest.raises(migrate.MigrationTransactionStateError):
                await migrate.ExplicitTransaction(conn).commit()
    finally:
        await engine.dispose()


async def test_ddl_rolls_back_inside_explicit_transaction(tmp_path):
    """P_B 复验：显式 BEGIN IMMEDIATE 下 DDL 可完整回滚（无残留表/索引）。"""
    engine, db_file, _ = await _make_engine(tmp_path, "rollback")
    try:
        async with engine.connect() as conn:
            async with migrate.ExplicitTransaction(conn) as tx:
                for statement in V4_STATEMENTS:
                    await conn.exec_driver_sql(statement)
                # 同一连接内可见自己未提交的 DDL
                in_tx = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"
                        ),
                        {"name": CFD_TABLE},
                    )
                ).first()
                assert in_tx, "事务内应能建出目标表（证明 DDL 确实执行）"
                await tx.rollback()
            assert not _object_exists(db_file, "table", CFD_TABLE)
            for index in CFD_INDEXES:
                assert not _object_exists(db_file, "index", index)
            assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B. F0–F5 失败注入：v4 每条 DDL 后失败 → 完全恢复 → 重试成功
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fail_after", [0, 1, 2, 3])
async def test_failed_v4_leaves_no_residue_and_retry_succeeds(tmp_path, monkeypatch, fail_after):
    engine, db_file, url = await _make_engine(tmp_path, f"f{fail_after}")
    try:
        await _prepare_stock_db(engine)

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                _inject_statement_failure(m, fail_after=fail_after)
                with pytest.raises(migrate.MigrationStepFailure) as excinfo:
                    await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        failure = excinfo.value
        assert failure.version == 4

        # 失败后：表、索引、版本记录全部不存在
        assert not _object_exists(db_file, "table", CFD_TABLE)
        for index in CFD_INDEXES:
            assert not _object_exists(db_file, "index", index)
        assert 4 not in _applied_versions(db_file)
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"

        # 重试：正常完成
        async with engine.connect() as conn:
            report = await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)
        assert report["ddl_executed"] is True
        assert report["signature"]["ok"] is True
        assert _object_exists(db_file, "table", CFD_TABLE)
        for index in CFD_INDEXES:
            assert _object_exists(db_file, "index", index)
        assert 4 in _applied_versions(db_file)
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


async def test_failure_after_signature_check_rolls_back(tmp_path, monkeypatch):
    """F3：建表 + 索引 + 签名校验全部通过后失败 → 仍须整体回滚。"""
    engine, db_file, _ = await _make_engine(tmp_path, "f3")
    try:
        await _prepare_stock_db(engine)
        original = migrate.verify_signatures
        calls = {"n": 0}

        async def wrapper(conn, statements, *, strict_nullable=False):
            report = await original(conn, statements, strict_nullable=strict_nullable)
            calls["n"] += 1
            if calls["n"] == 2:  # 第 2 次即事务内 DDL 后的签名校验
                raise RuntimeError("injected failure after signature check")
            return report

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                m.setattr(migrate, "verify_signatures", wrapper)
                with pytest.raises(migrate.MigrationStepFailure):
                    await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        assert calls["n"] == 2
        assert not _object_exists(db_file, "table", CFD_TABLE)
        for index in CFD_INDEXES:
            assert not _object_exists(db_file, "index", index)
        assert 4 not in _applied_versions(db_file)
    finally:
        await engine.dispose()


async def test_failure_during_version_record_rolls_back(tmp_path, monkeypatch):
    """F4：建表 / 索引 / 签名都 OK，写版本记录时失败 → 回滚 DDL。"""
    engine, db_file, _ = await _make_engine(tmp_path, "f4")
    try:
        await _prepare_stock_db(engine)

        async def boom(conn, version, name):
            raise RuntimeError("injected failure during record_version")

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                m.setattr(migrate, "record_version", boom)
                with pytest.raises(migrate.MigrationStepFailure):
                    await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        assert not _object_exists(db_file, "table", CFD_TABLE)
        assert 4 not in _applied_versions(db_file)
    finally:
        await engine.dispose()


async def test_failure_at_commit_rolls_back(tmp_path, monkeypatch):
    """F5：COMMIT 前失败 → DDL 与版本记录都不得残留。"""
    engine, db_file, _ = await _make_engine(tmp_path, "f5")
    try:
        await _prepare_stock_db(engine)

        async def boom(self):
            raise RuntimeError("injected failure at commit")

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                m.setattr(migrate.ExplicitTransaction, "commit", boom)
                with pytest.raises(migrate.MigrationStepFailure):
                    await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        assert not _object_exists(db_file, "table", CFD_TABLE)
        for index in CFD_INDEXES:
            assert not _object_exists(db_file, "index", index)
        assert 4 not in _applied_versions(db_file)
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# C. 预检 / 幂等
# ---------------------------------------------------------------------------
async def test_precheck_skips_ddl_when_signature_matches(tmp_path, monkeypatch):
    engine, db_file, _ = await _make_engine(tmp_path, "precheck")
    try:
        await _prepare_stock_db(engine, create_cfd=True)
        executed = {"n": 0}
        original = migrate._execute_migration_statement

        async def counting(conn, statement):
            executed["n"] += 1
            await original(conn, statement)

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                m.setattr(migrate, "_execute_migration_statement", counting)
                report = await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        assert executed["n"] == 0
        assert report["ddl_executed"] is False
        assert report["precheck_skipped_ddl"] is True
        assert report["signature"]["ok"] is True
        assert 4 in _applied_versions(db_file)
    finally:
        await engine.dispose()


async def test_existing_object_with_mismatched_signature_fails_closed(tmp_path):
    """已有对象但列结构不符 → 失败关闭，不写版本，不自动重建。"""
    engine, db_file, _ = await _make_engine(tmp_path, "mismatch")
    try:
        await _prepare_stock_db(engine)
        # 手工建一张「同名但缺列」的表：DDL 的 IF NOT EXISTS 无法补齐列差异
        async with engine.connect() as conn:
            async with migrate.ExplicitTransaction(conn) as tx:
                await conn.exec_driver_sql(MISMATCH_TABLE_SQL)
                await tx.commit()
        assert _object_exists(db_file, "table", CFD_TABLE)

        async with engine.connect() as conn:
            with pytest.raises(migrate.SchemaSignatureMismatchError) as excinfo:
                await migrate.apply_step_atomically(conn, 4, "v4", V4_STATEMENTS)

        report = excinfo.value.report
        assert report["ok"] is False
        assert report["hard_diffs"], "列结构不符必须计入 hard_diffs"
        # 版本未写
        assert 4 not in _applied_versions(db_file)
        # 表未被自动重建：仍是缺列的伪表（说明没有 DROP/CREATE 兜底）
        columns = {row[1] for row in _query(db_file, "PRAGMA table_info(chapter_fact_dependencies)")}
        assert columns == {"id", "chapter_id"}
    finally:
        await engine.dispose()


async def test_rerun_after_success_is_noop(tmp_path):
    engine, db_file, url = await _make_engine(tmp_path, "noop")
    try:
        await _prepare_stock_db(engine)
        async with engine.connect() as conn:
            first = await migrate.run_migrations(conn, url)
        assert first["status"] == "upgraded"
        assert first["applied_steps"] == [4, 5, 6, 7, 8, 9, 10]
        assert first["backups"], "存量升级必须产生备份"

        async with engine.connect() as conn:
            second = await migrate.run_migrations(conn, url)
        assert second["status"] == "noop"
        assert second["applied_steps"] == []
        assert second["backups"] == []
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# D. 版本路径：空库 / v3 存量 / v5 不执行
# ---------------------------------------------------------------------------
async def test_empty_db_initializes_to_v4(tmp_path):
    engine, db_file, url = await _make_engine(tmp_path, "empty")

    async def init_callback(conn):
        # D9：phase19 在 create_all 之前
        await ensure_phase19_schema(conn)
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with engine.connect() as conn:
            result = await migrate.run_migrations(conn, url, init_callback=init_callback)

        assert result["status"] == "initialized"
        # 2026-11-12：SCHEMA_VERSION=7（v5 索引增量 + v6 归属/消费列 + v7 规划任务租约/幂等）
        assert result["applied_steps"] == [2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert [row["version"] for row in result["applied"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert _applied_versions(db_file) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert result["signatures"]["4"]["ok"] is True

        # cfd 由 phase19 建出，签名与 v4 目标一致
        async with engine.connect() as conn:
            signature = await migrate.read_table_signature(conn, CFD_TABLE)
        expected = migrate.build_expected_signatures(V4_STATEMENTS)[CFD_TABLE]
        assert [c["name"] for c in signature["columns"]] == [c["name"] for c in expected["columns"]]
        assert sorted(i["name"] for i in signature["indexes"]) == sorted(
            i["name"] for i in expected["indexes"]
        )
        assert signature["foreign_keys"] == []

        # create_all 之后 v4 走预检跳过 DDL（不覆盖已有表）
        assert result["signatures"]["4"]["all_present"] is True
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


async def test_v3_stock_db_executes_v4_ddl_only(tmp_path, monkeypatch):
    engine, db_file, url = await _make_engine(tmp_path, "v3stock")
    v5_calls = {"n": 0}
    original = migrate._execute_migration_statement

    async def counting(conn, statement):
        # 迁移框架可能对语句做 strip/切分，按内容归一化后归属到 v5 步骤
        if statement.strip() in {s.strip() for s in migrate.MIGRATION_STEPS[5][1]}:
            v5_calls["n"] += 1
        await original(conn, statement)

    try:
        await _prepare_stock_db(engine, create_cfd=False)
        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                m.setattr(migrate, "_execute_migration_statement", counting)
                result = await migrate.run_migrations(conn, url)

        assert result["status"] == "upgraded"
        assert result["applied_steps"] == [4, 5, 6, 7, 8, 9, 10]
        assert _object_exists(db_file, "table", CFD_TABLE)
        # v5 = 1 条兜底建表（IF NOT EXISTS）+ 3 条索引增量。
        # 目标表已存在且签名一致时框架预检后跳过 DDL（skip_ddl，计数 0）；
        # 表缺失时才全量执行（计数 4）。两种路径都须保证终态：表与索引实际存在。
        assert 5 in _applied_versions(db_file)
        assert v5_calls["n"] in (0, 4), f"v5 调用计数异常：{v5_calls['n']}"
        assert _object_exists(db_file, "table", "consistency_review")
        for idx_name in (
            "ix_consistency_review_project_id",
            "ix_consistency_review_chapter_id",
            "ix_consistency_review_task_id",
        ):
            assert _object_exists(db_file, "index", idx_name), f"缺少索引 {idx_name}"
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


async def test_migration_steps_above_schema_version_registered_but_skipped(
    tmp_path, monkeypatch
):
    # 高于 SCHEMA_VERSION 的步骤必须被登记但跳过：临时注册一个 v11 验证
    # （2026-11-17 批次 5 起真实最高版本为 v10）
    monkeypatch.setitem(
        migrate.MIGRATION_STEPS,
        11,
        ("synthetic-v11", ["CREATE TABLE IF NOT EXISTS _synthetic_v11 (id INTEGER)"]),
    )
    assert migrate.SCHEMA_VERSION == 10
    assert sorted(migrate.MIGRATION_STEPS) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert 10 in migrate.MIGRATION_STEPS and 11 in migrate.MIGRATION_STEPS

    engine, db_file, url = await _make_engine(tmp_path, "skip11")

    async def init_callback(conn):
        await ensure_phase19_schema(conn)
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with engine.connect() as conn:
            result = await migrate.run_migrations(conn, url, init_callback=init_callback)
        assert result["applied_steps"] == [2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert 11 not in _applied_versions(db_file)
        assert not _object_exists(db_file, "table", "_synthetic_v11")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# E. 备份 API（D8）
# ---------------------------------------------------------------------------
async def test_backup_returns_observable_result(tmp_path):
    engine, db_file, _ = await _make_engine(tmp_path, "bk")
    try:
        await _prepare_stock_db(engine)
        result = migrate.backup_database(f"sqlite+aiosqlite:///{pathlib.Path(db_file).as_posix()}")

        assert result is not None
        for field in (
            "source_path",
            "backup_path",
            "source_size",
            "backup_size",
            "backup_sha256",
            "integrity_check",
            "quick_check",
            "created_at",
        ):
            assert field in result, f"备份结果缺少字段 {field}"

        assert pathlib.Path(result["backup_path"]).exists()
        assert result["source_path"] == db_file
        assert result["backup_size"] > 0
        assert result["integrity_check"] == "ok"
        assert result["quick_check"] == "ok"
        assert len(result["backup_sha256"]) == 64
        assert result["backup_sha256"] == _sha256_file(pathlib.Path(result["backup_path"]))
        assert result["method"] == "sqlite3.Connection.backup"
    finally:
        await engine.dispose()


async def test_backups_are_unique_and_independent(tmp_path):
    engine, db_file, _ = await _make_engine(tmp_path, "bkuniq")
    try:
        await _prepare_stock_db(engine)
        url = f"sqlite+aiosqlite:///{pathlib.Path(db_file).as_posix()}"
        first = migrate.backup_database(url)
        second = migrate.backup_database(url)

        assert first is not None and second is not None
        assert first["backup_path"] != second["backup_path"]
        for item in (first, second):
            assert pathlib.Path(item["backup_path"]).exists()
            assert item["integrity_check"] == "ok"
    finally:
        await engine.dispose()


async def test_backup_restores_to_isolated_copy_with_integrity(tmp_path):
    engine, db_file, _ = await _make_engine(tmp_path, "bkrestore")
    try:
        await _prepare_stock_db(engine)
        plan = _query(db_file, "SELECT count(*) FROM sqlite_master WHERE type='table'")[0][0]
        backup = migrate.backup_database(f"sqlite+aiosqlite:///{pathlib.Path(db_file).as_posix()}")
        assert backup is not None

        restore_dir = tmp_path / "restore"
        restore_dir.mkdir()
        restored = restore_dir / "restored.db"
        restored.write_bytes(pathlib.Path(backup["backup_path"]).read_bytes())

        conn = sqlite3.connect(str(restored))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            versions = [
                row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
        finally:
            conn.close()
        assert tables == plan
        assert versions == [1, 2, 3]
    finally:
        await engine.dispose()


async def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(migrate.MigrationBackupError):
        migrate.create_sqlite_backup(str(tmp_path / "not-exist.db"))


async def test_run_migrations_collects_every_backup(tmp_path):
    engine, db_file, url = await _make_engine(tmp_path, "bkmulti")
    try:
        await _prepare_stock_db(engine, create_cfd=False)
        async with engine.connect() as conn:
            result = await migrate.run_migrations(conn, url)

        assert len(result["backups"]) >= 1, "v4 升级必须留下备份"
        paths = {item["backup_path"] for item in result["backups"]}
        assert len(paths) == len(result["backups"]), "备份路径不得重复覆盖"
        for item in result["backups"]:
            assert item["integrity_check"] == "ok"
            assert pathlib.Path(item["backup_path"]).exists()
        # 兼容字段指向首个备份，但完整列表不丢
        assert result["backup"]["backup_path"] == result["backups"][0]["backup_path"]
    finally:
        await engine.dispose()


async def test_multi_step_upgrade_keeps_all_backups(tmp_path):
    """一次升级跨越多个版本（v1 → v4）时，每个变更点各留一份备份，互不覆盖。"""
    engine, db_file, url = await _make_engine(tmp_path, "bkmulti2")

    async def init_callback(conn):
        await ensure_phase19_schema(conn)
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with engine.connect() as conn:
            result = await migrate.run_migrations(conn, url, init_callback=init_callback)

        assert result["applied_steps"] == [2, 3, 4, 5, 6, 7, 8, 9, 10]
        paths = [item["backup_path"] for item in result["backups"]]
        assert len(paths) == len(set(paths))
        assert len(paths) >= 5, f"基线 + v2..v6 各自应有备份，实际 {paths}"
        for item in result["backups"]:
            assert item["integrity_check"] == "ok"
            assert pathlib.Path(item["backup_path"]).exists()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# F. v2 回归保护（D11：不改语义，只证明新网关不破坏它）
# ---------------------------------------------------------------------------
async def test_v2_applies_through_new_gateway(tmp_path):
    engine, db_file, url = await _make_engine(tmp_path, "v2apply")
    try:
        await _prepare_stock_db(engine, versions=())
        async with engine.connect() as conn:
            report = await migrate.apply_step_atomically(conn, 2, "v2", V2_STATEMENTS)

        # v2 无建表语句 → 签名不可校验，不得因此阻断
        assert report["signature"]["verifiable"] is False
        assert report["signature"]["ok"] is True
        assert report["ddl_executed"] is True
        assert 2 in _applied_versions(db_file)
        columns = {row[1] for row in _query(db_file, "PRAGMA table_info(ai_tasks)")}
        assert "model_name" in columns
    finally:
        await engine.dispose()


async def test_v2_rerun_is_idempotent(tmp_path):
    engine, db_file, url = await _make_engine(tmp_path, "v2idem")
    try:
        await _prepare_stock_db(engine)
        async with engine.connect() as conn:
            first = await migrate.run_migrations(conn, url)
        # v1/v2/v3 已存在，本次应补 v4 / v5 / v6
        assert first["status"] == "upgraded"
        assert first["applied_steps"] == [4, 5, 6, 7, 8, 9, 10]

        async with engine.connect() as conn:
            second = await migrate.run_migrations(conn, url)
        assert second["status"] == "noop"
        assert second["applied_steps"] == []
        assert second["backups"] == []
        assert _applied_versions(db_file) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    finally:
        await engine.dispose()


async def test_v2_duplicate_apply_does_not_corrupt_version_record(tmp_path):
    """v2 二次应用：ADD COLUMN 幂等跳过，重复版本记录冲突后整体回滚，版本表不变。"""
    engine, db_file, url = await _make_engine(tmp_path, "v2dup")
    try:
        await _prepare_stock_db(engine, versions=(1, 2))
        async with engine.connect() as conn:
            with pytest.raises(migrate.MigrationStepFailure):
                await migrate.apply_step_atomically(conn, 2, "v2", V2_STATEMENTS)
        assert _applied_versions(db_file) == [1, 2]
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


async def test_v2_failure_rolls_back_without_residue(tmp_path, monkeypatch):
    """v2 在 DDL 中途失败：以「是否残留新列」为唯一判据（D11 停止条件探测）。"""
    engine, db_file, _ = await _make_engine(tmp_path, "v2fail")
    try:
        await _prepare_stock_db(engine, versions=(1,))
        before = {row[1] for row in _query(db_file, "PRAGMA table_info(ai_tasks)")}

        async with engine.connect() as conn:
            with monkeypatch.context() as m:
                _inject_statement_failure(m, fail_after=4)
                with pytest.raises(migrate.MigrationStepFailure):
                    await migrate.apply_step_atomically(conn, 2, "v2", V2_STATEMENTS)

        after = {row[1] for row in _query(db_file, "PRAGMA table_info(ai_tasks)")}
        assert 2 not in _applied_versions(db_file)
        # 显式事务网关下 ALTER TABLE ADD COLUMN 应可回滚
        assert after == before, f"v2 失败后残留新列：{sorted(after - before)}"
        assert _query(db_file, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# G. 正式库基线留档（2026-11-15 决定：不再对正式库做「内容不得变化」的指纹强断言；
#    正式库会随业务操作（如恢复孤儿项目）合法变化，指纹测试已移除）
# ---------------------------------------------------------------------------


async def test_formal_database_baseline_is_kept_distinct_from_current():
    """迁移前基线与当前状态必须保持可区分，禁止用「改基线」方式让指纹测试变绿。"""
    assert BASELINE_SHA256 != FORMAL_SHA256
    assert BASELINE_MTIME != FORMAL_MTIME_NOT_BEFORE
    assert BASELINE_SIZE == FORMAL_SIZE  # v4 只新增表对象，未改变文件大小，属已知事实
