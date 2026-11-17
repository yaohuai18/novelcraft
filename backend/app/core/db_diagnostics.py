"""启动期数据库诊断：记录 SQLite 实际生效的 PRAGMA 与连接配置。

第三批 c 项。目的：把"我们以为的配置"和"数据库实际生效的配置"分开记录。
生产事故里最常见的一类误判，是把代码里的 `timeout=30` 当成
`busy_timeout=30000`，或以为已经开了 WAL / foreign_keys。

只读：全部是 `PRAGMA xxx` 查询，不修改任何设置。
输出走 logger（`app.db`）；调用方决定是否同时 print。
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("app.db")

# 只查询、不设置。pragma 的返回形态在不同 SQLite 版本上略有差异，统一按标量读第一个字段。
_READ_ONLY_PRAGMAS = (
    "journal_mode",
    "synchronous",
    "busy_timeout",
    "foreign_keys",
    "page_size",
    "cache_size",
    "wal_autocheckpoint",
    "user_version",
)


def _scalar(row) -> object:
    if row is None:
        return None
    values = tuple(row)
    return values[0] if values else None


async def collect_sqlite_diagnostics(engine: AsyncEngine) -> dict:
    """返回 {driver, database, pragmas, pool} —— 失败项记为错误字符串，不抛异常。"""
    info: dict = {
        "driver": engine.dialect.name,
        "database": None,
        "pragmas": {},
        "pool": type(engine.pool).__name__,
        "errors": {},
    }
    is_sqlite = engine.dialect.name == "sqlite"
    try:
        async with engine.connect() as conn:
            if is_sqlite:
                try:
                    db_row = await conn.execute(text("PRAGMA database_list"))
                    rows = db_row.fetchall()
                    # database_list: (seq, name, file)；取 main
                    for row in rows:
                        if len(row) >= 3 and row[1] == "main":
                            info["database"] = row[2]
                            break
                except Exception as exc:  # noqa: BLE001 - 诊断不得影响启动
                    info["errors"]["database_list"] = f"{type(exc).__name__}: {exc}"

                for pragma in _READ_ONLY_PRAGMAS:
                    try:
                        result = await conn.execute(text(f"PRAGMA {pragma}"))
                        info["pragmas"][pragma] = _scalar(result.fetchone())
                    except Exception as exc:  # noqa: BLE001
                        info["errors"][pragma] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        info["errors"]["connect"] = f"{type(exc).__name__}: {exc}"
    return info


async def log_database_diagnostics(engine: AsyncEngine, *, echo: bool = True) -> dict:
    """采集并记录诊断信息；`echo=True` 时同时打印一行（便于部署时肉眼确认）。"""
    info = await collect_sqlite_diagnostics(engine)
    pragmas = info.get("pragmas") or {}
    summary = (
        f"[db] driver={info['driver']} pool={info['pool']} "
        f"database={info['database'] or '<unknown>'} "
        f"journal_mode={pragmas.get('journal_mode')} "
        f"synchronous={pragmas.get('synchronous')} "
        f"busy_timeout={pragmas.get('busy_timeout')} "
        f"foreign_keys={pragmas.get('foreign_keys')}"
    )
    logger.info(summary)
    if info.get("errors"):
        logger.warning("[db] 诊断项失败：%s", info["errors"])
    if echo:
        print(summary)
        if info.get("errors"):
            print(f"[db] 诊断项失败：{info['errors']}")
    return info
