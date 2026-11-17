#!/usr/bin/env python3
"""Read-only rollback inventory for NovelCraft (v1.3).

Strictly read-only: stat/lstat/iterdir/read files. It never renames,
deletes, copies, opens a database or controls a service. Any rollback
must be executed manually under a separate authorization.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import sys
from pathlib import Path

TOOL_VERSION = "1.3.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GUARD = 3
EXIT_IO = 4
EXIT_MISMATCH = 5
EXIT_ADJUDICATION = 6
EXIT_STATE = 7
EXIT_INTERNAL = 8

REPARSE_POINT = 0x0400
MAX_RECORD_BYTES = 2_097_152
MAX_SIDECAR_BYTES = 4_096
MAX_HASH_BYTES = 64 * 1024 * 1024
FORMAL_DB = "novelcraft.db"

OP_ID = r"\d{8}-\d{6}-[0-9a-z]{6}"
OP_ID_RE = re.compile(rf"^{OP_ID}$")
STALE_RE = re.compile(rf"^novelcraft\.db(?:-(wal|shm|journal))?\.stale-({OP_ID})$")
FAILED_RE = re.compile(rf"^novelcraft\.db(?:-(wal|shm|journal))?\.failed-({OP_ID})$")
TEMP_RE = re.compile(rf"^novelcraft\.db\.restore-({OP_ID})\.tmp$")
PRE_ROLLBACK_RE = re.compile(rf"^novelcraft\.db\.pre-rollback-({OP_ID})$")

SIDECAR_ROLE = {"wal": "wal", "shm": "shm", "journal": "journal"}
VALID_STATES = {f"S{i}" for i in range(10)}
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ROLE_NAME = {
    "main": "novelcraft.db",
    "wal": "novelcraft.db-wal",
    "shm": "novelcraft.db-shm",
    "journal": "novelcraft.db-journal",
    "stale_main": "novelcraft.db.stale-<id>",
    "stale_wal": "novelcraft.db-wal.stale-<id>",
    "stale_shm": "novelcraft.db-shm.stale-<id>",
    "stale_journal": "novelcraft.db-journal.stale-<id>",
    "failed_new": "novelcraft.db.failed-<id>",
    "failed_wal": "novelcraft.db-wal.failed-<id>",
    "failed_shm": "novelcraft.db-shm.failed-<id>",
    "failed_journal": "novelcraft.db-journal.failed-<id>",
    "temp_restore": "novelcraft.db.restore-<id>.tmp",
}

_LIVE_ROLES = {"main", "wal", "shm", "journal"}
_STALE_ROLES = {"stale_main", "stale_wal", "stale_shm", "stale_journal"}
_FAILED_ROLES = {"failed_new", "failed_wal", "failed_shm", "failed_journal"}

FORBIDDEN_FLAGS = {
    "--restore",
    "--replace",
    "--target-database",
    "--source-database",
    "--database",
    "--db",
    "--snapshot-dir",
    "--staging-dir",
    "--clean",
    "--delete",
    "--force",
    "--retry",
    "--confirm",
    "--apply",
    "--start-service",
    "--health-check",
    "--migrate",
}

USAGE = """usage: rollback-db.py [--operation-id <id>] [--dry-run] [--json] [--version] [--help]

