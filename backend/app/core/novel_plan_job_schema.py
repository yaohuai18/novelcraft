"""novel_plan_jobs 表结构保障（幂等，只增不改）。

为什么单独一个 ensure 而不是走 migrate.MIGRATION_STEPS：
- 生产库 `schema_migrations` 已有 1~4 记录，`migrate.run_migrations` 在非首启路径
  不会执行 `create_all`（这是项目刻意的安全设计：禁止启动时隐性改表）；
- `MIGRATION_STEPS` 的 v5 已被 phase20（consistency_review）占用、且 `SCHEMA_VERSION=4`
  明确禁止执行，插队会连带触发与本次无关的 v5 结构变更；
- 本项目既有的同类做法就是 `ensure_phase*_schema` 系列：只增补缺失对象、不改既有表。
  本模块沿用该模式，只新增一张独立小表 + 两个索引，不触碰任何既有表。

双通道同源 DDL：
1. 启动通道：main.create_tables 在 migrate 之后调用 `ensure_novel_plan_job_schema(conn)`；
2. 新建库通道：模型已在 app.models 注册，`create_all` 会按同一结构建表。

因此这里刻意**不写外键**：SQLAlchemy 模型也没有外键，两边结构完全一致，
避免出现「同一张表两个 DDL 来源、签名不可判定」（该项目历史上踩过这个坑）。
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

NOVEL_PLAN_JOB_TABLE = "novel_plan_jobs"

# 与 app/models/novel_plan_job.py 逐列对应（列名 / 类型 / 是否 NOT NULL / 默认值）
NOVEL_PLAN_JOB_COLUMNS: tuple[tuple[str, str, bool, str | None], ...] = (
    ("id", "CHAR(32)", False, None),
    ("user_id", "CHAR(32)", True, None),
    ("status", "VARCHAR(16)", True, "'queued'"),
    ("mode", "VARCHAR(8)", False, None),
    ("request_json", "TEXT", True, None),
    ("result_json", "TEXT", False, None),
    ("error_code", "VARCHAR(64)", False, None),
    ("error_message", "TEXT", False, None),
    ("progress", "INTEGER", True, "0"),
    ("prompt_tokens", "INTEGER", False, None),
    ("completion_tokens", "INTEGER", False, None),
    ("worker_id", "VARCHAR(64)", False, None),
    ("heartbeat_at", "DATETIME", False, None),
    ("created_at", "DATETIME", True, None),
    ("started_at", "DATETIME", False, None),
    ("completed_at", "DATETIME", False, None),
    ("lease_expires_at", "DATETIME", False, None),
    ("attempt_count", "INTEGER", True, "0"),
    ("max_attempts", "INTEGER", True, "1"),
    ("cancel_requested_at", "DATETIME", False, None),
    ("finished_at", "DATETIME", False, None),
    ("idempotency_key", "VARCHAR(128)", False, None),
    ("idempotency_scope", "VARCHAR(64)", False, None),
    ("result_conflict_reason", "VARCHAR(64)", False, None),
    ("result_review_status", "VARCHAR(16)", False, None),
    ("late_result_json", "TEXT", False, None),
    ("late_result_reason", "VARCHAR(64)", False, None),
    ("late_result_at", "DATETIME", False, None),
    ("result_reviewed_at", "DATETIME", False, None),
    ("result_reviewed_by", "CHAR(32)", False, None),
    ("result_review_note", "VARCHAR(500)", False, None),
)

NOVEL_PLAN_JOB_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS novel_plan_jobs (
    id                CHAR(32)     NOT NULL,
    user_id           CHAR(32)     NOT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'queued',
    mode              VARCHAR(8),
    request_json      TEXT         NOT NULL,
    result_json       TEXT,
    error_code        VARCHAR(64),
    error_message     TEXT,
    progress          INTEGER      NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    worker_id         VARCHAR(64),
    heartbeat_at      DATETIME,
    created_at        DATETIME     NOT NULL,
    started_at        DATETIME,
    completed_at      DATETIME,
    lease_expires_at  DATETIME,
    attempt_count     INTEGER      NOT NULL DEFAULT 0,
    max_attempts      INTEGER      NOT NULL DEFAULT 1,
    cancel_requested_at DATETIME,
    finished_at       DATETIME,
    idempotency_key   VARCHAR(128),
    idempotency_scope VARCHAR(64),
    result_conflict_reason VARCHAR(64),
    result_review_status VARCHAR(16),
    late_result_json  TEXT,
    late_result_reason VARCHAR(64),
    late_result_at    DATETIME,
    result_reviewed_at DATETIME,
    result_reviewed_by CHAR(32),
    result_review_note VARCHAR(500),
    PRIMARY KEY (id)
)
"""

