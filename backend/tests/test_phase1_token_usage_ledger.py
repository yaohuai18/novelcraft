# -*- coding: utf-8 -*-
"""B-2 逐次 token 账目（cost_budget 业务层）正式回归测试（离线；不触正式库、不发真实网络）。

被测对象
  - app/services/token_usage_service.py   状态机 / 账目闸门 / 预算闸门 / 409·422 契约 / 钩子
  - app/models/ai_task_token_usage.py     账目 ORM（create_all 通道）
  - app/core/db_migrations.py             主 DDL + 由主 DDL 派生的补列常量 + ensure_phase22_schema
  - app/core/migrate.py                   _execute_migration_statement（空库首启重复列容忍）

口径底线（不得回归）
  1. 未知用量绝不折算为 0（token 字段留 NULL，不写估算值、不写字符数折算）；
  2. 已下发且无法结清的账目（request_uncertain / partial / unverifiable）→ 阻断后续派发（409）；
  3. 在途未决（pending_dispatch 且已下发）→ 阻断（422）；未下发的占用行不代表已烧钱；
  4. 每次调用前先落库占用行（dispatched_at 非空）再发请求，且每次调用前过账目 + 预算两道闸门；
  5. 阶段 1 冻结契约不回归：正文已取得时任务终态与修复前一致（未知 usage 不误杀任务）；
  6. 空库首启 step2 裸 ALTER 的重复列容忍不得回退为异常中断，真实失败不得被吞；
  7. 结构单源：ORM（create_all 通道）与迁移 DDL 通道的列名·类型·非空·默认·唯一约束必须一致，
     补列常量必须由主 DDL 现场派生（旧手写漂移已修复）；
  8. ai_tasks 表零变更、账目表不加外键、迁移不含 DML 回填。

运行：cd backend && python -m pytest tests/test_phase1_token_usage_ledger.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from datetime import datetime

import pytest
from sqlalchemy import UniqueConstraint, select, text
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import db_migrations as mig
from app.core import migrate
from app.core.config import settings
from app.core.database import Base
from app.models.ai_task import AITask
from app.models.ai_task_token_usage import (
    USAGE_LEDGER_TABLE_NAME,
    USAGE_LEDGER_UNIQUE_CONSTRAINT,
    AITaskTokenUsage,
)
from app.services import ai_service, ai_task_service, token_usage_service as ledger

from _phase1_helpers import completion_body, drain_ai_context, memory_db, patch_provider, run

SQLITE_DIALECT = sqlite_dialect.dialect()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _task(**kwargs) -> AITask:
    """游离（未入库）AITask：默认走通用兜底执行器，供闸门 / 分类纯判定用例使用。"""
    kwargs.setdefault("task_type", "legacy_dialog")
    return AITask(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="pending",
        prompt="B-2 离线用例",
        **kwargs,
    )


async def _insert_task(engine, **kwargs) -> AITask:
    """写入隔离内存库并返回**已加载全部列**的游离实例（可安全跨会话读取）。"""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        task = _task(**kwargs)
        db.add(task)
        await db.commit()
        await db.refresh(task)
        db.expunge(task)
        return task


async def _add_row(engine, task_id, **kwargs) -> None:
    """直接写入账目行（构造历史遗留 / 崩溃残留场景，不经过业务钩子）。"""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(AITaskTokenUsage(task_id=task_id, **kwargs))
        await db.commit()


def _code_only(fn) -> str:
    """取函数可执行代码（剥离 docstring），避免注释里的历史缺陷描述干扰源码级断言。"""
    import ast
    import textwrap

    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return ast.unparse(node)


# ---------------------------------------------------------------------------
# 1. 结构单源：补列常量 / ORM / 迁移 DDL 三者一致
# ---------------------------------------------------------------------------

def test_ledger_column_constants_are_derived_from_single_main_ddl():
    """补列常量必须由主 DDL 现场派生（漂移修复的防回归）。"""
    source = inspect.getsource(mig)
    phase22_block = source.split("阶段 B-2「逐次 token 账目表」", 1)[1].split(
        "PHASE22_ALLOWED_USAGE_STATUS", 1
    )[0]
    assert phase22_block.count("_PHASE22_USAGE_COLUMNS = ") == 1, "补列常量只允许有一份定义"
    assert "_column_definitions_from_ddl(_PHASE22_USAGE_TABLE_DDL)" in phase22_block, (
        "补列常量必须由主 DDL 现场派生（禁止第二份手写定义）"
    )

    derived = mig._column_definitions_from_ddl(mig._PHASE22_USAGE_TABLE_DDL)
    assert [name for name, _ in derived][0] == "id"
    assert len(derived) == 19, "主 DDL 应为 19 列（18 业务列 + id）"
    assert mig._PHASE22_USAGE_COLUMNS == [
        (name, definition) for name, definition in derived if name != "id"
    ], "补列常量必须与主 DDL 逐项一致（不允许第二份手写定义）"

    mapping = dict(mig._PHASE22_USAGE_COLUMNS)
    assert mapping["created_at"] == "DATETIME NOT NULL", "created_at 漂移：缺 NOT NULL"
    assert mapping["task_id"] == "CHAR(32) NOT NULL", "task_id 漂移：不得带 DEFAULT ''"
    assert "id" not in mapping, "SQLite 不支持补主键列，补列集合必须排除 id"


def test_orm_matches_main_ddl_type_notnull_and_defaults():
    """ORM 与主 DDL 逐项对齐：列名 / 类型 / 非空 / 默认值，缺一即漂移。"""
    table = AITaskTokenUsage.__table__
    derived = mig._column_definitions_from_ddl(mig._PHASE22_USAGE_TABLE_DDL)
    assert {column.name for column in table.columns} == {name for name, _ in derived}
    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert [
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ] == [USAGE_LEDGER_UNIQUE_CONSTRAINT]

    for name, definition in mig._PHASE22_USAGE_COLUMNS:
        column = table.c[name]
        type_sql = str(column.type.compile(dialect=SQLITE_DIALECT)).upper()
        assert definition.upper().startswith(type_sql), (
            f"{name} 类型漂移：DDL={definition} / ORM={type_sql}"
        )
        assert ("NOT NULL" in definition.upper()) is (not column.nullable), (
            f"{name} 非空漂移：DDL={definition} / ORM.nullable={column.nullable}"
        )
        if " DEFAULT " in f" {definition.upper()} ":
            literal = definition.upper().split(" DEFAULT ", 1)[1].strip().strip("'")
            assert column.server_default is not None, f"{name} 缺 server_default"
            actual = str(column.server_default.arg).strip().strip("'").upper()
            assert actual == literal, f"{name} 默认值漂移：DDL={literal} / ORM={actual}"
        else:
            assert column.server_default is None, (
                f"{name} 不得有 server_default：NULL 表示未知，不得给默认值"
            )


def test_two_build_channels_produce_identical_table_schema():
    """create_all 通道（ORM）与迁移 DDL 通道建出的表结构必须逐项一致。"""
    async def scenario():
        async with memory_db() as (engine, session):
            shadow = f"{USAGE_LEDGER_TABLE_NAME}_ddl"
            ddl = mig._PHASE22_USAGE_TABLE_DDL.replace(
                f"{USAGE_LEDGER_TABLE_NAME} (", f"{shadow} (", 1
            )
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
                orm_rows = (
                    await conn.execute(text(f"PRAGMA table_info({USAGE_LEDGER_TABLE_NAME})"))
                ).all()
                ddl_rows = (
                    await conn.execute(text(f"PRAGMA table_info({shadow})"))
                ).all()
                orm_sql = (
                    await conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE name = :n"),
                        {"n": USAGE_LEDGER_TABLE_NAME},
                    )
                ).scalar()
                ddl_sql = (
                    await conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE name = :n"),
                        {"n": shadow},
                    )
                ).scalar()

            assert len(orm_rows) == len(ddl_rows) == 19
            orm_map = {row[1]: row for row in orm_rows}
            for row in ddl_rows:
                name = row[1]
                other = orm_map[name]
                assert str(other[2]).upper() == str(row[2]).upper(), f"{name} 类型不一致"
                assert str(other[4]).upper() == str(row[4]).upper(), f"{name} 默认值不一致"
                if name == "id":
                    assert other[5] == row[5] == 1, "id 必须同为单列主键"
                else:
                    assert other[3] == row[3], f"{name} 非空不一致"
            for sql in (orm_sql, ddl_sql):
                assert USAGE_LEDGER_UNIQUE_CONSTRAINT in sql
                assert "UNIQUE" in sql.upper()
            await session.rollback()

    run(scenario())


def test_ensure_phase22_schema_is_idempotent_and_creates_indexes():
    """迁移函数可重复执行；19 列齐全；两个索引存在；空表枚举扫描无异常值。"""
    async def scenario():
        async with memory_db() as (engine, session):
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP TABLE IF EXISTS {USAGE_LEDGER_TABLE_NAME}"))
                await mig.ensure_phase22_schema(conn)
                await mig.ensure_phase22_schema(conn)  # 二次执行不得报错（幂等）
                columns = await mig._table_columns(conn, USAGE_LEDGER_TABLE_NAME)
                indexes = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                "SELECT name FROM sqlite_master WHERE type = 'index' "
                                "AND tbl_name = :n"
                            ),
                            {"n": USAGE_LEDGER_TABLE_NAME},
                        )
                    ).all()
                }
                report = await mig._report_phase22_enum_anomalies(conn)
            assert len(columns) == 19
            assert {"idx_usage_status_settled", "idx_usage_task_dispatched"} <= indexes
            assert report["rows"] == 0
            assert report["unexpected_usage_status"] == []
            assert report["unexpected_usage_source"] == []
            await session.rollback()

    run(scenario())


def test_ledger_migration_keeps_ai_tasks_untouched_and_adds_no_foreign_key():
    """B-2 红线：账目表不得改动 ai_tasks 结构，也不得外键挂接 ai_tasks。"""
    async def scenario():
        async with memory_db() as (engine, session):
            async with engine.begin() as conn:
                before = (await conn.execute(text("PRAGMA table_info(ai_tasks)"))).all()
                await mig.ensure_phase22_schema(conn)
                after = (await conn.execute(text("PRAGMA table_info(ai_tasks)"))).all()
                foreign_keys = (
                    await conn.execute(
                        text(f"PRAGMA foreign_key_list({USAGE_LEDGER_TABLE_NAME})")
                    )
                ).all()
            assert [row[1] for row in before] == [row[1] for row in after]
            assert foreign_keys == []
            await session.rollback()

    run(scenario())


def test_phase22_migration_statements_contain_no_dml_backfill():
    """迁移只增不改：不得含 INSERT/UPDATE/DELETE/DROP 回填。"""
    statements = [statement.upper() for statement in mig.PHASE22_MIGRATION_STATEMENTS]
    assert len(statements) == 3
    for statement in statements:
        stripped = statement.lstrip()
        assert stripped.startswith(("CREATE TABLE", "CREATE INDEX"))
        assert "DELETE" not in statement and "DROP" not in statement
        assert not stripped.startswith(("INSERT", "UPDATE"))


def test_empty_db_bootstrap_tolerates_duplicate_column_but_not_real_errors():
    """空库首启：create_all 已建列后再执行裸 ALTER 必须幂等跳过；其余异常照旧上抛。"""
    async def scenario():
        async with memory_db() as (engine, session):
            statement = "ALTER TABLE ai_tasks ADD COLUMN model_name VARCHAR(100)"
            async with engine.begin() as conn:
                await migrate._execute_migration_statement(conn, statement)
                await migrate._execute_migration_statement(conn, statement)
                assert "model_name" in await mig._table_columns(conn, "ai_tasks")
            async with engine.begin() as conn:
                with pytest.raises(OperationalError):
                    await migrate._execute_migration_statement(
                        conn, "ALTER TABLE ai_tasks ADD COLUMN 1bad INTEGER"
                    )
            await session.rollback()

    run(scenario())


def test_ledger_enum_domain_aligns_with_migration_layer():
    """状态 / 来源取值收敛为迁移层允许枚举（各 5 个值），导入即校验。"""
    assert len(ledger.ALL_USAGE_STATUSES) == 5
    assert len(ledger.ALL_USAGE_SOURCES) == 5
    assert set(ledger.ALL_USAGE_STATUSES) == set(mig.PHASE22_ALLOWED_USAGE_STATUS)
    assert set(ledger.ALL_USAGE_SOURCES) == set(mig.PHASE22_ALLOWED_USAGE_SOURCE)
    assert ledger.TERMINAL_BLOCKING_STATUSES == (
        ledger.USAGE_STATUS_REQUEST_UNCERTAIN,
        ledger.USAGE_STATUS_PARTIAL,
        ledger.USAGE_STATUS_UNVERIFIABLE,
    )
    # 阻断态必须落在允许枚举内，避免写出非法值
    assert set(ledger.TERMINAL_BLOCKING_STATUSES) <= set(ledger.ALL_USAGE_STATUSES)


# ---------------------------------------------------------------------------
# 2. 状态机（纯函数穷举）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code", [None, 400, 429, 500, 503])
def test_non_success_response_is_request_uncertain_and_blocking(status_code):
    """非 2xx / 无状态码：是否被计费无法确认 -> request_uncertain 且阻断后续派发。"""
    classification = ledger.classify_response(status_code, completion_body())
    assert classification.usage_status == ledger.USAGE_STATUS_REQUEST_UNCERTAIN
    assert classification.usage_source == ledger.USAGE_SOURCE_REQUEST_UNCERTAIN
    assert classification.blocking is True
    assert classification.prompt_tokens is None
    assert classification.total_tokens is None


def test_success_without_usage_is_unverifiable_and_blocking():
    classification = ledger.classify_response(200, completion_body(usage=False))
    assert classification.usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
    assert classification.usage_source == ledger.USAGE_SOURCE_MISSING
    assert classification.blocking is True
    assert classification.prompt_tokens is None and classification.completion_tokens is None


def test_usage_without_any_number_is_unverifiable():
    body = completion_body()
    body["usage"] = {"prompt_tokens": None, "completion_tokens": "abc", "total_tokens": True}
    classification = ledger.classify_response(200, body)
    assert classification.usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
    assert classification.usage_source == ledger.USAGE_SOURCE_MISSING
    assert classification.blocking is True
    assert (classification.prompt_tokens, classification.completion_tokens, classification.total_tokens) == (
        None,
        None,
        None,
    )


def test_self_consistent_breakdown_is_complete_and_not_blocking():
    classification = ledger.classify_response(
        200, completion_body(prompt_tokens=11, completion_tokens=22)
    )
    assert classification.usage_status == ledger.USAGE_STATUS_COMPLETE
    assert classification.usage_source == ledger.USAGE_SOURCE_PROMPT_COMPLETION
    assert classification.blocking is False
    assert (classification.prompt_tokens, classification.completion_tokens, classification.total_tokens) == (
        11,
        22,
        33,
    )


def test_total_mismatch_is_partial_and_blocking():
    """计费口径差异（total != prompt + completion）不得按 complete 静默放行。"""
    body = completion_body(prompt_tokens=10, completion_tokens=10)
    body["usage"]["total_tokens"] = 999
    classification = ledger.classify_response(200, body)
    assert classification.usage_status == ledger.USAGE_STATUS_PARTIAL
    assert classification.usage_source == ledger.USAGE_SOURCE_TOTAL
    assert classification.blocking is True


def test_single_side_breakdown_is_partial_and_blocking():
    body = completion_body()
    body["usage"] = {"total_tokens": 42}
    classification = ledger.classify_response(200, body)
    assert classification.usage_status == ledger.USAGE_STATUS_PARTIAL
    assert classification.usage_source == ledger.USAGE_SOURCE_TOTAL
    assert classification.blocking is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (7, 7),
        ("12", 12),
        (" 13 ", 13),
        (5.0, 5),
        (5.5, None),
        (True, None),
        ("abc", None),
        (None, None),
        ({}, None),
    ],
)
def test_strict_integer_parsing_never_folds_to_zero(raw, expected):
    """严格整型解析：无法确认的一律 None（绝不折算为 0 / 不猜数）。"""
    assert ledger._as_int(raw) == expected


def test_ledger_never_folds_unknown_usage_to_zero_in_source():
    """源码级防回归：账目落账 / 分类不得出现 `or 0` 或字符数折算。"""
    for fn in (
        ledger.classify_response,
        ledger.row_known_tokens,
        ledger.legacy_token_floor,
        ledger._as_int,
    ):
        code = _code_only(fn)
        assert "or 0" not in code, f"{fn.__name__} 把未知用量折叠为 0"
        assert "len(" not in code, f"{fn.__name__} 出现按字符数估算 token"


# ---------------------------------------------------------------------------
# 3. 调用前闸门与占用落库
# ---------------------------------------------------------------------------

def test_before_dispatch_writes_occupancy_row_then_blocks_second_call():
    """先落库、后发请求；占用行即为在途，第二次调用被 422 拦截且不新增行。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch(
                {"provider": "deepseek", "requested_model": "m-x", "normalized_model": "m-x"}
            )
            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            row = rows[0]
            assert (row.attempt_no, row.call_no) == (1, 1)
            assert row.usage_status == ledger.USAGE_STATUS_PENDING
            assert row.usage_source == ledger.USAGE_SOURCE_NOT_DISPATCHED
            assert row.dispatched_at is not None, "占用行必须带 dispatched_at（崩溃可见）"
            assert row.settled_at is None
            assert row.provider == "deepseek"

            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await hook.before_dispatch({"provider": "deepseek"})
            assert excinfo.value.code == ledger.CODE_LEDGER_IN_FLIGHT
            assert excinfo.value.http_status == ledger.HTTP_UNPROCESSABLE
            assert excinfo.value.api_code == "validation_failed"
            assert len(await ledger.ledger_rows(session, task.id)) == 1, "阻断时不得再落占用行"

    run(scenario())


