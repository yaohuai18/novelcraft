# -*- coding: utf-8 -*-
"""阶段 1 回归测试：DeepSeek 模型配置抽象与任务分流（单一 resolver）。

Coverage（阶段 1 验收）：
  1. test_policy_table_complete_and_consistent：
     策略表与 TaskType 枚举一一对应；模型仅取实测模型；思考任务无温度、强制 effort。
  2. test_default_task_matrix：
     任务分流矩阵：正文/通用 v4-pro 非思考（0.9/0.7）；大纲/审校/事实抽取/修复候选
     v4-pro 思考 high；逻辑审校 思考 max；摘要/章节记忆/分类 flash 非思考。
  3. test_legacy_alias_mapped_not_dispatched：
     旧别名 deepseek-chat / deepseek-reasoner 只做兼容映射，真实请求体 model 绝不为旧名。
  4. test_payload_keys_closed_and_no_legacy_dispatch：
     请求体字段封闭（白名单），全任务 × 全 override 组合下 model ∈ {deepseek-flash, deepseek-v4-pro}。
  5. test_thinking_task_drops_temperature：
     思考任务不下发 temperature（原 0.2 差异化被 thinking 取代），并记录 dropped_temperature。
  6. test_unsupported_model_name_ignored：未知 model_name 被忽略并回退任务策略。
  7. test_unknown_task_type_falls_back_to_config：未知 / 空任务类型走配置兜底，仍为实测模型。
  8. test_config_fallback_maps_legacy_alias：环境变量配置旧别名时兜底模型被映射为实测模型。
  9. test_invalid_effort_and_temperature_normalized：非法 effort 归一 high；越界温度回退策略值。
 10. test_request_body_end_to_end：拦截 httpx 请求体，端到端核对真实下发字段（离线、不写库）。
 11. test_legacy_names_confined_to_resolver：旧模型名字面量只允许出现在 model_policy.py。
 12. test_config_payload_exposes_policy_and_aliases：/ai/config 可只读核对策略表与映射表。
 13. test_blank_task_type_logs_warning：None / 空串 / 全空白 task_type 记 WARNING 级安全诊断
     （含 fallback 来源与规范化模型，不含任何敏感内容），且回退行为不变。
 14. test_summary_isolated_and_outline_plan_not_flash：摘要独立为 summary（flash 非思考）；
     真正的 outline_plan / 章节记忆 / 分类不得被误改为 Flash（策略 + 源码级守卫）。

（阶段 1 冻结期：本文件自 backend/temp/ 迁入 backend/tests/，纳入正式可追踪回归；
   用例内容与源脚本等价，仅调整文件位置与运行路径，测试逻辑未改动。）

Run: cd backend && python -m pytest tests/test_phase1_model_policy.py -q
"""
import asyncio
import ast
import json
import logging
import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.schemas.ai_novel import AiConfigResponse  # noqa: E402
from app.services import ai_service  # noqa: E402
from app.services import model_policy as mp  # noqa: E402
from app.services.ai_service import (  # noqa: E402
    build_ai_config_payload,
    chat_with_ai,
    chat_with_ai_novel,
    take_last_ai_policy,
)

LEGACY_NAMES = ("deepseek-chat", "deepseek-reasoner")

APP_DIR = os.path.join(BACKEND, "app")
# 旧模型名字面量允许出现的位置：唯一别名维护点 + 环境变量默认值（配置回退层）
SOURCE_SCAN_SKIP = {
    os.path.abspath(os.path.join(APP_DIR, "services", "model_policy.py")),
    os.path.abspath(os.path.join(APP_DIR, "core", "config.py")),
}

PAYLOAD_KEYS = {
    "model",
    "messages",
    "max_tokens",
    "temperature",
    "thinking",
    "reasoning_effort",
    "response_format",
}

