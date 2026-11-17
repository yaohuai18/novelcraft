#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NovelCraft SQLite 在线快照工具（只读源库，快照落在仓库外）。

设计要点
--------
* 只使用 Python 标准库 ``sqlite3``，**不 import 后端 app 包**，
  因此不会加载 ``app.core.config``、也不会读取 ``backend/.env``。
* 源数据库一律以 ``mode=ro`` 只读 URI 打开；快照通过
  ``sqlite3.Connection.backup`` 在线完成，能正确处理 WAL 中间态。
* 输出目录必须位于仓库之外；目标目录/文件已存在时拒绝覆盖。
* 先写临时文件，校验通过后再原子重命名；失败只清理本次新建的临时文件。
* 只计算**快照文件**的 SHA-256，绝不计算、也不输出源数据库的哈希。

用法::

    python scripts/db-snapshot.py --database backend/novelcraft.db --dry-run
    python scripts/db-snapshot.py --database backend/novelcraft.db --label "发布前"

退出码
------
0 成功 / dry-run 通过；2 参数错误；3 校验失败；4 目标冲突；
5 备份失败；6 完整性检查失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

TOOL_VERSION = "1.0.0"
SQLITE_MAGIC = b"SQLite format 3\x00"
ALLOWED_SUFFIXES = {".db", ".sqlite", ".sqlite3"}

# 并发与可审查参数：不无限重试，任何锁定都转成明确的非零退出码。
SOURCE_TIMEOUT_SECONDS = 30
BUSY_TIMEOUT_MS = 30_000
BACKUP_PAGES = 1024
BACKUP_SLEEP_SECONDS = 0.05
BACKUP_MAX_STEPS = 200_000

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_CONFLICT = 4
EXIT_BACKUP = 5
EXIT_INTEGRITY = 6


class SnapshotError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _fail(message: str, code: int) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_remote_path(raw: str) -> bool:
    """判断原始参数字符串是否为 UNC / 远程 / 设备路径。

    只做字符串判断，不触碰文件系统，也不发起任何网络访问。
    ``\\\\?\\C:\\...`` 是本地长路径，允许；``\\\\?\\UNC\\...`` 与 ``\\\\server\\share`` 拒绝。
    """
    value = raw.strip()
    if value.startswith("\\\\?\\"):
        return value.upper().startswith("\\\\?\\UNC\\")
    if value.startswith("\\\\.\\"):
        return True  # 设备路径，不是本地文件
    return value.startswith("\\\\") or value.startswith("//")


def _check_local_path(raw: str, label: str) -> None:
    if _is_remote_path(raw):
        _fail(f"{label} 是 UNC/远程或设备路径，拒绝执行: {raw}", EXIT_VALIDATION)


def _resolve_repo(arg: str | None) -> Path:
    if arg:
        repo = Path(arg).resolve()
    else:
        repo = Path(__file__).resolve().parents[1]
    if not repo.is_dir():
        _fail(f"--repo 不是目录: {repo}", EXIT_VALIDATION)
    return repo


def _resolve_head(arg: str | None, repo: Path) -> str | None:
    if arg:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", arg):
            _fail("--head 必须是 7-40 位十六进制提交号", EXIT_USAGE)
        return arg.lower()
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _readonly_uri(path: Path) -> str:
    # Path.as_uri() 会百分号编码空格与中文，避免 URI 解析失败。
    return path.as_uri() + "?mode=ro"


def _open_source(path: Path) -> sqlite3.Connection:
    """只读 + 明确 busy_timeout 的源连接；绝不使用可写连接。"""
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=SOURCE_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _open_target(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=SOURCE_TIMEOUT_SECONDS)


def _backup_into(source_conn: sqlite3.Connection, target_conn: sqlite3.Connection) -> None:
    """分批 copy，并用步数上限兜底，避免异常情况下无限复制。"""
    steps = 0

    def _progress(_status: int, _remaining: int, _total: int) -> None:
        nonlocal steps
        steps += 1
        if steps > BACKUP_MAX_STEPS:
            raise SnapshotError("backup 分页步数超过上限，已中止", EXIT_BACKUP)

    source_conn.backup(
        target_conn,
        pages=BACKUP_PAGES,
        progress=_progress,
        sleep=BACKUP_SLEEP_SECONDS,
    )


