# -*- coding: utf-8 -*-
"""阶段 E 六表 + B-2 账目表的 ORM↔正式库结构对齐回归（离线；不触正式库）。

被测对象
  - app/models/item.py / item_possession.py / secret_knower.py /
    chapter_fact_dependency.py / consistency_review.py / fact_issue_disposition.py
  - app/models/ai_task_token_usage.py
  - app/core/db_migrations.py（PHASE19/20 建表建索引常量抽取，行为等价）

冻结口径（正式库 PRAGMA 实测基线，不得回归）
  1. 六表零库级外键（PRAGMA foreign_key_list 为空），引用合法性由服务层校验；
  2. 索引名称/列/顺序/唯一性与正式库逐项相等，且不得多出正式库没有的索引
     （例如 items 无 name 索引、consistency_review 无 status 索引）；
  3. 列序与正式库一致：三张事实表的 created_at 位于 status（secret_knowers 为
     forbidden_before_chapter_id）之后、authority_level 之前；
  4. server_default 必须与正式库建表语句一致（category/status/authority_level/locked/
     scope/trigger/issue_count/suggestion_count/source）；
  5. B-2 账目表：sqlite_autoincrement 生效、业务唯一键 (task_id, attempt_no, call_no)
     唯一、两个业务索引 idx_usage_status_settled / idx_usage_task_dispatched；
  6. 迁移通道幂等：create_all 建表后重复执行 ensure_phase12~22 不得报错、不得增删索引；
  7. phase19/20 的建表建索引语句只允许存在一份（常量），ensure 函数不得再内联 DDL。

运行：cd backend && python -m pytest tests/test_phase1_schema_no_fk.py -q
"""
from __future__ import annotations

import asyncio
import inspect

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import db_migrations as mig
from app.core.database import Base
from app.models.ai_task_token_usage import USAGE_LEDGER_TABLE_NAME
from app.models.chapter_fact_dependency import phase19_metadata

FACT_TABLES = (
    "items",
    "item_possessions",
    "secret_knowers",
    "chapter_fact_dependencies",
    "consistency_review",
    "fact_issue_dispositions",
)
ALL_TABLES = FACT_TABLES + (USAGE_LEDGER_TABLE_NAME,)

# 正式库实测索引基线：{表: {索引名: (列元组, 是否唯一)}}
EXPECTED_INDEXES = {
    "items": {
        "ix_items_project": (("project_id",), False),
        "ix_items_locked": (("locked",), False),
    },
    "item_possessions": {
        "ix_item_possessions_item": (("item_id",), False),
        "ix_item_possessions_owner": (("owner_character_id",), False),
        "ix_item_possessions_locked": (("locked",), False),
    },
    "secret_knowers": {
        "ix_secret_knowers_secret": (("secret_id",), False),
        "ix_secret_knowers_character": (("character_id",), False),
        "ix_secret_knowers_locked": (("locked",), False),
    },
    "chapter_fact_dependencies": {
        "ix_chapter_fact_dependencies_chapter_id": (("chapter_id",), False),
        "ix_chapter_fact_dependencies_lookup": (("entity_type", "entity_id"), False),
        "ix_chapter_fact_dependencies_fact_key": (("fact_key",), False),
    },
    "consistency_review": {
        "ix_consistency_review_project_id": (("project_id",), False),
        "ix_consistency_review_chapter_id": (("chapter_id",), False),
        "ix_consistency_review_task_id": (("task_id",), False),
    },
    "fact_issue_dispositions": {
        "ix_fact_issue_dispositions_disposition": (("disposition",), False),
        "ix_fact_issue_dispositions_issue_key": (("issue_key",), False),
    },
    USAGE_LEDGER_TABLE_NAME: {
        "idx_usage_status_settled": (("usage_status", "settled_at"), False),
        "idx_usage_task_dispatched": (("task_id", "dispatched_at"), False),
    },
}

# 正式库各表 server_default 基线（PRAGMA table_info.dflt_value 形态）
EXPECTED_SERVER_DEFAULTS = {
    "items": {
        "category": "'item'",
        "status": "'active'",
        "authority_level": "'normal'",
        "locked": "0",
    },
    "item_possessions": {
        "status": "'active'",
        "authority_level": "'normal'",
        "locked": "0",
    },
    "secret_knowers": {
        "source": "'ai_extracted'",
        "authority_level": "'normal'",
        "locked": "0",
    },
    "consistency_review": {
        "scope": "'chapter'",
        "status": "'pending'",
        "trigger": "'manual'",
        "issue_count": "0",
        "suggestion_count": "0",
    },
}

# 正式库明确不存在的索引（防「多加索引」回归）
FORBIDDEN_INDEXES = (
    "ix_items_name",
    "ix_consistency_review_status",
    "ix_fact_issue_dispositions_project_id",
    "ix_fact_issue_dispositions_set_by",
    "ix_chapter_fact_dependencies_project_id",
    "ix_chapter_fact_dependencies_entity_type",
    "ix_chapter_fact_dependencies_entity_id",
)