def test_second_call_in_same_attempt_increments_call_no():
    """同一 attempt 内多次调用：call_no 单调递增（结清后可继续下一次调用）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_response(
                {"status_code": 200, "body": completion_body(), "provider": "deepseek"}
            )
            await hook.before_dispatch({"provider": "deepseek"})
            rows = await ledger.ledger_rows(session, task.id)
            assert [(row.attempt_no, row.call_no) for row in rows] == [(1, 1), (1, 2)]
            assert rows[0].usage_status == ledger.USAGE_STATUS_COMPLETE
            assert hook.blocked_error is None

    run(scenario())


@pytest.mark.parametrize(
    "status",
    [
        ledger.USAGE_STATUS_REQUEST_UNCERTAIN,
        ledger.USAGE_STATUS_PARTIAL,
        ledger.USAGE_STATUS_UNVERIFIABLE,
    ],
)
def test_unsettled_terminal_row_blocks_every_later_dispatch_with_409(status):
    """已下发但无法结清的账目：任何后续派发（含 resume / retry）一律 409。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            await _add_row(
                engine,
                task.id,
                attempt_no=1,
                call_no=1,
                usage_status=status,
                usage_source=ledger.USAGE_SOURCE_REQUEST_UNCERTAIN
                if status == ledger.USAGE_STATUS_REQUEST_UNCERTAIN
                else ledger.USAGE_SOURCE_MISSING,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_dispatch_allowed(session, task, action="调用前派发")
            error = excinfo.value
            assert error.code == ledger.CODE_LEDGER_UNSETTLED
            assert error.http_status == ledger.HTTP_CONFLICT
            assert error.api_code == "conflict"
            assert error.details["unsettled"][0]["usage_status"] == status
            assert error.details["usage_ledger"]["unsettled"] == 1

    run(scenario())


def test_pending_row_without_dispatch_does_not_block():
    """未下发的占用行不代表已烧钱：不进入任何阻断桶（不得误伤崩溃前的空占用）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            await _add_row(
                engine,
                task.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_PENDING,
                usage_source=ledger.USAGE_SOURCE_NOT_DISPATCHED,
                dispatched_at=None,
            )
            state = await ledger.load_ledger_state(session, task.id)
            assert state.blocked is False
            assert state.dispatched_calls == 0
            await ledger.assert_dispatch_allowed(session, task)  # 不抛异常

    run(scenario())


def test_assert_recoverable_contract_carries_409_and_422():
    """resume / retry 前置契约：无账目放行；冲突 409；在途 422；消息带动作名。"""
    async def scenario():
        async with memory_db() as (engine, session):
            clean = await _insert_task(engine)
            await ledger.assert_recoverable(session, clean, action="resume")

            in_flight = await _insert_task(engine)
            await _add_row(
                engine,
                in_flight.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_PENDING,
                usage_source=ledger.USAGE_SOURCE_NOT_DISPATCHED,
                dispatched_at=datetime.utcnow(),
            )
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_recoverable(session, in_flight, action="resume")
            assert excinfo.value.code == ledger.CODE_LEDGER_IN_FLIGHT
            assert excinfo.value.http_status == 422
            assert "恢复任务" in excinfo.value.message
            assert excinfo.value.details["in_flight"][0]["call_no"] == 1

            conflicting = await _insert_task(engine)
            await _add_row(
                engine,
                conflicting.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_PARTIAL,
                usage_source=ledger.USAGE_SOURCE_TOTAL,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_recoverable(session, conflicting, action="retry")
            assert excinfo.value.http_status == 409
            assert "重试任务" in excinfo.value.message

    run(scenario())


# ---------------------------------------------------------------------------
# 4. 预算闸门与消费下界
# ---------------------------------------------------------------------------

def test_budget_gate_off_without_budget_and_blocks_when_lower_bound_reached():
    """未设预算 -> 关闭；已知下界 >= cost_budget -> 409（口径与既有预检一致）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            open_task = await _insert_task(engine, cost_budget=None)
            decision = await ledger.evaluate_budget(session, open_task)
            assert decision.allowed is True and decision.budget is None

            tight = await _insert_task(engine, cost_budget=100, input_tokens=60, output_tokens=40)
            decision = await ledger.evaluate_budget(session, tight)
            assert decision.allowed is False
            assert decision.code == ledger.CODE_BUDGET_EXCEEDED
            assert decision.http_status == ledger.HTTP_CONFLICT
            assert decision.basis == 100 and decision.basis_source == "legacy"

            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_budget_allowed(session, tight)
            assert excinfo.value.code == ledger.CODE_BUDGET_EXCEEDED
            assert excinfo.value.details["cost_budget"] == 100

            roomy = await _insert_task(engine, cost_budget=100, input_tokens=60, output_tokens=39)
            assert (await ledger.evaluate_budget(session, roomy)).allowed is True

    run(scenario())


def test_budget_gate_rejects_amount_based_pricing_when_unit_price_unknown():
    """按金额核算预算且单价未知 -> 422（禁止按 0 折算继续调用）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine, cost_budget=None)
            capability = ledger.provider_usage_capability("deepseek")
            assert capability.pricing_known is False
            decision = await ledger.evaluate_budget(
                session, task, pricing_required=True, provider="deepseek"
            )
            assert decision.allowed is False
            assert decision.code == ledger.CODE_PRICING_UNAVAILABLE
            assert decision.http_status == ledger.HTTP_UNPROCESSABLE
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_budget_allowed(
                    session, task, pricing_required=True, provider="deepseek"
                )
            assert excinfo.value.code == ledger.CODE_PRICING_UNAVAILABLE

    run(scenario())


def test_known_consumed_tokens_takes_max_of_ledger_and_legacy_fields():
    """已知消费下界 = max(账簿已知, 历史字段已知)：既不重复计数也不低估。"""
    async def scenario():
        async with memory_db() as (engine, session):
            empty = await _insert_task(engine)
            assert await ledger.known_consumed_tokens(session, empty) == (0, "none")

            legacy = await _insert_task(engine, input_tokens=30, output_tokens=20)
            assert await ledger.known_consumed_tokens(session, legacy) == (50, "legacy")

            ledger_heavier = await _insert_task(engine, input_tokens=30, output_tokens=None)
            await _add_row(
                engine,
                ledger_heavier.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_COMPLETE,
                usage_source=ledger.USAGE_SOURCE_PROMPT_COMPLETION,
                total_tokens=100,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            assert await ledger.known_consumed_tokens(session, ledger_heavier) == (100, "ledger")

            legacy_heavier = await _insert_task(engine, input_tokens=200, output_tokens=50)
            await _add_row(
                engine,
                legacy_heavier.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_COMPLETE,
                usage_source=ledger.USAGE_SOURCE_PROMPT_COMPLETION,
                total_tokens=100,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            assert await ledger.known_consumed_tokens(session, legacy_heavier) == (250, "mixed")

            unknown_only = await _insert_task(engine, input_tokens=None, output_tokens=None)
            await _add_row(
                engine,
                unknown_only.id,
                attempt_no=1,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_UNVERIFIABLE,
                usage_source=ledger.USAGE_SOURCE_MISSING,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            basis, source = await ledger.known_consumed_tokens(session, unknown_only)
            assert (basis, source) == (0, "ledger")
            assert unknown_only.input_tokens is None and unknown_only.output_tokens is None
            assert unknown_only.cost_budget is None

    run(scenario())


def test_attempt_no_and_call_no_semantics():
    """attempt_no 用 confirmed attempt 语义；call_no 同 attempt 内单调。"""
    assert ledger.confirmed_attempt_no(_task(retry_count=0), []) == 1
    assert ledger.confirmed_attempt_no(_task(retry_count=2), []) == 3

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine, retry_count=2)
            assert ledger.confirmed_attempt_no(task, []) == 3
            await _add_row(
                engine,
                task.id,
                attempt_no=3,
                call_no=1,
                usage_status=ledger.USAGE_STATUS_COMPLETE,
                usage_source=ledger.USAGE_SOURCE_PROMPT_COMPLETION,
                total_tokens=5,
                dispatched_at=datetime.utcnow(),
                settled_at=datetime.utcnow(),
            )
            rows = await ledger.ledger_rows(session, task.id)
            assert ledger.confirmed_attempt_no(task, rows) == 3
            assert await ledger.next_call_no(session, task.id, 3) == 2
            assert await ledger.next_call_no(session, task.id, 4) == 1
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            rows = await ledger.ledger_rows(session, task.id)
            assert (rows[-1].attempt_no, rows[-1].call_no) == (3, 2)

    run(scenario())


# ---------------------------------------------------------------------------
# 5. 钩子结清与作用域
# ---------------------------------------------------------------------------

def test_response_hook_settles_unverifiable_and_raises_blocked_error():
    """200 但无 usage：账目结清为 unverifiable，token 留 NULL，后续派发被阻断。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_response(
                {
                    "status_code": 200,
                    "body": completion_body(usage=False),
                    "provider": "deepseek",
                }
            )
            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
            assert row.usage_source == ledger.USAGE_SOURCE_MISSING
            assert row.settled_at is not None
            assert row.total_tokens is None and row.prompt_tokens is None
            assert hook.blocked_error is not None
            assert hook.blocked_error.http_status == ledger.HTTP_CONFLICT
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await hook.before_dispatch({"provider": "deepseek"})
            assert excinfo.value.code == ledger.CODE_LEDGER_UNSETTLED

    run(scenario())


def test_error_hook_settles_request_uncertain_even_with_usage_in_error_body():
    """失败路径（含 4xx/5xx 异常体）：是否计费无法确认 -> request_uncertain，不落 token。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_error(
                {
                    "error_type": "http",
                    "detail": "ai_provider_error",
                    "status_code": 500,
                    "body": completion_body(),
                }
            )
            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.usage_status == ledger.USAGE_STATUS_REQUEST_UNCERTAIN
            assert row.usage_source == ledger.USAGE_SOURCE_REQUEST_UNCERTAIN
            assert row.prompt_tokens is None and row.total_tokens is None
            assert hook.blocked_error is not None

    run(scenario())


def test_postprocess_failure_keeps_settled_ledger():
    """已取得 usage 后的后处理失败：默认保留已落定账目（不把确认值回退成不确定）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_response(
                {"status_code": 200, "body": completion_body(), "provider": "deepseek"}
            )
            await hook.postprocess_failed({"detail": "json 解析失败"})
            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.usage_status == ledger.USAGE_STATUS_COMPLETE
            assert row.total_tokens == 33
            assert hook.blocked_error is None
            await ledger.assert_dispatch_allowed(session, task)

    run(scenario())


def test_unknown_stage_payload_is_ignored():
    """未知钩子阶段：只告警不落账、不抛异常（防御性分支）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook("no_such_stage", None)
            assert await ledger.ledger_rows(session, task.id) == []
            assert hook.blocked_error is None

    run(scenario())


def test_install_uninstall_usage_hook_is_scoped_and_idempotent():
    """钩子必须能装能卸：卸载后不留 contextvar，重复卸载无副作用。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            assert ai_service.usage_hook_installed() is False
            hook = ledger.install_usage_ledger_hook(session, task)
            assert isinstance(hook, ledger.UsageLedgerHook)
            assert ai_service.usage_hook_installed() is True
            ledger.uninstall_usage_ledger_hook()
            assert ai_service.usage_hook_installed() is False
            ledger.uninstall_usage_ledger_hook()
            assert ai_service.usage_hook_installed() is False

    run(scenario())


# ---------------------------------------------------------------------------
# 6. 端到端（隔离内存库 + mock 供应商）
# ---------------------------------------------------------------------------

def test_e2e_unknown_usage_delivers_content_and_blocks_resume(monkeypatch):
    """阶段 1 冻结契约 + B-2：正文照常交付、token 留 NULL，但账目阻断 resume。"""
    drain_ai_context()
    body_text = "未知用量下的正文"
    patch_provider(monkeypatch, body=completion_body(content=body_text, usage=False))

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await ai_task_service.run_ai_task(task.id, factory)
            fresh = (
                await session.execute(select(AITask).where(AITask.id == task.id))
            ).scalar_one()
            assert fresh.status == "succeeded"
            assert fresh.result == body_text
            assert fresh.input_tokens is None and fresh.output_tokens is None

            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            assert rows[0].usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
            assert rows[0].dispatched_at is not None
            assert rows[0].settled_at is not None
            assert rows[0].total_tokens is None

            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_recoverable(session, fresh, action="resume")
            assert excinfo.value.code == ledger.CODE_LEDGER_UNSETTLED
            assert excinfo.value.http_status == 409

    run(scenario())


def test_e2e_known_usage_settles_complete_and_keeps_task_contract(monkeypatch):
    """正常 provider usage：账目 complete、任务 token 落真实值、不产生阻断。"""
    drain_ai_context()
    patch_provider(
        monkeypatch,
        body=completion_body(content="正常正文", prompt_tokens=7, completion_tokens=8),
    )

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine, cost_budget=1000)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await ai_task_service.run_ai_task(task.id, factory)
            fresh = (
                await session.execute(select(AITask).where(AITask.id == task.id))
            ).scalar_one()
            assert fresh.status == "succeeded"
            assert (fresh.input_tokens, fresh.output_tokens) == (7, 8)

            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            assert rows[0].usage_status == ledger.USAGE_STATUS_COMPLETE
            assert rows[0].total_tokens == 15
            state = await ledger.assert_dispatch_allowed(session, fresh)
            assert state.complete_rows and state.blocked is False

    run(scenario())


