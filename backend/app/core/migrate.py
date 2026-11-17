"""轻量数据库迁移机制（正式迁移基础设施）。

设计原则（对齐项目先决条件清单"建立正式数据库迁移机制"）：
1. 新数据库可以从零迁移创建：首次启动时执行 create_all + ensure_* 系列并记录为基线 v1；
2. 旧数据库可以逐版本升级：通过 schema_migrations 记录已应用版本，未来每个 schema 变更
   写成独立迁移步骤，按版本顺序执行；
3. 迁移可以记录版本：schema_migrations 表保存 (version, name, applied_at)；
4. 迁移失败可以回滚或恢复：每次迁移前自动备份数据库文件，失败时可恢复备份；
5. 生产环境禁止自动执行不可控的 create_all：一旦 schema_migrations 已有记录，
   启动时不再执行 create_all / ensure_*（避免每次启动隐性改表）；
6. 种子数据只能在开发或测试环境运行：种子入口由 main 侧按环境显式调用；
7. 每次迁移前自动备份数据库。

I-3a-fix（2026-10-22，授权批次「只改代码 + 只做隔离验证」）新增/修订原则：
8. 事务边界必须显式：迁移 DDL、schema 签名校验、版本记录三者必须在同一个 DBAPI 显式
   事务内完成（BEGIN IMMEDIATE → DDL → 签名校验 → 写版本记录 → COMMIT）。异常时只能用
   同一 DBAPI 通道执行 ROLLBACK，禁止与 SQLAlchemy 的 commit() / rollback() 混用，
   也不得依赖未经验证的隐式事务状态（pysqlite legacy 模式下 SQLAlchemy 的 do_begin
   为空实现，engine.begin() 不会真正发出 BEGIN，DDL 会即时落盘且无法回滚）。
9. schema 签名可验证：迁移执行后必须做规范化结构签名比对（列名/类型/NULL/默认值/主键/
   唯一约束/索引名称与列顺序/外键）。签名不符一律失败关闭：不写版本号、不继续后续迁移、
   回滚，并输出明确差异；不得静默接受，也不得自动重建表。
10. 备份必须可观测：迁移前备份改用 SQLite backup API（不再使用 shutil.copy2），
   返回源路径/备份路径/大小/SHA-256/完整性检查结果；一次升级产生多个备份时全部保留，
   文件名唯一，不得互相覆盖。
11. 补偿删除仅作最后恢复手段：只有 ROLLBACK 本身失败时才启用，且只针对「本次语句中
   预声明创建」的对象；补偿成功不作为事务原子性证明，补偿失败必须停止并报告。

本批次明确不处理（按裁定登记为后续独立事项）：v4 四张未提交模型的索引、六个历史索引缺口
（不新增 v6、不补正式库）、重复索引、nullable 收敛、phase20/phase22 的同类事务边界、
v5 迁移内容与执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db_migrations import (
    PHASE19_MIGRATION_STATEMENTS,
    PHASE20_MIGRATION_STATEMENTS,
    PHASE22_MIGRATION_STATEMENTS,
)

logger = logging.getLogger(__name__)

# 当前 schema 基线版本号。后续每增加一次 schema 变更，版本号 +1 并追加迁移步骤。
# v5（consistency_review）保留原有登记，按 v5 -> v6 顺序正式执行：
# v5 只执行索引增量，不再包含 CREATE TABLE（2026-11-12 调整，理由见下）：
#   1) 证据：全机 8 个带 schema_migrations 的库最大版本均为 4，没有任何环境执行过 v5；
#   2) consistency_review 表由 Base.metadata.create_all 建立（空库初始化与存量库启动
#      都在 run_migrations 之前执行 create_all），生产库该表已存在且结构由早期 DDL 建出；
#   3) create_all 产出的列类型是 CHAR(32)（ORM UUID），早期 DDL 是 VARCHAR(36)，
#      两者在 SQLite 中同为 TEXT 亲和且无行为差异，但签名校验会把类型差异判为硬差异。
#      若 v5 保留 CREATE TABLE，任何由 create_all 建表的库（新装/开发库/测试夹具）
#      都会在 v5 预检阶段失败关闭。去掉 CREATE TABLE 后该批次不可预检，
#      只执行 CREATE INDEX IF NOT EXISTS，对上述两种表结构都安全。
# v6 = 第一批数据完整性与并发安全（generation_records 归属状态 + 消费标记）。
# v7 = 第二批规划任务可靠性（租约 / 领取计数 / 幂等键 / 取消 / 晚到结果）。
SCHEMA_VERSION = 10  # 2026-11-17 批次 5：chapters_fts 全文检索
BASELINE_NAME = "baseline v1 (create_all + ensure_phase1/3/4/7/8/9/10)"

# 版本化迁移步骤：v2 及以后的增量迁移在此登记。
# 已记录 v1 的数据库启动时按版本顺序执行未应用的步骤；新库先跑基线 v1 再顺序执行。
# 步骤必须幂等（SQLite 不支持 DROP COLUMN 等，只做 ADD COLUMN / CREATE INDEX 类增量）。
# v6（第一批数据完整性与并发安全）：generation_records 增加归属状态与消费标记。
# DDL 与 app/models/generation.py 的列定义同源：
#   attribution_status —— project_bound / pre_project / unattributed
#   consumed_at / consumed_chapter_id / consumed_version_id —— 一条生成结果只能被接受一次
# 只做 ADD COLUMN / CREATE INDEX 类增量，重复执行由 apply_step_atomically 容忍。
PHASE23_MIGRATION_STATEMENTS: list[str] = [
    "ALTER TABLE generation_records ADD COLUMN attribution_status VARCHAR(20)",
    "ALTER TABLE generation_records ADD COLUMN consumed_at DATETIME",
    "ALTER TABLE generation_records ADD COLUMN consumed_chapter_id CHAR(32)",
    "ALTER TABLE generation_records ADD COLUMN consumed_version_id CHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_generation_records_attribution_status "
    "ON generation_records (attribution_status)",
    "CREATE INDEX IF NOT EXISTS ix_generation_records_consumed_at "
    "ON generation_records (consumed_at)",
]

# v7（第二批规划任务可靠性）：novel_plan_jobs 增加租约、领取计数、幂等键、取消与晚到结果字段。
# DDL 与 app/models/novel_plan_job.py、app/core/novel_plan_job_schema.py 同源：
#   lease_expires_at      —— 租约到期时间；恢复只处理已过期租约
#   attempt_count         —— 已领取次数（同一行被领取才 +1）
#   max_attempts          —— 允许领取上限，整本规划默认 1（不自动重试）
#   cancel_requested_at   —— 用户请求取消的时间
#   finished_at           —— 终态时间（与 completed_at 同步写，兼容旧字段）
#   idempotency_key/scope —— 同一用户 + 同一上下文 + 同一 key 只产生一个任务
#   result_conflict_reason—— 晚到结果被保存时的冲突原因
# 迁移治理规则（2026-11-12 生效）：
#   1) 已被任何环境应用过的 migration 绝不可改写；
#   2) 未被任何环境应用、但已进入仓库的 migration，若经全量环境核验确认尚未应用，
#      可以重整其内容（增补/纠正），但必须保留审计记录、验证依据与最终语义。
#   本次应用：v7(PHASE24) 于 2026-11-12 重整一次 —— 增补 result_review_status /
#   late_result_json / late_result_reason / late_result_at 四列与一个索引，原因是
#   初版把晚到结果写成 status='result_pending_review'，会覆盖原始终态。
#   验证依据：全机 9 个 SQLite 库、8 个含 schema_migrations，最大版本均为 4，
#   无任何库登记 v5/v6/v7；生产库只读复查 schema_migrations=[1,2,3,4]。
PHASE24_MIGRATION_STATEMENTS: list[str] = [
    "ALTER TABLE novel_plan_jobs ADD COLUMN lease_expires_at DATETIME",
    "ALTER TABLE novel_plan_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE novel_plan_jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE novel_plan_jobs ADD COLUMN cancel_requested_at DATETIME",
    "ALTER TABLE novel_plan_jobs ADD COLUMN finished_at DATETIME",
    "ALTER TABLE novel_plan_jobs ADD COLUMN idempotency_key VARCHAR(128)",
    "ALTER TABLE novel_plan_jobs ADD COLUMN idempotency_scope VARCHAR(64)",
    "ALTER TABLE novel_plan_jobs ADD COLUMN result_conflict_reason VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_lease "
    "ON novel_plan_jobs (status, lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_scope "
    "ON novel_plan_jobs (user_id, idempotency_scope)",
    # --- 2026-11-12 语义修正（v7 尚未被任何环境应用，按迁移治理规则就地重整）---
    # 晚到结果不改写 status：原始终态、原始 error_code、取消/租约原因必须可查。
    "ALTER TABLE novel_plan_jobs ADD COLUMN result_review_status VARCHAR(16)",
    "ALTER TABLE novel_plan_jobs ADD COLUMN late_result_json TEXT",
    "ALTER TABLE novel_plan_jobs ADD COLUMN late_result_reason VARCHAR(64)",
    "ALTER TABLE novel_plan_jobs ADD COLUMN late_result_at DATETIME",
    # 人工处置留痕（第三批 g）：谁在什么时候把待审结果判为 accepted/discarded。
    "ALTER TABLE novel_plan_jobs ADD COLUMN result_reviewed_at DATETIME",
    "ALTER TABLE novel_plan_jobs ADD COLUMN result_reviewed_by CHAR(32)",
    "ALTER TABLE novel_plan_jobs ADD COLUMN result_review_note VARCHAR(500)",
    "CREATE INDEX IF NOT EXISTS ix_novel_plan_jobs_result_review "
    "ON novel_plan_jobs (result_review_status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_novel_plan_jobs_idem "
    "ON novel_plan_jobs (user_id, idempotency_scope, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
]

# consistency_review 兜底建表 DDL（与 models/consistency_review.py 逐列对齐）。
# 场景：恢复旧备份库（schema_migrations 非空但无此表）时 create_all 不执行，
# v5 建索引会因缺表失败导致服务起不来；IF NOT EXISTS 保证新库场景无副作用。
CONSISTENCY_REVIEW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS consistency_review (
    id CHAR(32) PRIMARY KEY,
    project_id CHAR(32) NOT NULL,
    chapter_id CHAR(32),
    chapter_title VARCHAR(255),
    scope VARCHAR(20) NOT NULL DEFAULT 'chapter',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
    model_name VARCHAR(100),
    prompt_version VARCHAR(50),
    score FLOAT,
    issue_count INTEGER NOT NULL DEFAULT 0,
    severity_counts TEXT,
    issues TEXT,
    suggestion_count INTEGER NOT NULL DEFAULT 0,
    task_id VARCHAR(32),
    raw_response TEXT,
    error_message TEXT,
    note TEXT,
    created_at DATETIME NOT NULL,
    completed_at DATETIME
)
"""

