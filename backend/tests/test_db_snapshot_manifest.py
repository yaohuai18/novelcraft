# -*- coding: utf-8 -*-
"""``scripts/db-snapshot.py`` 的安全边界与 manifest 契约测试。

约束（与任务书一致）：
* 只使用 ``tmp_path`` 下的临时 SQLite 数据库与仓库外临时输出目录；
* 不读取真实 backend 配置文件、不打开正式数据库、
  不连接运行中的服务、不修改正式数据库；
* 不输出任何数据库内容、SQL 或连接串。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "db-snapshot.py"
HEAD = "f23cbc2"


def _load_tool_module():
    spec = importlib.util.spec_from_file_location("db_snapshot_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cwd) if cwd else None,
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    db = repo / "backend" / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO note (body) VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()
    out = tmp_path / "out"
    return repo, db, out


def _snapshot_dirs(out: Path) -> list[Path]:
    return sorted(p for p in out.glob("novelcraft-db-*") if p.is_dir())


def test_dry_run_does_not_create_output_or_read_database(tmp_path: Path) -> None:
    repo, _db, out = _prepare(tmp_path)
    # 故意放一个不是 SQLite 的文件：dry-run 若读取数据库就会失败。
    fake = repo / "backend" / "fake.db"
    fake.write_text("NOT A DATABASE", encoding="utf-8")

    proc = _run("--repo", repo, "--database", fake, "--out-dir", out, "--dry-run", "--head", HEAD)

    assert proc.returncode == 0, proc.stderr
    assert "RESULT=DRY_RUN_OK" in proc.stdout
    assert not out.exists()


def test_snapshot_is_created_outside_repository(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)

    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)

    assert proc.returncode == 0, proc.stderr
    dirs = _snapshot_dirs(out)
    assert len(dirs) == 1
    snapshot_dir = dirs[0]
    assert (snapshot_dir / "novelcraft.db").is_file()
    assert (snapshot_dir / "manifest.json").is_file()
    assert not snapshot_dir.is_relative_to(repo)


def test_missing_head_in_non_git_repo_fails(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)

    proc = _run("--repo", repo, "--database", db, "--out-dir", out)

    assert proc.returncode == 3, proc.stderr
    assert "HEAD" in proc.stderr
    assert not out.exists()
    for token in ("CREATE TABLE", "INSERT INTO", "sqlite+aiosqlite", "password"):
        assert token not in proc.stderr


def test_existing_target_is_refused_and_preserved(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)
    args = (
        "--repo", repo,
        "--database", db,
        "--out-dir", out,
        "--stamp", "20260101-000001",
        "--head", HEAD,
    )

    first = _run(*args)
    assert first.returncode == 0, first.stderr

    snapshot = _snapshot_dirs(out)[0] / "novelcraft.db"
    manifest = _snapshot_dirs(out)[0] / "manifest.json"
    before_bytes = snapshot.read_bytes()
    before_manifest = manifest.read_text(encoding="utf-8")
    before_size = snapshot.stat().st_size

    second = _run(*args)

    assert second.returncode == 4, second.stderr
    assert "拒绝覆盖" in second.stderr
    assert snapshot.is_file()
    assert snapshot.stat().st_size == before_size
    # 只在内存中比较，不打印内容或哈希。
    assert snapshot.read_bytes() == before_bytes
    assert manifest.read_text(encoding="utf-8") == before_manifest


def test_manifest_fields_match_contract(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)
    proc = _run(
        "--repo", repo,
        "--database", db,
        "--out-dir", out,
        "--stamp", "20260101-000002",
        "--head", HEAD,
        "--label", "unit-test",
    )
    assert proc.returncode == 0, proc.stderr

    manifest = json.loads((_snapshot_dirs(out)[0] / "manifest.json").read_text(encoding="utf-8"))
    required = {
        "created_at",
        "head",
        "head_short",
        "source_path",
        "snapshot_file",
        "snapshot_size",
        "method",
        "service_stopped",
        "service_pid_before",
        "service_pid_after",
        "integrity_check",
        "quick_check",
        "backup_sha256",
        "tool_version",
    }
    assert required <= manifest.keys()
    assert manifest["source_path"] == "backend/test.db"
    assert "\\" not in manifest["source_path"]
    assert manifest["method"] == "sqlite-backup"
    assert manifest["service_stopped"] is False
    assert manifest["service_pid_before"] is None
    assert manifest["service_pid_after"] is None
    assert manifest["head"] == HEAD
    assert manifest["head_short"] == HEAD
    assert manifest["label"] == "unit-test"
    assert manifest["snapshot_size"] > 0
    assert len(manifest["backup_sha256"]) == 64


def test_snapshot_size_matches_disk(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)
    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)
    assert proc.returncode == 0, proc.stderr

    snapshot_dir = _snapshot_dirs(out)[0]
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_size"] == (snapshot_dir / "novelcraft.db").stat().st_size


def test_snapshot_integrity_checks_pass(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)
    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)
    assert proc.returncode == 0, proc.stderr

    manifest = json.loads((_snapshot_dirs(out)[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["integrity_check"] == "ok"
    assert manifest["quick_check"] == "ok"


def test_successful_snapshot_leaves_no_temp_sidecars(tmp_path: Path) -> None:
    """成功路径必须在原子替换前关闭连接并清掉临时库的 -wal/-shm 边车。"""
    repo, db, out = _prepare(tmp_path)
    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)
    assert proc.returncode == 0, proc.stderr

    snapshot_dir = _snapshot_dirs(out)[0]
    # 目录里只允许最终快照与 manifest；不得有任何 .tmp-* 残骸。
    assert sorted(p.name for p in snapshot_dir.iterdir()) == ["manifest.json", "novelcraft.db"]
    assert list(out.rglob(".novelcraft.db.tmp-*")) == []
    assert list(out.rglob("*.tmp-*-wal")) == []
    assert list(out.rglob("*.tmp-*-shm")) == []

    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_size"] == (snapshot_dir / "novelcraft.db").stat().st_size
    assert manifest["integrity_check"] == "ok"
    assert manifest["quick_check"] == "ok"


class _TracingConnection:
    """只转发 backup/close 的连接包装，用来记录事件顺序。"""

    def __init__(self, real, name: str, events: list[str]) -> None:
        self._real = real
        self._name = name
        self._events = events

    def backup(self, target, **kwargs):
        self._events.append("backup")
        real_target = target._real if isinstance(target, _TracingConnection) else target
        return self._real.backup(real_target, **kwargs)

    def close(self) -> None:
        self._events.append(f"close:{self._name}")
        return self._real.close()


def test_connections_close_before_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    module = _load_tool_module()
    repo, db, out = _prepare(tmp_path)
    events: list[str] = []

    real_open_source = module._open_source
    real_open_target = module._open_target

    monkeypatch.setattr(
        module, "_open_source",
        lambda path: _TracingConnection(real_open_source(path), "source", events),
    )
    monkeypatch.setattr(
        module, "_open_target",
        lambda path: _TracingConnection(real_open_target(path), "target", events),
    )

    real_replace = module.os.replace

    def _replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", _replace)

    code = module.main([
        "--repo", str(repo),
        "--database", str(db),
        "--out-dir", str(out),
        "--stamp", "20260101-000003",
        "--head", HEAD,
    ])

    assert code == 0
    replace_at = events.index("replace")
    assert events.index("backup") < events.index("close:target") < replace_at
    assert events.index("close:source") < replace_at


def test_sidecar_cleanup_failure_is_reported(tmp_path: Path) -> None:
    """边车清理失败必须抛 SnapshotError(EXIT_BACKUP)，由调用方转成非零退出码。"""
    module = _load_tool_module()
    tmp = tmp_path / "snap.db"
    tmp.write_bytes(b"")
    blocker = tmp.with_name(tmp.name + "-shm")
    blocker.mkdir()  # 目录无法 unlink，制造真实失败

    with pytest.raises(module.SnapshotError) as info:
        module._remove_sidecars(tmp)

    assert info.value.code == module.EXIT_BACKUP
    assert "边车" in info.value.message


def test_source_outside_repository_is_refused(tmp_path: Path) -> None:
    repo, _db, out = _prepare(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    db = outside / "outside.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)

    assert proc.returncode == 3, proc.stderr
    assert "不在仓库内" in proc.stderr
    assert not out.exists()


def test_output_directory_inside_repository_is_refused(tmp_path: Path) -> None:
    repo, db, _out = _prepare(tmp_path)
    inside = repo / "backups"

    proc = _run("--repo", repo, "--database", db, "--out-dir", inside, "--head", HEAD)

    assert proc.returncode == 3, proc.stderr
    assert not inside.exists()


def test_non_sqlite_source_is_refused(tmp_path: Path) -> None:
    repo, _db, out = _prepare(tmp_path)
    fake = repo / "backend" / "fake.db"
    fake.write_text("NOT A DATABASE", encoding="utf-8")

    proc = _run("--repo", repo, "--database", fake, "--out-dir", out, "--head", HEAD)

    assert proc.returncode == 3, proc.stderr
    assert "不是 SQLite" in proc.stderr
    assert not out.exists()


def test_remote_paths_are_rejected_without_network_access(tmp_path: Path) -> None:
    module = _load_tool_module()
    remote = (
        r"\\server\share\novelcraft.db",
        "//server/share/novelcraft.db",
        r"\\?\UNC\server\share\novelcraft.db",
        r"\\.\PhysicalDrive0",
    )
    for value in remote:
        assert module._is_remote_path(value) is True, value
    for value in ("C:\\local\\a.db", "backend/novelcraft.db", r"\\?\C:\local\a.db"):
        assert module._is_remote_path(value) is False, value

    repo, _db, out = _prepare(tmp_path)
    proc = _run(
        "--repo", repo,
        "--database", remote[0],
        "--out-dir", out,
        "--head", HEAD,
        "--dry-run",
    )
    assert proc.returncode == 3, proc.stderr
    assert "UNC" in proc.stderr
    assert not out.exists()


def test_source_connection_is_readonly_with_busy_timeout(tmp_path: Path) -> None:
    module = _load_tool_module()
    _repo, db, _out = _prepare(tmp_path)

    conn = module._open_source(db)
    try:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert busy == module.BUSY_TIMEOUT_MS

    conn = module._open_source(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_fail (id INTEGER)")
    finally:
        conn.close()


def test_backup_parameters_are_bounded(tmp_path: Path) -> None:
    module = _load_tool_module()
    assert module.BACKUP_PAGES > 0
    assert module.BACKUP_MAX_STEPS > 0
    assert module.SOURCE_TIMEOUT_SECONDS >= 10
    assert module.BUSY_TIMEOUT_MS >= 1000
    assert 0 <= module.BACKUP_SLEEP_SECONDS <= 1

    # 行为断言：真实分页 backup 能完成，且副本可独立打开。
    _repo, db, out = _prepare(tmp_path)
    out.mkdir()
    copy = out / "copy.db"
    source = module._open_source(db)
    try:
        target = module._open_target(copy)
        try:
            module._backup_into(source, target)
        finally:
            target.close()
    finally:
        source.close()
    probe = sqlite3.connect(str(copy))
    try:
        assert probe.execute("SELECT count(*) FROM note").fetchone()[0] == 1
    finally:
        probe.close()


def test_failure_cleans_temp_and_partial_snapshot(tmp_path: Path, monkeypatch) -> None:
    module = _load_tool_module()
    repo, db, out = _prepare(tmp_path)
    real_connect = module.sqlite3.connect

    def flaky_connect(target, *args, **kwargs):
        if ".novelcraft.db.tmp-" in str(target):
            raise RuntimeError("simulated backup failure")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", flaky_connect)

    code = module.main([
        "--repo", str(repo),
        "--database", str(db),
        "--out-dir", str(out),
        "--stamp", "20260101-000004",
        "--head", HEAD,
    ])

    assert code == 5
    assert list(out.rglob(".novelcraft.db.tmp-*")) == []
    assert list(out.rglob("novelcraft.db")) == []


def test_integrity_failure_returns_6_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    module = _load_tool_module()
    repo, db, out = _prepare(tmp_path)
    out.mkdir()
    sentinel = out / "keep-me.txt"
    sentinel.write_text("keep", encoding="utf-8")
    # 既有快照（模拟历史批次），失败时必须原样保留、不被删除或覆盖。
    old_dir = out / "novelcraft-db-19990101-000000-aaaaaaa"
    old_dir.mkdir()
    old_snapshot = old_dir / "novelcraft.db"
    old_snapshot.write_bytes(b"OLD-SNAPSHOT")

    monkeypatch.setattr(module, "_integrity", lambda _path: ("corrupt", "ok"))

    code = module.main([
        "--repo", str(repo),
        "--database", str(db),
        "--out-dir", str(out),
        "--stamp", "20260101-000005",
        "--head", HEAD,
    ])

    assert code == 6
    assert list(out.rglob(".novelcraft.db.tmp-*")) == []
    assert list(out.rglob("novelcraft.db")) == [old_snapshot]
    assert list(out.rglob("manifest.json")) == []
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert old_snapshot.read_bytes() == b"OLD-SNAPSHOT"


def _make_dir_link(link: Path, target: Path) -> str:
    """优先 symlink；Windows 无权限时退回 junction；都不行则 skip。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and link.exists():
            return "junction"
    pytest.skip("当前环境不允许创建符号链接/junction")


