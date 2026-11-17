#!/usr/bin/env python3
"""Create a verified staging copy of a database snapshot.

This tool only stages: it copies ``novelcraft.db`` from one real snapshot directory
into a fresh TEMP staging directory, verifies that the copy matches the source
(size + SHA-256) and that the copy is structurally intact (read-only PRAGMAs).

It never:
  * touches the formal database or its directory,
  * renames, replaces, migrates, or deletes anything,
  * opens the source database or reads its contents,
  * starts or stops services,
  * accepts a formal target database path.

Exit codes: 0 ok, 2 usage, 3 guard, 4 io, 5 size, 6 fingerprint, 7 check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TOOL_VERSION = "1.0.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GUARD = 3
EXIT_IO = 4
EXIT_SIZE = 5
EXIT_FINGERPRINT = 6
EXIT_CHECK = 7

SNAPSHOT_NAME_RE = re.compile(r"^novelcraft-db-\d{8}-\d{6}-[0-9a-f]{7}$")
SNAPSHOT_FILE = "novelcraft.db"
MANIFEST_FILE = "manifest.json"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

FORBIDDEN_FLAGS = (
    "--target-database",
    "--restore",
    "--replace",
    "--delete",
    "--clean",
    "--keep-on-failure",
    "--source-database",
)

REPARSE_POINT = 0x0400


class ToolError(Exception):
    def __init__(self, code: int, category: str, message: str):
        super().__init__(message)
        self.code = code
        self.category = category
        self.message = message


def _fail(code: int, category: str, message: str) -> None:
    raise ToolError(code, category, message)


def _reject_remote(raw: str, label: str) -> None:
    text = str(raw)
    if text.startswith("\\\\?\\UNC\\") or text.startswith("\\\\?\\unc\\"):
        _fail(EXIT_GUARD, "remote_path", f"{label} 不支持 UNC 路径")
    if text.startswith("\\\\.\\") or text.startswith("//./"):
        _fail(EXIT_GUARD, "device_path", f"{label} 不支持设备路径")
    if text.startswith("\\\\") or text.startswith("//"):
        _fail(EXIT_GUARD, "remote_path", f"{label} 不支持网络路径")


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _no_reparse_components(raw: str, stop: Path, label: str, category: str) -> None:
    current = Path(os.path.abspath(raw))
    stop_abs = Path(os.path.abspath(stop))
    while True:
        if _is_reparse_point(current):
            _fail(EXIT_GUARD, category, f"{label} 包含不受支持的链接或 reparse point")
        if current == stop_abs or current.parent == current:
            break
        if stop_abs not in current.parents:
            break
        current = current.parent


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict:
    info = path.stat()
    return {
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": _sha256_file(path),
    }


def _load_manifest(snapshot_dir: Path) -> dict:
    manifest_path = snapshot_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        _fail(EXIT_GUARD, "missing_manifest", "快照目录缺少 manifest.json")
    try:
        raw = manifest_path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        _fail(EXIT_GUARD, "bad_manifest", "manifest.json 无法解析")
    if not isinstance(data, dict):
        _fail(EXIT_GUARD, "bad_manifest", "manifest.json 顶层必须是对象")
    return data


def _validate_manifest(data: dict) -> int:
    if data.get("snapshot_file") != SNAPSHOT_FILE:
        _fail(EXIT_GUARD, "manifest_contract", "manifest.snapshot_file 不是 novelcraft.db")
    size = data.get("snapshot_size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        _fail(EXIT_GUARD, "manifest_contract", "manifest.snapshot_size 必须是正整数")
    if data.get("method") != "sqlite-backup":
        _fail(EXIT_GUARD, "manifest_contract", "manifest.method 不是 sqlite-backup")
    if data.get("integrity_check") != "ok":
        _fail(EXIT_GUARD, "manifest_contract", "manifest.integrity_check 不是 ok")
    if data.get("quick_check") != "ok":
        _fail(EXIT_GUARD, "manifest_contract", "manifest.quick_check 不是 ok")
    return size


def _source_head(data: dict):
    head = data.get("head")
    return head if isinstance(head, str) and head else None


def _validate_snapshot_dir(raw: str, backup_root: Path) -> tuple[Path, dict, int]:
    _reject_remote(raw, "--snapshot-dir")
    _no_reparse_components(raw, backup_root, "--snapshot-dir", "snapshot_link")
    snapshot_dir = Path(os.path.abspath(raw))
    if not snapshot_dir.exists():
        _fail(EXIT_GUARD, "missing_source", "快照目录不存在")
    if not snapshot_dir.is_dir():
        _fail(EXIT_GUARD, "not_directory", "快照路径不是目录")
    if snapshot_dir.parent != backup_root:
        _fail(EXIT_GUARD, "outside_backup_root", "快照目录不在备份根下")
    if not SNAPSHOT_NAME_RE.match(snapshot_dir.name):
        _fail(EXIT_GUARD, "bad_snapshot_name", "快照目录名不符合命名规则")
    allowed = {SNAPSHOT_FILE, MANIFEST_FILE}
    for child in snapshot_dir.iterdir():
        if child.name not in allowed:
            _fail(EXIT_GUARD, "extra_entry", "快照目录存在非白名单条目")
        if _is_reparse_point(child):
            _fail(
                EXIT_GUARD,
                "snapshot_link",
                "快照目录内的文件包含不受支持的链接或 reparse point",
            )
    source = snapshot_dir / SNAPSHOT_FILE
    if not source.is_file():
        _fail(EXIT_GUARD, "missing_source_file", "快照目录缺少 novelcraft.db")
    data = _load_manifest(snapshot_dir)
    size = _validate_manifest(data)
    if source.stat().st_size != size:
        _fail(EXIT_SIZE, "size_mismatch", "源文件大小与 manifest.snapshot_size 不一致")
    return source, data, size


def _decide_staging_root(raw: str | None) -> Path:
    system_temp = Path(os.path.abspath(tempfile.gettempdir())).resolve()  # resolve 统一 junction/物理路径（C:↔E: 盘符差异会导致比较误判）
    value = raw if raw else str(system_temp)
    _reject_remote(value, "--staging-root")
    _no_reparse_components(value, system_temp, "--staging-root", "temp_root_link")
    root = Path(os.path.abspath(value))
    if not root.exists():
        _fail(EXIT_GUARD, "staging_root_missing", "--staging-root 不存在")
    if not root.is_dir():
        _fail(EXIT_GUARD, "staging_root_not_dir", "--staging-root 不是目录")
    if system_temp not in root.parents and root != system_temp:
        _fail(EXIT_GUARD, "staging_root_outside_temp", "--staging-root 不在系统 TEMP 内")
    return root


def _staging_name() -> str:
    return "novelcraft-restore-staging-{}-{}".format(
        datetime.now().strftime("%Y%m%d-%H%M%S"),
        os.urandom(3).hex(),
    )


def _plan_staging_dir(root: Path) -> tuple[str | None, bool]:
    """Dry-run planning only: pick a free name without creating anything."""
    for _ in range(32):
        name = _staging_name()
        if not (root / name).exists():
            return name, True
    return None, False


def _new_staging_dir(root: Path) -> Path:
    for _ in range(32):
        candidate = root / _staging_name()
        if candidate.exists():
            continue
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError:
            _fail(EXIT_IO, "staging_dir_failed", "staging 目录创建失败")
        return candidate
    _fail(EXIT_IO, "staging_dir_failed", "staging 目录名持续冲突")


def _pragmas(target: Path) -> tuple[str, str]:
    try:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        _fail(EXIT_CHECK, "open_failed", "staging 副本只读打开失败")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        quick = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        _fail(EXIT_CHECK, "pragma_failed", "staging 副本检查执行失败")
    finally:
        conn.close()
    integrity_value = str(integrity[0]) if integrity else ""
    quick_value = str(quick[0]) if quick else ""
    if integrity_value.lower() != "ok":
        _fail(EXIT_CHECK, "integrity_failed", "staging 副本 integrity_check 不是 ok")
    if quick_value.lower() != "ok":
        _fail(EXIT_CHECK, "quick_failed", "staging 副本 quick_check 不是 ok")
    return integrity_value, quick_value


def _sidecars(target: Path) -> list[str]:
    found = []
    for suffix in SIDECAR_SUFFIXES:
        candidate = target.with_name(target.name + suffix)
        if candidate.exists():
            found.append(candidate.name)
    return found


def _current_head(repo: Path):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.decode("utf-8", "replace").strip()
    return head or None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _staging_collides(root: Path, backup_root: Path, repo: Path) -> bool:
    if root == backup_root or backup_root in root.parents:
        return True
    if root == repo or repo in root.parents:
        return True
    return False


def main(argv: list[str] | None = None, *, backup_root: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    for flag in FORBIDDEN_FLAGS:
        if any(arg == flag or arg.startswith(flag + "=") for arg in argv):
            payload = {
                "RESULT": "FAILED",
                "FAILED_STEP": 0,
                "FAILED_REASON": "forbidden_argument",
                "STAGING_PATH": None,
                "STAGING_KEPT": False,
                "TOOL_VERSION": TOOL_VERSION,
            }
            return _emit(payload, as_json, f"不接受参数 {flag}", EXIT_USAGE)

    parser = argparse.ArgumentParser(
        prog="stage-db-snapshot.py",
        description="Create a verified staging copy of a NovelCraft database snapshot.",
        allow_abbrev=False,
    )
    parser.add_argument("--snapshot-dir", default=None)
    parser.add_argument("--staging-root", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(f"stage-db-snapshot {TOOL_VERSION}")
        return EXIT_OK

    if not args.snapshot_dir:
        payload = {
            "RESULT": "FAILED",
            "FAILED_STEP": 0,
            "FAILED_REASON": "missing_argument",
            "STAGING_PATH": None,
            "STAGING_KEPT": False,
            "TOOL_VERSION": TOOL_VERSION,
        }
        return _emit(payload, args.as_json, "缺少必填参数 --snapshot-dir", EXIT_USAGE)

    repo = _repo_root()
    root = Path(backup_root) if backup_root is not None else Path.home() / "NovelCraft-backups" / "database"
    root = Path(os.path.abspath(root))
    result = {
        "RESULT": "FAILED",
        "SOURCE_DIR": None,
        "STAGING_ROOT": None,
        "STAGING_DIR": None,
        "STAGING_PATH": None,
        "STAGING_TARGET_AVAILABLE": None,
        "SNAPSHOT_SIZE": None,
        "SOURCE_SIZE_BEFORE": None,
        "SOURCE_SIZE_AFTER": None,
        "SOURCE_MTIME_BEFORE": None,
        "SOURCE_MTIME_AFTER": None,
        "SOURCE_SHA256_BEFORE": "not_run",
        "SOURCE_SHA256_AFTER": "not_run",
        "STAGING_SHA256": "not_run",
        "INTEGRITY_CHECK": "not_run",
        "QUICK_CHECK": "not_run",
        "SIDECARS": [],
        "STAGING_KEPT": False,
        "MTIME_DRIFT": False,
        "FAILED_STEP": None,
        "FAILED_REASON": None,
        "HEAD": _current_head(repo),
        "SOURCE_HEAD": None,
        "TOOL_VERSION": TOOL_VERSION,
    }

    step = 0
    staging_dir = None
    try:
        step = 1
        source, manifest, snapshot_size = _validate_snapshot_dir(args.snapshot_dir, root)
        result["SOURCE_DIR"] = source.parent.name
        result["SNAPSHOT_SIZE"] = snapshot_size
        result["SOURCE_HEAD"] = _source_head(manifest)

        step = 2
        staging_root = _decide_staging_root(args.staging_root)
        if _staging_collides(staging_root, root, repo):
            _fail(EXIT_GUARD, "staging_root_collides", "--staging-root 位于仓库或备份根内")
        result["STAGING_ROOT"] = str(staging_root)

        if args.dry_run:
            planned_name, available = _plan_staging_dir(staging_root)
            result["STAGING_DIR"] = planned_name
            result["STAGING_TARGET_AVAILABLE"] = available
            result["RESULT"] = "DRY_RUN_OK"
            return _emit(result, args.as_json)

        step = 3
        staging_dir = _new_staging_dir(staging_root)
        result["STAGING_DIR"] = staging_dir.name
        result["STAGING_PATH"] = str(staging_dir)
        result["STAGING_KEPT"] = True

        step = 4
        before = _fingerprint(source)
        result["SOURCE_SIZE_BEFORE"] = before["size"]
        result["SOURCE_MTIME_BEFORE"] = before["mtime_ns"]
        result["SOURCE_SHA256_BEFORE"] = before["sha256"]

        step = 5
        target = staging_dir / SNAPSHOT_FILE
        try:
            shutil.copy2(source, target)
        except OSError:
            _fail(EXIT_IO, "copy_failed", "复制快照到 staging 失败")

        step = 6
        after = _fingerprint(source)
        result["SOURCE_SIZE_AFTER"] = after["size"]
        result["SOURCE_MTIME_AFTER"] = after["mtime_ns"]
        result["SOURCE_SHA256_AFTER"] = after["sha256"]
        result["MTIME_DRIFT"] = before["mtime_ns"] != after["mtime_ns"]
        if before["size"] != after["size"] or before["sha256"] != after["sha256"]:
            _fail(EXIT_FINGERPRINT, "source_changed", "复制期间源文件发生变化")

        step = 7
        staged = _fingerprint(target)
        result["STAGING_SHA256"] = staged["sha256"]
        if staged["size"] != snapshot_size:
            _fail(EXIT_SIZE, "size_mismatch", "staging 大小与 manifest 不一致")
        if staged["sha256"] != after["sha256"]:
            _fail(EXIT_FINGERPRINT, "staging_hash_mismatch", "staging 哈希与复制后源哈希不一致")

        step = 8
        integrity, quick = _pragmas(target)
        result["INTEGRITY_CHECK"] = integrity
        result["QUICK_CHECK"] = quick

        step = 9
        result["SIDECARS"] = _sidecars(target)
        result["RESULT"] = "STAGED_OK"
        return _emit(result, args.as_json)

    except ToolError as exc:
        result["FAILED_STEP"] = step
        result["FAILED_REASON"] = exc.category
        if staging_dir is not None:
            result["STAGING_DIR"] = staging_dir.name
            result["STAGING_PATH"] = str(staging_dir)
            result["STAGING_KEPT"] = True
        return _emit(result, args.as_json, exc.message, exc.code)
    except OSError:
        # Unexpected filesystem errors (permission denied, source removed mid-run, ...)
        # must not escape as a traceback: keep the documented EXIT_IO code, preserve any
        # staging site, and never echo the exception text or a path.
        result["FAILED_STEP"] = step
        result["FAILED_REASON"] = "unexpected_io"
        if staging_dir is not None:
            result["STAGING_DIR"] = staging_dir.name
            result["STAGING_PATH"] = str(staging_dir)
            result["STAGING_KEPT"] = True
        return _emit(result, args.as_json, "读取或写入失败", EXIT_IO)


def _emit(result: dict, as_json: bool, message: str | None = None, code: int = EXIT_OK) -> int:
    if as_json:
        payload = dict(result)
        if message:
            payload["MESSAGE"] = message
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif value is None:
                rendered = "null"
            elif isinstance(value, list):
                rendered = ",".join(value)
            else:
                rendered = str(value)
            print(f"{key}={rendered}")
        if message:
            print(f"MESSAGE={message}")
    return code


if __name__ == "__main__":
    sys.exit(main())