def test_e2e_provider_error_records_uncertain_and_blocks_auto_retry_burn(monkeypatch):
    """供应商 5xx：账目落 request_uncertain，自动重试被账目闸门拦住（只发一次请求）。"""
    drain_ai_context()
    captured = patch_provider(monkeypatch, body=completion_body(), status_code=500)

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine, max_retries=1, idempotency_key="b2-offline-1")
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await ai_task_service.run_ai_task(task.id, factory)
            fresh = (
                await session.execute(select(AITask).where(AITask.id == task.id))
            ).scalar_one()
            assert fresh.status == "failed"
            assert len(captured["calls"]) == 1, "账目闸门必须拦住自动重试，禁止重复烧钱"
            assert fresh.retry_count == 1
            assert ledger.CODE_LEDGER_UNSETTLED in (fresh.error_message or "")

            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            assert rows[0].usage_status == ledger.USAGE_STATUS_REQUEST_UNCERTAIN
            assert rows[0].usage_source == ledger.USAGE_SOURCE_REQUEST_UNCERTAIN
            assert rows[0].prompt_tokens is None and rows[0].total_tokens is None

    run(scenario())



# ---------------------------------------------------------------------------
# 6. B-2 复核修正补充（A-3 / A-4 / A-5 / C-2~C-5）
# ---------------------------------------------------------------------------