def test_symlink_output_resolving_inside_repo_is_refused(tmp_path: Path) -> None:
    repo, db, _out = _prepare(tmp_path)
    inside = repo / "backups"
    inside.mkdir()

    link = tmp_path / "link"
    _make_dir_link(link, inside)

    proc = _run("--repo", repo, "--database", db, "--out-dir", link, "--head", HEAD)

    assert proc.returncode == 3, proc.stderr
    assert not any(inside.iterdir())


def test_case_insensitive_output_path_is_normalized(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("仅在 Windows 上验证大小写路径归一")
    repo, db, _out = _prepare(tmp_path)
    inside = repo / "Backups"
    inside.mkdir()

    proc = _run(
        "--repo", repo,
        "--database", db,
        "--out-dir", str(inside).lower(),
        "--head", HEAD,
    )

    assert proc.returncode == 3, proc.stderr
    assert not any(inside.iterdir())


def test_database_argument_is_required_and_env_is_not_read(tmp_path: Path) -> None:
    repo, _db, out = _prepare(tmp_path)

    proc = _run("--repo", repo, "--out-dir", out)

    assert proc.returncode == 2
    assert ".env" in proc.stderr


def test_help_lists_database_option(tmp_path: Path) -> None:
    proc = _run("--help")

    assert proc.returncode == 0
    assert "--database" in proc.stdout
    assert "--dry-run" in proc.stdout


def test_manifest_contains_no_secrets_or_source_hash(tmp_path: Path) -> None:
    repo, db, out = _prepare(tmp_path)
    proc = _run("--repo", repo, "--database", db, "--out-dir", out, "--head", HEAD)
    assert proc.returncode == 0, proc.stderr

    text = (_snapshot_dirs(out)[0] / "manifest.json").read_text(encoding="utf-8")
    forbidden = (
        "DATABASE_URL",
        "sqlite+aiosqlite",
        "password",
        "source_sha256",
        "api_key",
        "CREATE TABLE",
        "INSERT INTO",
    )
    for token in forbidden:
        assert token not in text, f"manifest 不应包含 {token}"