def _run(coro):
    return asyncio.run(coro)


async def _make_engine():
    """内存库 + 全量 ORM 建表（create_all 通道）。"""
    import app.models  # noqa: F401  注册全部 ORM 表

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await mig.ensure_phase19_schema(conn)
    return engine


async def _snapshot(conn, table: str) -> dict:
    """读取单表结构：列（含默认值/非空）、索引（名/列序/唯一）、外键、原始 DDL。"""
    columns = (await conn.execute(text(f"PRAGMA table_info({table})"))).mappings().all()
    indexes = {}
    for row in (await conn.execute(text(f"PRAGMA index_list({table})"))).mappings().all():
        name = row["name"]
        if name.startswith("sqlite_autoindex"):
            # 由 UNIQUE 约束隐式生成，单独按「唯一列组合」统计
            info = (await conn.execute(text(f"PRAGMA index_info({name})"))).mappings().all()
            indexes[name] = {
                "columns": tuple(r["name"] for r in sorted(info, key=lambda r: r["seqno"])),
                "unique": bool(row["unique"]),
                "auto": True,
            }
            continue
        info = (await conn.execute(text(f"PRAGMA index_info({name})"))).mappings().all()
        indexes[name] = {
            "columns": tuple(r["name"] for r in sorted(info, key=lambda r: r["seqno"])),
            "unique": bool(row["unique"]),
            "auto": False,
        }
    foreign_keys = (await conn.execute(text(f"PRAGMA foreign_key_list({table})"))).mappings().all()
    ddl = (
        await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}
        )
    ).scalar() or ""
    return {
        "columns": [(c["name"], c["notnull"], c["dflt_value"]) for c in columns],
        "column_names": [c["name"] for c in columns],
        "defaults": {c["name"]: c["dflt_value"] for c in columns},
        "indexes": indexes,
        "foreign_keys": [dict(r) for r in foreign_keys],
        "ddl": ddl,
    }


async def _all_snapshots() -> dict[str, dict]:
    engine = await _make_engine()
    try:
        async with engine.connect() as conn:
            return {t: await _snapshot(conn, t) for t in ALL_TABLES}
    finally:
        await engine.dispose()


def test_six_fact_tables_have_no_foreign_keys():
    """六表必须零库级外键：ORM 声明层与 SQLite 实际 DDL 双通道一致。"""
    snapshots = _run(_all_snapshots())
    for table in FACT_TABLES:
        assert snapshots[table]["foreign_keys"] == [], f"{table} 仍存在库级外键"
        # chapter_fact_dependencies 已退出 Base.metadata（唯一建表来源为 phase19 迁移），
        # 其 ORM 声明位于独立 metadata；其余表仍从 Base.metadata 取。
        orm_table = (
            phase19_metadata.tables[table]
            if table in phase19_metadata.tables
            else Base.metadata.tables[table]
        )
        assert list(orm_table.foreign_keys) == [], f"{table} ORM 仍声明 ForeignKey"


def test_index_names_columns_and_uniqueness_match_production():
    """索引名称、列顺序、唯一性必须与正式库逐项相等（不得多、不得少、不得改名）。"""
    snapshots = _run(_all_snapshots())
    for table, expected in EXPECTED_INDEXES.items():
        actual = {
            name: (meta["columns"], meta["unique"])
            for name, meta in snapshots[table]["indexes"].items()
            if not meta["auto"]
        }
        assert actual == expected, f"{table} 索引与正式库不一致：{actual}"


def test_no_extra_or_forbidden_indexes():
    """正式库没有的索引一律不得出现（历史 .index=True 漂移不得回潮）。"""
    snapshots = _run(_all_snapshots())
    for table in ALL_TABLES:
        names = set(snapshots[table]["indexes"])
        for forbidden in FORBIDDEN_INDEXES:
            assert forbidden not in names, f"{table} 出现正式库不存在的索引 {forbidden}"


def test_created_at_column_order_matches_production():
    """三张事实表的 created_at 必须落在与正式库相同的列位置。"""
    snapshots = _run(_all_snapshots())
    col = {t: snapshots[t]["column_names"] for t in FACT_TABLES}

    # items / item_possessions：... status -> created_at -> authority_level ...
    for table in ("items", "item_possessions"):
        names = col[table]
        assert names.index("created_at") == names.index("status") + 1, f"{table} 列序不符"
        assert names.index("authority_level") == names.index("created_at") + 1, f"{table} 列序不符"

    # secret_knowers：... forbidden_before_chapter_id -> created_at -> authority_level ...
    names = col["secret_knowers"]
    assert names.index("created_at") == names.index("forbidden_before_chapter_id") + 1
    assert names.index("authority_level") == names.index("created_at") + 1

    # chapter_fact_dependencies：created_at 为末列
    assert col["chapter_fact_dependencies"][-1] == "created_at"

    # consistency_review：created_at / completed_at 为最后两列
    assert col["consistency_review"][-2:] == ["created_at", "completed_at"]