# 任务分流矩阵：(task_type, 预期模型, 是否思考, 预期 effort, 预期温度)
TASK_MATRIX = [
    ("novel_body", mp.MODEL_V4_PRO, False, None, 0.9),
    ("general", mp.MODEL_V4_PRO, False, None, 0.7),
    ("outline_plan", mp.MODEL_V4_PRO, True, "high", None),
    ("scene_contract", mp.MODEL_V4_PRO, True, "high", None),
    ("timeline_analysis", mp.MODEL_V4_PRO, True, "high", None),
    ("fact_extraction", mp.MODEL_V4_PRO, True, "high", None),
    ("consistency_review", mp.MODEL_V4_PRO, True, "high", None),
    ("logic_review", mp.MODEL_V4_PRO, True, "max", None),
    ("repair_candidate", mp.MODEL_V4_PRO, True, "high", None),
    ("summary", mp.MODEL_FLASH, False, None, 0.3),
    ("chapter_memory", mp.MODEL_FLASH, False, None, 0.7),
    ("classification", mp.MODEL_FLASH, False, None, 0.2),
]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    """最小 httpx 响应替身（不发网络，不触碰任何数据库）。"""

    def __init__(self, data: dict, status_code: int = 200, headers: dict | None = None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def text(self) -> str:
        return json.dumps(self._data, ensure_ascii=False)

    def json(self) -> dict:
        return self._data


def _effective_ai_config() -> tuple[str, str, str]:
    """复现生产解析优先级 DEEPSEEK_* > AI_* > OPENAI_*。

    注：部分历史 smoke 测试会在模块级永久替换 ai_service.get_effective_ai_config，
    这里显式注入等价实现，使本测试独立于跨模块全局替换，同时保持真实解析语义。
    """
    key = settings.deepseek_api_key or settings.ai_api_key or settings.openai_api_key or ""
    base = (
        settings.deepseek_base_url or settings.ai_base_url or settings.openai_base_url or ""
    ).rstrip("/")
    model = settings.deepseek_model or settings.ai_model or settings.openai_model or ""
    return key, base, model


def _patch_provider(monkeypatch) -> dict:
    """注入假密钥并拦截 httpx 请求，返回 captured 容器（离线）。"""
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-phase1-only")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(ai_service, "get_effective_ai_config", _effective_ai_config)
    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None, **kwargs):  # noqa: A002
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "阶段1测试内容"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            }
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


async def _call_and_policy(task_type, *, override=None, system="系统提示", user="用户提示 json"):
    text = await chat_with_ai(system, user, override=override, task_type=task_type)
    return text, take_last_ai_policy()


# ---------------------------------------------------------------------------
# 1-2：策略表一致性与任务分流矩阵
# ---------------------------------------------------------------------------

def test_policy_table_complete_and_consistent():
    """策略表：TaskType 全覆盖、模型仅实测模型、思考/温度/effort 语义自洽。"""
    assert mp.SUPPORTED_MODELS == (mp.MODEL_FLASH, mp.MODEL_V4_PRO)
    assert mp.MODEL_V4_PRO == "deepseek-v4-pro"
    assert mp.MODEL_FLASH == "deepseek-flash"

    for task in mp.TaskType:
        assert task.value in mp.TASK_POLICIES, f"任务类型 {task.value} 未在策略表登记"
        policy = mp.TASK_POLICIES[task.value]
        assert policy.model in mp.SUPPORTED_MODELS
        if policy.thinking:
            assert policy.temperature is None, f"{task.value} 思考任务不应带温度"
            assert policy.reasoning_effort in mp.ALLOWED_REASONING_EFFORTS
        else:
            assert policy.reasoning_effort is None
            assert policy.temperature is not None and 0 <= policy.temperature <= 2

    for alias, item in mp.LEGACY_MODEL_ALIASES.items():
        assert alias in LEGACY_NAMES
        assert item.model in mp.SUPPORTED_MODELS
        if item.thinking:
            assert item.reasoning_effort in mp.ALLOWED_REASONING_EFFORTS


@pytest.mark.parametrize("task_type,model,thinking,effort,temperature", TASK_MATRIX)
def test_default_task_matrix(task_type, model, thinking, effort, temperature):
    """任务分流矩阵：默认（无 override）下模型 / thinking / effort / 温度逐项核对。"""
    resolved = mp.resolve_model_policy(task_type=task_type)
    assert resolved.model == model
    assert resolved.thinking is thinking
    assert resolved.reasoning_effort == effort
    assert resolved.temperature == temperature
    assert resolved.model_source == "task_policy"
    assert resolved.model not in LEGACY_NAMES


