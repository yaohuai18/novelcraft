# -*- coding: utf-8 -*-
"""阶段 1 · 续写链路回归测试（离线、无网络、无正式数据库）。

覆盖：阶段 1 token 修复对「续写链路」的可运行性影响 —— 修复后的 generation_service /
ai_novel_generation 依赖 ai_service 的上下文取用接口（take_last_ai_usage /
take_last_ai_meta），该依赖若缺失会在真实续写时直接 NameError/ImportError。

被测点：
  1. take_last_ai_meta 三字段（finish_reason / prompt_tokens / completion_tokens）
     取值与「取一次即清空」语义；无 usage 时为 (None, None, None)；
  2. take_last_ai_usage 同为「取一次即清空」，未知时 (None, None)；
  3. 续写结果只取 message.content，思考模型返回的 reasoning_content 不得混入正文；
  4. 关键模块 import 链完整（证明工作区修复不存在未提交依赖导致的导入失败）。

运行：cd backend && python -m pytest tests/test_phase1_continuation_regression.py -q
"""
from __future__ import annotations

import importlib
import inspect

from app.schemas.ai_novel import NovelGenerateRequest
from app.services import ai_service, generation_service

from _phase1_helpers import completion_body, drain_ai_context, patch_provider, run


# ---------------------------------------------------------------------------
# 1. 上下文取用接口：三字段 + 取一次即清空
# ---------------------------------------------------------------------------

def test_take_last_ai_meta_roundtrip_and_reset():
    """真实 usage：三字段一次取回，再取为空（不得跨次复用，避免张冠李戴）。"""
    drain_ai_context()
    ai_service._set_last_ai_usage(completion_body(prompt_tokens=1200, completion_tokens=800))
    assert ai_service.take_last_ai_meta() == ("stop", 1200, 800)
    assert ai_service.take_last_ai_meta() == (None, None, None)


def test_take_last_ai_usage_roundtrip_and_reset():
    drain_ai_context()
    ai_service._set_last_ai_usage(completion_body(prompt_tokens=7, completion_tokens=9))
    assert ai_service.take_last_ai_usage() == (7, 9)
    assert ai_service.take_last_ai_usage() == (None, None)


def test_take_last_ai_meta_without_usage_is_null():
    """无供应商 usage：finish_reason 仍真实，token 两字段为 NULL（不得估算）。"""
    drain_ai_context()
    ai_service._set_last_ai_usage(completion_body(usage=False))
    assert ai_service.take_last_ai_meta() == ("stop", None, None)


# ---------------------------------------------------------------------------
# 2. 续写正文：只取 message.content
# ---------------------------------------------------------------------------

def test_continuation_content_excludes_reasoning_content(monkeypatch):
    """思考模型返回 reasoning_content 时，正文只取 content（思考内容不得混入）。"""
    drain_ai_context()
    patch_provider(
        monkeypatch,
        body=completion_body(
            content="第1章：风声渐起\n正文开始。",
            reasoning_content="【思考过程】先规划情节冲突，再落笔正文……",
        ),
    )

    async def scenario():
        setting = NovelGenerateRequest(title="阶段1离线回归", words_per_chapter=800, chapter_count=1)
        text = await ai_service.chat_with_ai_novel(
            setting, 1, 1, previous_chapter="上一章结尾摘要（离线构造，非真实正文）"
        )
        assert text == "第1章：风声渐起\n正文开始。"
        assert "思考过程" not in text
        # 真实 usage 已被记录，供后续 record_generation 落库
        assert ai_service.take_last_ai_meta() == ("stop", 11, 22)

    run(scenario())


# ---------------------------------------------------------------------------
# 3. 依赖链完整性（修复后的工作区必须自洽可导入）
# ---------------------------------------------------------------------------

def test_repair_dependency_chain_importable():
    """generation_service / 续写路由 / 任务服务均可正常导入且接口齐备。"""
    assert hasattr(ai_service, "take_last_ai_meta")
    assert "take_last_ai_meta" in inspect.getsource(generation_service)

    router = importlib.import_module("app.routers.ai_novel_generation")
    assert "take_last_ai_meta" in inspect.getsource(router)

    task_service = importlib.import_module("app.services.ai_task_service")
    assert hasattr(task_service, "_budget_exceeded_before_run")
    assert hasattr(task_service, "_budget_exceeded_after_run")