MIGRATION_STEPS: dict[int, tuple[str, list[str]]] = {
    2: (
        "ai_tasks 任务系统扩展（超时/幂等/模型/快照/token 字段）",
        [
            "ALTER TABLE ai_tasks ADD COLUMN model_name VARCHAR(100)",
            "ALTER TABLE ai_tasks ADD COLUMN prompt_version VARCHAR(50)",
            "ALTER TABLE ai_tasks ADD COLUMN idempotency_key VARCHAR(128)",
            "ALTER TABLE ai_tasks ADD COLUMN request_snapshot TEXT",
            "ALTER TABLE ai_tasks ADD COLUMN input_tokens INTEGER",
            "ALTER TABLE ai_tasks ADD COLUMN output_tokens INTEGER",
            "ALTER TABLE ai_tasks ADD COLUMN timeout_at DATETIME",
            "CREATE INDEX IF NOT EXISTS ix_ai_tasks_idempotency_key ON ai_tasks (idempotency_key)",
            "CREATE INDEX IF NOT EXISTS ix_ai_tasks_status_created ON ai_tasks (status, created_at)",
        ],
    ),
    3: (
        "ai_task_token_usage 逐次 token 账目表（cost_budget B-2；DDL 与 "
        "db_migrations.ensure_phase22_schema 同源）",
        PHASE22_MIGRATION_STATEMENTS,
    ),
    4: (
        "chapter_fact_dependencies 依赖图回归表（phase19；DDL 与 "
        "db_migrations.ensure_phase19_schema 同源）",
        PHASE19_MIGRATION_STATEMENTS,
    ),
    5: (
        "consistency_review 表与索引（phase20；表由 create_all 建立，本步先兜底建表再补索引）",
        [
            CONSISTENCY_REVIEW_TABLE_SQL,
            *[
                statement
                for statement in PHASE20_MIGRATION_STATEMENTS
                if "CREATE INDEX" in statement.upper()
            ],
        ],
    ),
    6: (
        "generation_records 归属状态与消费标记（第一批数据完整性与并发安全）",
        PHASE23_MIGRATION_STATEMENTS,
    ),
    7: (
        "novel_plan_jobs 租约/幂等/取消/晚到结果（第二批规划任务可靠性）",
        PHASE24_MIGRATION_STATEMENTS,
    ),
    8: (
        "projects 软删除（回收站：deleted_at / deleted_by，2026-11-15 第一档升级）",
        [
            "ALTER TABLE projects ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE projects ADD COLUMN deleted_by CHAR(32)",
        ],
    ),
    9: (
        "failure_samples 失败样本（2026-11-16 第三档升级：生成时注入固定教训）",
        [
            "CREATE TABLE IF NOT EXISTS failure_samples ("
            "id CHAR(32) PRIMARY KEY, "
            "project_id CHAR(32), "
            "category VARCHAR(30) NOT NULL, "
            "bad_example TEXT NOT NULL, "
            "correct_way TEXT NOT NULL, "
            "source VARCHAR(32) NOT NULL DEFAULT 'manual', "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS ix_failure_samples_project_id "
            "ON failure_samples (project_id)",
            "CREATE INDEX IF NOT EXISTS ix_failure_samples_category "
            "ON failure_samples (category)",
        ],
    ),
    10: (
        "chapters_fts 章节正文全文检索（2026-11-17 批次 5：FTS5/BM25 + 字符 bigram，替代向量 RAG）",
        [
            # SQLite 自带 tokenizer 对中文不切词，索引列存字符 bigram 串（由写入端维护），
            # 查询词同样转 bigram 短语即可命中任意长度词条。
            # 注意：chapter_contents.content_bigrams 列不在此 ALTER——存量库由启动时
            # ensure_chapters_fts_index 检测补列（避免 create_all 新库重复加列报错）。
            "CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5("
            "title, content_bigrams, project_id UNINDEXED, tokenize = 'unicode61')",
            "CREATE TRIGGER IF NOT EXISTS chapters_fts_ai AFTER INSERT ON chapter_contents BEGIN "
            "INSERT INTO chapters_fts(rowid, title, content_bigrams, project_id) "
            "SELECT new.rowid, "
            "COALESCE((SELECT o.title FROM outlines o WHERE o.id = new.outline_id), ''), "
            "COALESCE(new.content_bigrams, ''), "
            "(SELECT o.project_id FROM outlines o WHERE o.id = new.outline_id) "
            "WHERE COALESCE(new.content_bigrams, '') != ''; "
            "END",
            "CREATE TRIGGER IF NOT EXISTS chapters_fts_au AFTER UPDATE ON chapter_contents BEGIN "
            "DELETE FROM chapters_fts WHERE rowid = old.rowid; "
            "INSERT INTO chapters_fts(rowid, title, content_bigrams, project_id) "
            "SELECT new.rowid, "
            "COALESCE((SELECT o.title FROM outlines o WHERE o.id = new.outline_id), ''), "
            "COALESCE(new.content_bigrams, ''), "
            "(SELECT o.project_id FROM outlines o WHERE o.id = new.outline_id) "
            "WHERE COALESCE(new.content_bigrams, '') != ''; "
            "END",
            "CREATE TRIGGER IF NOT EXISTS chapters_fts_ad AFTER DELETE ON chapter_contents BEGIN "
            "DELETE FROM chapters_fts WHERE rowid = old.rowid; "
            "END",
        ],
    ),
}

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# 异常类型（失败关闭，全部可被上层识别）
# ---------------------------------------------------------------------------
class MigrationError(RuntimeError):
    """迁移相关错误基类。"""


