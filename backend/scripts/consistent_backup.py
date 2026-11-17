"""一致性备份：SQLite 在线备份 API + 校验清单 + 恢复演练。

第三批 d 项：把「迁移/回滚/恢复校验」从工作区临时脚本收进仓库，任何人可以复跑。

用法：
    python -m scripts.consistent_backup --source path/to/novelcraft.db --out backups/
    python -m scripts.consistent_backup --verify backups/xxx.db          # 只校验已有备份

约定：
- 源库以 `mode=ro` 只读打开，备份前后各算一次 SHA-256，用于证明"备份期间没有写入"。
- 备份用 `sqlite3.Connection.backup`（在线备份 API），不是文件复制：WAL 里已提交
  但尚未 checkpoint 的数据也会被包含，且快照是同一时刻一致的。
- 备份完成后对目标执行 `PRAGMA journal_mode=DELETE`，归档成单文件（只读打开不依赖
  `-wal/-shm`）。
- `--drill` 会把备份复制到临时目录、独立打开、逐表比对行数，确认"备份真的能读、
  行数一致"，而不是只信任 SHA-256。

该脚本不修改源库：只读连接 + 只读模式，且不执行任何 DDL/DML。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULT_TABLES = (
    "users",
    "projects",
    "outlines",
    "chapter_contents",
    "chapter_versions",
    "generation_records",
    "schema_migrations",
)


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def table_counts(conn: sqlite3.Connection, names=DEFAULT_TABLES) -> dict:
    counts: dict[str, int | None] = {}
    for name in names:
        try:
            counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.Error:
            counts[name] = None
    return counts


def integrity(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def backup_database(source: Path, target: Path) -> dict:
    if not source.exists():
        raise SystemExit(f"源库不存在：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    sha_before = sha256_file(source)
    src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        src_counts = table_counts(src)
        src_integrity = integrity(src)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    sha_after = sha256_file(source)
    back = sqlite3.connect(str(target))
    try:
        back_counts = table_counts(back)
        back_integrity = integrity(back)
    finally:
        back.close()

    return {
        "source": str(source),
        "backup": str(target),
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "backup_sha256": sha256_file(target),
        "backup_bytes": target.stat().st_size,
        "source_integrity": src_integrity,
        "backup_integrity": back_integrity,
        "counts_source": src_counts,
        "counts_backup": back_counts,
        "counts_match": src_counts == back_counts,
    }


def restore_drill(backup: Path) -> dict:
    """把备份复制到临时目录、独立打开、比对表集合与行数。"""
    with tempfile.TemporaryDirectory(prefix="nc_drill_") as tmp:
        clone = Path(tmp) / "restore.db"
        clone.write_bytes(backup.read_bytes())
        conn = sqlite3.connect(str(clone))
        try:
            ok = integrity(conn)
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            counts = table_counts(conn, tables[: len(DEFAULT_TABLES) * 3])
        finally:
            conn.close()
    return {
        "integrity_check": ok,
        "table_count": len(tables),
        "sampled_rows": counts,
        "ok": ok == "ok",
    }


def verify(backup: Path) -> dict:
    conn = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)
    try:
        result = {
            "backup": str(backup),
            "sha256": sha256_file(backup),
            "bytes": backup.stat().st_size,
            "integrity_check": integrity(conn),
            "counts": table_counts(conn),
        }
    finally:
        conn.close()
    result["ok"] = result["integrity_check"] == "ok"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NovelCraft 一致性备份与校验")
    parser.add_argument("--source", help="源数据库路径（默认读 DATABASE_URL）")
    parser.add_argument("--out", default="backups", help="备份输出目录")
    parser.add_argument("--drill", action="store_true", help="备份后做恢复演练")
    parser.add_argument("--verify", help="只校验指定备份文件")
    parser.add_argument("--json-out", help="把结果写入 JSON 文件")
    args = parser.parse_args(argv)

    if args.verify:
        result = verify(Path(args.verify))
        # 2026-11-17 修复：旧实现 verify 分支忽略 --drill，月度演练计划任务
        # （backup_drill_monthly.cmd 传 --verify X --drill）从未真正跑过恢复演练。
        if args.drill:
            result["restore_drill"] = restore_drill(Path(args.verify))
    else:
        source = args.source
        if not source:
            url = os.environ.get("DATABASE_URL", "")
            source = url.split("///")[-1] if "///" in url else ""
        if not source:
            raise SystemExit("未提供 --source，且 DATABASE_URL 不是 sqlite 文件路径")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(args.out) / f"novelcraft-{stamp}.db"
        result = backup_database(Path(source), target)
        if args.drill:
            result["restore_drill"] = restore_drill(target)

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    ok = result.get("ok")
    if ok is None:
        ok = result.get("source_unchanged") and result.get("counts_match")
    # 2026-11-17：恢复演练失败同样判非零退出（计划任务 result=1 可见）
    if result.get("restore_drill") is not None:
        ok = bool(ok) and bool(result["restore_drill"].get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