# ---------------------------------------------------------------------------
# 3-4：旧别名兼容映射与请求体封闭
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alias,expect_model,expect_thinking,expect_effort",
    [
        ("deepseek-chat", mp.MODEL_FLASH, False, None),
        ("  DeepSeek-Chat  ", mp.MODEL_FLASH, False, None),
        ("deepseek-reasoner", mp.MODEL_V4_PRO, True, "high"),
        ("  DeepSeek-Reasoner  ", mp.MODEL_V4_PRO, True, "high"),
    ],
)
def test_legacy_alias_mapped_not_dispatched(alias, expect_model, expect_thinking, expect_effort):
    """旧别名只作兼容映射：真实请求体 model 为实测模型，绝不下发旧名。"""
    resolved = mp.resolve_model_policy(task_type="novel_body", override={"model_name": alias})
    assert resolved.model == expect_model
    assert resolved.thinking is expect_thinking
    assert resolved.reasoning_effort == expect_effort
    assert resolved.model_source == "legacy_alias"
    assert any("兼容映射" in note for note in resolved.notes)

    payload = mp.build_chat_payload(resolved, "系统", "用户")
    assert payload["model"] == expect_model
    assert payload["model"] not in LEGACY_NAMES
    assert json.dumps(payload, ensure_ascii=False).count("deepseek-chat") == 0
    assert json.dumps(payload, ensure_ascii=False).count("deepseek-reasoner") == 0


def test_payload_keys_closed_and_no_legacy_dispatch():
    """请求体字段封闭 + 全任务 × 全 override 组合下模型恒为实测模型。"""
    overrides = [
        None,
        {"model_name": "deepseek-chat"},
        {"model_name": "deepseek-reasoner"},
        {"model_name": "deepseek-v3"},
        {"model_name": "gpt-4o"},
        {"model_name": "deepseek-v4-pro"},
        {"model_name": "deepseek-flash"},
    ]
    for task in mp.TaskType:
        for override in overrides:
            resolved = mp.resolve_model_policy(task_type=task.value, override=override)
            payload = mp.build_chat_payload(resolved, "系统", "输出 json")
            assert set(payload) <= PAYLOAD_KEYS, f"{task.value} 请求体出现非白名单字段"
            assert payload["model"] in mp.SUPPORTED_MODELS
            assert payload["thinking"]["type"] in ("enabled", "disabled")
            if resolved.thinking:
                assert "temperature" not in payload
                assert payload["reasoning_effort"] in mp.ALLOWED_REASONING_EFFORTS
            else:
                assert "reasoning_effort" not in payload
                assert "temperature" in payload


# ---------------------------------------------------------------------------
# 5-9：override 受策略约束
# ---------------------------------------------------------------------------

def test_thinking_task_drops_temperature():
    """审校（思考任务）不再下发 temperature 0.2，并记录丢弃值以便排查。"""
    resolved = mp.resolve_model_policy(
        task_type="consistency_review", override={"temperature": 0.2}
    )
    assert resolved.thinking is True
    assert resolved.temperature is None
    assert resolved.dropped_temperature == 0.2
    assert any("temperature" in note for note in resolved.notes)
    payload = mp.build_chat_payload(resolved, "系统", "审校")
    assert "temperature" not in payload
    meta = mp.policy_metadata(resolved)
    assert meta["dropped_temperature"] == 0.2
    assert meta["thinking"] == "enabled"


def test_unsupported_model_name_ignored():
    """未知 model_name 被忽略，回退任务策略（不做静默透传）。"""
    resolved = mp.resolve_model_policy(task_type="novel_body", override={"model_name": "gpt-4o"})
    assert resolved.model_source == "task_policy"
    assert resolved.model == mp.MODEL_V4_PRO
    assert any("忽略" in note for note in resolved.notes)


