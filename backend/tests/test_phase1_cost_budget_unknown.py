# -*- coding: utf-8 -*-
"""阶段 1 冻结期 ·「cost_budget 遇到未知 usage」最小安全修复 正式回归测试（离线）。

被测对象（app/services/ai_task_service.py 预算守卫，本阶段唯一被修改的生产文件）
  - _known_token_floor
  - _budget_exceeded_before_run / _budget_exceeded_after_run
  - _budget_exceeded_message
  - run_ai_task 的预算预检 / 后检两条链路

口径底线（对应踩坑：NULL 被 `(input or 0) + (output or 0)` 折叠为 0）
  1. input/output_tokens 为 NULL = 「供应商未返回真实 usage，用量未知」，**绝不得折算为 0**；
  2. 未知用量不得被判成「零消耗」而静默放行且无痕迹 → 必须有 warning 可观测；
  3. 未知用量不得误杀任务（保守放行，不改任务终态：succeeded/failed 与修复前一致）；
  4. 已知真实 token 的判定行为与修复前逐位一致（含 `>=` 预检、`>` 后检边界）；
  5. 守卫只用已知真实值构成「下界」，不写字符数、不写估算值进 token 字段；
  6. 端到端（隔离内存库 + mock 供应商）：无 usage 的调用落账后 token 仍为 NULL 且任务
     succeeded；真实 usage 超预算仍照旧 failed。

运行：cd backend && python -m pytest tests/test_phase1_cost_budget_unknown.py -q
"""
from __future__ import annotations

import ast
import inspect
import logging
import textwrap
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.ai_task import AITask
from app.services import ai_task_service as svc

from _phase1_helpers import completion_body, drain_ai_context, memory_db, patch_provider, run

GUARD_LOGGER = "app.services.ai_task_service"


# ---------------------------------------------------------------------------
# 夹具：构造游离（不入库）的 AITask，供守卫纯函数用例使用
# ---------------------------------------------------------------------------