class _NonJsonSuccessResponse:
    """2xx 但响应体不是合法 JSON 的响应替身（A-5；不发网络）。"""

    def __init__(self, error: Exception, status_code: int = 200) -> None:
        self._error = error
        self.status_code = status_code
        self.headers = {"x-request-id": "req-a5-broken-json"}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def text(self) -> str:
        return "<html>502 upstream error</html>"

    def json(self):
        raise self._error


# --- A-3：已结算账目不得被后处理失败改判 --------------------------------

def test_postprocess_failure_keeps_complete_settlement():
    """A-3：已结算为 complete 的账目，后处理失败后仍保持 complete。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_response(
                {
                    "status_code": 200,
                    "body": completion_body(),
                    "provider": "deepseek",
                }
            )
            assert hook.row.usage_status == ledger.USAGE_STATUS_COMPLETE

            await hook.postprocess_failed({"detail": "bad_response: 缺少 message.content"})

            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.usage_status == ledger.USAGE_STATUS_COMPLETE, (
                "已结算账目不得被后处理失败改判"
            )
            assert row.usage_source == ledger.USAGE_SOURCE_PROMPT_COMPLETION
            assert row.settled_at is not None
            assert hook.blocked_error is None, "complete 非阻断态，不得因后处理失败转阻断"

    run(scenario())


def test_postprocess_failure_keeps_partial_settlement():
    """A-3：已结算为 partial 的账目，后处理失败后仍保持 partial（不回退为不确定）。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})
            await hook.settle_from_response(
                {
                    "status_code": 200,
                    "body": {"usage": {"total_tokens": 41}, "choices": []},
                    "provider": "deepseek",
                }
            )
            assert hook.row.usage_status == ledger.USAGE_STATUS_PARTIAL

            await hook.postprocess_failed({"detail": "json 后处理失败"})

            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.usage_status == ledger.USAGE_STATUS_PARTIAL
            assert row.usage_source == ledger.USAGE_SOURCE_TOTAL
            assert row.total_tokens == 41
            assert row.settled_at is not None

    run(scenario())