def test_unknown_task_type_falls_back_to_config():
    """未知 / 空任务类型走配置兜底，仍为实测模型（不回落旧名）。"""
    resolved = mp.resolve_model_policy(task_type="no_such_task_type")
    assert resolved.task_type is None
    assert resolved.model_source == "config_fallback"
    assert resolved.model in mp.SUPPORTED_MODELS
    assert resolved.thinking is False
    assert resolved.temperature == mp.DEFAULT_TEMPERATURE

    blank = mp.resolve_model_policy(task_type="   ")
    assert blank.model_source == "config_fallback"
    assert blank.model in mp.SUPPORTED_MODELS

    # 业务别名兼容（旧调用方传 continue / review 等语义别名）
    assert mp.normalize_task_type("continue") == "novel_body"
    assert mp.normalize_task_type("review") == "consistency_review"
    assert mp.normalize_task_type("generate_summary") == "summary"
    assert mp.normalize_task_type("summary") == "summary"
    assert mp.normalize_task_type(mp.TaskType.SUMMARY) == "summary"
    assert mp.normalize_task_type(mp.TaskType.CHAPTER_MEMORY) == "chapter_memory"


def test_config_fallback_maps_legacy_alias(monkeypatch):
    """环境变量配了旧别名时，兜底模型同样被映射为实测模型（旧名不下发）。"""
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-reasoner")
    monkeypatch.setattr(settings, "ai_model", "")
    monkeypatch.setattr(settings, "openai_model", "")
    resolved = mp.resolve_model_policy(task_type=None)
    assert resolved.model_source == "config_fallback"
    assert resolved.model == mp.MODEL_V4_PRO
    assert resolved.thinking is True
    assert resolved.reasoning_effort == "high"
    payload = mp.build_chat_payload(resolved, "系统", "用户")
    assert payload["model"] == mp.MODEL_V4_PRO
    assert "deepseek-reasoner" not in json.dumps(payload)


def test_invalid_effort_and_temperature_normalized(monkeypatch):
    """非法 effort 归一为 high；越界温度被忽略并回退策略温度。"""
    bad_policy = mp.TaskPolicy(
        task_type="general",
        model=mp.MODEL_V4_PRO,
        thinking=True,
        temperature=None,
        reasoning_effort="ultra",
    )
    monkeypatch.setitem(mp.TASK_POLICIES, "general", bad_policy)
    resolved = mp.resolve_model_policy(task_type="general")
    assert resolved.reasoning_effort == "high"
    assert any("归一" in note for note in resolved.notes)

    too_hot = mp.resolve_model_policy(task_type="novel_body", override={"temperature": 5})
    assert too_hot.temperature == 0.9
    negative = mp.resolve_model_policy(task_type="novel_body", override={"temperature": -1})
    assert negative.temperature == 0.9
    text_value = mp.resolve_model_policy(task_type="novel_body", override={"temperature": "abc"})
    assert text_value.temperature == 0.9


def test_max_tokens_priority_and_defaults():
    """输出预算优先级：override.max_tokens > 显式参数 > 业务默认 > 策略 > 全局。"""
    a = mp.resolve_model_policy(task_type="novel_body", override={"max_tokens": 1234})
    assert a.max_tokens == 1234
    b = mp.resolve_model_policy(task_type="novel_body", max_tokens=2048)
    assert b.max_tokens == 2048
    c = mp.resolve_model_policy(task_type="novel_body", default_max_tokens=3000)
    assert c.max_tokens == 3000
    d = mp.resolve_model_policy(task_type="novel_body")
    assert d.max_tokens > 0


# ---------------------------------------------------------------------------
# 10：端到端请求体（拦截 httpx，不联网、不写库）
# ---------------------------------------------------------------------------