def _task(*, cost_budget=None, input_tokens=None, output_tokens=None) -> AITask:
    """游离 AITask：只用于预算守卫判定，不涉及任何数据库/网络。"""
    return AITask(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_type="legacy_dialog",  # 走通用兜底执行器
        status="pending",
        prompt="阶段1离线用例",
        cost_budget=cost_budget,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _insert_task(engine, **kwargs) -> uuid.UUID:
    """把 pending 任务写入隔离内存库，返回 id。"""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        task = _task(**kwargs)
        db.add(task)
        await db.commit()
        return task.id


# ---------------------------------------------------------------------------
# 1. 未知 usage 不得被折叠为 0（核心修复点）
# ---------------------------------------------------------------------------

def test_unknown_usage_is_not_folded_to_zero_and_does_not_kill_task():
    """两侧都未知：下界 0 + has_unknown=True；不误杀任务（放行，不改终态）。"""
    task = _task(cost_budget=1, input_tokens=None, output_tokens=None)
    assert svc._known_token_floor(task) == (0, True)
    assert svc._budget_exceeded_before_run(task) is False
    assert svc._budget_exceeded_after_run(task) is False


def _code_only(fn) -> str:
    """取函数可执行代码（剥离 docstring，避免注释/文档里的历史缺陷描述干扰断言）。"""
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return ast.unparse(node)


def test_guard_source_has_no_or_zero_folding():
    """源码级防回归：守卫代码不得再出现 `or 0` / 字符数折算。"""
    for fn in (
        svc._budget_exceeded_before_run,
        svc._budget_exceeded_after_run,
        svc._known_token_floor,
    ):
        code = _code_only(fn)
        assert "or 0" not in code, f"{fn.__name__} 仍把 NULL 折叠为 0"
        assert "// 2" not in code, f"{fn.__name__} 出现字符数折半折算"
        assert "len(" not in code, f"{fn.__name__} 出现按字符数估算 token"
    assert "is not None" in _code_only(svc._known_token_floor), "下界只允许累加「已取得的真实值」"


def test_known_usage_boundaries_unchanged():
    """已知真实 usage：判定边界与修复前完全一致（预检 >=，后检 >）。"""
    # 预检：达到预算即失败
    assert svc._budget_exceeded_before_run(
        _task(cost_budget=15, input_tokens=10, output_tokens=5)
    ) is True
    assert svc._budget_exceeded_before_run(
        _task(cost_budget=15, input_tokens=10, output_tokens=4)
    ) is False
    # 后检：严格超过预算才失败
    assert svc._budget_exceeded_after_run(
        _task(cost_budget=15, input_tokens=10, output_tokens=5)
    ) is False
    assert svc._budget_exceeded_after_run(
        _task(cost_budget=15, input_tokens=10, output_tokens=6)
    ) is True


def test_null_budget_never_blocks():
    """cost_budget 为 NULL = 默认关闭，任何用量都不判定超限。"""
    task = _task(cost_budget=None, input_tokens=10**9, output_tokens=10**9)
    assert svc._budget_exceeded_before_run(task) is False
    assert svc._budget_exceeded_after_run(task) is False


def test_partial_unknown_uses_known_lower_bound_only():
    """单侧未知：只用已知真实值做下界，既不高估（不误杀）也不吞掉已确定超限。"""
    # 下界已达预算 -> 确定超限，照常阻断
    assert svc._budget_exceeded_before_run(
        _task(cost_budget=100, input_tokens=100, output_tokens=None)
    ) is True
    # 下界未达预算 -> 放行（未知侧不参与比较，也不折算为 0 去「补差价」）
    assert svc._budget_exceeded_before_run(
        _task(cost_budget=100, input_tokens=50, output_tokens=None)
    ) is False
    assert svc._budget_exceeded_after_run(
        _task(cost_budget=100, input_tokens=100, output_tokens=None)
    ) is False, "后检为严格大于，恰好等于预算不应失败"


# ---------------------------------------------------------------------------
# 2. 可观测性：未知用量必须留痕（不落库、不改 schema）
# ---------------------------------------------------------------------------

def test_unknown_usage_writes_warning(caplog):
    """存在未知侧时写 warning（含未知侧与判定依据）；全已知时不产生该 warning。"""
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        assert svc._budget_exceeded_before_run(_task(cost_budget=1000)) is False
        assert "未知 token 用量" in caplog.text
        assert "执行前" in caplog.text
        assert "cost_budget=1000" in caplog.text

        caplog.clear()
        assert svc._budget_exceeded_after_run(
            _task(cost_budget=1000, input_tokens=1, output_tokens=None)
        ) is False
        assert "未知 token 用量" in caplog.text
        assert "执行后" in caplog.text

        caplog.clear()
        assert svc._budget_exceeded_after_run(
            _task(cost_budget=1000, input_tokens=1, output_tokens=1)
        ) is False
        assert "未知 token 用量" not in caplog.text, "全已知用量不应产生未知告警"


def test_message_never_renders_unknown_as_zero():
    """超限提示：未知侧显示「未知」（不得显示 0）；全已知时保持修复前原文。"""
    msg = svc._budget_exceeded_message(_task(cost_budget=5))
    assert "未知" in msg
    assert "cost_budget=5" in msg
    assert "=0 + 0" not in msg, "未知不得被渲染成零消耗"

    known = svc._budget_exceeded_message(
        _task(cost_budget=5, input_tokens=10, output_tokens=5)
    )
    assert known == "任务成本预算已超限（cost_budget=5，累计 token=10 + 5），已直接失败"


# ---------------------------------------------------------------------------
# 3. 端到端（隔离内存库 + mock 供应商，绝不出网、绝不触碰 novelcraft.db）
# ---------------------------------------------------------------------------

def test_run_ai_task_unknown_usage_keeps_tokens_null_and_succeeds(monkeypatch):
    """无 usage 响应：任务照旧成功，token 落 NULL（不得回填字符数/估算值）。"""
    drain_ai_context()
    body_text = "正文" * 500
    patch_provider(monkeypatch, body=completion_body(content=body_text, usage=False))

    async def scenario():
        async with memory_db() as (engine, db):
            task_id = await _insert_task(engine, cost_budget=7)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await svc.run_ai_task(task_id, factory)

            row = (
                await db.execute(select(AITask).where(AITask.id == task_id))
            ).scalar_one()
            assert row.status == "succeeded", row.error_message
            assert row.input_tokens is None
            assert row.output_tokens is None, "未知用量不得被字符数折算填充"
            assert row.result == body_text
            await db.rollback()

    run(scenario())


def test_run_ai_task_real_usage_over_budget_still_fails(monkeypatch):
    """真实 usage 超预算：仍照旧 failed，且落真实 token、提示含预算与非零用量。"""
    drain_ai_context()
    patch_provider(
        monkeypatch,
        body=completion_body(content="超预算正文", prompt_tokens=100, completion_tokens=50),
    )

    async def scenario():
        async with memory_db() as (engine, db):
            task_id = await _insert_task(engine, cost_budget=10)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await svc.run_ai_task(task_id, factory)

            row = (
                await db.execute(select(AITask).where(AITask.id == task_id))
            ).scalar_one()
            assert row.status == "failed"
            assert (row.input_tokens, row.output_tokens) == (100, 50)
            assert "cost_budget=10" in row.error_message
            assert "累计 token=100 + 50" in row.error_message
            await db.rollback()

    run(scenario())


def test_run_ai_task_precheck_blocks_before_calling_provider(monkeypatch, caplog):
    """预检链路：已确定超限（已知下界达预算）时不调用供应商，直接 failed 并留痕。"""
    drain_ai_context()
    captured = patch_provider(monkeypatch)

    async def scenario():
        async with memory_db() as (engine, db):
            task_id = await _insert_task(
                engine, cost_budget=10, input_tokens=100, output_tokens=None
            )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
                await svc.run_ai_task(task_id, factory)

            row = (
                await db.execute(select(AITask).where(AITask.id == task_id))
            ).scalar_one()
            assert row.status == "failed"
            assert "cost_budget=10" in row.error_message
            await db.rollback()

    run(scenario())
    assert captured["calls"] == [], "预检失败的任务不得调用 AI 供应商"
    assert "未知 token 用量" in caplog.text
