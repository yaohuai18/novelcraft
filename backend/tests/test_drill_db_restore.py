from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "drill-db-restore.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("drill_db_restore", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["drill_db_restore"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    backup_root = tmp_path / "backup-root" / "database"
    backup_root.mkdir(parents=True)
    drills = tmp_path / "drills"
    drills.mkdir()
    monkeypatch.setattr(mod, "BACKUP_ROOT", backup_root)
    return {"backup_root": backup_root, "temp": drills, "tmp": tmp_path}


def _make_snapshot(root: Path, name: str = "novelcraft-db-20260101-000000-abcdef0", manifest_overrides=None, with_db=True):
    snapshot = root / name
    snapshot.mkdir()
    db = snapshot / "novelcraft.db"
    if with_db:
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE drill_probe (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO drill_probe (value) VALUES ('x')")
        connection.commit()
        connection.close()
    manifest = {
        "created_at": "2026-01-01T00:00:00+08:00",
        "head": "abcdef0123456789abcdef0123456789abcdef01",
        "head_short": "abcdef0",
        "source_path": "backend/novelcraft.db",
        "snapshot_file": "novelcraft.db",
        "snapshot_size": db.stat().st_size if with_db else 1,
        "method": "sqlite-backup",
        "service_stopped": False,
        "integrity_check": "ok",
        "quick_check": "ok",
        "tool_version": "1.0.0",
        "label": "test",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
        manifest = {key: value for key, value in manifest.items() if value is not None}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return snapshot


def _argv(env, snapshot, *extra):
    return ["--snapshot-dir", str(snapshot), "--temp-root", str(env["temp"]), *extra]


def _make_dir_link(target: Path, link: Path) -> None:
    """Create a directory symlink or junction; skip when the platform refuses."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if not sys.platform.startswith("win"):
        pytest.skip("directory link creation is not permitted in this environment")
    if shutil.which("cmd") is None:
        pytest.skip("cmd is not available to create a junction in this environment")
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        pytest.skip(f"cmd could not be executed to create a junction: {type(exc).__name__}")
    if result.returncode != 0 or not link.exists():
        pytest.skip("link creation is not permitted in this environment")


def _remove_dir_link(link: Path) -> None:
    try:
        os.unlink(link)
    except OSError:
        try:
            os.rmdir(link)
        except OSError:
            pass


def test_help_returns_zero(capsys):
    assert mod.main(["--help"]) == 0
    out = capsys.readouterr().out.lower()
    assert "usage" in out
    assert "--keep-on-failure" not in out


def test_missing_snapshot_dir_returns_two(capsys):
    assert mod.main([]) == 2
    assert "snapshot-dir" in capsys.readouterr().err


def test_snapshot_outside_backup_root_returns_three(env, tmp_path):
    outside = tmp_path / "outside" / "novelcraft-db-20260101-000000-abcdef0"
    outside.mkdir(parents=True)
    assert mod.main(_argv(env, outside)) == 3


def test_unc_and_device_paths_are_rejected(env):
    assert mod.main(["--snapshot-dir", r"\\server\share\snap", "--temp-root", str(env["temp"])]) == 3
    assert mod.main(["--snapshot-dir", r"\\.\PhysicalDrive0", "--temp-root", str(env["temp"])]) == 3


def test_invalid_directory_name_returns_three(env):
    snapshot = _make_snapshot(env["backup_root"], name="novelcraft-db-bad-name")
    assert mod.main(_argv(env, snapshot)) == 3


def test_missing_directory_returns_three(env):
    missing = env["backup_root"] / "novelcraft-db-20260101-000000-abcdef0"
    assert mod.main(_argv(env, missing)) == 3


def test_missing_manifest_returns_three(env):
    snapshot = _make_snapshot(env["backup_root"])
    (snapshot / "manifest.json").unlink()
    assert mod.main(_argv(env, snapshot)) == 3


def test_corrupt_manifest_returns_three(env):
    snapshot = _make_snapshot(env["backup_root"])
    (snapshot / "manifest.json").write_text("{not json", encoding="utf-8")
    assert mod.main(_argv(env, snapshot)) == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"snapshot_file": None},
        {"snapshot_size": "big"},
        {"method": None},
        {"integrity_check": None},
        {"quick_check": None},
        {"created_at": 123},
    ],
)
def test_manifest_field_problems_return_three(env, overrides):
    snapshot = _make_snapshot(env["backup_root"], manifest_overrides=overrides)
    assert mod.main(_argv(env, snapshot)) == 3


def test_snapshot_file_must_be_novelcraft_db(env):
    snapshot = _make_snapshot(env["backup_root"], manifest_overrides={"snapshot_file": "other.db"})
    assert mod.main(_argv(env, snapshot)) == 3


@pytest.mark.parametrize("size", [0, -5, "154", True])
def test_snapshot_size_must_be_positive_integer(env, size):
    snapshot = _make_snapshot(env["backup_root"], manifest_overrides={"snapshot_size": size})
    assert mod.main(_argv(env, snapshot)) == 3


def test_method_must_be_sqlite_backup(env):
    snapshot = _make_snapshot(env["backup_root"], manifest_overrides={"method": "copy"})
    assert mod.main(_argv(env, snapshot)) == 3


def test_extra_entries_or_sidecars_are_rejected(env):
    snapshot = _make_snapshot(env["backup_root"])
    (snapshot / "notes.txt").write_text("x", encoding="utf-8")
    assert mod.main(_argv(env, snapshot)) == 3

    snapshot2 = _make_snapshot(env["backup_root"], name="novelcraft-db-20260102-000000-abcdef1")
    (snapshot2 / "novelcraft.db-wal").write_bytes(b"")
    assert mod.main(_argv(env, snapshot2)) == 3


def test_dry_run_creates_nothing_and_opens_nothing(env, monkeypatch, capsys):
    snapshot = _make_snapshot(env["backup_root"])

    def _forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not copy or open the database")

    monkeypatch.setattr(mod, "_copy_snapshot", _forbidden)
    monkeypatch.setattr(mod, "_open_readonly", _forbidden)
    assert mod.main(_argv(env, snapshot, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "DRY_RUN_OK" in out
    assert list(env["temp"].iterdir()) == []


def test_synthetic_snapshot_drill_succeeds(env, capsys):
    snapshot = _make_snapshot(env["backup_root"])
    assert mod.main(_argv(env, snapshot)) == 0
    out = capsys.readouterr().out
    assert "DRILL_OK" in out
    assert "INTEGRITY_CHECK=ok" in out
    assert "QUICK_CHECK=ok" in out


def test_success_runs_both_checks_and_cleans(env, monkeypatch):
    snapshot = _make_snapshot(env["backup_root"])
    calls = []
    original_open = mod._open_readonly
    original_checks = mod._run_checks

    def _open(path):
        calls.append("open")
        return original_open(path)

    def _checks(connection):
        calls.append("checks")
        return original_checks(connection)

    monkeypatch.setattr(mod, "_open_readonly", _open)
    monkeypatch.setattr(mod, "_run_checks", _checks)
    code = mod.main(_argv(env, snapshot))
    assert code == 0
    assert calls == ["open", "checks"]
    assert list(env["temp"].iterdir()) == []


def test_success_leaves_no_temp_directory(env):
    snapshot = _make_snapshot(env["backup_root"])
    assert mod.main(_argv(env, snapshot)) == 0
    assert list(env["temp"].iterdir()) == []


def test_size_mismatch_returns_five_and_keeps_scene(env, monkeypatch, capsys):
    snapshot = _make_snapshot(env["backup_root"])

    def _copy(source, target):
        target.write_bytes(b"short")

    monkeypatch.setattr(mod, "_copy_snapshot", _copy)
    assert mod.main(_argv(env, snapshot)) == 5
    drills = list(env["temp"].iterdir())
    assert len(drills) == 1
    assert "size_mismatch" in capsys.readouterr().out


def test_open_failure_returns_six_and_keeps_scene(env, monkeypatch):
    snapshot = _make_snapshot(env["backup_root"])

    def _open(path):
        raise mod.ToolError(mod.EXIT_CHECK_FAILED, "open_failed", "只读打开副本失败: OperationalError")

    monkeypatch.setattr(mod, "_open_readonly", _open)
    assert mod.main(_argv(env, snapshot)) == 6
    assert len(list(env["temp"].iterdir())) == 1


def test_non_ok_check_returns_six_and_keeps_scene(env, monkeypatch):
    snapshot = _make_snapshot(env["backup_root"])

    class FakeConnection:
        def execute(self, statement):
            return self

        def fetchone(self):
            return ("corrupt",)

        def close(self):
            pass

    monkeypatch.setattr(mod, "_open_readonly", lambda path: FakeConnection())
    assert mod.main(_argv(env, snapshot)) == 6
    assert len(list(env["temp"].iterdir())) == 1


def test_cleanup_failure_returns_seven(env, monkeypatch):
    snapshot = _make_snapshot(env["backup_root"])
    monkeypatch.setattr(mod, "_remove_drill", lambda drill, temp_root: False)
    assert mod.main(_argv(env, snapshot)) == 7
    assert len(list(env["temp"].iterdir())) == 1


def test_source_junction_is_rejected(env, monkeypatch, capsys):
    snapshot = _make_snapshot(env["backup_root"])
    link = env["backup_root"] / "novelcraft-db-20260103-000000-ccccccc"
    _make_dir_link(snapshot, link)

    def _forbidden(*args, **kwargs):
        raise AssertionError("a linked source must be rejected before copying or opening")

    monkeypatch.setattr(mod, "_copy_snapshot", _forbidden)
    monkeypatch.setattr(mod, "_open_readonly", _forbidden)
    try:
        assert mod.main(_argv(env, link)) == 3
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "CATEGORY=snapshot_link" in output
        assert str(link) not in output
        assert str(snapshot) not in output
        assert str(env["backup_root"]) not in output
        assert str(env["temp"]) not in output
        assert list(env["temp"].iterdir()) == []
    finally:
        _remove_dir_link(link)


def test_source_parent_junction_is_rejected(env, monkeypatch, capsys):
    snapshot = _make_snapshot(env["backup_root"])
    linked_root = env["tmp"] / "linked-backup"
    _make_dir_link(env["backup_root"], linked_root)

    def _forbidden(*args, **kwargs):
        raise AssertionError("a linked parent must be rejected before copying or opening")

    monkeypatch.setattr(mod, "_copy_snapshot", _forbidden)
    monkeypatch.setattr(mod, "_open_readonly", _forbidden)
    try:
        assert mod.main(_argv(env, linked_root / snapshot.name)) == 3
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "CATEGORY=snapshot_link" in output
        assert str(linked_root) not in output
        assert str(snapshot) not in output
        assert str(env["backup_root"]) not in output
        assert str(env["temp"]) not in output
        assert list(env["temp"].iterdir()) == []
    finally:
        _remove_dir_link(linked_root)


def test_temp_root_junction_is_rejected(env, capsys):
    snapshot = _make_snapshot(env["backup_root"])
    real_temp = env["tmp"] / "real-drills"
    real_temp.mkdir()
    linked_temp = env["tmp"] / "linked-drills"
    _make_dir_link(real_temp, linked_temp)
    try:
        assert mod.main(["--snapshot-dir", str(snapshot), "--temp-root", str(linked_temp)]) == 3
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "CATEGORY=temp_root_link" in output
        assert str(linked_temp) not in output
        assert str(real_temp) not in output
        assert str(snapshot) not in output
        assert list(real_temp.iterdir()) == []
    finally:
        _remove_dir_link(linked_temp)


@pytest.mark.parametrize(
    "argument",
    ["--target-database", "--restore", "--replace", "--delete", "--clean", "--source-database", "--keep-on-failure"],
)
def test_restore_style_arguments_are_unsupported(env, argument):
    snapshot = _make_snapshot(env["backup_root"])
    assert mod.main(_argv(env, snapshot, argument, "x")) == 2


def test_source_has_no_restore_or_destructive_api():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--target-database" not in text
    assert "shutil.move" not in text
    assert "os.replace" not in text
    assert "os.remove" not in text
    assert "DROP TABLE" not in text.upper()
    assert ".env" not in text


def test_output_contains_no_sensitive_content(env, capsys):
    snapshot = _make_snapshot(env["backup_root"])
    assert mod.main(_argv(env, snapshot)) == 0
    out = capsys.readouterr().out
    assert str(env["tmp"]) not in out
    assert str(env["backup_root"]) not in out
    assert "backup_sha256" not in out
    assert "SELECT" not in out.upper()
    assert "PASSWORD" not in out.upper()
    assert "DRILL_OK" in out


def test_fixtures_are_not_modified(env):
    snapshot = _make_snapshot(env["backup_root"])

    def _digest():
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(snapshot.iterdir())
        }

    before = _digest()
    assert mod.main(_argv(env, snapshot, "--dry-run")) == 0
    assert _digest() == before
    assert mod.main(_argv(env, snapshot)) == 0
    assert _digest() == before


def test_module_never_touches_real_snapshot_root(env):
    assert mod.BACKUP_ROOT == env["backup_root"]
    text = SCRIPT.read_text(encoding="utf-8")
    assert "report-db-snapshots" not in text
    assert "NovelCraft-backups" in text  # default constant only, never read in tests