def _sidecar_paths(tmp_snapshot: Path) -> tuple[Path, Path]:
    """由临时快照路径派生 SQLite 边车文件（-wal / -shm），不拼接通配符。"""
    return (
        tmp_snapshot.with_name(tmp_snapshot.name + "-wal"),
        tmp_snapshot.with_name(tmp_snapshot.name + "-shm"),
    )


def _remove_sidecars(tmp_snapshot: Path) -> None:
    """只删除本次临时快照的两个边车文件；不存在视为正常，失败则明确报错。"""
    for sidecar in _sidecar_paths(tmp_snapshot):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SnapshotError(
                f"临时边车文件清理失败: {sidecar} ({type(exc).__name__})",
                EXIT_BACKUP,
            ) from exc


def _check_source(path: Path, *, deep: bool) -> None:
    if not path.exists():
        _fail(f"源数据库不存在: {path}", EXIT_VALIDATION)
    if not path.is_file():
        _fail(f"源路径不是文件: {path}", EXIT_VALIDATION)
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        _fail(f"源文件后缀不是 SQLite 数据库: {path.suffix or '<none>'}", EXIT_VALIDATION)
    if not deep:
        return
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except OSError as exc:
        _fail(f"无法读取源文件头: {exc}", EXIT_VALIDATION)
    if header != SQLITE_MAGIC:
        _fail("源文件不是 SQLite 数据库（文件头不匹配）", EXIT_VALIDATION)


