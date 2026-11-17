"""迁移演练：在**副本**上回放 `run_migrations`，前后对比并验证二次运行是 noop。

第三批 d 项。只接受副本：脚本会拒绝在 `schema_migrations` 已被本机服务使用中的
路径上直接跑（通过 `--force` 才能绕过，默认不开）。

用法：
    python -m scripts.verify_migration --db copy.db
    python -m scripts.verify_migration --db copy.db --json-out report.json

输出（stdout + 可选 JSON）：
    before / after 的版本列表、novel_plan_jobs 列数与行数、generation_records 行数、
    integrity_check、foreign_key 悬空数、两次运行的 applied 列表。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import migrate
from app.core.migrate import run_migrations
from app.core.novel_plan_job_schema import ensure_table_before_migrations


def _sync_snapshot(path: Path) -> dict:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() else []
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        jobs_cols = 0
        jobs_rows = None
        if "novel_plan_jobs" in tables:
            jobs_cols = len(conn.execute("PRAGMA table_info(novel_plan_jobs)").fetchall())
            jobs_rows = conn.execute("SELECT COUNT(*) FROM novel_plan_jobs").fetchone()[0]
        gen_rows = None
        if "generation_records" in tables:
            gen_rows = conn.execute("SELECT COUNT(*) FROM generation_records").fetchone()[0]
        return {
            "versions": versions,
            "tables": len(tables),
            "novel_plan_jobs_columns": jobs_cols,
            "novel_plan_jobs_rows": jobs_rows,
            "generation_records_rows": gen_rows,
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                conn.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    finally:
        conn.close()


async def _run(path: Path) -> dict:
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    first = second = None
    table_created = None
    try:
        async with engine.connect() as conn:
            # 与 app.main 启动路径保持一致：旧备份可能在 novel_plan_jobs 建表前产生，
            # 该表由历史兼容路径补建，之后 v7 的 ALTER 才有对象可加列。
            table_created = await ensure_table_before_migrations(conn)
            if table_created:
                await conn.commit()
            first = await run_migrations(conn, url)
        async with engine.connect() as conn:
            second = await run_migrations(conn, url)
    finally:
        await engine.dispose()
    return {
        "table_created_before_migration": table_created,
        "first": {
            "status": first.get("status"),
            "applied": first.get("applied"),
            "current_version": first.get("current_version"),
        },
        "second": {
            "status": second.get("status"),
            "applied": second.get("applied"),
            "current_version": second.get("current_version"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NovelCraft 迁移演练（仅副本）")
    parser.add_argument("--db", required=True, help="副本数据库路径")
    parser.add_argument("--json-out", help="结果写入 JSON")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"副本不存在：{path}")

    before = _sync_snapshot(path)
    runs = asyncio.run(_run(path))
    after = _sync_snapshot(path)

    # run_migrations 的 `applied` 是“当前已应用的版本全表”，不是本次新增，
    # 所以幂等只能看第二次的 status：noop 表示没有任何 DDL 被执行。
    idempotent = runs["second"]["status"] == "noop"
    result = {
        "db": str(path),
        "code_schema_version": migrate.SCHEMA_VERSION,
        "before": before,
        "runs": runs,
        "after": after,
        "idempotent": idempotent,
        "ok": after["integrity_check"] == "ok" and idempotent,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
