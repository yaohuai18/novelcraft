# -*- coding: utf-8 -*-
"""B2 模型层结构对齐测试（阶段 B2「最小修改 + 补测」）。

覆盖范围（仅这 8 个模型 / 8 张表）：
    character_state_snapshots、timeline_events、ai_tasks、audit_logs、
    chapter_contents、outlines、publications、locations

冻结基线（2026-10-25 只读快照，落为模块内常量；运行期不再读取正式库生成期望值）：
    - 列：166 列（列名 / 声明类型 / NOT NULL / DEFAULT；2026-11-17 批次 5 新增 content_bigrams）
    - 索引：34 条命名索引（不含 sqlite_autoindex_* 隐式索引）
    - 数据库级外键：13 条
ORM create_all 侧期望：166 列 / 34 条命名索引 / 22 条外键

白名单（已知且被接受的差异，共 30 条）：
    - W-DEF 9 条 = 类型名义差异 3 + 整数默认值引号差异 2 + 存量缺 server_default 4
    - W-IDX 12 条 = 仅正式库存在 6 + 仅 ORM 生成 6
    - W-FK  9 条 = 仅 ORM 声明 9

硬约束：
    - 只用内存库（sqlite://），绝不打开 backend 下的正式库文件；
    - 不执行迁移、不写业务数据、不联网；
    - 期望值全部来自本文件常量，不在运行期读正式库反推基线。
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

B2_TABLES = (
    "character_state_snapshots",
    "timeline_events",
    "ai_tasks",
    "audit_logs",
    "chapter_contents",
    "outlines",
    "publications",
    "locations",
)

EXPECTED_COLUMN_COUNT = 166
EXPECTED_INDEX_COUNT = 34
EXPECTED_DB_FK_COUNT = 13
EXPECTED_ORM_FK_COUNT = 22

# 正式库只读快照基线：表 -> ((列名, 类型, NOT NULL, DEFAULT), ...)
EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, bool, str | None], ...]] = {
    "character_state_snapshots": (
        ("id", "CHAR(32)", True, None),
        ("character_id", "CHAR(32)", True, None),
        ("chapter_id", "CHAR(32)", True, None),
        ("state_json", "TEXT", True, None),
        ("source", "VARCHAR(20)", True, None),
        ("status", "VARCHAR(20)", True, None),
        ("confirmed_at", "DATETIME", False, None),
        ("created_at", "DATETIME", True, None),
        ("authority_level", "VARCHAR(20)", True, "'normal'"),
        ("confidence", "REAL", False, None),
        ("locked", "BOOLEAN", True, '0'),
        ("source_record_id", "VARCHAR(64)", False, None),
        ("age", "INTEGER", False, None),
        ("birth_year", "INTEGER", False, None),
        ("is_alive", "BOOLEAN", False, None),
        ("location_id", "CHAR(32)", False, None),
        ("possessions", "TEXT", False, None),
        ("known_secrets", "TEXT", False, None),
        ("injuries", "TEXT", False, None),
        ("fact_kind", "VARCHAR(20)", True, "'fact'"),
        ("holder_character_id", "CHAR(32)", False, None),
    ),
    "timeline_events": (
        ("id", "CHAR(32)", True, None),
        ("project_id", "CHAR(32)", True, None),
        ("title", "VARCHAR(255)", True, None),
        ("description", "TEXT", False, None),
        ("event_time", "VARCHAR(255)", False, None),
        ("sort_order", "INTEGER", True, None),
        ("importance", "VARCHAR(20)", True, None),
        ("created_at", "DATETIME", True, None),
        ("chapter_id", "CHAR(32)", False, None),
        ("event_date_label", "VARCHAR(255)", False, None),
        ("sequence_order", "INTEGER", False, None),
        ("location_id", "CHAR(32)", False, None),
        ("participants", "TEXT", False, None),
        ("source", "VARCHAR(20)", True, "'manual'"),
        ("confirmed", "BOOLEAN", True, '0'),
        ("time_label", "VARCHAR(255)", False, None),
        ("status", "VARCHAR(16)", True, "'draft'"),
        ("authority_level", "VARCHAR(20)", True, "'normal'"),
        ("confidence", "REAL", False, None),
        ("locked", "BOOLEAN", True, '0'),
        ("source_record_id", "VARCHAR(64)", False, None),
        ("absolute_time", "VARCHAR(255)", False, None),
        ("relative_time", "VARCHAR(255)", False, None),
        ("participant_ids", "TEXT", False, None),
        ("cause_event_id", "CHAR(32)", False, None),
        ("result", "TEXT", False, None),
        ("is_public", "BOOLEAN", True, '1'),
        ("time_type", "VARCHAR(20)", True, "'unknown'"),
        ("time_start", "VARCHAR(255)", False, None),
        ("time_end", "VARCHAR(255)", False, None),
        ("narrated_at", "VARCHAR(255)", False, None),
        ("known_at", "VARCHAR(255)", False, None),
        ("fact_kind", "VARCHAR(20)", True, "'fact'"),
        ("holder_character_id", "CHAR(32)", False, None),
    ),
    "ai_tasks": (
        ("id", "CHAR(32)", True, None),
        ("user_id", "CHAR(32)", True, None),
        ("project_id", "CHAR(32)", True, None),
        ("outline_id", "CHAR(32)", False, None),
        ("task_type", "VARCHAR(50)", True, None),
        ("status", "VARCHAR(20)", True, None),
        ("prompt", "TEXT", True, None),
        ("result", "TEXT", False, None),
        ("error_message", "TEXT", False, None),
        ("retry_count", "INTEGER", True, None),
        ("created_at", "DATETIME", True, None),
        ("started_at", "DATETIME", False, None),
        ("completed_at", "DATETIME", False, None),
        ("model_name", "VARCHAR(100)", False, None),
        ("prompt_version", "VARCHAR(50)", False, None),
        ("idempotency_key", "VARCHAR(128)", False, None),
        ("request_snapshot", "TEXT", False, None),
        ("input_tokens", "INTEGER", False, None),
        ("output_tokens", "INTEGER", False, None),
        ("timeout_at", "DATETIME", False, None),
        ("max_retries", "INTEGER", True, '0'),
        ("cost_budget", "INTEGER", False, None),
        ("resume_token", "TEXT", False, None),
    ),
    "audit_logs": (
        ("id", "CHAR(32)", True, None),
        ("organization_id", "CHAR(32)", False, None),
        ("actor_id", "CHAR(32)", False, None),
        ("actor_type", "VARCHAR(20)", True, None),
        ("action", "VARCHAR(60)", True, None),
        ("target_type", "VARCHAR(40)", False, None),
        ("target_id", "VARCHAR(64)", False, None),
        ("before_value", "TEXT", False, None),
        ("after_value", "TEXT", False, None),
        ("reason", "TEXT", False, None),
        ("ip_hash", "VARCHAR(64)", False, None),
        ("user_agent", "VARCHAR(255)", False, None),
        ("metadata", "TEXT", False, None),
        ("created_at", "DATETIME", True, None),
        ("project_id", "VARCHAR(36)", False, None),
        ("diff_json", "TEXT", False, None),
        ("content_hash_before", "VARCHAR(64)", False, None),
        ("content_hash_after", "VARCHAR(64)", False, None),
        ("content_version", "INTEGER", False, None),
        ("change_source", "VARCHAR(30)", False, None),
    ),
    "chapter_contents": (
        ("id", "CHAR(32)", True, None),
        ("outline_id", "CHAR(32)", True, None),
        ("content", "TEXT", True, None),
        ("content_bigrams", "TEXT", True, None),
        ("word_count", "INTEGER", True, None),
        ("version", "INTEGER", True, None),
        ("created_at", "DATETIME", True, None),
        ("updated_at", "DATETIME", True, None),
        ("lifecycle_status", "VARCHAR(30)", True, "'draft'"),
        ("generation_meta", "TEXT", False, None),
        ("validation_report", "TEXT", False, None),
        ("repair_records", "TEXT", False, None),
        ("outline_version_id", "CHAR(32)", False, None),
        ("facts_version", "INTEGER", True, '0'),
        ("validation_summary", "TEXT", False, None),
        ("validation_updated_at", "DATETIME", False, None),
    ),
    "outlines": (
        ("id", "CHAR(32)", True, None),
        ("project_id", "CHAR(32)", True, None),
        ("parent_id", "CHAR(32)", False, None),
        ("title", "VARCHAR(255)", True, None),
        ("outline_type", "VARCHAR(30)", True, None),
        ("summary", "TEXT", False, None),
        ("content", "TEXT", False, None),
        ("sort_order", "INTEGER", True, None),
        ("visibility", "VARCHAR(30)", True, None),
        ("chapter_slug", "VARCHAR(255)", False, None),
        ("published_at", "DATETIME", False, None),
        ("status", "VARCHAR(30)", True, None),
        ("created_at", "DATETIME", True, None),
        ("updated_at", "DATETIME", True, None),
        ("chapter_plan", "TEXT", False, None),
        ("chapter_time", "VARCHAR(255)", False, None),
        ("location_id", "CHAR(32)", False, None),
        ("present_characters", "TEXT", False, None),
        ("chapter_goal", "TEXT", False, None),
        ("chapter_outcome", "TEXT", False, None),
        ("state_facts_updated_at", "DATETIME", False, None),
        ("lifecycle_status", "VARCHAR(30)", True, "'draft'"),
        ("generation_meta", "TEXT", False, None),
        ("validation_report", "TEXT", False, None),
        ("repair_records", "TEXT", False, None),
        ("outline_version_id", "CHAR(32)", False, None),
        ("facts_version", "INTEGER", True, '0'),
    ),
    "publications": (
        ("id", "CHAR(32)", True, None),
        ("project_id", "CHAR(32)", True, None),
        ("chapter_id", "CHAR(32)", True, None),
        ("title", "VARCHAR(255)", True, None),
        ("description", "TEXT", False, None),
        ("cover_text", "VARCHAR(255)", False, None),
        ("content", "TEXT", True, None),
        ("content_text", "TEXT", False, None),
        ("word_count", "INTEGER", True, None),
        ("status", "VARCHAR(30)", True, None),
        ("visibility", "VARCHAR(30)", True, None),
        ("published_at", "DATETIME", False, None),
        ("updated_at", "DATETIME", True, None),
        ("created_at", "DATETIME", True, None),
        ("baseline_meta", "TEXT", False, None),
        ("baseline_snapshot", "TEXT", False, None),
    ),
    "locations": (
        ("id", "CHAR(32)", True, None),
        ("project_id", "CHAR(32)", True, None),
        ("name", "VARCHAR(255)", True, None),
        ("category", "VARCHAR(60)", False, None),
        ("description", "TEXT", False, None),
        ("importance", "VARCHAR(20)", True, None),
        ("created_at", "DATETIME", True, None),
        ("parent_location_id", "CHAR(32)", False, None),
        ("travel_hours_from", "INTEGER", False, None),
    ),
}

# 正式库只读快照基线：表 -> ((索引名, 列元组, 是否唯一), ...)
EXPECTED_INDEXES: dict[str, tuple[tuple[str, tuple[str, ...], bool], ...]] = {
    "character_state_snapshots": (
        ("ix_character_state_snapshots_chapter_id", ('chapter_id',), False),
        ("ix_character_state_snapshots_character_id", ('character_id',), False),
        ("ix_character_state_snapshots_holder_character_id", ('holder_character_id',), False),
        ("ix_character_state_snapshots_locked", ('locked',), False),
        ("ix_character_state_snapshots_status", ('status',), False),
    ),
    "timeline_events": (
        ("ix_timeline_events_holder_character_id", ('holder_character_id',), False),
        ("ix_timeline_events_locked", ('locked',), False),
        ("ix_timeline_events_project_id", ('project_id',), False),
        ("ix_timeline_events_title", ('title',), False),
    ),
    "ai_tasks": (
        ("ix_ai_tasks_idempotency_key", ('idempotency_key',), False),
        ("ix_ai_tasks_outline_id", ('outline_id',), False),
        ("ix_ai_tasks_project_id", ('project_id',), False),
        ("ix_ai_tasks_status", ('status',), False),
        ("ix_ai_tasks_status_created", ('status', 'created_at'), False),
        ("ix_ai_tasks_user_id", ('user_id',), False),
    ),
    "audit_logs": (
        ("ix_audit_logs_action", ('action',), False),
        ("ix_audit_logs_actor_id", ('actor_id',), False),
        ("ix_audit_logs_content_target", ('target_type', 'target_id'), False),
        ("ix_audit_logs_created_at", ('created_at',), False),
        ("ix_audit_logs_ip_hash", ('ip_hash',), False),
        ("ix_audit_logs_organization_id", ('organization_id',), False),
        ("ix_audit_logs_project_id", ('project_id',), False),
        ("ix_audit_logs_target_id", ('target_id',), False),
        ("ix_audit_logs_target_type", ('target_type',), False),
    ),
    "chapter_contents": (
        ("ix_chapter_contents_lifecycle", ('lifecycle_status',), False),
        ("ix_chapter_contents_outline_id", ('outline_id',), True),
    ),
    "outlines": (
        ("ix_outlines_chapter_slug", ('chapter_slug',), True),
        ("ix_outlines_lifecycle", ('lifecycle_status',), False),
        ("ix_outlines_parent_id", ('parent_id',), False),
        ("ix_outlines_project_id", ('project_id',), False),
    ),
    "publications": (
        ("ix_publications_chapter_id", ('chapter_id',), True),
        ("ix_publications_project_id", ('project_id',), False),
    ),
    "locations": (
        ("ix_locations_name", ('name',), False),
        ("ix_locations_project_id", ('project_id',), False),
    ),
}

# 正式库只读快照基线：表 -> ((本表列, 目标表, 目标列, ON DELETE), ...)
EXPECTED_DB_FKS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "character_state_snapshots": (
        ("chapter_id", "outlines", "id", "CASCADE"),
        ("character_id", "characters", "id", "CASCADE"),
    ),
    "timeline_events": (
        ("project_id", "projects", "id", "CASCADE"),
    ),
    "ai_tasks": (
        ("outline_id", "outlines", "id", "SET NULL"),
        ("project_id", "projects", "id", "CASCADE"),
        ("user_id", "users", "id", "CASCADE"),
    ),
    "audit_logs": (
        ("actor_id", "users", "id", "SET NULL"),
    ),
    "chapter_contents": (
        ("outline_id", "outlines", "id", "CASCADE"),
    ),
    "outlines": (
        ("parent_id", "outlines", "id", "CASCADE"),
        ("project_id", "projects", "id", "CASCADE"),
    ),
    "publications": (
        ("chapter_id", "outlines", "id", "CASCADE"),
        ("project_id", "projects", "id", "CASCADE"),
    ),
    "locations": (
        ("project_id", "projects", "id", "CASCADE"),
    ),
}

# W-DEF：列级差异白名单（表, 列, 属性, 正式库值, ORM 值, 编号与理由）
COLUMN_WHITELIST = (
    ("ai_tasks", "max_retries", "default", '0', None, "W-DEF-01 存量差异：ORM 缺 server_default，正式库 DEFAULT 0（列入后续事项）"),
    ("chapter_contents", "facts_version", "default", '0', "'0'", "W-DEF-02 整数默认值引号差异（DEFAULT '0' 经 INTEGER 亲和性等价于 0）"),
    ("character_state_snapshots", "confidence", "type", 'REAL', 'FLOAT', "W-DEF-03 Float 类型名义差异（REAL/FLOAT 同亲和性）"),
    ("outlines", "facts_version", "default", '0', "'0'", "W-DEF-04 整数默认值引号差异（语义等价）"),
    ("timeline_events", "confidence", "type", 'REAL', 'FLOAT', "W-DEF-05 Float 类型名义差异（REAL/FLOAT 同亲和性）"),
    ("timeline_events", "confirmed", "default", '0', None, "W-DEF-06 存量差异：ORM 缺 server_default，正式库 DEFAULT 0（列入后续事项）"),
    ("timeline_events", "source", "default", "'manual'", None, "W-DEF-07 存量差异：ORM 缺 server_default，正式库 DEFAULT 'manual'（列入后续事项）"),
    ("timeline_events", "status", "default", "'draft'", None, "W-DEF-08 存量差异：ORM 缺 server_default，正式库 DEFAULT 'draft'（列入后续事项）"),
    ("timeline_events", "status", "type", 'VARCHAR(16)', 'VARCHAR(20)', "W-DEF-09 长度名义差异（正式库 16 / ORM 20，均满足 draft|confirmed|archived）"),
)

# W-IDX：索引差异白名单（表, 索引名, 列元组, 差异侧, 编号与理由）
INDEX_WHITELIST = (
    ("character_state_snapshots", "ix_character_state_snapshots_locked", ('locked',), "formal_only", "W-IDX-01 仅正式库存在"),
    ("character_state_snapshots", "ix_character_state_snapshots_location_id", ('location_id',), "orm_only", "W-IDX-02 仅 ORM 生成"),
    ("timeline_events", "ix_timeline_events_locked", ('locked',), "formal_only", "W-IDX-03 仅正式库存在"),
    ("timeline_events", "ix_timeline_events_chapter_id", ('chapter_id',), "orm_only", "W-IDX-04 仅 ORM 生成"),
    ("timeline_events", "ix_timeline_events_status", ('status',), "orm_only", "W-IDX-05 仅 ORM 生成"),
    ("ai_tasks", "ix_ai_tasks_status_created", ('status', 'created_at'), "formal_only", "W-IDX-06 仅正式库存在（复合索引）"),
    ("audit_logs", "ix_audit_logs_content_target", ('target_type', 'target_id'), "formal_only", "W-IDX-07 仅正式库存在（复合索引）"),
    ("chapter_contents", "ix_chapter_contents_lifecycle", ('lifecycle_status',), "formal_only", "W-IDX-08 仅正式库存在"),
    ("chapter_contents", "ix_chapter_contents_outline_version_id", ('outline_version_id',), "orm_only", "W-IDX-09 仅 ORM 生成"),
    ("outlines", "ix_outlines_lifecycle", ('lifecycle_status',), "formal_only", "W-IDX-10 仅正式库存在"),
    ("outlines", "ix_outlines_location_id", ('location_id',), "orm_only", "W-IDX-11 仅 ORM 生成"),
    ("outlines", "ix_outlines_outline_version_id", ('outline_version_id',), "orm_only", "W-IDX-12 仅 ORM 生成"),
)

# W-FK：外键差异白名单（表, (本表列, 目标表, 目标列, ON DELETE), 编号与理由）
FK_WHITELIST = (
    ("character_state_snapshots", ("holder_character_id", "characters", "id", "SET NULL"), "W-FK-01 仅 ORM 声明"),
    ("character_state_snapshots", ("location_id", "locations", "id", "SET NULL"), "W-FK-02 仅 ORM 声明"),
    ("timeline_events", ("cause_event_id", "timeline_events", "id", "SET NULL"), "W-FK-03 仅 ORM 声明（自引用）"),
    ("timeline_events", ("chapter_id", "outlines", "id", "SET NULL"), "W-FK-04 仅 ORM 声明"),
    ("timeline_events", ("holder_character_id", "characters", "id", "SET NULL"), "W-FK-05 仅 ORM 声明"),
    ("timeline_events", ("location_id", "locations", "id", "SET NULL"), "W-FK-06 仅 ORM 声明"),
    ("outlines", ("location_id", "locations", "id", "SET NULL"), "W-FK-07 仅 ORM 声明"),
    ("outlines", ("outline_version_id", "outline_plan_versions", "id", "SET NULL"), "W-FK-08 仅 ORM 声明"),
    ("locations", ("parent_location_id", "locations", "id", "SET NULL"), "W-FK-09 仅 ORM 声明（自引用）"),
)

# B2 本次补齐的 8 个 server_default（表, 列, PRAGMA 读回的默认值文本）
B2_SERVER_DEFAULTS = (
    ("character_state_snapshots", "authority_level", "'normal'"),
    ("character_state_snapshots", "locked", '0'),
    ("character_state_snapshots", "fact_kind", "'fact'"),
    ("timeline_events", "authority_level", "'normal'"),
    ("timeline_events", "locked", '0'),
    ("timeline_events", "is_public", '1'),
    ("timeline_events", "time_type", "'unknown'"),
    ("timeline_events", "fact_kind", "'fact'"),
)

# 存量 4 列：仍缺 server_default，已登记为后续事项，本期不处理
HISTORICAL_MISSING_SERVER_DEFAULT = (
    ("ai_tasks", "max_retries"),
    ("timeline_events", "source"),
    ("timeline_events", "confirmed"),
    ("timeline_events", "status"),
)

_NON_DEFAULT_ATTRS = ("type", "notnull", "default")


def _table_snapshot(conn, table: str):
    """读取单表结构：列 / 命名索引 / 外键（与 PRAGMA 原样一致）。"""
    columns = []
    for row in conn.execute(f"PRAGMA table_xinfo('{table}')").fetchall():
        if row[6]:
            continue  # 跳过 hidden 生成列
        columns.append((row[1], (row[2] or "").upper(), bool(row[3]), row[4]))

    indexes = []
    for idx in conn.execute(f"PRAGMA index_list('{table}')").fetchall():
        name = idx[1]
        if name.startswith("sqlite_autoindex"):
            continue  # 隐式索引不计入 34 条基线
        cols = tuple(
            r[2] for r in conn.execute(f"PRAGMA index_info('{name}')").fetchall()
        )
        indexes.append((name, cols, bool(idx[2])))

    fks = []
    for fk in conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall():
        fks.append((fk[3], fk[2], fk[4], (fk[6] or "NO ACTION").upper()))

    return columns, indexes, fks


@pytest.fixture(scope="module")
def mem_db():
    """内存 SQLite + ORM create_all：全程不触碰正式库文件。"""
    from app.core.database import Base
    import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    raw = engine.raw_connection()
    try:
        yield engine, raw.driver_connection, Base
    finally:
        raw.close()
        engine.dispose()


@pytest.fixture(scope="module")
def snapshot(mem_db):
    _, conn, _ = mem_db
    return {table: _table_snapshot(conn, table) for table in B2_TABLES}


def test_module_never_references_formal_database_file():
    """守卫：本测试文件不得出现正式库文件名或只读连接串。"""
    with open(__file__, encoding="utf-8") as fh:
        src = fh.read()
    forbidden_db = "novelcraft" + chr(46) + "db"
    forbidden_uri = "mode" + "=" + "ro"
    assert forbidden_db not in src
    assert forbidden_uri not in src


def test_engine_is_in_memory(mem_db):
    engine, _, _ = mem_db
    assert engine.dialect.name == "sqlite"
    assert engine.url.database in (None, "", ":memory:")


def test_b2_tables_present(mem_db):
    _, conn, _ = mem_db
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(set(B2_TABLES) - names)
    assert not missing, f"create_all 未建出这些表：{missing}"


def test_column_baseline_is_165(snapshot):
    total = sum(len(snapshot[t][0]) for t in B2_TABLES)
    assert total == EXPECTED_COLUMN_COUNT == 166
    for table in B2_TABLES:
        assert len(snapshot[table][0]) == len(EXPECTED_COLUMNS[table]), table


def test_columns_align_with_baseline_except_whitelist(snapshot):
    diffs = set()
    for table in B2_TABLES:
        expected = {c[0]: c for c in EXPECTED_COLUMNS[table]}
        actual = {c[0]: c for c in snapshot[table][0]}
        assert set(expected) == set(actual), f"{table} 列集合与基线不一致"
        for name, exp in expected.items():
            got = actual[name]
            for i, attr in enumerate(_NON_DEFAULT_ATTRS, start=1):
                if exp[i] != got[i]:
                    diffs.add((table, name, attr, exp[i], got[i]))
    whitelisted = {(t, c, a, fv, ov) for t, c, a, fv, ov, _ in COLUMN_WHITELIST}
    unexpected = sorted(diffs - whitelisted)
    stale = sorted(whitelisted - diffs)
    assert not unexpected, f"出现未登记的结构差异：{unexpected}"
    assert not stale, f"白名单条目已不再成立，应删除：{stale}"


def test_column_whitelist_entries_really_diverge(snapshot):
    for table, column, attr, formal_value, orm_value, reason in COLUMN_WHITELIST:
        expected = {c[0]: c for c in EXPECTED_COLUMNS[table]}[column]
        actual = {c[0]: c for c in snapshot[table][0]}[column]
        index = {"type": 1, "notnull": 2, "default": 3}[attr]
        assert expected[index] == formal_value, f"{reason}：基线值被改动"
        assert actual[index] == orm_value, f"{reason}：ORM 实际值已变"


def test_eight_b2_server_defaults_present(snapshot):
    assert len(B2_SERVER_DEFAULTS) == 8
    for table, column, expected_default in B2_SERVER_DEFAULTS:
        actual = {c[0]: c for c in snapshot[table][0]}[column]
        assert actual[3] is not None, f"{table}.{column} 仍缺 DEFAULT"
        assert actual[3] == expected_default, (
            f"{table}.{column} DEFAULT 期望 {expected_default!r}，实际 {actual[3]!r}"
        )


def test_eight_b2_server_defaults_declared_in_orm_metadata(mem_db):
    _, _, Base = mem_db
    for table, column, _ in B2_SERVER_DEFAULTS:
        col = Base.metadata.tables[table].columns[column]
        assert col.server_default is not None, f"{table}.{column} 未声明 server_default"


def test_historical_columns_still_lack_server_default(snapshot):
    """存量 4 列本期不动：create_all 侧仍无 DEFAULT，登记为后续事项。"""
    assert len(HISTORICAL_MISSING_SERVER_DEFAULT) == 4
    for table, column in HISTORICAL_MISSING_SERVER_DEFAULT:
        actual = {c[0]: c for c in snapshot[table][0]}[column]
        assert actual[3] is None, f"{table}.{column} 已被改动，与「本期不改」约定冲突"


def test_index_baseline_and_whitelist(snapshot):
    diffs = set()
    total = 0
    for table in B2_TABLES:
        formal = {(n, c, u) for n, c, u in EXPECTED_INDEXES[table]}
        actual = {(n, c, u) for n, c, u in snapshot[table][1]}
        total += len(actual)
        for entry in actual - formal:
            diffs.add((table, entry[0], entry[1], "orm_only"))
        for entry in formal - actual:
            diffs.add((table, entry[0], entry[1], "formal_only"))
    assert total == EXPECTED_INDEX_COUNT == 34
    whitelisted = {(t, n, c, side) for t, n, c, side, _ in INDEX_WHITELIST}
    unexpected = sorted(diffs - whitelisted)
    stale = sorted(whitelisted - diffs)
    assert not unexpected, f"出现未登记的索引差异：{unexpected}"
    assert not stale, f"索引白名单条目已不再成立：{stale}"
    assert len(INDEX_WHITELIST) == 12


def test_db_fk_baseline_and_whitelist(snapshot):
    diffs = set()
    total = 0
    for table in B2_TABLES:
        formal = set(EXPECTED_DB_FKS[table])
        actual = set(snapshot[table][2])
        total += len(actual)
        for entry in actual - formal:
            diffs.add((table, entry))
    assert total == EXPECTED_ORM_FK_COUNT == 22, "create_all 外键总数应为 22"
    assert sum(len(snapshot[t][2]) - len(EXPECTED_DB_FKS[t]) for t in B2_TABLES) == (
        EXPECTED_ORM_FK_COUNT - EXPECTED_DB_FK_COUNT
    )
    whitelisted = {(t, e) for t, e, _ in FK_WHITELIST}
    unexpected = sorted(diffs - whitelisted)
    stale = sorted(whitelisted - diffs)
    assert not unexpected, f"出现未登记的外键差异：{unexpected}"
    assert not stale, f"外键白名单条目已不再成立：{stale}"
    assert len(FK_WHITELIST) == 9
    formal_total = sum(len(v) for v in EXPECTED_DB_FKS.values())
    assert formal_total == EXPECTED_DB_FK_COUNT == 13


def test_create_all_is_idempotent(mem_db, snapshot):
    engine, conn, Base = mem_db
    Base.metadata.create_all(engine)
    again = {table: _table_snapshot(conn, table) for table in B2_TABLES}
    for table in B2_TABLES:
        assert again[table] == snapshot[table], f"{table} 二次 create_all 后结构发生变化"