def test_postprocess_failure_settles_in_flight_pending_row_as_unverifiable():
    """A-5：已下发但未落定的 pending 行，后处理失败必须结清为 unverifiable。"""
    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine)
            hook = ledger.UsageLedgerHook(session, task)
            await hook.before_dispatch({"provider": "deepseek"})

            await hook.postprocess_failed({"detail": "响应体不是合法 JSON"})

            row = (await ledger.ledger_rows(session, task.id))[0]
            assert row.dispatched_at is not None
            assert row.usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
            assert row.usage_source == ledger.USAGE_SOURCE_MISSING
            assert row.settled_at is not None, "不得遗留永久 pending 行"
            assert row.total_tokens is None and row.prompt_tokens is None

            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await hook.before_dispatch({"provider": "deepseek"})
            assert excinfo.value.code == ledger.CODE_LEDGER_UNSETTLED
            assert excinfo.value.http_status == ledger.HTTP_CONFLICT

    run(scenario())


@pytest.mark.parametrize(
    "usage_body, expected_status, expected_source",
    [
        pytest.param(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
            ledger.USAGE_STATUS_COMPLETE,
            ledger.USAGE_SOURCE_PROMPT_COMPLETION,
            id="complete",
        ),
        pytest.param(
            {"usage": {"total_tokens": 30}},
            ledger.USAGE_STATUS_PARTIAL,
            ledger.USAGE_SOURCE_TOTAL,
            id="partial",
        ),
    ],
)
def test_e2e_postprocess_failure_keeps_settled_usage_and_budget(
    monkeypatch, usage_body, expected_status, expected_source
):
    """A-3 端到端：usage 已落定 -> 后处理失败 -> 账目保持 complete/partial 且按已消费计入预算。"""
    drain_ai_context()
    body = {
        "id": "chatcmpl-a3-offline",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {}}],
    }
    body.update(usage_body)
    captured = patch_provider(monkeypatch, body=body)

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(
                engine, max_retries=1, cost_budget=30, idempotency_key="b2-a3-e2e"
            )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await ai_task_service.run_ai_task(task.id, factory)

            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            row = rows[0]
            assert row.usage_status == expected_status, "已结算账目不得被后处理失败改判"
            assert row.usage_source == expected_source
            assert row.total_tokens == 30
            assert row.settled_at is not None

            fresh = (
                await session.execute(select(AITask).where(AITask.id == task.id))
            ).scalar_one()
            assert fresh.status == "failed"
            assert len(captured["calls"]) == 1, (
                "后处理失败后的重试必须被闸门拦住，不得再次调用供应商"
            )

            decision = await ledger.evaluate_budget(session, fresh)
            assert decision.allowed is False
            assert decision.basis == 30
            assert decision.basis_source == "ledger"
            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_budget_allowed(session, fresh)
            assert excinfo.value.code == ledger.CODE_BUDGET_EXCEEDED

    run(scenario())