def test_server_defaults_match_production():
    """server_default 必须与正式库建表语句一致，且不能漏到 NULL 语义。"""
    snapshots = _run(_all_snapshots())
    for table, expected in EXPECTED_SERVER_DEFAULTS.items():
        defaults = snapshots[table]["defaults"]
        for column, default in expected.items():
            assert defaults[column] is not None, f"{table}.{column} 缺少 server_default"
            assert str(defaults[column]) == default, (
                f"{table}.{column} server_default={defaults[column]!r}，正式库为 {default!r}"
            )


def test_token_ledger_autoincrement_unique_and_business_indexes():
    """B-2 账目表：AUTOINCREMENT 生效、业务唯一键唯一、两个业务索引齐备。"""
    snapshots = _run(_all_snapshots())
    ledger = snapshots[USAGE_LEDGER_TABLE_NAME]

    assert "AUTOINCREMENT" in ledger["ddl"].upper(), "账目表缺少 AUTOINCREMENT（sqlite_sequence 不复用）"

    unique_sets = {
        meta["columns"]
        for meta in ledger["indexes"].values()
        if meta["unique"] and meta["columns"] != ("id",)
    }
    assert ("task_id", "attempt_no", "call_no") in unique_sets, "账目表业务唯一键缺失"

    for name in ("idx_usage_status_settled", "idx_usage_task_dispatched"):
        assert name in ledger["indexes"], f"账目表缺少业务索引 {name}"


def test_phase19_phase20_statements_are_single_source_constants():
    """phase19/20 的建表建索引语句只允许存在一份常量，ensure 函数不得再内联 DDL。"""
    for func, constant, table in (
        (mig.ensure_phase19_schema, "PHASE19_MIGRATION_STATEMENTS", "chapter_fact_dependencies"),
        (mig.ensure_phase20_schema, "PHASE20_MIGRATION_STATEMENTS", "consistency_review"),
    ):
        body = inspect.getsource(func)
        assert constant in body, f"{func.__name__} 未引用 {constant}"
        assert "CREATE INDEX" not in body.upper(), f"{func.__name__} 仍内联 CREATE INDEX"

        statements = getattr(mig, constant)
        assert statements and "CREATE TABLE" in statements[0].upper(), f"{constant} 首条应为建表"
        expected = {name for name in EXPECTED_INDEXES[table]}
        # 常量可能拆成多行字符串，按整条语句拼接后再取索引名
        names = set()
        for statement in statements[1:]:
            flat = " ".join(statement.split())
            names.add(flat.split("IF NOT EXISTS", 1)[1].split()[0])
        assert names == expected, f"{constant} 索引集合与 ORM 基线不一致：{names}"


def test_migration_chain_is_idempotent_after_create_all():
    """create_all 后再重复跑 ensure_phase12~22：不报错、结构不变（双通道等价）。"""

    async def _scenario():
        import app.models  # noqa: F401

        engine = await _make_engine()
        try:
            before = {}
            async with engine.connect() as conn:
                for table in ALL_TABLES:
                    before[table] = await _snapshot(conn, table)

            async with engine.begin() as conn:
                for _ in range(2):
                    await mig.ensure_phase12_schema(conn)
                    await mig.ensure_phase13_schema(conn)
                    await mig.ensure_phase14_schema(conn)
                    await mig.ensure_phase15_schema(conn)
                    await mig.ensure_phase16_schema(conn)
                    await mig.ensure_phase17_schema(conn)
                    await mig.ensure_phase18_schema(conn)
                    await mig.ensure_phase19_schema(conn)
                    await mig.ensure_phase20_schema(conn)
                    await mig.ensure_phase21_schema(conn)
                    await mig.ensure_phase22_schema(conn)

            async with engine.connect() as conn:
                after = {}
                for table in ALL_TABLES:
                    after[table] = await _snapshot(conn, table)
            return before, after
        finally:
            await engine.dispose()

    before, after = _run(_scenario())
    for table in ALL_TABLES:
        assert after[table]["indexes"] == before[table]["indexes"], f"{table} 迁移链改变了索引"
        assert after[table]["column_names"] == before[table]["column_names"], f"{table} 迁移链改变了列序"
        assert after[table]["foreign_keys"] == [], f"{table} 迁移链引入了外键"


def test_models_package_exports_fact_issue_disposition():
    """导出一致性：FactIssueDisposition / DISPOSITION_TYPES 必须能被 app.models 直接取到并列入 __all__。

    回归背景：模型文件已定义并导入，但 __all__ 漏登记，导致 `from app.models import *`
    与显式导入行为不一致。
    """
    import app.models as models
    from app.models.fact_issue_disposition import DISPOSITION_TYPES as _TYPES
    from app.models.fact_issue_disposition import FactIssueDisposition as _MODEL

    assert models.FactIssueDisposition is _MODEL
    assert models.DISPOSITION_TYPES is _TYPES
    assert "FactIssueDisposition" in models.__all__
    assert "DISPOSITION_TYPES" in models.__all__
