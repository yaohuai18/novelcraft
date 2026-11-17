# -*- coding: utf-8 -*-
"""阶段 1 · token 口径修复 正式回归测试（离线、无网络、无正式数据库）。

被测对象（阶段 1 冻结基线：工作区修复后代码）
  - app/services/ai_stats_service.py   estimate_cost / record_usage / get_ai_usage
  - app/schemas/stats_schemas.py        AIUsageRecordCreate
  - app/services/generation_service.py  record_generation
  - app/models/generation.py            GenerationRecord（token 字段可空性）
  - app/models/ai_usage_daily_stat.py   AIUsageDailyStat

覆盖的口径底线（对应踩坑记录「字符数 // 2 伪装写入 token 字段」）：
  1. 未知 usage（None）→ 成本贡献 0，禁止用字符数折算替代；
  2. 日聚合 token 列落 0 只代表「未取得真实 token」，请求数/成本口径不变；
  3. 已知真实 usage 正常累计，未知调用不污染既有累计值；
  4. 读取侧显式标注 token_data_note + cost_billable=False（历史数据不可计费）；
  5. 上报 schema 允许 input/output_tokens 为 None（int|None, ge=0，向后兼容）；
  6. record_generation：无真实 usage → 四个 token 字段全 NULL，字符数只进
     raw_content_length / final_content_length；有真实 usage → 落真实值；
  7. 源码级防回归：修复后的关键函数不得再出现 "// 2" 或按 len() 折算 token。

运行：cd backend && python -m pytest tests/test_phase1_token_accounting.py -q
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.generation import GenerationRecord
from app.schemas.stats_schemas import AIUsageRecordCreate
from app.services import ai_service, ai_stats_service, generation_service

from _phase1_helpers import completion_body, drain_ai_context, memory_db, patch_provider, run


# ---------------------------------------------------------------------------
# 1. 成本折算：未知 token 贡献 0，字符数绝不折算成 token
# ---------------------------------------------------------------------------

def test_estimate_cost_unknown_tokens_contribute_zero():
    """未知（None）按 0 计成本；已知侧照旧（单价与计费规则本轮不变）。"""
    assert ai_stats_service.estimate_cost(None, None) == 0.0
    assert ai_stats_service.estimate_cost(0, 0) == 0.0
    assert ai_stats_service.estimate_cost(None, 1000) == 0.0002
    assert ai_stats_service.estimate_cost(1000, None) == 0.0001
    assert ai_stats_service.estimate_cost(1000, 1000) == 0.0003


def test_cost_and_token_source_has_no_char_fabrication():
    """源码级防回归：关键函数不得再出现字符数折算（"// 2" / len(...)）。"""
    est_src = inspect.getsource(ai_stats_service.estimate_cost)
    assert "len(" not in est_src
    assert "// 2" not in est_src

    usage_src = inspect.getsource(ai_stats_service.record_usage)
    assert "// 2" not in usage_src
    assert "len(" not in usage_src

    gen_src = inspect.getsource(generation_service.record_generation)
    assert "// 2" not in gen_src


def test_generation_record_token_columns_are_nullable():
    """token 字段必须允许 NULL（"未知"是合法语义）；字符数字段独立存在。"""
    cols = GenerationRecord.__table__.c
    for name in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
        assert cols[name].nullable is True, f"{name} 必须可空，否则无法表达「未知」"
    for name in ("raw_content_length", "final_content_length"):
        assert name in cols, f"{name} 必须独立存在，字符数不得挤占 token 字段"


# ---------------------------------------------------------------------------
# 2. 日聚合落库：未知 usage 不伪造 token
# ---------------------------------------------------------------------------

def test_record_usage_unknown_usage_writes_zero_not_characters():
    """未知 usage：聚合 token 列落 0、成本 0，请求数照常 +1。"""

    async def scenario():
        async with memory_db() as (engine, db):
            uid, pid = uuid.uuid4(), uuid.uuid4()
            stat = await ai_stats_service.record_usage(
                db,
                user_id=uid,
                project_id=pid,
                provider="deepseek",
                model="deepseek-v4-pro",
                task_type="novel_body",
                success=True,
                input_tokens=None,
                output_tokens=None,
            )
            assert stat.input_tokens == 0
            assert stat.output_tokens == 0
            assert stat.estimated_cost == 0.0
            assert stat.request_count == 1
            assert stat.success_count == 1
            # 若沿用旧实现把字符数（如 8000/12000）折半写入，这里会是 4000/6000
            assert stat.input_tokens != 8000 // 2
            assert stat.output_tokens != 12000 // 2
            await db.rollback()

    run(scenario())


def test_record_usage_known_usage_accumulates_and_unknown_does_not_pollute():
    """已知真实 usage 正常累计；随后一次未知调用不得重置或污染累计值。"""

    async def scenario():
        async with memory_db() as (engine, db):
            uid, pid = uuid.uuid4(), uuid.uuid4()
            common = dict(
                user_id=uid,
                project_id=pid,
                provider="deepseek",
                model="deepseek-v4-pro",
                task_type="novel_body",
                success=True,
            )
            await ai_stats_service.record_usage(db, **common, input_tokens=1000, output_tokens=500)
            stat = await ai_stats_service.record_usage(db, **common, input_tokens=2000, output_tokens=1000)
            assert (stat.input_tokens, stat.output_tokens) == (3000, 1500)
            assert stat.estimated_cost == pytest.approx(0.0006, abs=1e-9)

            stat = await ai_stats_service.record_usage(db, **common, input_tokens=None, output_tokens=None)
            assert stat.request_count == 3
            assert (stat.input_tokens, stat.output_tokens) == (3000, 1500), "未知调用不得改写累计 token"
            assert stat.estimated_cost == pytest.approx(0.0006, abs=1e-9)
            await db.rollback()

    run(scenario())


def test_get_ai_usage_marks_tokens_not_billable():
    """读取侧标注：token_data_note 说明口径 + cost_billable=False。"""

    async def scenario():
        async with memory_db() as (engine, db):
            uid, pid = uuid.uuid4(), uuid.uuid4()
            await ai_stats_service.record_usage(
                db,
                user_id=uid,
                project_id=pid,
                provider="deepseek",
                model="deepseek-v4-pro",
                task_type="novel_body",
                success=True,
                input_tokens=None,
                output_tokens=None,
            )
            data = await ai_stats_service.get_ai_usage(db, project_id=pid, user_id=uid, group_by="model")
            assert data["cost_billable"] is False
            note = data["token_data_note"]
            assert "不再用字符数折算" in note
            assert "无法区分" in note
            assert data["total"]["request_count"] == 1
            assert data["total"]["input_tokens"] == 0
            assert data["total"]["output_tokens"] == 0
            assert data["total"]["estimated_cost"] == 0.0
            assert data["groups"], "按 model 分组应至少有一组"
            assert data["groups"][0]["input_tokens"] == 0
            await db.rollback()

    run(scenario())


# ---------------------------------------------------------------------------
# 3. 上报 schema：未知 token 可在 API 层表达
# ---------------------------------------------------------------------------

def test_ai_usage_record_create_accepts_null_tokens():
    """input/output_tokens 为 int|None：缺省与显式 None 均合法，负数仍被拒。"""
    base = {
        "user_id": uuid.uuid4(),
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "task_type": "novel_body",
    }
    obj = AIUsageRecordCreate(**base)
    assert obj.input_tokens is None and obj.output_tokens is None

    obj2 = AIUsageRecordCreate(**base, input_tokens=None, output_tokens=10)
    assert obj2.input_tokens is None and obj2.output_tokens == 10

    with pytest.raises(ValidationError):
        AIUsageRecordCreate(**base, input_tokens=-1)


# ---------------------------------------------------------------------------
# 4. 落库端到端：真实 usage 落真实值；无 usage 落 NULL 且字符数走独立字段
# ---------------------------------------------------------------------------

def test_record_generation_with_real_usage_writes_real_tokens(monkeypatch):
    """有供应商真实 usage：token 四字段 + finish_reason + 提示词均落真实值。"""
    drain_ai_context()
    patch_provider(
        monkeypatch,
        body=completion_body(content="第1章：续写正文", prompt_tokens=1234, completion_tokens=567),
    )

    async def scenario():
        async with memory_db() as (engine, db):
            await ai_service.chat_with_ai("系统提示", "用户提示", task_type="novel_body")
            record = await generation_service.record_generation(
                db,
                attribution_status="pre_project",
                action_type="continue",
                response_content="第1章：续写正文",
                raw_content_length=8,
                final_content_length=8,
                status="completed",
            )
            assert record is not None
            row = (
                await db.execute(select(GenerationRecord).where(GenerationRecord.id == record.id))
            ).scalar_one()
            assert (row.input_tokens, row.output_tokens) == (1234, 567)
            assert (row.prompt_tokens, row.completion_tokens) == (1234, 567)
            assert row.finish_reason == "stop"
            assert row.system_prompt == "系统提示"
            assert row.user_prompt == "用户提示"
            assert row.raw_content_length == 8 and row.final_content_length == 8
            await db.rollback()

    run(scenario())


def test_record_generation_without_usage_keeps_tokens_null(monkeypatch):
    """无真实 usage：四个 token 字段全 NULL，字符数只进独立字段（不折半伪装）。"""
    drain_ai_context()
    long_system = "系" * 4000
    long_user = "用" * 3000
    body_text = "正文" * 2000
    patch_provider(monkeypatch, body=completion_body(content=body_text, usage=False))

    async def scenario():
        async with memory_db() as (engine, db):
            await ai_service.chat_with_ai(long_system, long_user, task_type="novel_body")
            record = await generation_service.record_generation(
                db,
                attribution_status="pre_project",
                action_type="continue",
                response_content=body_text,
                raw_content_length=4000,
                final_content_length=4000,
                status="completed",
            )
            assert record is not None
            row = (
                await db.execute(select(GenerationRecord).where(GenerationRecord.id == record.id))
            ).scalar_one()
            fabricated = (len(long_system) + len(body_text)) // 2
            assert fabricated > 0, "构造数据必须能触发旧实现的折半估算"
            assert row.input_tokens is None
            assert row.output_tokens is None
            assert row.prompt_tokens is None
            assert row.completion_tokens is None
            assert row.raw_content_length == 4000
            assert row.final_content_length == 4000
            await db.rollback()

    run(scenario())