# --- A-5：解析异常不得遗留永久 pending，且后续派发被阻断 ------------------

@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: json.JSONDecodeError("Expecting value", "<html>502</html>", 0),
            id="json-decode-error",
        ),
        pytest.param(
            lambda: ValueError("响应体解析失败：非法 JSON 文本"),
            id="plain-value-error",
        ),
        pytest.param(
            lambda: json.JSONDecodeError("Unterminated string", '{"usage": ', 10),
            id="truncated-json",
        ),
    ],
)
def test_e2e_non_json_success_response_settles_unverifiable_and_blocks_redispatch(
    monkeypatch, error_factory
):
    """A-5：2xx 但 JSON 解析失败 -> 结清 unverifiable（不留 pending），后续派发 409 且不再调供应商。"""
    drain_ai_context()
    import httpx

    captured = patch_provider(monkeypatch, body=completion_body())
    error = error_factory()

    async def broken_post(self, url, headers=None, json=None, **kwargs):  # noqa: A002
        captured["calls"].append({"url": url, "payload": json})
        return _NonJsonSuccessResponse(error)

    monkeypatch.setattr(httpx.AsyncClient, "post", broken_post)

    async def scenario():
        async with memory_db() as (engine, session):
            task = await _insert_task(engine, max_retries=1, idempotency_key="b2-a5-e2e")
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await ai_task_service.run_ai_task(task.id, factory)

            rows = await ledger.ledger_rows(session, task.id)
            assert len(rows) == 1
            row = rows[0]
            assert row.dispatched_at is not None
            assert row.usage_status == ledger.USAGE_STATUS_UNVERIFIABLE
            assert row.usage_source == ledger.USAGE_SOURCE_MISSING
            assert row.settled_at is not None, "已下发行不得永久 pending（A-5）"
            assert row.total_tokens is None and row.prompt_tokens is None

            fresh = (
                await session.execute(select(AITask).where(AITask.id == task.id))
            ).scalar_one()
            assert fresh.status == "failed"

            with pytest.raises(ledger.UsageLedgerError) as excinfo:
                await ledger.assert_dispatch_allowed(session, fresh)
            assert excinfo.value.code == ledger.CODE_LEDGER_UNSETTLED
            assert excinfo.value.http_status == ledger.HTTP_CONFLICT

            assert len(captured["calls"]) == 1, "阻断后不得再次调用供应商"

    run(scenario())