def test_request_body_end_to_end(monkeypatch):
    """端到端核对真实下发请求体：正文 / 审校 / 章节记忆 / 摘要 / 旧别名 五条链路。"""
    captured = _patch_provider(monkeypatch)

    # ① 正文：v4-pro 非思考 + 温度 0.9
    text, meta = _run(_call_and_policy("novel_body", user="续写本章正文"))
    assert text == "阶段1测试内容"
    payload = captured["payload"]
    assert payload["model"] == mp.MODEL_V4_PRO
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert payload["temperature"] == 0.9
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert meta["model"] == mp.MODEL_V4_PRO
    assert meta["task_type"] == "novel_body"
    assert meta["thinking"] == "disabled"

    # ② 审校：v4-pro 思考 high，无 temperature，无 response_format
    text, meta = _run(
        _call_and_policy("consistency_review", override={"temperature": 0.2}, user="审校本章")
    )
    payload = captured["payload"]
    assert payload["model"] == mp.MODEL_V4_PRO
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "response_format" not in payload
    assert meta["dropped_temperature"] == 0.2

    # ③ 章节记忆：flash 非思考 + JSON 输出
    text, meta = _run(_call_and_policy("chapter_memory", user="请输出 json 摘要"))
    payload = captured["payload"]
    assert payload["model"] == mp.MODEL_FLASH
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.7

    # ⑤ 摘要（独立 summary）：flash 非思考 + 自然语言（不下发 response_format / effort）
    text, meta = _run(_call_and_policy("summary", user="请用自然语言输出故事概要"))
    payload = captured["payload"]
    assert text == "阶段1测试内容"
    assert payload["model"] == mp.MODEL_FLASH
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert "response_format" not in payload
    assert payload["temperature"] == 0.3
    assert meta["task_type"] == "summary"
    assert meta["model"] == mp.MODEL_FLASH
    assert meta["thinking"] == "disabled"

    # ④ 旧别名 override：映射为 v4-pro 思考，请求体不含旧名
    text, meta = _run(
        _call_and_policy("novel_body", override={"model_name": "deepseek-reasoner"}, user="续写")
    )
    payload = captured["payload"]
    assert payload["model"] == mp.MODEL_V4_PRO
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "deepseek-reasoner" not in json.dumps(payload, ensure_ascii=False)
    assert meta["model_source"] == "legacy_alias"


def test_chat_entry_points_declare_task_type():
    """两个 chat 入口均支持 task_type，且小说生成入口默认真实任务类型。"""
    import inspect

    assert "task_type" in inspect.signature(chat_with_ai).parameters
    novel_sig = inspect.signature(chat_with_ai_novel)
    assert "task_type" in novel_sig.parameters
    assert novel_sig.parameters["task_type"].default == mp.TaskType.NOVEL_BODY.value


# ---------------------------------------------------------------------------
# 11-12：规则集中性与只读诊断
# ---------------------------------------------------------------------------