Read-only inventory of a NovelCraft rollback scene. Always dry-run.
Without --operation-id the tool lists visible operation ids and exits 6.
"""


class ToolError(Exception):
    def __init__(self, code: int, category: str) -> None:
        super().__init__(category)
        self.code = code
        self.category = category


# ------------------------------------------------------------------- helpers


def _is_remote(raw: str) -> bool:
    text = str(raw)
    if text.startswith("\\\\?\\"):
        return text.upper().startswith("\\\\?\\UNC\\")
    if text.startswith("\\\\.\\"):
        return True
    return text.startswith("\\\\") or text.startswith("//")


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _no_reparse_components(path: Path, stop: Path, label: str) -> None:
    current = Path(os.path.abspath(str(path)))
    stop_abs = Path(os.path.abspath(str(stop)))
    while True:
        if _is_reparse_point(current):
            raise ToolError(EXIT_GUARD, f"{label}_reparse")
        if current == stop_abs or current.parent == current:
            return
        current = current.parent


def _identity(info: os.stat_result) -> tuple[int, int, int, int, bool]:
    for attribute in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode"):
        if not hasattr(info, attribute):
            raise ToolError(EXIT_ADJUDICATION, "identity_unavailable")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        stat_module.S_ISREG(info.st_mode),
    )


def _fd_identity(fd: int) -> tuple[int, int, int, int, bool]:
    return _identity(os.fstat(fd))


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOINHERIT", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _protected_read(
    path: Path, root: Path, label: str, max_bytes: int | None = None
) -> tuple[bytes, os.stat_result]:
    """Read bytes only after proving the opened handle is the lstat'ed object.

    The handle identity (dev/ino/size/mtime) is compared before any byte is
    read, re-checked after the read, and the path is lstat'ed again afterwards.
    A swap to a symlink/junction is therefore detected *before* bytes are used.
    Windows has no O_NOFOLLOW; there the handle identity check is the guard and
    a link swap fails it (see docs/BACKUP-RESTORE.md platform limits).
    """
    _no_reparse_components(path, root, label)
    if _is_reparse_point(path):
        raise ToolError(EXIT_GUARD, f"{label}_reparse")
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise ToolError(EXIT_ADJUDICATION, f"{label}_missing")
    except OSError:
        raise ToolError(EXIT_IO, f"{label}_stat_failed")
    if not stat_module.S_ISREG(before.st_mode):
        raise ToolError(EXIT_GUARD, f"{label}_not_regular")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ToolError(EXIT_ADJUDICATION, f"{label}_too_large")
    fd = None
    try:
        fd = os.open(path, _OPEN_FLAGS)
        if _fd_identity(fd) != _identity(before):
            raise ToolError(EXIT_ADJUDICATION, f"{label}_identity_changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        if _fd_identity(fd) != _identity(before):
            raise ToolError(EXIT_ADJUDICATION, f"{label}_changed_while_reading")
    except ToolError:
        raise
    except OSError:
        raise ToolError(EXIT_IO, f"{label}_read_failed")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        after = path.lstat()
    except OSError:
        raise ToolError(EXIT_ADJUDICATION, f"{label}_vanished")
    if _identity(after) != _identity(before):
        raise ToolError(EXIT_ADJUDICATION, f"{label}_replaced")
    if _is_reparse_point(path):
        raise ToolError(EXIT_GUARD, f"{label}_reparse")
    return data, before


def _sha256_file(path: Path, root: Path | None = None, label: str = "hash") -> tuple[str | None, str]:
    """Hash one staging/stale/failed/temp file through the protected reader.

    Live database files never reach this function: callers keep them
    metadata-only.
    """
    try:
        size = path.lstat().st_size
    except OSError:
        return None, "unreadable"
    if size > MAX_HASH_BYTES:
        return None, "skipped_large"
    try:
        data, _ = _protected_read(path, root if root is not None else path.parent, label)
    except ToolError as exc:
        if exc.code in {EXIT_ADJUDICATION, EXIT_GUARD}:
            raise
        return None, "unreadable"
    return hashlib.sha256(data).hexdigest(), "computed"


def _scan(backend: Path) -> tuple[list[dict], dict, dict, list[str]]:
    entries: list[dict] = []
    groups: dict[str, dict] = {}
    live: dict[str, dict] = {}
    duplicates: list[str] = []
    try:
        children = sorted(backend.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise ToolError(EXIT_IO, "backend_unreadable") from exc
    for child in children:
        try:
            info = child.lstat()
        except OSError as exc:
            raise ToolError(EXIT_IO, "stat_failed") from exc
        name = child.name
        role = kind = op_id = None
        match = STALE_RE.match(name)
        if match:
            kind = "stale"
            role = "stale_main" if match.group(1) is None else "stale_" + SIDECAR_ROLE[match.group(1)]
            op_id = match.group(2)
        else:
            match = FAILED_RE.match(name)
            if match:
                kind = "failed"
                role = "failed_new" if match.group(1) is None else "failed_" + SIDECAR_ROLE[match.group(1)]
                op_id = match.group(2)
            else:
                match = TEMP_RE.match(name)
                if match:
                    kind, role, op_id = "temp", "temp_restore", match.group(1)
                elif name == FORMAL_DB:
                    kind, role = "live", "main"
                elif name in {"novelcraft.db-wal", "novelcraft.db-shm", "novelcraft.db-journal"}:
                    kind, role = "live", SIDECAR_ROLE[name.split("-", 1)[1]]
        reparse = _is_reparse_point(child)
        sha, fingerprint = (None, "not_applicable")
        if kind == "live":
            # v1 boundary: the formal database and its live sidecars are
            # metadata-only.  Never read their bytes or hash them.
            fingerprint = "not_allowed_formal_db"
        elif kind in {"stale", "failed", "temp"} and not reparse:
            sha, fingerprint = _sha256_file(child, backend, f"{kind}_hash")
        entry = {
            "name": name,
            "role": role,
            "kind": kind,
            "operation_id": op_id,
            "exists": True,
            "is_file": stat_module.S_ISREG(info.st_mode),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "volume_id": getattr(info, "st_dev", None),
            "reparse": reparse,
            "sha256": sha,
            "fingerprint_status": fingerprint,
        }
        entries.append(entry)
        if kind == "live":
            if role in live:
                duplicates.append(f"live.{role}")
            else:
                live[role] = entry
        if op_id:
            slot = groups.setdefault(op_id, {"stale": {}, "failed": {}, "live": {}, "temp": []})
            if kind == "temp":
                slot["temp"].append(entry)
            else:
                bucket = slot[kind]
                if role in bucket:
                    duplicates.append(f"{kind}.{role}")
                else:
                    bucket[role] = entry
    return entries, groups, live, duplicates


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _parse_sidecar_bytes(raw: bytes) -> tuple[bool, str | None, str | None]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False, "sidecar_format_invalid", None
    tokens = text.strip().split()
    if len(tokens) != 1:
        return False, "sidecar_format_invalid", None
    token = tokens[0]
    if not HEX64_RE.match(token):
        return False, "sidecar_format_invalid", None
    return True, None, token.lower()


def _hash_matches(path: Path, sidecar: Path, root: Path, label: str) -> tuple[bool, str | None]:
    try:
        sidecar_raw, _ = _protected_read(sidecar, root, f"{label}_sidecar", MAX_SIDECAR_BYTES)
        ok, problem, expected = _parse_sidecar_bytes(sidecar_raw)
        if not ok:
            return False, problem
        raw, _ = _protected_read(path, root, label, MAX_RECORD_BYTES)
    except ToolError:
        raise
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "json_unparsable"
    actual = hashlib.sha256(_canonical_bytes(parsed)).hexdigest()
    if actual != expected:
        return False, "hash_mismatch"
    return True, None


def _guarded_file(json_path: Path, sidecar_path: Path, root: Path, label: str) -> tuple[dict | None, str | None]:
    """Read one operations JSON + sidecar through protected reads only."""
    try:
        _no_reparse_components(json_path, root, label)
        _no_reparse_components(sidecar_path, root, label)
        if _is_reparse_point(json_path) or _is_reparse_point(sidecar_path):
            raise ToolError(EXIT_GUARD, f"{label}_reparse")
        try:
            json_info = json_path.lstat()
        except FileNotFoundError:
            return None, f"{label}_missing"
        try:
            sidecar_info = sidecar_path.lstat()
        except FileNotFoundError:
            return None, f"{label}_sidecar_missing"
        # lstat + S_ISREG only: Path.is_file()/stat() would follow links.
        if not stat_module.S_ISREG(json_info.st_mode):
            return None, f"{label}_not_regular"
        if not stat_module.S_ISREG(sidecar_info.st_mode):
            return None, f"{label}_sidecar_not_regular"
        if json_info.st_size > MAX_RECORD_BYTES:
            return None, f"{label}_too_large"
        ok, problem = _hash_matches(json_path, sidecar_path, root, label)
        if not ok:
            return None, f"{label}_{problem}"
        raw, _ = _protected_read(json_path, root, label, MAX_RECORD_BYTES)
        parsed = json.loads(raw.decode("utf-8"))
    except ToolError:
        raise
    except OSError:
        return None, f"{label}_unreadable"
    if not isinstance(parsed, dict):
        return None, f"{label}_schema_invalid"
    return parsed, None


def _expected_operations_root() -> Path:
    """Return the one allow-listed operations root for this user."""
    return Path.home() / "NovelCraft-backups" / "operations"


def _normalized_path(path: Path) -> str:
    """Absolute, case-folded form used for identity comparison only."""
    return os.path.normcase(os.path.abspath(str(path)))


def _operations_root(operations_root: Path) -> None:
    if _is_remote(str(operations_root)):
        raise ToolError(EXIT_GUARD, "operations_root_remote")
    _no_reparse_components(operations_root, Path(operations_root.anchor), "operations_root")
    if _is_reparse_point(operations_root):
        raise ToolError(EXIT_GUARD, "operations_root_reparse")
    try:
        info = operations_root.lstat()
    except FileNotFoundError:
        raise ToolError(EXIT_ADJUDICATION, "operations_root_missing")
    except OSError as exc:
        raise ToolError(EXIT_ADJUDICATION, "operations_root_identity_unavailable") from exc
    if not stat_module.S_ISDIR(info.st_mode):
        raise ToolError(EXIT_GUARD, "operations_root_not_directory")
    expected = _expected_operations_root()
    if _is_remote(str(expected)):
        raise ToolError(EXIT_ADJUDICATION, "expected_root_unavailable")
    try:
        _no_reparse_components(expected, Path(expected.anchor), "expected_root")
        if _is_reparse_point(expected):
            raise ToolError(EXIT_ADJUDICATION, "expected_root_unavailable")
        expected_info = expected.lstat()
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(EXIT_ADJUDICATION, "expected_root_unavailable") from exc
    if not stat_module.S_ISDIR(expected_info.st_mode):
        raise ToolError(EXIT_ADJUDICATION, "expected_root_unavailable")
    if _normalized_path(operations_root) != _normalized_path(expected):
        raise ToolError(EXIT_GUARD, "operations_root_unexpected")


def _dir_identity(path: Path, label: str) -> dict:
    """Capture a directory's identity without following links.

    The tuple is intentionally metadata-only. A platform without a stable
    file index still gives us (dev, ino) on Windows, but if lstat fails or the
    object is not a directory we refuse to continue rather than guessing.
    """
    if _is_reparse_point(path):
        raise ToolError(EXIT_GUARD, f"{label}_reparse")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ToolError(EXIT_ADJUDICATION, f"{label}_identity_unavailable") from exc
    if not stat_module.S_ISDIR(info.st_mode):
        raise ToolError(EXIT_GUARD, f"{label}_not_directory")
    for attribute in ("st_dev", "st_ino", "st_mtime_ns"):
        if not hasattr(info, attribute):
            raise ToolError(EXIT_ADJUDICATION, f"{label}_identity_unavailable")
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
    }


def _directory_change(before: dict, after: dict, label: str) -> str | None:
    if before["dev"] != after["dev"]:
        return f"{label}_volume_changed"
    if before["ino"] != after["ino"]:
        return f"{label}_directory_replaced"
    if before["mtime_ns"] != after["mtime_ns"]:
        return f"{label}_directory_modified"
    return None


def _read_record(operations_root: Path, op_id: str) -> tuple[dict | None, str | None]:
    parsed, problem = _guarded_file(
        operations_root / f"{op_id}.json",
        operations_root / f"{op_id}.json.sha256",
        operations_root,
        "record",
    )
    if problem:
        return None, problem
    for key in ("operation_id", "state", "current_step", "files", "authorization_ref"):
        if key not in parsed:
            return None, "record_schema_invalid"
    if parsed["operation_id"] != op_id:
        return None, "record_id_mismatch"
    if not isinstance(parsed["state"], str) or parsed["state"] not in VALID_STATES:
        return None, "record_schema_invalid"
    if not isinstance(parsed["current_step"], int) or isinstance(parsed["current_step"], bool):
        return None, "record_schema_invalid"
    if not 0 <= parsed["current_step"] <= 12:
        return None, "record_schema_invalid"
    if not isinstance(parsed["files"], list) or not isinstance(parsed["authorization_ref"], str):
        return None, "record_schema_invalid"
    for item in parsed["files"]:
        if not isinstance(item, dict):
            return None, "record_schema_invalid"
    return parsed, None


def _read_scene(operations_root: Path, op_id: str) -> tuple[dict | None, str | None]:
    parsed, problem = _guarded_file(
        operations_root / f"{op_id}-scene.json",
        operations_root / f"{op_id}-scene.json.sha256",
        operations_root,
        "scene",
    )
    if problem:
        return None, None if problem == "scene_missing" else problem
    if parsed.get("operation_id") != op_id:
        return None, "scene_id_mismatch"
    if not isinstance(parsed.get("files"), list):
        return None, "scene_schema_invalid"
    record_path = parsed.get("record_path")
    if record_path is not None:
        if not _valid_rel_name(record_path, f"{op_id}.json"):
            return None, "scene_record_path_invalid"
    record_sha = parsed.get("record_sha256")
    if record_sha is not None:
        if not isinstance(record_sha, str) or not HEX64_RE.match(record_sha):
            return None, "scene_record_hash_invalid"
        try:
            raw, _ = _protected_read(
                operations_root / f"{op_id}.json", operations_root, "scene_record_hash", MAX_RECORD_BYTES
            )
            actual = hashlib.sha256(raw).hexdigest()
        except ToolError:
            raise
        except OSError:
            return None, "scene_record_hash_invalid"
        if actual.lower() != record_sha.lower():
            return None, "scene_record_hash_mismatch"
    return parsed, None


def _valid_rel_name(value, expected: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value in {".", ".."}:
        return False
    if "/" in value or "\\" in value or ":" in value:
        return False
    return value == expected


def _record_path_ok(value, expected: str) -> bool:
    """Allow either the bare file name or 'backend/<name>' (both separators)."""
    if not isinstance(value, str) or not value:
        return False
    if value == expected:
        return True
    parts = re.split(r"[\\/]", value)
    return len(parts) == 2 and parts[0] == "backend" and parts[1] == expected


def _read_pre_record(operations_root: Path, op_id: str) -> tuple[dict | None, str | None]:
    parsed, problem = _guarded_file(
        operations_root / f"pre-rollback-{op_id}.json",
        operations_root / f"pre-rollback-{op_id}.json.sha256",
        operations_root,
        "pre_rollback",
    )
    if problem:
        return None, problem
    if parsed.get("operation_id") != op_id:
        return None, "pre_rollback_id_mismatch"
    path = parsed.get("path")
    if not isinstance(path, str) or not PRE_ROLLBACK_RE.match(path):
        return None, "pre_rollback_schema_invalid"
    digest = parsed.get("sha256")
    if not isinstance(digest, str) or not HEX64_RE.match(digest):
        return None, "pre_rollback_schema_invalid"
    size = parsed.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None, "pre_rollback_schema_invalid"
    return parsed, None


def _pre_rollback(operations_root: Path, record: dict | None, op_id: str) -> tuple[bool, str | None]:
    if not record:
        return False, "pre_rollback_record_missing"
    value = record.get("pre_rollback")
    if not isinstance(value, dict) or value.get("present") is not True:
        return False, "pre_rollback_record_missing"
    path = value.get("path")
    if not isinstance(path, str) or not PRE_ROLLBACK_RE.match(path) or not path.endswith(op_id):
        return False, "pre_rollback_path_invalid"
    digest = value.get("sha256")
    if not isinstance(digest, str) or not HEX64_RE.match(digest):
        return False, "pre_rollback_hash_invalid"
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return False, "pre_rollback_size_invalid"
    pre, problem = _read_pre_record(operations_root, op_id)
    if problem:
        return False, problem
    if pre.get("path") != path or pre.get("sha256", "").lower() != digest.lower():
        return False, "pre_rollback_mismatch"
    if pre.get("size") != size:
        return False, "pre_rollback_size_mismatch"
    return True, None


def _state_consistency(state: str, live: bool, group: dict, record: dict) -> list[str]:
    stale = group.get("stale") or {}
    failed = group.get("failed") or {}
    temp_files = group.get("temp") or []
    problems: list[str] = []
    if state in {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"} and "stale_main" not in stale:
        problems.append("state_requires_stale_main")
    if state == "S0" and (stale or failed or temp_files):
        problems.append("state_contradicts_scene")
    if state in {"S1", "S2", "S3", "S4", "S5", "S6"} and live:
        problems.append("target_name_conflict")
    if state in {"S2", "S3"} and not any(k.startswith("stale_") for k in stale if k != "stale_main"):
        problems.append("state_requires_stale_sidecar")
    if state in {"S4", "S5", "S6"} and not temp_files:
        problems.append("state_requires_temp_file")
    if state in {"S5", "S6"} and temp_files and "stale_main" in stale:
        temp, main = temp_files[0], stale["stale_main"]
        if temp["size"] != main["size"] or temp.get("sha256") != main.get("sha256"):
            problems.append("temp_fingerprint_mismatch")
    if state in {"S7", "S8", "S9"} and not live:
        problems.append("state_requires_live_target")
    if state in {"S8", "S9"} and not failed:
        problems.append("state_requires_failed_group")
    if state == "S7" and failed:
        problems.append("state_contradicts_failed_group")
    if state in {"S1", "S2", "S3", "S4", "S5", "S6"} and failed:
        problems.append("failed_group_mixed_with_stale")
    if record.get("current_step") == 0 and state not in {"S0"}:
        problems.append("record_step_contradicts_state")
    return problems


def _entry_for_role(group: dict, role: str, live: dict | None = None) -> dict | None:
    if role in _LIVE_ROLES:
        if live is not None:
            return live.get(role)
        return group["live"].get(role)
    if role in _STALE_ROLES:
        return group["stale"].get(role)
    if role in _FAILED_ROLES:
        return group["failed"].get(role)
    if role == "temp_restore":
        temps = group["temp"]
        return temps[0] if temps else None
    return None


def _record_mismatches(record: dict | None, group: dict, op_id: str, live: dict | None = None) -> tuple[list[str], list[str]]:
    mismatches: list[str] = []
    unverifiable: list[str] = []
    if not record:
        return mismatches, unverifiable
    files = record.get("files")
    if not isinstance(files, list):
        mismatches.append("record_schema_invalid:files")
        return mismatches, unverifiable
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            mismatches.append("record_schema_invalid:item")
            continue
        role_raw = item.get("role")
        if not isinstance(role_raw, str) or not role_raw:
            mismatches.append("record_schema_invalid:role")
            continue
        role = role_raw
        expected = _ROLE_NAME.get(role)
        if expected is None:
            mismatches.append(f"unknown_role:{role}")
            continue
        if "<id>" in expected:
            at = expected.index("<id>")
            expected = expected[:at] + op_id + expected[at + 4 :]
        if role in seen:
            mismatches.append(f"duplicate_record_role:{role}")
            continue
        seen.add(role)
        for field in ("original_path", "actual_path"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                mismatches.append(f"record_field_invalid:{role}:{field}")
            elif not _record_path_ok(value, expected):
                mismatches.append(f"{field}_invalid:{role}")
        exists = item.get("exists")
        if not isinstance(exists, bool):
            mismatches.append(f"record_field_invalid:{role}:exists")
            exists = None
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            mismatches.append(f"record_field_invalid:{role}:size")
            size = None
        mtime = item.get("mtime_ns")
        if not isinstance(mtime, int) or isinstance(mtime, bool):
            mismatches.append(f"record_field_invalid:{role}:mtime_ns")
            mtime = None
        digest = item.get("sha256")
        if role in _LIVE_ROLES:
            # Formal database and live sidecars are metadata-only in v1.
            if digest is not None:
                if isinstance(digest, str) and HEX64_RE.match(digest):
                    unverifiable.append(f"live_sha_not_comparable:{role}")
                else:
                    mismatches.append(f"record_field_invalid:{role}:sha256")
            digest = None
        elif not isinstance(digest, str) or not HEX64_RE.match(digest):
            mismatches.append(f"record_field_invalid:{role}:sha256")
            digest = None
        actual = _entry_for_role(group, role, live)
        if exists is True and actual is None:
            mismatches.append(f"missing:{role}")
            continue
        if exists is False:
            if actual is not None:
                mismatches.append(f"unexpected_present:{role}")
            continue
        if actual is None:
            continue
        if size is not None and actual["size"] != size:
            mismatches.append(f"size:{role}")
        if mtime is not None and actual["mtime_ns"] != mtime:
            mismatches.append(f"mtime:{role}")
        if digest is not None:
            if actual.get("sha256") is None:
                unverifiable.append(f"sha_unavailable:{role}")
            elif actual["sha256"].lower() != digest.lower():
                mismatches.append(f"sha256:{role}")
    return mismatches, unverifiable


def _inspect_target(backend: Path) -> dict:
    target = backend / FORMAL_DB
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {"exists": False, "is_file": False, "reparse": False}
    except OSError as exc:
        raise ToolError(EXIT_IO, "formal_target_stat_failed") from exc
    reparse = _is_reparse_point(target)
    return {
        "exists": True,
        # Never consult Path.is_file(): it follows links.  The lstat result is
        # authoritative and a reparse target is never treated as a regular file.
        "is_file": stat_module.S_ISREG(info.st_mode) and not reparse,
        "reparse": reparse,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "volume_id": getattr(info, "st_dev", None),
    }


def _inventory(operations_root: Path, backend: Path, json_mode: bool) -> int:
    _operations_root(operations_root)
    operations_before = _dir_identity(operations_root, "operations")
    backend_before = _dir_identity(backend, "backend")
    entries, backend_groups, _, duplicates = _scan(backend)
    verified: set[str] = set()
    rejected: list[str] = []
    try:
        children = sorted(operations_root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise ToolError(EXIT_IO, "operations_root_unreadable") from exc
    for child in children:
        name = child.name
        if not name.endswith(".json") or name.endswith(".json.sha256"):
            continue
        stem = name[:-5]
        if not OP_ID_RE.match(stem):
            rejected.append("invalid_name")
            continue
        if _is_reparse_point(child):
            rejected.append(f"reparse:{stem}")
            continue
        try:
            info = child.lstat()
        except OSError:
            rejected.append(f"unreadable:{stem}")
            continue
        if not stat_module.S_ISREG(info.st_mode):
            rejected.append(f"not_a_file:{stem}")
            continue
        if info.st_size > MAX_RECORD_BYTES:
            rejected.append(f"too_large:{stem}")
            continue
        sidecar = operations_root / f"{stem}.json.sha256"
        if _is_reparse_point(sidecar):
            rejected.append(f"sidecar_reparse:{stem}")
            continue
        try:
            sinfo = sidecar.lstat()
        except OSError:
            rejected.append(f"sidecar_missing:{stem}")
            continue
        if not stat_module.S_ISREG(sinfo.st_mode):
            rejected.append(f"sidecar_not_file:{stem}")
            continue
        if sinfo.st_size > MAX_SIDECAR_BYTES:
            rejected.append(f"sidecar_too_large:{stem}")
            continue
        try:
            ok, problem = _hash_matches(child, sidecar, operations_root, f"inventory_{stem}")
        except ToolError as exc:
            rejected.append(f"{exc.category}:{stem}")
            continue
        if not ok:
            rejected.append(f"{problem}:{stem}")
            continue
        verified.add(stem)
    ids = sorted(verified | set(backend_groups))
    identity_problems = [
        problem
        for problem in (
            _directory_change(backend_before, _dir_identity(backend, "backend"), "backend"),
            _directory_change(operations_before, _dir_identity(operations_root, "operations"), "operations"),
        )
        if problem
    ]
    if identity_problems:
        rejected.extend(identity_problems)
    payload = {
        "RESULT": "INVENTORY",
        "MODE": "inventory",
        "STATE": None,
        "OPERATION_ID": None,
        "OPERATION_IDS": ids,
        "INVENTORY_REJECTED": sorted(set(rejected)),
        "GROUPS": {
            key: sorted(
                list(backend_groups[key]["stale"]) + list(backend_groups[key]["failed"])
            )
            for key in sorted(backend_groups)
        },
        "MISSING_MEMBERS": [],
        "NEEDS_HUMAN_ADJUDICATION": ["operation_id_required"],
        "FAILED_GROUP": None,
        "TEMP_FILES": [],
        "FINGERPRINTS": {},
        "GUARDS": {},
        "GUARDS_PASS": False,
        "BLOCKERS": ["inventory_mode"] + (["duplicate_members"] if duplicates else []) + identity_problems,
        "MANUAL_CONFIRMATIONS": ["select_operation_id"],
        "DUPLICATE_MEMBERS": sorted(set(duplicates)),
        "RECORD_MISMATCHES": [],
        "ROLLBACK_PLAN_ONLY": True,
        "SIDE_EFFECTS": False,
        "TOOL_VERSION": TOOL_VERSION,
    }
    _emit(payload, json_mode)
    return EXIT_ADJUDICATION

def _guards(probe, target: dict, group: dict | None, record: dict | None, state: str | None, checks: dict) -> dict:
    stale = (group or {}).get("stale") or {}
    service = probe() if callable(probe) else None
    guards: dict[str, dict] = {}

    def put(name: str, status: str, detail: str) -> None:
        guards[name] = {"status": status, "detail": detail}

    put("independent_rollback_authorization", "pass" if record and record.get("authorization_ref") else "unknown", "recorded" if record else "no_record")
    if service is None:
        put("backend_stopped", "unknown", "service_state_not_probed")
        put("port_8000_not_listening", "unknown", "service_state_not_probed")
        put("no_db_access_process", "unknown", "process_state_not_probed")
    else:
        put("backend_stopped", "pass" if not service.get("backend_running") else "fail", "probed")
        put("port_8000_not_listening", "pass" if not service.get("port_8000_listening") else "fail", "probed")
        put("no_db_access_process", "pass" if not service.get("db_access_process") else "fail", "probed")
    if target.get("reparse"):
        put("formal_target_non_reparse", "fail", "reparse")
    elif not target.get("exists"):
        put("formal_target_non_reparse", "unknown", "not_present")
    elif not target.get("is_file"):
        put("formal_target_regular_file", "fail", "not_regular_file")
        put("formal_target_non_reparse", "pass", "not_reparse")
    else:
        put("formal_target_non_reparse", "pass", "not_reparse")
        put("formal_target_regular_file", "pass", "regular_file")
    put("directory_object_stable", checks["dir_status"], checks["dir_detail"])
    put("volume_matches_record", "pass" if checks["volume_ok"] else "fail", "compared")
    put("fingerprints_available", "pass" if checks["fingerprints_ok"] else "fail", "compared")
    put("stale_main_unique", "pass" if checks["stale_unique"] else "fail", "checked")
    put("stale_main_present", "pass" if "stale_main" in stale else "unknown", "present" if "stale_main" in stale else "absent")
    put("stale_group_same_operation_id", "pass", "same_id")
    put("stale_sidecars_same_operation_id", "pass", "same_id")
    put("temp_in_backend_directory", "pass" if checks["temp_in_backend"] else "fail", "checked")
    put("target_name_conflict", "fail" if state in {"S3", "S4", "S5", "S6"} and target.get("exists") and "stale_main" in stale else "pass", "checked")
    put("no_delete_or_overwrite", "pass", "policy")
    put("no_auto_second_rollback", "pass", "policy")
    put("no_start_or_migrate", "pass", "policy")
    return guards


def _emit(payload: dict, json_mode: bool) -> None:
    if json_mode:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            sys.stdout.write(f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}\n")
        elif isinstance(value, bool):
            sys.stdout.write(f"{key}={'true' if value else 'false'}\n")
        elif value is None:
            sys.stdout.write(f"{key}=null\n")
        else:
            sys.stdout.write(f"{key}={value}\n")


def _fail(code: int, category: str, json_mode: bool) -> int:
    _emit({"RESULT": "ERROR", "ERROR_CATEGORY": category, "SIDE_EFFECTS": False}, json_mode)
    return code


def _parse(argv: list[str]) -> dict:
    if "--help" in argv:
        return {"help": True}
    if "--version" in argv:
        return {"version": True}
    options = {"operation_id": None, "json": "--json" in argv}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in FORBIDDEN_FLAGS:
            raise ToolError(EXIT_USAGE, "unsupported_argument")
        if arg in {"--json", "--dry-run"}:
            index += 1
            continue
        if arg == "--operation-id":
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise ToolError(EXIT_USAGE, "invalid_operation_id")
            value = argv[index + 1]
            if not OP_ID_RE.match(value):
                raise ToolError(EXIT_USAGE, "invalid_operation_id")
            options["operation_id"] = value
            index += 2
            continue
        raise ToolError(EXIT_USAGE, "unsupported_argument")
    return options


def _run(options: dict, repo_root: Path, operations_root: Path, probe) -> int:
    json_mode = bool(options.get("json"))
    backend = repo_root / "backend"
    if _is_remote(str(backend)):
        raise ToolError(EXIT_GUARD, "path_remote")
    _no_reparse_components(backend, repo_root, "backend")
    if _is_reparse_point(backend):
        raise ToolError(EXIT_GUARD, "backend_reparse")
    try:
        backend_info = backend.lstat()
    except FileNotFoundError:
        raise ToolError(EXIT_GUARD, "backend_missing")
    if not stat_module.S_ISDIR(backend_info.st_mode):
        raise ToolError(EXIT_GUARD, "backend_missing")

    if options["operation_id"] is None:
        return _inventory(operations_root, backend, json_mode)

    if operations_root.exists():
        _operations_root(operations_root)

    backend_before = _dir_identity(backend, "backend")
    operations_before = _dir_identity(operations_root, "operations") if operations_root.exists() else None
    before = backend.lstat()
    entries, groups, live_entries, duplicates = _scan(backend)
    after = backend.lstat()

    op_id = options["operation_id"]
    record, record_problem = _read_record(operations_root, op_id) if operations_root.exists() else (None, "record_missing")
    scene, scene_problem = _read_scene(operations_root, op_id) if operations_root.exists() else (None, None)

    group = groups.get(op_id) or {"stale": {}, "failed": {}, "live": {}, "temp": []}
    other_ids = sorted(key for key in groups if key != op_id)
    target = _inspect_target(backend)
    live = target.get("exists", False)
    reparse_entries = [entry for entry in entries if entry["reparse"]]

    adjudication: list[str] = []
    blockers: list[str] = []
    manual: list[str] = []
    if record_problem:
        adjudication.append(record_problem)
    if scene_problem:
        adjudication.append(scene_problem)
    if other_ids:
        blockers.append("other_operation_groups_present")
    if reparse_entries:
        blockers.append("reparse_present")

    state: str | None = None
    mismatches: list[str] = []
    unverifiable: list[str] = []
    if record is not None:
        state = record["state"]
        problems = _state_consistency(state, live, group, record)
        if problems:
            adjudication.append("state_contradicts_scene")
            adjudication.extend(problems)
        mismatches, unverifiable = _record_mismatches(record, group, op_id, live_entries)
        pre_ok, pre_problem = _pre_rollback(operations_root, record, op_id)
        if state in {"S7", "S8", "S9"} and not pre_ok:
            blockers.append("pre_rollback_record_incomplete")
            if pre_problem:
                adjudication.append(pre_problem)
    else:
        state = None
        adjudication.append("state_indeterminate")

    declared_groups = (record or {}).get("groups") or {}
    volume_ok = True
    for bucket in ("stale", "failed", "live"):
        meta = declared_groups.get(bucket) or {}
        for role, item in meta.items():
            if isinstance(item, dict) and item.get("volume_id") is not None:
                entry = _entry_for_role(group, role, live_entries)
                if not entry or entry.get("volume_id") != item.get("volume_id"):
                    volume_ok = False

    dir_object = (record or {}).get("dir_object")
    dir_status = "fail" if (after.st_mtime_ns != before.st_mtime_ns or after.st_dev != before.st_dev) else "pass"
    dir_detail = "scan_window_compared"
    if isinstance(dir_object, dict):
        if dir_object.get("mtime_ns") is not None:
            if after.st_mtime_ns != dir_object.get("mtime_ns"):
                dir_status = "fail"
            dir_detail = "compared_mtime"
        if dir_object.get("volume_id") is not None:
            if after.st_dev != dir_object.get("volume_id"):
                dir_status = "fail"
            dir_detail = "compared_volume"
        if dir_object.get("index") is not None:
            dir_status = "unknown"
            dir_detail = "index_unavailable"

    temp_in_backend = True
    for entry in group["temp"]:
        if entry.get("kind") != "temp":
            temp_in_backend = False
    declared_temp = False
    if record:
        for item in record.get("files") or []:
            if isinstance(item, dict) and item.get("role") == "temp_restore":
                declared_temp = True
                value = item.get("actual_path")
                if not isinstance(value, str) or not _record_path_ok(value, f"novelcraft.db.restore-{op_id}.tmp"):
                    temp_in_backend = False
        if group["temp"] and not declared_temp:
            temp_in_backend = False
    stale_unique = "stale.stale_main" not in duplicates

    fingerprints_ok = not mismatches and not unverifiable
    for kind in ("stale", "failed"):
        for role, entry in (group[kind] or {}).items():
            if entry.get("fingerprint_status") not in {"computed", "not_applicable"}:
                fingerprints_ok = False
    for entry in group["temp"]:
        if entry.get("fingerprint_status") not in {"computed", "not_applicable"}:
            fingerprints_ok = False

    # Final directory identity check: after every record/scene/pre-rollback read
    # and hash, immediately before the guards and plan are computed.
    identity_problems = [_directory_change(backend_before, _dir_identity(backend, "backend"), "backend")]
    if operations_before is not None:
        identity_problems.append(
            _directory_change(operations_before, _dir_identity(operations_root, "operations"), "operations")
        )
    for problem in identity_problems:
        if problem:
            blockers.append(problem)
            adjudication.append(problem)

    checks = {
        "dir_status": dir_status,
        "dir_detail": dir_detail,
        "volume_ok": volume_ok,
        "fingerprints_ok": fingerprints_ok,
        "stale_unique": stale_unique,
        "temp_in_backend": temp_in_backend,
        "duplicates": duplicates,
    }
    guards = _guards(probe, target, group, record, state, checks)
    if adjudication:
        guards["state_consistency"] = {"status": "fail", "detail": "needs_human_adjudication"}
    blockers.extend([f"unverified:{name}" for name, item in guards.items() if item["status"] == "unknown"])
    if any(item["status"] == "fail" for item in guards.values()):
        blockers.extend([name for name, item in guards.items() if item["status"] == "fail"])

    stale = group.get("stale") or {}
    failed = group.get("failed") or {}
    temp_files = group["temp"]

    missing_members: list[str] = []
    if record:
        for item in record.get("files") or []:
            if isinstance(item, dict) and item.get("exists") is False:
                missing_members.append(str(item.get("role")))
    if "stale_wal" in stale and stale["stale_wal"].get("size", 0) > 0:
        manual.append("nonempty_stale_wal")
    if failed:
        manual.append("failed_group_requires_manual_isolation")
    if record is None:
        manual.append("record_missing")
    if mismatches:
        manual.append("record_scene_mismatch")
    if unverifiable:
        manual.append("fingerprints_not_comparable")

    if state == "S0":
        blockers.append("state_not_recoverable")

    guards_pass = all(item["status"] == "pass" for item in guards.values())
    if not guards_pass and state != "S0":
        manual.append("guards_not_all_pass")

    code = EXIT_OK
    if any(item in {"reparse_present", "path_reparse", "path_remote", "volume_matches_record", "directory_object_stable", "formal_target_non_reparse", "formal_target_regular_file"} for item in blockers):
        code = EXIT_GUARD
    elif state == "S0" or "pre_rollback_record_incomplete" in blockers:
        code = EXIT_STATE
    elif mismatches:
        code = EXIT_MISMATCH
    elif blockers:
        code = EXIT_ADJUDICATION
    elif adjudication or unverifiable:
        code = EXIT_ADJUDICATION
    elif not guards_pass:
        code = EXIT_ADJUDICATION

    _emit(
        {
            "RESULT": "PLAN" if code == EXIT_OK else "BLOCKED",
            "MODE": "single",
            "STATE": state,
            "OPERATION_ID": op_id,
            "OPERATION_IDS": sorted(set(groups) | {op_id}),
            "GROUPS": {
                "stale": sorted(stale),
                "failed": sorted(failed),
                "temp": sorted(entry["name"] for entry in temp_files),
            },
            "MISSING_MEMBERS": sorted(set(missing_members)),
            "NEEDS_HUMAN_ADJUDICATION": sorted(set(adjudication)),
            "FAILED_GROUP": sorted(failed) if failed else None,
            "TEMP_FILES": sorted(entry["name"] for entry in temp_files),
            "FINGERPRINTS": {
                f"{kind}.{role}": entry.get("fingerprint_status", "unknown")
                for kind in ("stale", "failed")
                for role, entry in (group[kind] or {}).items()
            },
            "GUARDS": guards,
            "GUARDS_PASS": guards_pass,
            "BLOCKERS": sorted(set(blockers)),
            "MANUAL_CONFIRMATIONS": sorted(set(manual)),
            "DUPLICATE_MEMBERS": sorted(set(duplicates)),
            "RECORD_MISMATCHES": sorted(set(mismatches)),
            "ROLLBACK_PLAN_ONLY": True,
            "SIDE_EFFECTS": False,
            "TOOL_VERSION": TOOL_VERSION,
        },
        json_mode,
    )
    return code


def main(argv: list[str] | None = None, *, repo_root: Path | None = None, operations_root: Path | None = None, probe_service=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    try:
        options = _parse(args)
        if options.get("help"):
            sys.stdout.write(USAGE)
            return EXIT_OK
        if options.get("version"):
            sys.stdout.write(f"rollback-db {TOOL_VERSION}\n")
            return EXIT_OK
        root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
        ops = Path(operations_root) if operations_root is not None else Path.home() / "NovelCraft-backups" / "operations"
        return _run(options, root, ops, probe_service)
    except ToolError as exc:
        return _fail(exc.code, exc.category, json_mode)
    except OSError:
        return _fail(EXIT_IO, "io_error", json_mode)
    except Exception:
        return _fail(EXIT_INTERNAL, "internal_error", json_mode)


if __name__ == "__main__":
    sys.exit(main())
