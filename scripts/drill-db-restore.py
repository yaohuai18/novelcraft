#!/usr/bin/env python3
"""Isolated restore drill for NovelCraft database snapshots.

Copies ONE snapshot database into a fresh temporary directory, opens the copy
read-only and runs ``PRAGMA integrity_check`` / ``PRAGMA quick_check``. It never
touches the production database and has no restore/replace capability.

Exit codes:
    0  drill succeeded, checks passed, drill directory removed
    2  argument error
    3  snapshot or path validation failed
    4  drill directory creation or file copy failed
    5  copied file size does not match manifest.snapshot_size
    6  read-only open or integrity check failed
    7  cleanup of the drill directory failed (scene kept)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TOOL_VERSION = "1.0.0"

EXIT_OK = 0
EXIT_ARGUMENT = 2
EXIT_SNAPSHOT_INVALID = 3
EXIT_DRILL_IO = 4
EXIT_SIZE_MISMATCH = 5
EXIT_CHECK_FAILED = 6
EXIT_CLEANUP_FAILED = 7

SNAPSHOT_NAME_RE = re.compile(r"^novelcraft-db-(\d{8}-\d{6})-([0-9a-f]{7})$")
DRILL_NAME_RE = re.compile(r"^nc-restore-drill-\d{8}-\d{6}-[0-9a-z]{6,}$")
SNAPSHOT_DB = "novelcraft.db"
SNAPSHOT_MANIFEST = "manifest.json"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

ALLOWED_MANIFEST_FIELDS = (
    "created_at",
    "head",
    "head_short",
    "source_path",
    "snapshot_file",
    "snapshot_size",
    "method",
    "service_stopped",
    "integrity_check",
    "quick_check",
    "tool_version",
    "label",
)

# Overridable in tests; production always uses the real user profile path.
BACKUP_ROOT = Path.home() / "NovelCraft-backups" / "database"


class ToolError(Exception):
    """Failure carrying the process exit code and a non-sensitive category."""

    def __init__(self, code: int, category: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.category = category
        self.detail = detail


@dataclass
class DrillReport:
    status: str = "FAIL"
    source: str = ""
    drill: str = ""
    restored_file: str = SNAPSHOT_DB
    snapshot_size: int | None = None
    restored_size: int | None = None
    integrity: str = "not_run"
    quick: str = "not_run"
    sidecars: list[str] = field(default_factory=list)
    cleaned: bool | None = None
    head: str | None = None
    tool_version: str = TOOL_VERSION
    dry_run: bool = False
    category: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "drill": self.drill,
            "restored_file": self.restored_file,
            "snapshot_size": self.snapshot_size,
            "restored_size": self.restored_size,
            "integrity_check": self.integrity,
            "quick_check": self.quick,
            "sidecars": list(self.sidecars),
            "cleaned": self.cleaned,
            "head": self.head,
            "tool_version": self.tool_version,
            "dry_run": self.dry_run,
            "category": self.category,
            "detail": self.detail,
        }


def _is_remote_path(raw: str) -> bool:
    text = str(raw)
    if text.startswith("\\\\?\\"):
        return text[4:].upper().startswith("UNC\\")
    if text.startswith("\\\\.\\"):
        return True
    return text.startswith("\\\\") or text.startswith("//")


def _reject_remote(raw: str, label: str) -> None:
    if _is_remote_path(raw):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "path_remote", f"{label} 不能是 UNC/设备/远程路径")


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        st = path.lstat()
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _no_reparse_components(raw_path: Path, stop: Path, label: str, category: str) -> None:
    """Reject links on the raw path and every parent up to (and including) ``stop``.

    Uses ``os.path.abspath`` (which normalizes ``..`` but does NOT follow links)
    and ``lstat`` so a junction/symlink is seen before it can be resolved away.
    """
    current = Path(os.path.abspath(str(raw_path)))
    stop_path = Path(os.path.abspath(str(stop)))
    while True:
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current):
                raise ToolError(
                    EXIT_SNAPSHOT_INVALID,
                    category,
                    f"{label} 包含不受支持的链接或 reparse point",
                )
        if current == stop_path or current.parent == current:
            break
        current = current.parent


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - defensive
        raise ToolError(EXIT_SNAPSHOT_INVALID, "path_invalid", f"路径无法解析: {type(exc).__name__}") from exc


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_snapshot_dir(raw: str, backup_root: Path) -> tuple[Path, dict]:
    _reject_remote(raw, "--snapshot-dir")
    _no_reparse_components(Path(raw), backup_root, "--snapshot-dir", "snapshot_link")
    snapshot_dir = _resolve(Path(raw))
    if not snapshot_dir.exists():
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_missing", "快照目录不存在")
    if not snapshot_dir.is_dir():
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_not_dir", "快照路径不是目录")
    if _is_reparse_point(snapshot_dir):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_link", "快照目录不能是链接或 reparse point")
    root = _resolve(backup_root)
    if not _within(snapshot_dir, root):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_outside_root", "快照目录不在备份根目录下")
    if snapshot_dir.parent != root:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_depth", "快照目录必须是备份根的直接子目录")
    if not SNAPSHOT_NAME_RE.match(snapshot_dir.name):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_name", "快照目录名不符合约定格式")

    try:
        entries = list(snapshot_dir.iterdir())
    except OSError as exc:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_unreadable", f"快照目录无法读取: {type(exc).__name__}") from exc

    names = {entry.name for entry in entries}
    if names != {SNAPSHOT_DB, SNAPSHOT_MANIFEST}:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_extra_entries", "快照目录只能包含 novelcraft.db 与 manifest.json")
    for entry in entries:
        if not entry.is_file() or _is_reparse_point(entry):
            raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_extra_entries", "快照目录存在非普通文件")

    manifest = _read_manifest(snapshot_dir / SNAPSHOT_MANIFEST)
    _validate_manifest(manifest)

    database = snapshot_dir / SNAPSHOT_DB
    try:
        stat = database.stat()
    except OSError as exc:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_db_missing", "快照数据库文件不存在") from exc
    if stat.st_size != manifest["snapshot_size"]:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "snapshot_size_mismatch", "源文件大小与 manifest.snapshot_size 不一致")
    return snapshot_dir, manifest


def _read_manifest(path: Path) -> dict:
    if not path.is_file():
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_missing", "manifest.json 不存在")
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_invalid", f"manifest.json 无法解析: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_invalid", "manifest.json 必须是对象")
    return {key: data.get(key) for key in ALLOWED_MANIFEST_FIELDS if key in data}


def _validate_manifest(manifest: dict) -> None:
    if manifest.get("snapshot_file") != SNAPSHOT_DB:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_snapshot_file", "manifest.snapshot_file 必须是 novelcraft.db")
    size = manifest.get("snapshot_size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_snapshot_size", "manifest.snapshot_size 必须是正整数")
    if manifest.get("method") != "sqlite-backup":
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_method", "manifest.method 必须是 sqlite-backup")
    if manifest.get("integrity_check") != "ok":
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_integrity", "manifest.integrity_check 必须是 ok")
    if manifest.get("quick_check") != "ok":
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_quick", "manifest.quick_check 必须是 ok")
    for key in ("created_at", "head", "head_short", "source_path", "tool_version", "label"):
        if key in manifest and not isinstance(manifest[key], str):
            raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_field_type", f"manifest.{key} 类型错误")
    if "service_stopped" in manifest and not isinstance(manifest["service_stopped"], bool):
        raise ToolError(EXIT_SNAPSHOT_INVALID, "manifest_field_type", "manifest.service_stopped 类型错误")


def _decide_temp_root(raw: str | None, backup_root: Path) -> Path:
    temp_root_raw = Path(raw) if raw else Path(tempfile.gettempdir())
    _reject_remote(str(temp_root_raw), "--temp-root")
    system_temp = Path(os.path.abspath(str(Path(tempfile.gettempdir()))))
    _no_reparse_components(temp_root_raw, system_temp, "--temp-root", "temp_root_link")
    temp_root = _resolve(temp_root_raw)
    system_temp = _resolve(system_temp)
    if not _within(temp_root, system_temp):
        raise ToolError(EXIT_ARGUMENT, "temp_root_outside", "--temp-root 必须位于当前用户 TEMP 目录内")
    if not temp_root.exists() or not temp_root.is_dir():
        raise ToolError(EXIT_ARGUMENT, "temp_root_invalid", "--temp-root 不是已存在的目录")
    repo_root = Path(__file__).resolve().parents[1]
    if _within(temp_root, repo_root):
        raise ToolError(EXIT_ARGUMENT, "temp_root_in_repo", "--temp-root 不能位于仓库内")
    root = _resolve(backup_root)
    if _within(temp_root, root):
        raise ToolError(EXIT_ARGUMENT, "temp_root_in_backup", "--temp-root 不能位于备份根目录内")
    return temp_root


def _new_drill_dir(temp_root: Path) -> Path:
    for _ in range(5):
        name = "nc-restore-drill-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + "".join(
            random.choice("0123456789abcdef") for _ in range(6)
        )
        candidate = temp_root / name
        try:
            candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError(EXIT_DRILL_IO, "drill_dir_create_failed", f"演练目录创建失败: {type(exc).__name__}") from exc
        return candidate
    raise ToolError(EXIT_DRILL_IO, "drill_dir_create_failed", "演练目录创建失败: 名称冲突")


def _copy_snapshot(source: Path, target: Path) -> None:
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise ToolError(EXIT_DRILL_IO, "copy_failed", f"复制快照失败: {type(exc).__name__}") from exc


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.as_posix() + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise ToolError(EXIT_CHECK_FAILED, "open_failed", f"只读打开副本失败: {type(exc).__name__}") from exc


def _run_checks(connection: sqlite3.Connection) -> tuple[str, str]:
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        quick = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise ToolError(EXIT_CHECK_FAILED, "check_failed", f"完整性检查失败: {type(exc).__name__}") from exc
    integrity_value = str(integrity[0]) if integrity else "not_run"
    quick_value = str(quick[0]) if quick else "not_run"
    if integrity_value != "ok" or quick_value != "ok":
        raise ToolError(EXIT_CHECK_FAILED, "check_not_ok", "完整性检查结果不是 ok")
    return integrity_value, quick_value


def _collect_sidecars(restored: Path) -> list[str]:
    return [restored.name + suffix for suffix in SIDECAR_SUFFIXES if (restored.parent / (restored.name + suffix)).exists()]


def _remove_drill(drill_dir: Path, temp_root: Path) -> bool:
    """Remove only the drill directory created by this run, after re-checking it."""
    resolved = _resolve(drill_dir)
    if resolved.parent != _resolve(temp_root):
        return False
    if not DRILL_NAME_RE.match(resolved.name):
        return False
    if _is_reparse_point(resolved):
        return False
    allowed = {"restored"}
    restored = resolved / "restored"
    allowed_restored = {SNAPSHOT_DB} | {SNAPSHOT_DB + suffix for suffix in SIDECAR_SUFFIXES}
    try:
        top_entries = list(resolved.iterdir())
        for entry in top_entries:
            if entry.name not in allowed:
                return False
        if restored.is_dir():
            for entry in restored.iterdir():
                if entry.name not in allowed_restored:
                    return False
        shutil.rmtree(resolved)
    except OSError:
        return False
    return True


def _read_head() -> str | None:
    repo_root = Path(__file__).resolve().parents[1]
    git_dir = repo_root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref_path = git_dir / head[4:].strip()
            value = ref_path.read_text(encoding="utf-8").strip()
        else:
            value = head
    except OSError:
        return None
    return value[:7] if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _emit(report: DrillReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False))
        return
    lines = [f"RESULT={report.status}"]
    for key, value in report.as_dict().items():
        if key == "status" or value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ",".join(value)
        lines.append(f"{key.upper()}={value}")
    print("\n".join(lines))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="drill-db-restore.py",
        description="隔离恢复演练：把指定快照复制到 TEMP 目录并做只读完整性检查。",
    )
    parser.add_argument("--snapshot-dir", required=True, help="快照目录（必须位于备份根目录下）")
    parser.add_argument("--temp-root", default=None, help="演练目录的父目录，默认当前用户 TEMP")
    parser.add_argument("--dry-run", action="store_true", help="只校验路径与 manifest，不创建目录、不复制、不打开数据库")
    parser.add_argument("--json", action="store_true", help="输出非敏感 JSON")
    parser.add_argument("--version", action="version", version=f"drill-db-restore {TOOL_VERSION}")
    return parser.parse_args(argv)


def run(args: argparse.Namespace, report: DrillReport | None = None) -> DrillReport:
    if report is None:
        report = DrillReport(dry_run=args.dry_run)
    report.dry_run = bool(args.dry_run)
    report.head = _read_head()
    backup_root = BACKUP_ROOT
    snapshot_dir, manifest = _validate_snapshot_dir(args.snapshot_dir, backup_root)
    report.source = snapshot_dir.name
    report.snapshot_size = int(manifest["snapshot_size"])
    if args.dry_run:
        report.status = "DRY_RUN_OK"
        report.detail = "仅校验，未创建目录、未复制、未打开数据库"
        return report

    temp_root = _decide_temp_root(args.temp_root, backup_root)
    drill_dir = _new_drill_dir(temp_root)
    report.drill = drill_dir.name
    restored_dir = drill_dir / "restored"
    restored = restored_dir / SNAPSHOT_DB
    try:
        restored_dir.mkdir(mode=0o700)
        _copy_snapshot(snapshot_dir / SNAPSHOT_DB, restored)
        restored_size = restored.stat().st_size
        report.restored_size = restored_size
        if restored_size != report.snapshot_size:
            raise ToolError(EXIT_SIZE_MISMATCH, "size_mismatch", "副本大小与 manifest.snapshot_size 不一致")

        connection = _open_readonly(restored)
        try:
            integrity, quick = _run_checks(connection)
        finally:
            connection.close()
        report.integrity = integrity
        report.quick = quick
        report.sidecars = _collect_sidecars(restored)

        if not _remove_drill(drill_dir, temp_root):
            raise ToolError(EXIT_CLEANUP_FAILED, "cleanup_failed", "演练现场清理失败，现场已保留")
        report.cleaned = True
        report.status = "DRILL_OK"
        return report
    except ToolError:
        report.cleaned = False
        raise
    except OSError as exc:
        report.cleaned = False
        raise ToolError(EXIT_DRILL_IO, "drill_io_failed", f"演练过程失败: {type(exc).__name__}") from exc


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:  # argparse already printed usage; keep its code
        return int(exc.code or 0)
    as_json = bool(getattr(args, "json", False))
    report = DrillReport(dry_run=getattr(args, "dry_run", False))
    try:
        run(args, report)
    except ToolError as exc:
        report.status = "FAIL"
        report.category = exc.category
        report.detail = exc.detail
        _emit(report, as_json)
        return exc.code
    _emit(report, as_json)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