class MigrationTransactionStateError(MigrationError):
    """事务边界不符合显式网关要求（已处于事务中 / 网关未开启 / 重复收尾）。"""


class SchemaSignatureMismatchError(MigrationError):
    """schema 结构签名与 DDL 声明不一致：失败关闭，不写版本、不继续后续迁移。"""

    def __init__(self, version: int, report: dict[str, Any]) -> None:
        self.version = version
        self.report = report
        diffs = report.get("hard_diffs") or []
        super().__init__(
            f"v{version} schema 签名校验失败：{len(diffs)} 项硬差异；"
            f"缺失表={report.get('missing_tables')}"
        )


class MigrationStepFailure(MigrationError):
    """单个迁移步骤执行失败（携带回滚后诊断）。"""

    def __init__(self, version: int, diagnostics: dict[str, Any], cause: BaseException | None = None) -> None:
        self.version = version
        self.diagnostics = diagnostics
        self.__cause__ = cause
        super().__init__(
            f"v{version} 迁移失败并已回滚；残留对象={diagnostics.get('residual_objects')}；"
            f"integrity={diagnostics.get('integrity_check')}"
        )


class MigrationBackupError(MigrationError):
    """备份失败（无法观测即视为失败，不再返回 None 蒙混）。"""


# ---------------------------------------------------------------------------
# 显式 DBAPI 事务网关（D1）
# ---------------------------------------------------------------------------
_BEGIN_SQL = "BEGIN IMMEDIATE"
_COMMIT_SQL = "COMMIT"
_ROLLBACK_SQL = "ROLLBACK"
_IN_TRANSACTION_MARKERS = ("cannot start a transaction within a transaction", "within a transaction")