# --- A-4：生产启动路径接入 phase22 --------------------------------------

def test_init_schema_startup_path_reaches_phase22_ensure(tmp_path, monkeypatch, caplog):
    """A-4：main.create_tables（生产启动入口）必须可达 ensure_phase22_schema。

    用隔离临时库跑两次真实启动路径（不触碰正式库）：
      1) 首启（空库，migrate -> initialized）：初始化回调内接入 phase22（增补对象）；
      2) 次启（已有 schema_migrations，migrate -> noop）：只做只读兼容检查，
         但仍报告异常枚举值（不被静默吞掉），且不改写既有数据。
    """
    import sqlite3

    from app import main as main_module

    calls: list[bool] = []
    real_ensure = mig.ensure_phase22_schema

    async def spy_ensure(conn, *, repair: bool = True):
        calls.append(repair)
        return await real_ensure(conn, repair=repair)

    async def _noop_recover():
        """启动恢复任务走全局会话工厂（指向正式库），隔离测试内替换为空实现。"""

    monkeypatch.setattr(main_module, "ensure_phase22_schema", spy_ensure, raising=False)
    monkeypatch.setattr(main_module, "_recover_stale_ai_tasks", _noop_recover, raising=False)

    db_path = tmp_path / "startup_gate.sqlite3"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", db_url, raising=False)
    isolated_engine = create_async_engine(db_url)
    monkeypatch.setattr(main_module, "engine", isolated_engine, raising=False)

    def _table_names():
        with sqlite3.connect(db_path) as conn:
            return {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    try:
        run(main_module.create_tables())
        assert calls and all(calls), "首启所有 phase22 调用必须是增补模式"
        assert "ai_task_token_usage" in _table_names()
        assert "schema_migrations" in _table_names()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ai_task_token_usage "
                "(task_id, attempt_no, call_no, usage_status, usage_source, created_at) "
                "VALUES ('a4taskid', 1, 1, 'not_a_status', 'not_a_source', "
                "'2026-10-19 00:00:00')"
            )
            conn.commit()

        calls.clear()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.core.db_migrations"):
            run(main_module.create_tables())
        assert calls == [False], "noop 启动路径只做只读兼容检查（不执行 DDL）"
        assert "not_a_status" in caplog.text or "not_a_source" in caplog.text, (
            "异常枚举值必须在启动兼容检查中被报告"
        )
        with sqlite3.connect(db_path) as conn:
            kept = conn.execute(
                "SELECT usage_status, usage_source FROM ai_task_token_usage "
                "WHERE task_id = 'a4taskid'"
            ).fetchone()
        assert kept == ("not_a_status", "not_a_source"), "只读检查不得改写既有异常值"
        assert "ai_task_token_usage" in _table_names()
    finally:
        run(isolated_engine.dispose())