NOVEL_PLAN_JOB_INDEX_DDL: tuple[str, ...] = (
    # 列表/恢复查询：按用户取最近任务
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_user_id "
    "ON novel_plan_jobs (user_id)",
    # 状态过滤
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_status "
    "ON novel_plan_jobs (status)",
    # 重启恢复扫描：找 status in (queued, running) 的遗留行
    "CREATE INDEX IF NOT EXISTS idx_novel_plan_jobs_status_heartbeat "
    "ON novel_plan_jobs (status, heartbeat_at)",
    # 恢复扫描：只取租约已过期的 running
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_lease "
    "ON novel_plan_jobs (status, lease_expires_at)",
    # 活跃任务查询：同一作用域并发限制
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_scope "
    "ON novel_plan_jobs (user_id, idempotency_scope)",
    # 幂等：同一用户 + 同一作用域 + 同一 key 只允许一个任务
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_novel_plan_jobs_idem "
    "ON novel_plan_jobs (user_id, idempotency_scope, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    # 待人工处置的晚到结果
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_result_review "
    "ON novel_plan_jobs (result_review_status)",
)


async def _existing_columns(conn: AsyncConnection) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({NOVEL_PLAN_JOB_TABLE})"))).all()
    return {row[1] for row in rows}


async def _table_exists(conn: AsyncConnection) -> bool:
    row = (
        await conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"
            ),
            {"name": NOVEL_PLAN_JOB_TABLE},
        )
    ).first()
    return row is not None


async def ensure_table_before_migrations(conn: AsyncConnection) -> bool:
    """迁移前兜底：表不存在时按最终形态建出来（仅此一种情况）。

    为什么需要：v7 的 ALTER 假定 novel_plan_jobs 已存在。恢复 2026-11-10 之前的
    旧备份时表可能缺失；先建表，后续 ALTER 会全部命中 duplicate column，
    由迁移网关容忍。生产库常态（表已存在）返回 False，schema 变更仍由 v7 唯一负责。
    """
    if await _table_exists(conn):
        return False
    await conn.execute(text(NOVEL_PLAN_JOB_TABLE_DDL))
    for statement in NOVEL_PLAN_JOB_INDEX_DDL:
        await conn.execute(text(statement))
    logger.warning("novel_plan_jobs 表缺失，已在迁移前按最终形态补建")
    return True


async def ensure_novel_plan_job_schema(
    conn: AsyncConnection, *, repair: bool = True
) -> dict:
    """确保 novel_plan_jobs 表与索引存在（幂等，可重复执行）。

    repair=True  ：缺表则建表、缺索引则建索引；不改既有行、不动其他表。
    repair=False ：只做只读检查并返回现状，不执行任何 DDL。

    返回 {"table": str, "created": bool, "columns_ok": bool,
          "missing_columns": list[str], "rows": int | None, "repair": bool}
    """
    created = False
    if repair:
        existed = await _table_exists(conn)
        await conn.execute(text(NOVEL_PLAN_JOB_TABLE_DDL))
        for statement in NOVEL_PLAN_JOB_INDEX_DDL:
            await conn.execute(text(statement))
        created = not existed
        logger.info(
            "novel_plan_jobs 结构保障完成（新建=%s，repair=%s）", created, repair
        )

    if not await _table_exists(conn):
        return {
            "table": NOVEL_PLAN_JOB_TABLE,
            "created": False,
            "columns_ok": False,
            "missing_columns": [name for name, *_ in NOVEL_PLAN_JOB_COLUMNS],
            "rows": None,
            "repair": repair,
        }

    existing = await _existing_columns(conn)
    expected = [name for name, *_ in NOVEL_PLAN_JOB_COLUMNS]
    missing = [name for name in expected if name not in existing]
    if missing:
        # 新表缺列只能是异常残留。SQLite 不能补主键列，这里只报告不重建，
        # 避免把已有任务记录连带清掉。
        logger.warning(
            "novel_plan_jobs 缺少列 %s（仅报告，不重建表、不清数据）", missing
        )
    rows = int(
        (await conn.execute(text(f"SELECT COUNT(*) FROM {NOVEL_PLAN_JOB_TABLE}"))).scalar()
        or 0
    )
    return {
        "table": NOVEL_PLAN_JOB_TABLE,
        "created": created,
        "columns_ok": not missing,
        "missing_columns": missing,
        "rows": rows,
        "repair": repair,
    }
