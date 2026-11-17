"""Tests for scripts/stage-db-snapshot.py.

The tool is intentionally testable in-process: ``main(argv, backup_root=...)`` lets the
tests point the snapshot guard at a temporary backup root, while staging roots still have
to live under the real system TEMP.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "stage-db-snapshot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("stage_db_snapshot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _make_db(path: Path, rows: int = 1) -> int:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        for index in range(rows):
            conn.execute("INSERT INTO t (note) VALUES (?)", (f"row-{index}",))
        conn.commit()
    finally:
        conn.close()
    return path.stat().st_size


def _write_manifest(snapshot_dir: Path, size: int, **overrides) -> None:
    data = {
        "snapshot_file": "novelcraft.db",
        "snapshot_size": size,
        "method": "sqlite-backup",
        "integrity_check": "ok",
        "quick_check": "ok",
        "head": "abcdef1234567890",
    }
    data.update(overrides)
    (snapshot_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _build_snapshot(base: Path, name: str = "novelcraft-db-20260101-000000-abcdef1", **overrides) -> Path:
    backup_root = base / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = backup_root / name
    snapshot_dir.mkdir()
    size = _make_db(snapshot_dir / "novelcraft.db")
    _write_manifest(snapshot_dir, size, **overrides)
    return snapshot_dir


def _build_garbage_snapshot(base: Path, name: str = "novelcraft-db-20260101-000000-abcdef1") -> Path:
    backup_root = base / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = backup_root / name
    snapshot_dir.mkdir()
    payload = b"this is definitely not a sqlite database" * 32
    (snapshot_dir / "novelcraft.db").write_bytes(payload)
    _write_manifest(snapshot_dir, len(payload))
    return snapshot_dir


def _run(backup_root: Path, *argv: str) -> int:
    return MODULE.main(list(argv), backup_root=backup_root)


def _text(capsys) -> dict:
    out = capsys.readouterr().out
    result = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if os.name != "nt":
        pytest.skip("directory links are not permitted in this environment")
    try:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, FileNotFoundError):
        pytest.skip("directory links are not permitted in this environment")
    if completed.returncode != 0 or not link.exists():
        pytest.skip("directory links are not permitted in this environment")


def _make_file_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
        return
    except OSError:
        pass
    pytest.skip("file links are not permitted in this environment")


def test_help_lists_allowed_flags_only(capsys):
    with pytest.raises(SystemExit) as exc:
        MODULE.main(["--help"])
    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "--snapshot-dir" in out
    assert "--target-database" not in out


def test_missing_snapshot_dir_exits_two(tmp_path, capsys):
    code = _run(tmp_path / "backup")
    assert code == 2
    assert _text(capsys)["FAILED_REASON"] == "missing_argument"


@pytest.mark.parametrize(
    "flag",
    ["--target-database", "--restore", "--replace", "--delete", "--clean", "--keep-on-failure", "--source-database"],
)
def test_forbidden_flags_exit_two(tmp_path, capsys, flag):
    code = _run(tmp_path / "backup", flag, "x")
    assert code == 2
    assert _text(capsys)["FAILED_REASON"] == "forbidden_argument"


def test_version_exits_zero(tmp_path, capsys):
    code = _run(tmp_path / "backup", "--version")
    assert code == 0
    assert MODULE.TOOL_VERSION in capsys.readouterr().out


def test_unc_source_is_rejected(tmp_path, capsys):
    code = _run(tmp_path / "backup", "--snapshot-dir", r"\\server\share\snapshot")
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "remote_path"


def test_unc_staging_root_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", r"\\server\share\tmp")
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "remote_path"


def test_source_outside_backup_root_is_rejected(tmp_path, capsys):
    stray = tmp_path / "novelcraft-db-20260101-000000-abcdef1"
    stray.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(stray))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "outside_backup_root"


def test_bad_snapshot_name_is_rejected(tmp_path, capsys):
    bad = tmp_path / "backup" / "novelcraft-db-latest"
    bad.mkdir(parents=True)
    _write_manifest(bad, 1)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(bad))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "bad_snapshot_name"


def test_snapshot_junction_is_rejected(tmp_path, capsys):
    real = _build_snapshot(tmp_path, "novelcraft-db-20260101-000000-abcdef1")
    link = tmp_path / "backup" / "novelcraft-db-20260101-000000-abcdef2"
    _make_dir_link(link, real)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(link))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "snapshot_link"


def test_staging_root_junction_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    real = tmp_path / "realtemp"
    real.mkdir()
    link = tmp_path / "linktemp"
    _make_dir_link(link, real)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(link))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "temp_root_link"


def test_staging_root_missing_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(tmp_path / "nope"))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "staging_root_missing"


def test_staging_root_file_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(blocker))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "staging_root_not_dir"


def test_staging_root_inside_backup_root_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    inside = tmp_path / "backup" / "stage"
    inside.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(inside))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "staging_root_collides"


def test_staging_root_inside_repo_is_rejected(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(MODULE, "_repo_root", lambda: repo)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(repo))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "staging_root_collides"


def test_missing_source_file_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    (snapshot / "novelcraft.db").unlink()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "missing_source_file"


def test_missing_manifest_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    (snapshot / "manifest.json").unlink()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "missing_manifest"


def test_corrupt_manifest_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    (snapshot / "manifest.json").write_text("{not json", encoding="utf-8")
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "bad_manifest"


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"snapshot_file": "other.db"}, "manifest_contract"),
        ({"snapshot_size": True}, "manifest_contract"),
        ({"snapshot_size": 0}, "manifest_contract"),
        ({"method": "manual"}, "manifest_contract"),
        ({"integrity_check": "fail"}, "manifest_contract"),
        ({"quick_check": "fail"}, "manifest_contract"),
    ],
)
def test_manifest_contract_violations(tmp_path, capsys, overrides, reason):
    snapshot = _build_snapshot(tmp_path, **overrides)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == reason


def test_extra_entry_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    (snapshot / "extra.txt").write_text("x", encoding="utf-8")
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 3
    assert _text(capsys)["FAILED_REASON"] == "extra_entry"


def test_source_size_disagreeing_with_manifest_is_exit_five(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_size"] = manifest["snapshot_size"] + 1
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot))
    assert code == 5
    assert _text(capsys)["FAILED_REASON"] == "size_mismatch"


def test_dry_run_does_not_hash_or_copy(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(*args, **kwargs):
        raise AssertionError("must not be called during --dry-run")

    monkeypatch.setattr(MODULE, "_sha256_file", boom)
    monkeypatch.setattr(MODULE.shutil, "copy2", boom)

    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root), "--dry-run")
    out = _text(capsys)
    assert code == 0
    assert out["RESULT"] == "DRY_RUN_OK"
    assert out["SOURCE_SHA256_BEFORE"] == "not_run"
    assert out["STAGING_SHA256"] == "not_run"
    assert list(staging_root.iterdir()) == []


def test_successful_staging_keeps_copy_and_hashes(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 0
    assert out["RESULT"] == "STAGED_OK"
    assert out["STAGING_KEPT"] == "true"
    staged = Path(out["STAGING_PATH"]) / "novelcraft.db"
    assert staged.is_file()
    assert out["SOURCE_SHA256_BEFORE"] == out["STAGING_SHA256"] == out["SOURCE_SHA256_AFTER"]
    assert out["SOURCE_HEAD"] == "abcdef1234567890"
    assert out["MTIME_DRIFT"] == "false"
    assert out["HEAD"]


def test_success_leaves_no_sqlite_handle_open(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    staged = Path(_text(capsys)["STAGING_PATH"]) / "novelcraft.db"
    assert code == 0
    os.unlink(staged)


def test_success_never_renames_or_replaces(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(*args, **kwargs):
        raise AssertionError("rename/replace must never be called")

    monkeypatch.setattr(MODULE.os, "replace", boom)
    monkeypatch.setattr(MODULE.os, "rename", boom)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    assert code == 0


def test_source_changed_same_size_is_exit_six(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    source = snapshot / "novelcraft.db"
    real_copy = MODULE.shutil.copy2

    def mutate(src, dst, *args, **kwargs):
        data = Path(src).read_bytes()
        Path(src).write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(MODULE.shutil, "copy2", mutate)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 6
    assert out["FAILED_REASON"] == "source_changed"
    assert out["STAGING_KEPT"] == "true"
    assert Path(out["STAGING_PATH"]).is_dir()


def test_source_mtime_only_change_is_tolerated(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    source = snapshot / "novelcraft.db"
    real_copy = MODULE.shutil.copy2

    def bump_mtime(src, dst, *args, **kwargs):
        info = Path(src).stat()
        os.utime(src, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(MODULE.shutil, "copy2", bump_mtime)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 0
    assert out["RESULT"] == "STAGED_OK"
    assert out["MTIME_DRIFT"] == "true"
    assert out["SOURCE_SIZE_BEFORE"] == out["SOURCE_SIZE_AFTER"]
    assert out["SOURCE_SHA256_BEFORE"] == out["SOURCE_SHA256_AFTER"] == out["STAGING_SHA256"]
    assert out["STAGING_KEPT"] == "true"
    staged = Path(out["STAGING_PATH"]) / "novelcraft.db"
    assert staged.is_file()
    assert staged.stat().st_size == int(out["SNAPSHOT_SIZE"])


def test_staging_hash_mismatch_is_exit_six(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    real_copy = MODULE.shutil.copy2

    def corrupt(dst):
        data = Path(dst).read_bytes()
        Path(dst).write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])

    def copy_then_corrupt(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        corrupt(dst)
        return result

    monkeypatch.setattr(MODULE.shutil, "copy2", copy_then_corrupt)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 6
    assert out["FAILED_REASON"] == "staging_hash_mismatch"


def test_staging_size_mismatch_is_exit_five(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    real_copy = MODULE.shutil.copy2

    def copy_then_truncate(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        with open(dst, "r+b") as handle:
            handle.truncate(max(0, Path(dst).stat().st_size - 1))
        return result

    monkeypatch.setattr(MODULE.shutil, "copy2", copy_then_truncate)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    assert code == 5
    assert _text(capsys)["FAILED_REASON"] == "size_mismatch"


def test_copy_failure_keeps_staging_directory(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(MODULE.shutil, "copy2", boom)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 4
    assert out["FAILED_REASON"] == "copy_failed"
    assert Path(out["STAGING_PATH"]).is_dir()
    assert list(staging_root.iterdir())


def test_unexpected_oserror_after_staging_created_is_exit_four(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(path, chunk=1024 * 1024):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(MODULE, "_sha256_file", boom)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    raw = capsys.readouterr().out
    out = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    assert code == 4
    assert out["RESULT"] == "FAILED"
    assert out["FAILED_REASON"] == "unexpected_io"
    assert out["STAGING_KEPT"] == "true"
    staged = Path(out["STAGING_PATH"])
    assert staged.is_dir()
    assert staged.parent == staging_root
    assert str(snapshot) not in raw
    assert "PermissionError" not in raw
    assert "denied" not in raw


def test_unexpected_oserror_before_staging_created_is_exit_four_json(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(directory):
        raise PermissionError(13, "denied", str(tmp_path / "secret-path"))

    monkeypatch.setattr(MODULE, "_load_manifest", boom)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir",
        str(snapshot),
        "--staging-root",
        str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 4
    assert payload["RESULT"] == "FAILED"
    assert payload["FAILED_REASON"] == "unexpected_io"
    assert payload["STAGING_PATH"] is None
    assert payload["STAGING_KEPT"] is False
    assert list(staging_root.iterdir()) == []
    blob = json.dumps(payload)
    assert str(snapshot) not in blob
    assert "PermissionError" not in blob
    assert "denied" not in blob
    assert "secret-path" not in blob


def test_pragma_failure_keeps_site_and_reports_sidecars(tmp_path, capsys):
    snapshot = _build_garbage_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 7
    assert out["FAILED_REASON"] in {"pragma_failed", "integrity_failed", "quick_failed", "open_failed"}
    assert Path(out["STAGING_PATH"]).is_dir()


def test_source_head_missing_is_null(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    data = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    data.pop("head")
    (snapshot / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 0
    assert out["SOURCE_HEAD"] == "null"


def test_source_head_wrong_type_is_null_and_does_not_block(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path, head=12345)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 0
    assert out["SOURCE_HEAD"] == "null"


def test_output_does_not_leak_source_path_or_secrets(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = capsys.readouterr().out
    assert code == 0
    assert str(snapshot) not in out
    for forbidden in ("backup_sha256", ".env", "password", "Bearer", "DATABASE_URL"):
        assert forbidden not in out


def test_json_output_has_stable_types(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 0
    assert payload["RESULT"] == "STAGED_OK"
    assert isinstance(payload["SIDECARS"], list)
    assert payload["STAGING_KEPT"] is True
    assert payload["MTIME_DRIFT"] is False
    assert payload["FAILED_STEP"] is None
    assert payload["FAILED_REASON"] is None
    assert isinstance(payload["STAGING_PATH"], str)
    assert payload["SNAPSHOT_SIZE"] == payload["SOURCE_SIZE_BEFORE"]
    assert payload["TOOL_VERSION"] == MODULE.TOOL_VERSION


# --- supplementary safety coverage -------------------------------------------------


def _assert_no_paths_or_secrets(text: str, snapshot: Path) -> None:
    assert str(snapshot) not in text
    assert str(snapshot / "novelcraft.db") not in text
    for forbidden in ("backend\\novelcraft.db", "backend/novelcraft.db", ".env", "password", "Bearer", "DATABASE_URL", "backup_sha256"):
        assert forbidden not in text


def test_guard_failure_creates_no_staging_and_reports_null_path(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path, method="not-a-snapshot-method")
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 3
    assert payload["STAGING_PATH"] is None
    assert payload["STAGING_KEPT"] is False
    assert list(staging_root.iterdir()) == []
    _assert_no_paths_or_secrets(json.dumps(payload), snapshot)


def test_staging_root_guard_failure_output_does_not_leak_paths(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(REPO / "backend"),
        "--json",
    )
    payload = _json(capsys)
    assert code == 3
    assert payload["FAILED_REASON"] in {"staging_root_collides", "staging_root_outside_temp"}
    assert payload["STAGING_PATH"] is None
    _assert_no_paths_or_secrets(json.dumps(payload), snapshot)


def test_fingerprint_change_keeps_staging_file(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    source = snapshot / "novelcraft.db"
    real_copy = MODULE.shutil.copy2

    def mutate(src, dst, *args, **kwargs):
        data = Path(src).read_bytes()
        Path(src).write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(MODULE.shutil, "copy2", mutate)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 6
    assert payload["FAILED_REASON"] == "source_changed"
    staged = Path(payload["STAGING_PATH"]) / "novelcraft.db"
    assert staged.is_file()
    assert staged.stat().st_size == source.stat().st_size
    _assert_no_paths_or_secrets(json.dumps(payload), snapshot)


def test_staging_name_race_preserves_existing_directory(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    fixed = "novelcraft-restore-staging-20260101-000000-abcdef"
    occupied = staging_root / fixed
    occupied.mkdir()
    (occupied / "keep-me.txt").write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(MODULE, "_staging_name", lambda: fixed)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 4
    assert out["FAILED_REASON"] == "staging_dir_failed"
    assert (occupied / "keep-me.txt").read_text(encoding="utf-8") == "sentinel"
    assert list(staging_root.iterdir()) == [occupied]


def test_partial_copy_failure_keeps_partial_file(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def partial(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(MODULE.shutil, "copy2", partial)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 4
    assert out["FAILED_REASON"] == "copy_failed"
    staged = Path(out["STAGING_PATH"]) / "novelcraft.db"
    assert staged.is_file()
    assert staged.stat().st_size == len(b"partial")


def test_open_failure_keeps_staging_and_reports_scene(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(*args, **kwargs):
        raise MODULE.sqlite3.Error("cannot open")

    monkeypatch.setattr(MODULE.sqlite3, "connect", boom)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 7
    assert out["FAILED_REASON"] == "open_failed"
    assert Path(out["STAGING_PATH"]).is_dir()
    assert (Path(out["STAGING_PATH"]) / "novelcraft.db").is_file()


def test_manifest_failure_creates_no_staging_directory(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path, snapshot_size="not-an-int")
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    assert code == 3
    assert list(staging_root.iterdir()) == []


def test_existing_sidecars_are_preserved_and_listed(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def fake_pragmas(target):
        Path(str(target) + "-wal").write_bytes(b"")
        Path(str(target) + "-shm").write_bytes(b"\x00" * 8)
        return "ok", "ok"

    monkeypatch.setattr(MODULE, "_pragmas", fake_pragmas)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 0
    assert set(payload["SIDECARS"]) == {"novelcraft.db-wal", "novelcraft.db-shm"}
    staged = Path(payload["STAGING_PATH"]) / "novelcraft.db"
    assert staged.is_file()
    assert Path(str(staged) + "-wal").exists()
    assert Path(str(staged) + "-shm").exists()


def test_sidecars_are_scanned_after_connection_closed(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    events: list[str] = []

    class FakeCursor:
        def fetchone(self):
            return ("ok",)

    class FakeConn:
        def execute(self, sql):
            return FakeCursor()

        def close(self):
            events.append("close")

    real_sidecars = MODULE._sidecars

    def connect(*args, **kwargs):
        events.append("connect")
        return FakeConn()

    def scan(target):
        events.append("scan")
        return real_sidecars(target)

    monkeypatch.setattr(MODULE.sqlite3, "connect", connect)
    monkeypatch.setattr(MODULE, "_sidecars", scan)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    assert code == 0
    assert events.index("connect") < events.index("close") < events.index("scan")


def test_source_never_deletes_moves_or_renames(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    def boom(*args, **kwargs):
        raise AssertionError("destructive API must never be called")

    for name in ("remove", "unlink", "rename", "replace"):
        if hasattr(MODULE.os, name):
            monkeypatch.setattr(MODULE.os, name, boom)
    for name in ("rmtree", "move"):
        if hasattr(MODULE.shutil, name):
            monkeypatch.setattr(MODULE.shutil, name, boom)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    assert code == 0


def test_source_has_no_destructive_ap_is_or_sql():
    import ast

    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    destructive = {"remove", "unlink", "rmtree", "move", "rename", "replace", "rmdir", "removedirs"}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            used.add(node.func.attr)
    assert not (used & destructive), f"destructive APIs present: {used & destructive}"
    for sql in ("DROP TABLE", "DELETE FROM", "VACUUM", "ATTACH DATABASE", "PRAGMA journal_mode"):
        assert sql not in source


def test_backup_root_being_a_link_is_rejected(tmp_path, capsys):
    real_root = tmp_path / "real_backup"
    real_root.mkdir()
    snapshot = _build_snapshot(tmp_path)
    link = tmp_path / "backup_link"
    _make_dir_link(link, real_root)
    code = _run(link, "--snapshot-dir", str(link / snapshot.name), "--staging-root", str(tmp_path / "stage"))
    out = _text(capsys)
    assert code == 3
    assert out["FAILED_REASON"] == "snapshot_link"


def test_staging_root_parent_link_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    outer = tmp_path / "outer"
    outer.mkdir()
    target = tmp_path / "target"
    (target / "deeper").mkdir(parents=True)
    link = outer / "alias"
    _make_dir_link(link, target)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(link / "deeper"),
    )
    out = _text(capsys)
    assert code == 3
    assert out["FAILED_REASON"] == "temp_root_link"


def test_snapshot_db_file_link_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    external = tmp_path / "outside.db"
    _make_db(external)
    (snapshot / "novelcraft.db").unlink()
    _make_file_link(snapshot / "novelcraft.db", external)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    raw = capsys.readouterr().out
    assert code == 3
    assert "FAILED_REASON=snapshot_link" in raw
    assert list(staging_root.iterdir()) == []
    assert str(snapshot) not in raw
    assert str(external) not in raw


def test_snapshot_manifest_file_link_is_rejected(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    external = tmp_path / "outside-manifest.json"
    external.write_text("{}", encoding="utf-8")
    (snapshot / "manifest.json").unlink()
    _make_file_link(snapshot / "manifest.json", external)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    raw = capsys.readouterr().out
    assert code == 3
    assert "FAILED_REASON=snapshot_link" in raw
    assert list(staging_root.iterdir()) == []
    assert str(snapshot) not in raw
    assert str(external) not in raw


class _FakeLinkWithoutAttributes:
    def is_symlink(self):
        return True

    def lstat(self):
        class _Info:
            st_file_attributes = 0

        return _Info()


def test_is_reparse_point_detects_symlink_without_file_attributes():
    assert MODULE._is_reparse_point(_FakeLinkWithoutAttributes()) is True


def test_is_reparse_point_detects_real_file_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "link.txt"
    _make_file_link(link, target)
    assert MODULE._is_reparse_point(link) is True


@pytest.mark.parametrize("linked_name", ["novelcraft.db", "manifest.json"])
def test_reparse_file_guard_runs_before_reading_the_file(tmp_path, capsys, monkeypatch, linked_name):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()

    real_is_reparse = MODULE._is_reparse_point

    def fake_is_reparse(path):
        if Path(path).name == linked_name:
            return True
        return real_is_reparse(path)

    def manifest_must_not_be_read(directory):
        raise AssertionError("manifest must not be read before the file-level guard")

    def source_must_not_be_fingerprinted(path):
        raise AssertionError("source must not be read before the file-level guard")

    monkeypatch.setattr(MODULE, "_is_reparse_point", fake_is_reparse)
    monkeypatch.setattr(MODULE, "_load_manifest", manifest_must_not_be_read)
    monkeypatch.setattr(MODULE, "_fingerprint", source_must_not_be_fingerprinted)
    code = _run(tmp_path / "backup", "--snapshot-dir", str(snapshot), "--staging-root", str(staging_root))
    out = _text(capsys)
    assert code == 3
    assert out["FAILED_REASON"] == "snapshot_link"
    assert list(staging_root.iterdir()) == []


def test_dry_run_reports_planned_target_and_creates_nothing(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--dry-run",
        "--json",
    )
    payload = _json(capsys)
    assert code == 0
    assert payload["RESULT"] == "DRY_RUN_OK"
    assert payload["STAGING_DIR"]
    assert payload["STAGING_TARGET_AVAILABLE"] is True
    assert payload["SOURCE_SHA256_BEFORE"] == "not_run"
    assert list(staging_root.iterdir()) == []


def test_dry_run_reports_unavailable_name_without_overwriting(tmp_path, capsys, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    fixed = "novelcraft-restore-staging-20260101-000000-abcdef"
    occupied = staging_root / fixed
    occupied.mkdir()
    (occupied / "keep-me.txt").write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(MODULE, "_staging_name", lambda: fixed)
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--dry-run",
        "--json",
    )
    payload = _json(capsys)
    assert code == 0
    assert payload["STAGING_TARGET_AVAILABLE"] is False
    assert (occupied / "keep-me.txt").read_text(encoding="utf-8") == "sentinel"
    assert list(staging_root.iterdir()) == [occupied]


def test_failure_output_uses_json_null_for_staging_path(tmp_path, capsys):
    snapshot = _build_snapshot(tmp_path, method="bogus")
    staging_root = tmp_path / "stage"
    staging_root.mkdir()
    code = _run(
        tmp_path / "backup",
        "--snapshot-dir", str(snapshot),
        "--staging-root", str(staging_root),
        "--json",
    )
    payload = _json(capsys)
    assert code == 3
    assert payload["STAGING_PATH"] is None
    assert "STAGING_PATH" in payload