def test_legacy_names_confined_to_resolver():
    """旧模型名字面量只允许出现在 model_policy.py（唯一别名维护点）。"""
    offenders: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(APP_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            if os.path.abspath(path) in SOURCE_SCAN_SKIP:
                continue
            content = open(path, encoding="utf-8").read()
            found = [name for name in LEGACY_NAMES if name in content]
            if found:
                offenders[os.path.relpath(path, BACKEND)] = found
    assert offenders == {}, f"旧模型名字面量散落在：{offenders}"

    # 唯一维护点必须保留映射，且不得触碰数据库（按 AST 校验 import，避免误伤说明文字）
    resolver_path = os.path.join(APP_DIR, "services", "model_policy.py")
    source = open(resolver_path, encoding="utf-8").read()
    for name in LEGACY_NAMES:
        assert name in source

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden_prefixes = ("sqlalchemy", "app.models", "app.core.database", "app.services.generation_service")
    for module in sorted(imported):
        assert not module.startswith(forbidden_prefixes), f"resolver 不应导入 {module}"


def test_config_payload_exposes_policy_and_aliases():
    """/ai/config 返回内容可只读核对：兜底模型为实测模型 + 策略表 + 旧名映射表。"""
    payload = build_ai_config_payload()
    assert payload["model"] in mp.SUPPORTED_MODELS
    assert payload["thinking"] in ("enabled", "disabled")
    assert payload["temperature_note"]
    assert len(payload["task_policies"]) == len(mp.TASK_POLICIES)
    aliases = payload["legacy_model_aliases"]
    assert set(aliases) == set(LEGACY_NAMES)
    for item in aliases.values():
        assert item["model"] in mp.SUPPORTED_MODELS
        assert item["thinking"] in ("enabled", "disabled")

    response = AiConfigResponse(**payload)
    assert response.model in mp.SUPPORTED_MODELS
    assert response.task_policies
    assert response.legacy_model_aliases
    assert response.model not in LEGACY_NAMES


# ---------------------------------------------------------------------------
# 13-14：缺失 task_type 的安全诊断 + 摘要独立性与 outline_plan 守卫
# ---------------------------------------------------------------------------

def test_blank_task_type_logs_warning(caplog):
    """None / 空串 / 全空白 task_type：回退行为不变，同时记 WARNING 级安全诊断。"""
    cases = [None, "", "   ", "\t\n "]
    with caplog.at_level(logging.WARNING, logger="app.services.model_policy"):
        for case in cases:
            caplog.clear()
            assert mp.normalize_task_type(case) is None
            records = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert records, f"task_type={case!r} 未产生 WARNING 级诊断日志"
            text = "\n".join(r.getMessage() for r in records)
            assert "[task_type-missing]" in text
            assert "fallback_source=config_fallback" in text
            assert "规范化模型=" in text
            assert mp.MODEL_FLASH in text or mp.MODEL_V4_PRO in text
            # 安全红线：不得出现密钥 / 鉴权 / 提示词 / 正文 / reasoning 等敏感内容
            for forbidden in ("sk-", "Authorization", "Bearer", "prompt", "reasoning_content"):
                assert forbidden not in text

    # 回退行为不变：仍走配置兜底，且不回落旧模型名
    blank_resolved = mp.resolve_model_policy(task_type="   ")
    assert blank_resolved.model_source == "config_fallback"
    assert blank_resolved.model in mp.SUPPORTED_MODELS
    assert blank_resolved.task_type is None

    # 未知非空 task_type 保留既有 warning 行为（文案区分，不串用缺失诊断）
    with caplog.at_level(logging.WARNING, logger="app.services.model_policy"):
        caplog.clear()
        assert mp.normalize_task_type("no_such_task_type") is None
        unknown_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "未知任务类型" in unknown_text
    assert "[task_type-missing]" not in unknown_text


def test_summary_isolated_and_outline_plan_not_flash():
    """摘要独立为 summary（flash 非思考 / 无 effort / 无 json_object）；大纲与记忆类不得被误改。"""
    # ① summary：flash + thinking disabled + 无 effort + 自然语言（即使提示词提到 json 也不下发）
    summary = mp.resolve_model_policy(task_type=mp.TaskType.SUMMARY.value)
    assert summary.model == mp.MODEL_FLASH
    assert summary.thinking is False
    assert summary.reasoning_effort is None
    assert summary.temperature == 0.3
    payload = mp.build_chat_payload(summary, "系统提示", "请输出 json 格式的故事概要")
    assert payload["model"] == mp.MODEL_FLASH
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert "response_format" not in payload

    # ② 真正的大纲 / 分卷 / 分章仍是 v4-pro 思考 high，不下发 temperature
    for task_type in ("outline_plan", "generate_volumes", "generate_chapters"):
        outline = mp.resolve_model_policy(task_type=task_type)
        assert outline.model == mp.MODEL_V4_PRO, f"{task_type} 不得被改成 Flash"
        assert outline.thinking is True
        assert outline.reasoning_effort == "high"
        outline_payload = mp.build_chat_payload(outline, "系统", "生成结构化大纲 JSON")
        assert outline_payload["thinking"] == {"type": "enabled"}
        assert "temperature" not in outline_payload

    # ③ 章节记忆 / 分类不被误改（保持 flash + JSON 输出）
    for task_type, temperature in (("chapter_memory", 0.7), ("classification", 0.2)):
        memory = mp.resolve_model_policy(task_type=task_type)
        assert memory.model == mp.MODEL_FLASH
        assert memory.temperature == temperature
        assert mp.build_chat_payload(memory, "系统", "json")["response_format"] == {
            "type": "json_object"
        }
    assert mp.normalize_task_type("summarize") == "chapter_memory"
    assert mp.normalize_task_type("memory") == "chapter_memory"

    # ④ 源码级守卫：只有真实摘要调用点声明 summary；大纲链路仍声明 outline_plan
    router_src = open(
        os.path.join(APP_DIR, "routers", "ai_generation.py"), encoding="utf-8"
    ).read()
    assert "task_type=TaskType.SUMMARY.value" in router_src
    assert router_src.count("TaskType.SUMMARY.value") == 1
    assert "task_type: str | None = TaskType.OUTLINE_PLAN.value" in router_src

    task_service_src = open(
        os.path.join(APP_DIR, "services", "ai_task_service.py"), encoding="utf-8"
    ).read()
    assert "TaskType.OUTLINE_PLAN.value" in task_service_src
    assert "TaskType.SUMMARY" not in task_service_src