def _integrity(path: Path) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        quick = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    return (
        integrity[0] if integrity else None,
        quick[0] if quick else None,
    )


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        if path.exists():
            raise SnapshotError(f"manifest 已存在，拒绝覆盖: {path}", EXIT_CONFLICT)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db-snapshot.py",
        description="NovelCraft SQLite 在线快照（只读源库；快照只写到仓库外目录）。",
        epilog="本工具不读取 backend/.env；必须显式提供 --database。",
    )
    parser.add_argument("--repo", help="仓库根目录（默认按脚本位置推断）")
    parser.add_argument(
        "--database",
        required=False,
        help="源 SQLite 文件路径（必填；不接受自动猜测）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path.home() / "NovelCraft-backups" / "database"),
        help="仓库外快照根目录（默认：用户主目录下 NovelCraft-backups/database）",
    )
    parser.add_argument("--label", default=None, help="可选的人类可读标签")
    parser.add_argument("--head", default=None, help="可选 Git HEAD（缺省时只读查询）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验路径与参数；不读取数据库、不创建目录、不写快照",
    )
    # 测试钩子：固定时间戳，避免同一秒内重复运行导致命名不确定。
    parser.add_argument("--stamp", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"db-snapshot {TOOL_VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.database:
        _fail(
            "--database 必填：本工具不读取 backend/.env，也不会猜测数据库路径",
            EXIT_USAGE,
        )

    _check_local_path(args.database, "--database")
    _check_local_path(args.out_dir, "--out-dir")

    repo = _resolve_repo(args.repo)
    database = Path(args.database).resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if _is_within(out_dir, repo):
        _fail(f"输出目录位于仓库内，拒绝执行: {out_dir}", EXIT_VALIDATION)
    if not _is_within(database, repo):
        _fail(
            f"源数据库不在仓库内，拒绝执行（manifest 需要稳定的仓库相对路径）: {database}",
            EXIT_VALIDATION,
        )
    if _is_within(database, out_dir):
        _fail(f"源数据库位于快照目录内，拒绝执行: {database}", EXIT_VALIDATION)

    _check_source(database, deep=not args.dry_run)

    stamp = args.stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"\d{8}-\d{6}", stamp):
        _fail("--stamp 格式必须为 yyyyMMdd-HHmmss", EXIT_USAGE)

    head = _resolve_head(args.head, repo)
    if not head:
        _fail(
            "无法确定 Git HEAD：请在 Git 仓库内运行，或显式传入 --head；"
            "缺少来源标识的快照不允许生成",
            EXIT_VALIDATION,
        )
    head_short = head[:7]
    target_dir = out_dir / f"novelcraft-db-{stamp}-{head_short}"
    snapshot_file = target_dir / "novelcraft.db"
    manifest_file = target_dir / "manifest.json"

    source_display = database.relative_to(repo).as_posix()

    if args.dry_run:
        print(f"REPO={repo}")
        print(f"SOURCE={database}")
        print(f"OUT_DIR={out_dir}")
        print(f"TARGET_DIR={target_dir}")
        print(f"HEAD={head or ''}")
        print("RESULT=DRY_RUN_OK")
        return EXIT_OK

    created_dir = False
    tmp_snapshot: Path | None = None
    snapshot_created = False

    def _cleanup_partial() -> None:
        """只清理本次运行新建的临时文件/快照/空目录，不碰任何既有文件。"""
        if tmp_snapshot is not None:
            for sidecar in _sidecar_paths(tmp_snapshot):
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            if tmp_snapshot.exists():
                try:
                    tmp_snapshot.unlink()
                except OSError:
                    pass
        if snapshot_created and snapshot_file.exists():
            try:
                snapshot_file.unlink()
            except OSError:
                pass
        if created_dir and target_dir.exists():
            try:
                if not any(target_dir.iterdir()):
                    target_dir.rmdir()
            except OSError:
                pass

    try:
        if target_dir.exists():
            raise SnapshotError(f"目标目录已存在，拒绝覆盖: {target_dir}", EXIT_CONFLICT)
        out_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir()
        created_dir = True

        if snapshot_file.exists():
            raise SnapshotError(f"快照文件已存在，拒绝覆盖: {snapshot_file}", EXIT_CONFLICT)

        tmp_snapshot = target_dir / f".novelcraft.db.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        try:
            source_conn = _open_source(database)
            try:
                target_conn = _open_target(tmp_snapshot)
                try:
                    _backup_into(source_conn, target_conn)
                finally:
                    target_conn.close()
            finally:
                source_conn.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise SnapshotError(
                    "源数据库忙或已被锁定，未生成快照（可稍后重试）",
                    EXIT_BACKUP,
                ) from exc
            raise

        if not tmp_snapshot.exists():
            raise SnapshotError("备份未生成目标文件", EXIT_BACKUP)

        integrity, quick = _integrity(tmp_snapshot)
        if integrity != "ok" or quick != "ok":
            raise SnapshotError(
                f"快照完整性检查失败: integrity={integrity!r} quick={quick!r}",
                EXIT_INTEGRITY,
            )

        snapshot_size = tmp_snapshot.stat().st_size
        snapshot_sha256 = _sha256_file(tmp_snapshot)
        if snapshot_file.exists():
            raise SnapshotError(f"快照文件已存在，拒绝覆盖: {snapshot_file}", EXIT_CONFLICT)
        # 源/目标连接已在上面的 finally 中关闭；先清掉本次临时库的边车文件，再原子替换。
        _remove_sidecars(tmp_snapshot)
        os.replace(tmp_snapshot, snapshot_file)
        snapshot_created = True

        manifest = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "head": head,
            "head_short": head_short if head else None,
            "source_path": source_display,
            "snapshot_file": snapshot_file.name,
            "snapshot_size": snapshot_size,
            "method": "sqlite-backup",
            "service_stopped": False,
            "service_pid_before": None,
            "service_pid_after": None,
            "integrity_check": integrity,
            "quick_check": quick,
            "backup_sha256": snapshot_sha256,
            "label": args.label,
            "tool_version": TOOL_VERSION,
        }
        _write_json_atomic(manifest_file, manifest)

        print(f"SNAPSHOT_DIR={target_dir}")
        print(f"SNAPSHOT_FILE={snapshot_file}")
        print(f"SNAPSHOT_SIZE={snapshot_size}")
        print(f"SNAPSHOT_SHA256={snapshot_sha256}")
        print(f"INTEGRITY={integrity}")
        print(f"QUICK={quick}")
        print(f"HEAD={head or ''}")
        print(f"METHOD=sqlite-backup")
        print("RESULT=DONE")
        return EXIT_OK

    except SnapshotError as exc:
        _cleanup_partial()
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - 统一转成非零退出码
        _cleanup_partial()
        print(f"ERROR: 快照失败: {exc}", file=sys.stderr)
        return EXIT_BACKUP


if __name__ == "__main__":
    raise SystemExit(main())