async def probe_in_transaction(conn: AsyncConnection) -> bool:
    """用「重入 BEGIN IMMEDIATE」探针判断连接是否已处于真实事务中。

    不可依赖 SQLAlchemy 的 ``conn.in_transaction()``：pysqlite legacy 模式下
    SQLAlchemy 只会置自身事务状态，DBAPI 层并未真正 BEGIN，二者不一致。
    探针在事务内会抛 "cannot start a transaction within a transaction"，而在事务外
    会成功开启，随后立即 ROLLBACK 复原，不改变连接状态。
    """
    try:
        await conn.exec_driver_sql(_BEGIN_SQL)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if any(marker in message for marker in _IN_TRANSACTION_MARKERS):
            return True
        raise MigrationTransactionStateError(
            f"事务探针无法判定连接状态：{exc!r}"
        ) from exc
    await conn.exec_driver_sql(_ROLLBACK_SQL)
    return False


class ExplicitTransaction:
    """显式 DBAPI 事务网关。

    用法::

        async with ExplicitTransaction(conn) as tx:
            await do_ddl(conn)
            await tx.commit()      # 显式 COMMIT；未调用则 __aexit__ 自动 ROLLBACK

    约束：进入时若连接已处于真实事务中直接抛错（禁止嵌套、禁止混用 SQLAlchemy 事务）。
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self.opened = False
        self.closed = False

    async def __aenter__(self) -> "ExplicitTransaction":
        if await probe_in_transaction(self.conn):
            raise MigrationTransactionStateError(
                "连接已处于真实事务中：禁止嵌套事务，也禁止与 SQLAlchemy commit()/rollback() 混用"
            )
        await self.conn.exec_driver_sql(_BEGIN_SQL)
        self.opened = True
        self.closed = False
        logger.debug("显式事务已开启：BEGIN IMMEDIATE")
        return self

    async def commit(self) -> None:
        if not self.opened or self.closed:
            raise MigrationTransactionStateError("事务未开启或已收尾，禁止重复 COMMIT")
        await self.conn.exec_driver_sql(_COMMIT_SQL)
        self.closed = True
        logger.debug("显式事务已提交：COMMIT")

    async def rollback(self) -> None:
        """回滚（幂等）。回滚本身失败时抛 MigrationError，由调用方触发补偿。"""
        if not self.opened or self.closed:
            return
        try:
            await self.conn.exec_driver_sql(_ROLLBACK_SQL)
        finally:
            self.closed = True
        logger.debug("显式事务已回滚：ROLLBACK")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self.opened and not self.closed:
            await self.rollback()
        return False


# ---------------------------------------------------------------------------
# 迁移语句执行 / 幂等兜底
# ---------------------------------------------------------------------------
def _is_duplicate_column_error(error: Exception) -> bool:
    """判断异常是否为 SQLite「列已存在」（ADD COLUMN 重复）。"""
    message = str(getattr(error, "orig", error)).lower()
    return "duplicate column name" in message


async def _execute_migration_statement(conn: AsyncConnection, statement: str) -> None:
    """执行单条迁移语句（幂等兜底）。

    空库首启缺陷修复（B-2）：首启路径先跑基线 create_all（此时新列已在表内），
    再顺序执行 MIGRATION_STEPS，其中的裸 ALTER TABLE ADD COLUMN 会因列已存在
    报 duplicate column 而中断首启。此处把「ADD COLUMN 且列已存在」视为该语句
    已生效并跳过（其余异常一律继续上抛，不得吞掉真实失败）。

    I-3a-fix：本函数仅在 ExplicitTransaction 内部被调用，语句与版本记录同属一个
    DBAPI 事务；异常由 apply_step_atomically 统一 ROLLBACK。
    """
    try:
        await conn.execute(text(statement))
    except (OperationalError, ProgrammingError) as error:
        if statement.lstrip().upper().startswith("ALTER TABLE") and _is_duplicate_column_error(error):
            logger.warning("迁移语句幂等跳过（列已存在）：%s", statement)
            return
        raise


# ---------------------------------------------------------------------------
# schema 规范化签名（D3）
# ---------------------------------------------------------------------------
_SQLITE_AFFINITY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("INT",), "INTEGER"),
    (("CHAR", "CLOB", "TEXT"), "TEXT"),
    (("BLOB",), "BLOB"),
    (("REAL", "FLOA", "DOUB"), "REAL"),
)

_LENGTH_RE = re.compile(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)")


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def normalize_type(raw_type: str | None) -> dict[str, Any]:
    """把 SQLite 声明类型规范化为可比较键。

    SQLite 实际只按「类型亲和性 + 长度」区分行为，`CHAR(32)` 与 `VARCHAR(32)` 在库内
    语义等价（同为 TEXT 亲和、闸值 32）。因此判定键取 (亲和性, 长度)，同时保留原始
    声明文本作为差异说明，便于审计。v4 目标表两种写法在正式库与 DDL 中均为 CHAR(32)。
    """
    raw = (raw_type or "").strip()
    upper = raw.upper()
    base = upper.split("(", 1)[0].strip()
    match = _LENGTH_RE.search(upper)
    length: int | None = None
    if match:
        length = int(match.group(2) or match.group(1))
    affinity = "NUMERIC"
    for markers, name in _SQLITE_AFFINITY_RULES:
        if any(marker in base for marker in markers):
            affinity = name
            break
    if not base:
        affinity = "BLOB"
    return {"raw": raw, "affinity": affinity, "length": length}


def _type_key(raw_type: str | None) -> tuple[str, int | None]:
    normalized = normalize_type(raw_type)
    return (normalized["affinity"], normalized["length"])


def _signature_from_sqlite(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = []
    for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})"):
        columns.append(
            {
                "name": row[1],
                "type": normalize_type(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
            }
        )
    indexes = []
    for row in conn.execute(f"PRAGMA index_list({_quote_identifier(table)})"):
        name, unique, origin = row[1], int(row[2]), row[3]
        if origin == "pk":
            # 主键自动索引不参与索引判定：主键已由 column.pk 单独比对
            continue
        index_columns = [
            r[2] for r in conn.execute(f"PRAGMA index_info({_quote_identifier(name)})")
        ]
        indexes.append({"name": name, "unique": unique, "columns": index_columns})
    foreign_keys = [
        {
            "from": row[3],
            "table": row[2],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
        }
        for row in conn.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})")
    ]
    indexes.sort(key=lambda item: item["name"])
    foreign_keys.sort(key=lambda item: (item["from"], item["table"], item["to"] or ""))
    return {
        "table": table,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
    }


_EXPECTED_SIGNATURE_CACHE: dict[str, tuple[dict[str, dict[str, Any]], list[str]]] = {}


def _build_expected(statements: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """在 sqlite3 内存库重放本批 DDL，得到 (期望签名, 被跳过的语句说明)。

    部分迁移批次（例如 v2 的纯 ADD COLUMN / CREATE INDEX ON 既有表）不包含建表语句，
    其语句在空内存库里执行必然因「表不存在」失败。这类语句无法脱离既有表推导结构，
    故按「不可校验」处理并记入 skipped，而不是让整个签名流程报错。
    """
    key = hashlib.sha256("\n".join(statements).encode("utf-8")).hexdigest()
    cached = _EXPECTED_SIGNATURE_CACHE.get(key)
    if cached is not None:
        return cached

    memory_db = sqlite3.connect(":memory:")
    skipped: list[str] = []
    try:
        for statement in statements:
            try:
                memory_db.execute(statement)
            except sqlite3.Error as exc:
                head = " ".join(statement.split())[:100]
                skipped.append(f"{head} :: {exc}")
        tables = [
            row[0]
            for row in memory_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        signatures = {name: _signature_from_sqlite(memory_db, name) for name in tables}
    finally:
        memory_db.close()
    if skipped:
        logger.debug("签名推导跳过 %s 条不可脱离既有表的语句：%s", len(skipped), skipped)
    _EXPECTED_SIGNATURE_CACHE[key] = (signatures, skipped)
    return signatures, skipped


def build_expected_signatures(statements: list[str]) -> dict[str, dict[str, Any]]:
    """从迁移 DDL 语句本身推导期望签名（同源，杜绝手写漂移）。

    返回 {表名: 签名}；不含建表语句的批次（如 v2 的纯 ADD COLUMN）返回空字典，
    此时该批次不参与结构预检与签名判定（D11：不改变 v2 迁移语义）。
    """
    return _build_expected(statements)[0]


async def read_table_signature(conn: AsyncConnection, table: str) -> dict[str, Any] | None:
    """读取真实库中单表签名；表不存在返回 None。"""
    exists = (
        await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
            {"name": table},
        )
    ).first()
    if not exists:
        return None

    columns = []
    rows = (await conn.execute(text(f"PRAGMA table_info({_quote_identifier(table)})"))).all()
    for row in rows:
        columns.append(
            {
                "name": row[1],
                "type": normalize_type(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
            }
        )
    indexes = []
    index_rows = (await conn.execute(text(f"PRAGMA index_list({_quote_identifier(table)})"))).all()
    for row in index_rows:
        name, unique, origin = row[1], int(row[2]), row[3]
        if origin == "pk":
            continue
        index_columns = [
            r[2]
            for r in (
                await conn.execute(text(f"PRAGMA index_info({_quote_identifier(name)})"))
            ).all()
        ]
        indexes.append({"name": name, "unique": unique, "columns": index_columns})
    fk_rows = (
        await conn.execute(text(f"PRAGMA foreign_key_list({_quote_identifier(table)})"))
    ).all()
    foreign_keys = [
        {
            "from": row[3],
            "table": row[2],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
        }
        for row in fk_rows
    ]
    indexes.sort(key=lambda item: item["name"])
    foreign_keys.sort(key=lambda item: (item["from"], item["table"], item["to"] or ""))
    return {
        "table": table,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
    }


def _columns_by_name(signature: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in signature["columns"]}


def compare_table_signatures(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    strict_nullable: bool = False,
) -> dict[str, Any]:
    """单个表签名比对。

    硬差异（导致失败关闭）：列缺失/多余、类型（亲和性+长度）、主键、索引名称/唯一性/
    列顺序、外键。
    软差异（仅记录、不阻断本轮 v4 判定）：NULL 属性、默认值、列顺序。
    依据：D7 明确 nullable 继续延期、不纳入本批次；默认值同属该延期项，故同样降级为
    软差异。需要严格判定时传 strict_nullable=True 即可把二者升为硬差异。
    """
    hard: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []

    expected_columns = _columns_by_name(expected)
    actual_columns = _columns_by_name(actual)

    for name in sorted(set(expected_columns) - set(actual_columns)):
        hard.append({"kind": "column_missing", "column": name})
    for name in sorted(set(actual_columns) - set(expected_columns)):
        hard.append({"kind": "column_extra", "column": name})

    for name in sorted(set(expected_columns) & set(actual_columns)):
        exp = expected_columns[name]
        act = actual_columns[name]
        if _type_key(exp["type"]["raw"]) != _type_key(act["type"]["raw"]):
            hard.append(
                {
                    "kind": "column_type",
                    "column": name,
                    "expected": exp["type"],
                    "actual": act["type"],
                }
            )
        if int(exp["pk"]) != int(act["pk"]):
            hard.append(
                {"kind": "column_pk", "column": name, "expected": exp["pk"], "actual": act["pk"]}
            )
        nullability_diff = {
            "kind": "column_notnull",
            "column": name,
            "expected": exp["notnull"],
            "actual": act["notnull"],
        }
        default_diff = {
            "kind": "column_default",
            "column": name,
            "expected": exp["default"],
            "actual": act["default"],
        }
        if int(exp["notnull"]) != int(act["notnull"]):
            (hard if strict_nullable else soft).append(nullability_diff)
        if (exp["default"] if exp["default"] is not None else None) != (
            act["default"] if act["default"] is not None else None
        ):
            (hard if strict_nullable else soft).append(default_diff)

    expected_order = [column["name"] for column in expected["columns"]]
    actual_order = [column["name"] for column in actual["columns"]]
    if expected_order != actual_order:
        soft.append({"kind": "column_order", "expected": expected_order, "actual": actual_order})

    expected_indexes = {index["name"]: index for index in expected["indexes"]}
    actual_indexes = {index["name"]: index for index in actual["indexes"]}
    for name in sorted(set(expected_indexes) | set(actual_indexes)):
        if name not in actual_indexes:
            hard.append({"kind": "index_missing", "index": name})
            continue
        if name not in expected_indexes:
            hard.append({"kind": "index_extra", "index": name})
            continue
        exp, act = expected_indexes[name], actual_indexes[name]
        if int(exp["unique"]) != int(act["unique"]):
            hard.append(
                {
                    "kind": "index_unique",
                    "index": name,
                    "expected": exp["unique"],
                    "actual": act["unique"],
                }
            )
        if list(exp["columns"]) != list(act["columns"]):
            hard.append(
                {
                    "kind": "index_columns",
                    "index": name,
                    "expected": exp["columns"],
                    "actual": act["columns"],
                }
            )

    if expected["foreign_keys"] != actual["foreign_keys"]:
        hard.append(
            {
                "kind": "foreign_keys",
                "expected": expected["foreign_keys"],
                "actual": actual["foreign_keys"],
            }
        )

    return {"ok": not hard, "hard_diffs": hard, "soft_diffs": soft}


async def verify_signatures(
    conn: AsyncConnection,
    statements: list[str],
    *,
    strict_nullable: bool = False,
) -> dict[str, Any]:
    """对一批 DDL 语句的目标表做签名校验。

    返回：
    - ok：无硬差异（表缺失也算硬差异）
    - verifiable：这批语句是否含可校验的建表 DDL（v2 的纯 ADD COLUMN 批次不可预检）
    - all_present：目标表是否均已存在
    - tables：逐表 {present, ok, hard_diffs, soft_diffs}
    - hard_diffs / soft_diffs / missing_tables
    """
    expected_map, skipped_statements = _build_expected(statements)
    if not expected_map:
        return {
            "ok": True,
            "verifiable": False,
            "all_present": False,
            "tables": {},
            "hard_diffs": [],
            "soft_diffs": [],
            "missing_tables": [],
            "expected_tables": [],
            "skipped_statements": skipped_statements,
        }

    table_reports: dict[str, Any] = {}
    hard_diffs: list[dict[str, Any]] = []
    soft_diffs: list[dict[str, Any]] = []
    missing_tables: list[str] = []
    all_present = True

    for table, expected in expected_map.items():
        actual = await read_table_signature(conn, table)
        if actual is None:
            all_present = False
            missing_tables.append(table)
            hard_diffs.append({"kind": "table_missing", "table": table})
            table_reports[table] = {"present": False, "ok": False, "hard_diffs": [], "soft_diffs": []}
            continue
        comparison = compare_table_signatures(expected, actual, strict_nullable=strict_nullable)
        table_reports[table] = {
            "present": True,
            "ok": comparison["ok"],
            "hard_diffs": comparison["hard_diffs"],
            "soft_diffs": comparison["soft_diffs"],
        }
        hard_diffs.extend(dict(item, table=table) for item in comparison["hard_diffs"])
        soft_diffs.extend(dict(item, table=table) for item in comparison["soft_diffs"])

    return {
        "ok": not hard_diffs,
        "verifiable": True,
        "all_present": all_present,
        "tables": table_reports,
        "hard_diffs": hard_diffs,
        "soft_diffs": soft_diffs,
        "missing_tables": missing_tables,
        "expected_tables": sorted(expected_map),
        "skipped_statements": skipped_statements,
    }


# ---------------------------------------------------------------------------
# 预声明对象解析 + 补偿删除（D2，仅作最后恢复手段）
# ---------------------------------------------------------------------------
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'\[]?([\w$]+)", re.IGNORECASE
)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'\[]?([\w$]+)", re.IGNORECASE
)


def declared_objects(statements: list[str]) -> dict[str, list[str]]:
    """解析本批语句「预声明创建」的表/索引名（补偿删除的唯一允许范围）。"""
    tables: list[str] = []
    indexes: list[str] = []
    for statement in statements:
        table_match = _CREATE_TABLE_RE.search(statement)
        if table_match:
            tables.append(table_match.group(1))
        index_match = _CREATE_INDEX_RE.search(statement)
        if index_match:
            indexes.append(index_match.group(1))
    return {"tables": tables, "indexes": indexes}


async def compensate_declared_objects(
    conn: AsyncConnection, declared: dict[str, list[str]]
) -> dict[str, Any]:
    """补偿删除：只针对预声明对象，DROP 顺序为先索引后表。"""
    dropped: list[str] = []
    failed: list[dict[str, str]] = []
    for name in declared.get("indexes", []):
        try:
            await conn.exec_driver_sql(f"DROP INDEX IF EXISTS {_quote_identifier(name)}")
            dropped.append(f"index:{name}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"object": f"index:{name}", "error": repr(exc)})
    for name in declared.get("tables", []):
        try:
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_quote_identifier(name)}")
            dropped.append(f"table:{name}")
        except Exception as exc:  # noqa: BLE001
            failed.append({"object": f"table:{name}", "error": repr(exc)})
    return {"dropped": dropped, "failed": failed}


# ---------------------------------------------------------------------------
# 失败诊断
# ---------------------------------------------------------------------------
async def collect_failure_diagnostics(
    conn: AsyncConnection, declared: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """回滚后采集诊断：对象快照 / schema_migrations / integrity_check / 残留对象。"""
    diagnostics: dict[str, Any] = {"declared": declared or {"tables": [], "indexes": []}}

    try:
        rows = (
            await conn.execute(
                text(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
        ).all()
        diagnostics["objects"] = [f"{row[0]}:{row[1]}" for row in rows]
    except Exception as exc:  # noqa: BLE001
        diagnostics["objects"] = []
        diagnostics["objects_error"] = repr(exc)

    residual: list[str] = []
    for name in diagnostics["declared"].get("tables", []):
        found = (
            await conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
                {"name": name},
            )
        ).first()
        if found:
            residual.append(f"table:{name}")
    for name in diagnostics["declared"].get("indexes", []):
        found = (
            await conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :name"),
                {"name": name},
            )
        ).first()
        if found:
            residual.append(f"index:{name}")
    diagnostics["residual_objects"] = residual

    try:
        diagnostics["schema_migrations"] = await get_applied_versions(conn)
    except Exception as exc:  # noqa: BLE001
        diagnostics["schema_migrations"] = []
        diagnostics["schema_migrations_error"] = repr(exc)

    try:
        diagnostics["integrity_check"] = (
            await conn.execute(text("PRAGMA integrity_check"))
        ).scalar()
    except Exception as exc:  # noqa: BLE001
        diagnostics["integrity_check"] = f"error:{exc!r}"

    return diagnostics


# ---------------------------------------------------------------------------
# 单步原子应用（D1 + D2 + D3 的统一入口）
# ---------------------------------------------------------------------------
async def apply_step_atomically(
    conn: AsyncConnection,
    version: int,
    name: str,
    statements: list[str],
    *,
    strict_nullable: bool = False,
) -> dict[str, Any]:
    """在单个显式事务内完成：预检 → DDL → 签名校验 → 写版本 → COMMIT。

    预检（D2）：目标对象已完整存在且签名一致时，不执行任何 DDL，只补写版本记录。
    失败（任一步异常 / 签名不符）：ROLLBACK，不写版本，采集诊断后上抛。
    ROLLBACK 本身失败才启用补偿删除（仅预声明对象）；补偿失败立即停止并报告。
    """
    declared = declared_objects(statements)

    # 预检：只对「可校验」的建表批次生效，且不写任何东西
    precheck = await verify_signatures(conn, statements, strict_nullable=strict_nullable)
    skip_ddl = bool(precheck["verifiable"] and precheck["all_present"] and precheck["ok"])

    # D3：目标表已存在但与目标签名存在「IF NOT EXISTS 无法补齐」的硬差异时，
    # 必须失败关闭——不执行任何 DDL、不写版本、不自动重建表。
    # 仅 index_missing 例外：它可由创建索引的 IF NOT EXISTS 语句合法补齐。
    if precheck["verifiable"] and precheck["all_present"] and not precheck["ok"]:
        unfixable = [
            diff for diff in precheck["hard_diffs"] if diff.get("kind") != "index_missing"
        ]
        if unfixable:
            precheck["unfixable_diffs"] = unfixable
            logger.error(
                "v%s 预检发现不可补齐的结构差异，失败关闭：%s",
                version,
                ", ".join(sorted({str(diff.get("kind")) for diff in unfixable})),
            )
            raise SchemaSignatureMismatchError(version, precheck)
    ddl_executed = False
    signature_report = precheck
    transaction = ExplicitTransaction(conn)
    try:
        await transaction.__aenter__()
        if not skip_ddl:
            for statement in statements:
                await _execute_migration_statement(conn, statement)
            ddl_executed = True
            signature_report = await verify_signatures(
                conn, statements, strict_nullable=strict_nullable
            )
            if not signature_report["ok"]:
                raise SchemaSignatureMismatchError(version, signature_report)
        await record_version(conn, version, name)
        await transaction.commit()
    except BaseException as exc:  # noqa: BLE001
        rollback_error: BaseException | None = None
        try:
            await transaction.rollback()
        except BaseException as rollback_exc:  # noqa: BLE001
            rollback_error = rollback_exc
            logger.error("v%s ROLLBACK 失败：%r；启用补偿删除", version, rollback_exc)

        compensation: dict[str, Any] | None = None
        if rollback_error is not None:
            compensation = await compensate_declared_objects(conn, declared)
            if compensation["failed"]:
                raise MigrationError(
                    f"v{version} 回滚失败且补偿失败，已停止：rollback={rollback_error!r} "
                    f"compensation={compensation} declared={declared}"
                ) from exc
            logger.warning("v%s 已通过补偿删除恢复：%s", version, compensation["dropped"])

        diagnostics = await collect_failure_diagnostics(conn, declared)
        if compensation is not None:
            diagnostics["compensation"] = compensation
        if rollback_error is not None:
            diagnostics["rollback_error"] = repr(rollback_error)

        if isinstance(exc, SchemaSignatureMismatchError):
            exc.report = signature_report
            raise
        if isinstance(exc, MigrationError):
            raise
        raise MigrationStepFailure(version, diagnostics, exc) from exc
    finally:
        # 兜底：网关若仍开启（例如 commit 抛错），确保不留悬挂事务
        if transaction.opened and not transaction.closed:
            await transaction.rollback()

    return {
        "version": version,
        "name": name,
        "ddl_executed": ddl_executed,
        "precheck_skipped_ddl": skip_ddl,
        "signature": signature_report,
    }


# ---------------------------------------------------------------------------
# 备份（D8：SQLite backup API，可观测、唯一命名、多备份保留）
# ---------------------------------------------------------------------------
def _resolve_db_path(database_url: str) -> str | None:
    """从 sqlite URL 解析物理文件路径；非 sqlite / 内存库返回 None。"""
    if not database_url.startswith("sqlite"):
        return None
    raw = database_url.split(":///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    return os.path.abspath(raw)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _readonly_sqlite(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def create_sqlite_backup(
    source_path: str,
    *,
    stamp: str | None = None,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """用 SQLite backup API 生成一份可观测备份。

    与 shutil.copy2 的差别：backup API 会取一致性快照（正确处理 WAL / 页缓存中间态），
    并保证目标文件是完整可打开的库。文件名含时间戳 + pid + 随机串，绝不互相覆盖。
    """
    source_path = os.path.abspath(source_path)
    if not os.path.exists(source_path):
        raise MigrationBackupError(f"备份源不存在：{source_path}")

    late_stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = backup_dir or os.path.dirname(source_path) or "."
    os.makedirs(directory, exist_ok=True)
    filename = (
        f"{os.path.basename(source_path)}.bak-{late_stamp}-"
        f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    backup_path = os.path.abspath(os.path.join(directory, filename))
    if os.path.exists(backup_path):  # 极端兜底：唯一性必须成立
        raise MigrationBackupError(f"备份路径已存在，拒绝覆盖：{backup_path}")

    created_at = datetime.now().isoformat(timespec="seconds")
    source = _readonly_sqlite(source_path)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    if not os.path.exists(backup_path):
        raise MigrationBackupError(f"备份未生成：{backup_path}")

    check_conn = _readonly_sqlite(backup_path)
    try:
        integrity_rows = [row[0] for row in check_conn.execute("PRAGMA integrity_check")]
        quick_rows = [row[0] for row in check_conn.execute("PRAGMA quick_check")]
    finally:
        check_conn.close()

    result = {
        "source_path": source_path,
        "backup_path": backup_path,
        "source_size": os.path.getsize(source_path),
        "backup_size": os.path.getsize(backup_path),
        "backup_sha256": _sha256_file(backup_path),
        "integrity_check": integrity_rows[0] if integrity_rows else None,
        "integrity_check_all": integrity_rows,
        "quick_check": quick_rows[0] if quick_rows else None,
        "quick_check_all": quick_rows,
        "created_at": created_at,
        "method": "sqlite3.Connection.backup",
    }
    logger.info("迁移前备份完成：%s → %s", source_path, backup_path)
    return result


def backup_database(database_url: str) -> dict[str, Any] | None:
    """迁移前自动备份（兼容入口）：返回可观测字典，非文件库返回 None。"""
    db_path = _resolve_db_path(database_url)
    if not db_path or not os.path.exists(db_path):
        return None
    return create_sqlite_backup(db_path)


async def backup_database_async(database_url: str) -> dict[str, Any] | None:
    """异步包装：backup API 为阻塞调用，放到线程池执行，避免卡住事件循环。"""
    return await asyncio.to_thread(backup_database, database_url)


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------
async def ensure_migration_table(conn: AsyncConnection) -> None:
    """确保 schema_migrations 表存在（同样走显式事务）。"""
    async with ExplicitTransaction(conn) as transaction:
        await conn.execute(text(_MIGRATION_TABLE_SQL))
        await transaction.commit()


async def get_applied_versions(conn: AsyncConnection) -> list[dict]:
    """返回已应用的迁移记录（按版本升序）。"""
    rows = (
        await conn.execute(
            text("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
        )
    ).all()
    return [{"version": r[0], "name": r[1], "applied_at": r[2]} for r in rows]


async def record_version(conn: AsyncConnection, version: int, name: str) -> None:
    """记录一次迁移版本（必须在调用方事务内执行）。"""
    await conn.execute(
        text(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": version,
            "name": name,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


async def _backup_before_change(database_url: str) -> dict[str, Any] | None:
    """变更前备份；文件库备份失败必须上抛（不允许静默失去回退点）。"""
    backup = await backup_database_async(database_url)
    if backup is None:
        return None
    if backup.get("integrity_check") != "ok":
        raise MigrationBackupError(
            f"备份完整性检查未通过：{backup.get('integrity_check')!r} path={backup.get('backup_path')}"
        )
    return backup


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
async def run_migrations(
    conn: AsyncConnection,
    database_url: str,
    init_callback=None,
    *,
    strict_nullable: bool = False,
) -> dict:
    """执行迁移编排。

    返回结构（I-3a-fix 起新增 backups / signatures）：
    - status: "initialized"（首次从零初始化并记录 v1）| "upgraded"（应用了增量迁移）| "noop"
    - backup: 首个备份的可观测字典（兼容旧字段；无备份为 None）
    - backups: 本次产生的全部备份（一次升级多个版本时不会只保留最后一个）
    - applied: 当前已应用版本列表
    - applied_steps: 本次实际应用的增量版本列表（未变更时为空）
    - signatures: 本次各版本的签名校验报告（按版本号字符串索引）
    """
    backups: list[dict[str, Any]] = []
    signatures: dict[str, Any] = {}
    applied_steps: list[int] = []

    await ensure_migration_table(conn)
    applied = await get_applied_versions(conn)
    applied_versions = {row["version"] for row in applied}

    status = "noop"

    if not applied_versions:
        # 首次初始化：备份 → 基线 v1（init_callback：phase19 → create_all → 其他 ensure）
        backup = await _backup_before_change(database_url)
        if backup is not None:
            backups.append(backup)
        async with ExplicitTransaction(conn) as transaction:
            if init_callback is not None:
                await init_callback(conn)
            await record_version(conn, 1, BASELINE_NAME)
            await transaction.commit()
        applied_versions.add(1)
        status = "initialized"

    for version, (name, statements) in sorted(MIGRATION_STEPS.items()):
        # 启动执行上限：仅执行 <= SCHEMA_VERSION 的步骤（v5 已登记但本批次禁止执行）
        if version > SCHEMA_VERSION:
            continue
        if version in applied_versions:
            continue

        backup = await _backup_before_change(database_url)
        if backup is not None:
            backups.append(backup)

        report = await apply_step_atomically(
            conn, version, name, statements, strict_nullable=strict_nullable
        )
        signatures[str(version)] = report["signature"]
        applied_versions.add(version)
        applied_steps.append(version)
        if status == "noop":
            status = "upgraded"

    applied = await get_applied_versions(conn)
    return {
        "status": status,
        "backup": backups[0] if backups else None,
        "backups": backups,
        "applied": applied,
        "applied_steps": applied_steps,
        "signatures": signatures,
    }