def test_ensure_phase22_repair_false_is_read_only_and_repair_true_backfills():
    """A-4：repair=False 只做只读兼容检查（不执行 DDL）；repair=True 才增补缺失对象。"""
    async def scenario():
        async with memory_db() as (engine, _session):
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE ai_task_token_usage"))

                def _exists_sql():
                    return text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name = 'ai_task_token_usage'"
                    )

                report = await mig.ensure_phase22_schema(conn, repair=False)
                assert report.get("error"), "缺对象时只读检查必须报告异常，不得静默"
                assert (await conn.execute(_exists_sql())).first() is None, (
                    "只读检查不得执行任何 DDL"
                )

                report = await mig.ensure_phase22_schema(conn, repair=True)
                assert not report.get("error")
                assert report["rows"] == 0
                assert (await conn.execute(_exists_sql())).first() is not None

    run(scenario())


# --- C-2~C-5：同进程并行任务的 ContextVar 隔离 ---------------------------

def test_usage_hook_contextvar_is_isolated_between_parallel_tasks(tmp_path):
    """C-2~C-5：同进程并行任务各自安装账目钩子 -> ContextVar 隔离，互不串账。

    生产取钩子路径是 ai_service._call_usage_hook(stage, **payload) 经 ContextVar 解析；
    这里让两个并发任务交错安装后再触发同一 stage，断言各自只写各自库 / 各自 task。
    """
    drain_ai_context()

    async def worker(name, engine, key):
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            task = await _insert_task(engine, idempotency_key=key)
            hook = ledger.install_usage_ledger_hook(session, task)
            try:
                await asyncio.sleep(0)
                await ai_service._call_usage_hook("before_dispatch", provider="deepseek")
                await asyncio.sleep(0)
                rows = await ledger.ledger_rows(session, task.id)
            finally:
                ledger.uninstall_usage_ledger_hook()
            return name, task.id, hook, rows

    async def scenario():
        import app.models  # noqa: F401
        import app.models.chapter  # noqa: F401

        engines = {
            name: create_async_engine(
                f"sqlite+aiosqlite:///{(tmp_path / ('iso_' + name + '.sqlite3')).as_posix()}"
            )
            for name in ("a", "b")
        }
        try:
            for engine in engines.values():
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            results = await asyncio.gather(
                *(worker(name, engine, "iso-" + name) for name, engine in engines.items())
            )
        finally:
            for engine in engines.values():
                await engine.dispose()

        assert len(results) == 2
        for name, task_id, hook, rows in results:
            assert len(rows) == 1, f"任务 {name} 的钩子只能写本任务一行账目"
            assert rows[0].task_id == task_id
            assert rows[0].usage_status == ledger.USAGE_STATUS_PENDING
            assert rows[0].dispatched_at is not None
            assert hook.task.id == task_id
        assert results[0][2] is not results[1][2], "两个并发任务的钩子实例不得相互复用"

    run(scenario())
