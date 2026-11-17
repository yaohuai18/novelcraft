# -*- coding: utf-8 -*-
"""阶段 1：DeepSeek 模型策略集中解析器（服务端唯一模型策略事实来源）。

职责边界（对应任务书 A~E 节）：

1. ``TASK_POLICIES`` 是全服务端唯一维护「任务类型 → 真实请求模型 / thinking /
   reasoning_effort / temperature / response_format / max_tokens」的映射表；
2. ``LEGACY_MODEL_ALIASES`` 是唯一维护旧模型名兼容映射的地方，旧名
   （deepseek-chat / deepseek-reasoner）只做映射，绝不作为真实请求模型下发；
3. ``resolve_model_policy`` 是唯一把「调用方 override + 旧模型名 + 配置兜底」
   归一化为最终请求参数（模型 + thinking + effort + temperature + max_tokens +
   response_format）的入口；
4. ``build_chat_payload`` 是唯一构造 Chat Completions 请求体的地方，保证请求体
   字段封闭（只允许 model / messages / max_tokens / temperature / thinking /
   reasoning_effort / response_format），禁止供应商私有字段透传。

硬约束：

- 新任务只允许实测通过的 ``deepseek-flash`` / ``deepseek-v4-pro``；
- ``thinking.enabled`` 时不发送 ``temperature``（调用方传入的将被丢弃并记 INFO 日志，
  不做静默改写）；
- ``reasoning_effort`` 仅允许 low / high / max，非法值归一为 high 并记 WARNING；
- 本模块不读写数据库、不修改历史 generation_records、不触碰章节正文。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 一、实测通过的模型与参数常量（阶段 0 事实基线）
# ---------------------------------------------------------------------------

# 快模型：非思考、支持 JSON 输出（摘要 / 分类等轻任务）
MODEL_FLASH = "deepseek-flash"
# 强模型：正文非思考生成；审校 / 复检 / 事实抽取 / 时间线等深度任务以思考模式 + effort
MODEL_V4_PRO = "deepseek-v4-pro"

SUPPORTED_MODELS: tuple[str, ...] = (MODEL_FLASH, MODEL_V4_PRO)

THINKING_DISABLED: dict[str, str] = {"type": "disabled"}
THINKING_ENABLED: dict[str, str] = {"type": "enabled"}

ALLOWED_REASONING_EFFORTS: tuple[str, ...] = ("low", "high", "max")
DEFAULT_REASONING_EFFORT = "high"

DEFAULT_TEMPERATURE = 0.7
RESPONSE_FORMAT_JSON = "json_object"


# ---------------------------------------------------------------------------
# 二、任务类型与策略表（唯一维护点）
# ---------------------------------------------------------------------------

class TaskType(str, Enum):
    """阶段 1 任务类型枚举；所有新调用点必须显式声明任务类型。"""

    NOVEL_BODY = "novel_body"
    GENERAL = "general"
    OUTLINE_PLAN = "outline_plan"
    SCENE_CONTRACT = "scene_contract"
    TIMELINE_ANALYSIS = "timeline_analysis"
    FACT_EXTRACTION = "fact_extraction"
    CONSISTENCY_REVIEW = "consistency_review"
    LOGIC_REVIEW = "logic_review"
    REPAIR_CANDIDATE = "repair_candidate"
    CHAPTER_MEMORY = "chapter_memory"
    # 面向用户的自然语言摘要（故事概要 / 内容摘要），与章节记忆、分类区分
    SUMMARY = "summary"
    CLASSIFICATION = "classification"


# 旧调用方 / 业务语义别名 → 任务类型（兼容历史调用，避免遗漏分流）
_TASK_TYPE_ALIASES: dict[str, str] = {
    # 正文类
    "continue": TaskType.NOVEL_BODY.value,
    "rewrite": TaskType.NOVEL_BODY.value,
    "regenerate": TaskType.NOVEL_BODY.value,
    "expand": TaskType.NOVEL_BODY.value,
    "polish": TaskType.NOVEL_BODY.value,
    "next_chapter": TaskType.NOVEL_BODY.value,
    "first_chapter": TaskType.NOVEL_BODY.value,
    "chapter_body": TaskType.NOVEL_BODY.value,
    "generate_novel": TaskType.NOVEL_BODY.value,
    # 大纲 / 规划类
    "outline": TaskType.OUTLINE_PLAN.value,
    "outline_plan": TaskType.OUTLINE_PLAN.value,
    "plan": TaskType.OUTLINE_PLAN.value,
    "plan_novel": TaskType.OUTLINE_PLAN.value,
    "plan_update": TaskType.OUTLINE_PLAN.value,
    # 摘要：独立 summary 任务类型（见 TaskType.SUMMARY），不再借用 outline_plan
    "generate_summary": TaskType.SUMMARY.value,
    "generate_volumes": TaskType.OUTLINE_PLAN.value,
    "generate_chapters": TaskType.OUTLINE_PLAN.value,
    # 目与场景
    "scene": TaskType.SCENE_CONTRACT.value,
    "scene_contract": TaskType.SCENE_CONTRACT.value,
    # 时间线
    "timeline": TaskType.TIMELINE_ANALYSIS.value,
    "timeline_analysis": TaskType.TIMELINE_ANALYSIS.value,
    # 事实 / 状态抽取
    "facts": TaskType.FACT_EXTRACTION.value,
    "fact": TaskType.FACT_EXTRACTION.value,
    "state_extraction": TaskType.FACT_EXTRACTION.value,
    # 审校 / 复检 / 修复
    "review": TaskType.CONSISTENCY_REVIEW.value,
    "consistency": TaskType.CONSISTENCY_REVIEW.value,
    "consistency_check": TaskType.CONSISTENCY_REVIEW.value,
    "consistency_review": TaskType.CONSISTENCY_REVIEW.value,
    "logic_review": TaskType.LOGIC_REVIEW.value,
    "repair": TaskType.REPAIR_CANDIDATE.value,
    "repair_candidate": TaskType.REPAIR_CANDIDATE.value,
    "suggestions": TaskType.REPAIR_CANDIDATE.value,
    "generate_suggestions": TaskType.REPAIR_CANDIDATE.value,
    # 章节记忆（注意：摘要 summary 已是独立任务类型 TaskType.SUMMARY，不在此映射）
    "summarize": TaskType.CHAPTER_MEMORY.value,
    "chapter_summary": TaskType.CHAPTER_MEMORY.value,
    "memory": TaskType.CHAPTER_MEMORY.value,
    # 分类
    "classify": TaskType.CLASSIFICATION.value,
    "classification": TaskType.CLASSIFICATION.value,
}


@dataclass(frozen=True)
class TaskPolicy:
    """某一任务类型的静态策略（全服务端唯一维护点）。

    thinking=True 的任务必须给出 reasoning_effort，且 temperature 应为 None
    （思考模式不发送温度）。
    """

    task_type: str
    model: str
    thinking: bool
    temperature: float | None
    reasoning_effort: str | None = None
    json_output: bool = False
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    description: str = ""


def _thinking_task(
    task_type: str,
    *,
    effort: str = DEFAULT_REASONING_EFFORT,
    description: str = "",
    timeout_seconds: int | None = None,
) -> TaskPolicy:
    """构造「deepseek-v4-pro 思考」任务策略（请求体下发 thinking + reasoning_effort）。"""
    return TaskPolicy(
        task_type=task_type,
        model=MODEL_V4_PRO,
        thinking=True,
        temperature=None,
        reasoning_effort=effort,
        json_output=False,
        timeout_seconds=timeout_seconds,
        description=description,
    )


def _flash_task(
    task_type: str,
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    json_output: bool = False,
    description: str = "",
    max_tokens: int | None = None,
) -> TaskPolicy:
    """构造「deepseek-flash 非思考」任务策略。"""
    return TaskPolicy(
        task_type=task_type,
        model=MODEL_FLASH,
        thinking=False,
        temperature=temperature,
        reasoning_effort=None,
        json_output=json_output,
        max_tokens=max_tokens,
        description=description,
    )


# 任务类型 → 策略（唯一映射表；新增任务类型必须在此登记）
TASK_POLICIES: dict[str, TaskPolicy] = {
    # 正文：非思考，保留原 0.9 温度语义
    TaskType.NOVEL_BODY.value: TaskPolicy(
        task_type=TaskType.NOVEL_BODY.value,
        model=MODEL_V4_PRO,
        thinking=False,
        reasoning_effort=None,
        temperature=0.9,
        json_output=False,
        description="章节正文（续写/重写/扩写/润色），非思考，温度 0.9",
    ),
    # 通用对话：非思考，保留原 0.7 温度语义
    TaskType.GENERAL.value: TaskPolicy(
        task_type=TaskType.GENERAL.value,
        model=MODEL_V4_PRO,
        thinking=False,
        reasoning_effort=None,
        temperature=0.7,
        json_output=False,
        description="通用问答 / 兜底对话，非思考，温度 0.7",
    ),
    # 大纲与规划：思考 high
    TaskType.OUTLINE_PLAN.value: _thinking_task(
        TaskType.OUTLINE_PLAN.value,
        description="全书大纲 / 分卷 / 分章规划与局部修改，思考 high",
    ),
    # 场景契约：思考 high
    TaskType.SCENE_CONTRACT.value: _thinking_task(
        TaskType.SCENE_CONTRACT.value,
        description="场景契约生成与校验，思考 high",
    ),
    # 时间线分析：思考 high
    TaskType.TIMELINE_ANALYSIS.value: _thinking_task(
        TaskType.TIMELINE_ANALYSIS.value,
        description="时间线一致性分析，思考 high",
    ),
    # 事实 / 状态抽取：思考 high
    TaskType.FACT_EXTRACTION.value: _thinking_task(
        TaskType.FACT_EXTRACTION.value,
        description="章节事实与人物状态抽取，思考 high",
    ),
    # 连续性审校：思考 high（原 temperature 0.2 被本层拦截，不再下发）
    TaskType.CONSISTENCY_REVIEW.value: _thinking_task(
        TaskType.CONSISTENCY_REVIEW.value,
        description="独立 LLM 连续性审校 / 复检，思考 high",
    ),
    # 逻辑审校：思考 max
    TaskType.LOGIC_REVIEW.value: _thinking_task(
        TaskType.LOGIC_REVIEW.value,
        effort="max",
        description="深度逻辑审校，思考 max",
    ),
    # 修复补丁候选：思考 high
    TaskType.REPAIR_CANDIDATE.value: _thinking_task(
        TaskType.REPAIR_CANDIDATE.value,
        description="修复补丁候选 / 修改建议生成，思考 high",
    ),
    # 章节摘要与记忆：flash，非思考，可 JSON
    TaskType.CHAPTER_MEMORY.value: _flash_task(
        TaskType.CHAPTER_MEMORY.value,
        temperature=0.7,
        json_output=True,
        description="章节摘要与结构化记忆提取，flash 非思考 + JSON",
    ),
    # 面向用户的摘要：flash，非思考，自然语言（不发送 response_format / reasoning_effort）
    TaskType.SUMMARY.value: _flash_task(
        TaskType.SUMMARY.value,
        temperature=0.3,
        json_output=False,
        description="面向用户的自然语言摘要（故事概要 / 内容摘要），flash 非思考，温度 0.3，不下发 json_object",
    ),
    # 分类 / 打标：flash，非思考，可 JSON
    TaskType.CLASSIFICATION.value: _flash_task(
        TaskType.CLASSIFICATION.value,
        temperature=0.2,
        json_output=True,
        description="分类 / 打标等轻任务，flash 非思考 + JSON",
    ),
}


# ---------------------------------------------------------------------------
# 三、旧模型名别名兼容映射（唯一维护点）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LegacyAlias:
    """旧模型名 → 新模型 + thinking 语义的兼容映射条目。"""

    alias: str
    model: str
    thinking: bool
    reasoning_effort: str | None
    note: str = ""


LEGACY_MODEL_ALIASES: dict[str, LegacyAlias] = {
    # deepseek-chat：旧的非思考通用模型 → deepseek-flash（非思考、可 JSON）
    "deepseek-chat": LegacyAlias(
        alias="deepseek-chat",
        model=MODEL_FLASH,
        thinking=False,
        reasoning_effort=None,
        note="旧通用对话模型，映射为 flash 非思考",
    ),
    # deepseek-reasoner：旧的思考模型 → deepseek-v4-pro 思考 high
    "deepseek-reasoner": LegacyAlias(
        alias="deepseek-reasoner",
        model=MODEL_V4_PRO,
        thinking=True,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        note="旧推理模型，映射为 v4-pro 思考 high",
    ),
}


# 具备思考能力的模型名集合（含实测模型与历史/兼容名），仅用于「能力识别」与
# 配置诊断展示；旧名一律不作为真实请求模型下发（真实请求模型只取自
# SUPPORTED_MODELS）。集中在本模块维护，避免旧模型名字面量散落各处。
REASONING_MODEL_NAMES: frozenset[str] = frozenset(
    {
        MODEL_V4_PRO,
        "deepseek-r1",
        "o1",
        "o1-mini",
        "o3",
        "o3-mini",
        *LEGACY_MODEL_ALIASES.keys(),
    }
)


# ---------------------------------------------------------------------------
# 四、解析结果与诊断
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedModelPolicy:
    """一次请求最终生效的模型策略（resolve_model_policy 的输出）。"""

    task_type: str | None
    model: str
    thinking: bool
    reasoning_effort: str | None
    temperature: float | None
    max_tokens: int
    response_format: str | None
    timeout_seconds: int
    model_source: str
    requested_model: str | None = None
    dropped_temperature: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def thinking_type(self) -> str:
        """thinking 的 type 取值（enabled / disabled）。"""
        return "enabled" if self.thinking else "disabled"

    @property
    def thinking_param(self) -> dict[str, str]:
        """thinking 请求参数（thinking disabled 时同样显式下发，语义明确）。"""
        return dict(THINKING_ENABLED if self.thinking else THINKING_DISABLED)


def _missing_task_type_warning(task_type: str | None) -> str:
    """缺失 task_type 的安全诊断文案。

    仅包含调用上下文标识、task_type 缺失形态、fallback 来源与规范化模型；
    严禁包含 API Key / Authorization / 完整 prompt / 正文 / 完整响应 / reasoning_content。
    """
    shape = "None" if task_type is None else f"{type(task_type).__name__}={task_type!r}"
    fallback_model, fallback_thinking, _ = _config_fallback_model()
    return (
        "model_policy: [task_type-missing] 调用方未声明 task_type（%s），已保持兼容回退；"
        "fallback_source=config_fallback（DEEPSEEK_MODEL > AI_MODEL > OPENAI_MODEL > 内置默认 flash），"
        "规范化模型=%s，thinking=%s"
        % (shape, fallback_model, "enabled" if fallback_thinking else "disabled")
    )


def normalize_task_type(task_type: str | None) -> str | None:
    """归一化任务类型：支持 TaskType 值、枚举实例与业务别名；未知值返回 None。

    阶段 1 最小补正：``None`` / 空串 / 全空白不再静默退化，统一记 WARNING 级安全
    诊断日志（回退行为不变），便于定位漏声明 task_type 的调用点。
    """
    if task_type is None:
        logger.warning(_missing_task_type_warning(None))
        return None
    if isinstance(task_type, TaskType):
        return task_type.value
    key = str(task_type).strip()
    if not key:
        logger.warning(_missing_task_type_warning(task_type))
        return None
    key = key.lower()
    if key in TASK_POLICIES:
        return key
    mapped = _TASK_TYPE_ALIASES.get(key)
    if mapped and mapped in TASK_POLICIES:
        return mapped
    logger.warning("model_policy: 未知任务类型 %r，已退化为无任务策略（配置兜底模型）", task_type)
    return None


def get_task_policy(task_type: str | None) -> TaskPolicy | None:
    """按任务类型取静态策略；未登记任务类型返回 None。"""
    normalized = normalize_task_type(task_type)
    if normalized is None:
        return None
    return TASK_POLICIES.get(normalized)


def classify_model_name(model_name: str | None) -> tuple[str, str] | None:
    """把任意模型名归类为 (真实请求模型, 来源)。未识别返回 None。

    来源取值：legacy_alias / supported。
    """
    if not model_name:
        return None
    key = str(model_name).strip().lower()
    alias = LEGACY_MODEL_ALIASES.get(key)
    if alias is not None:
        return alias.model, "legacy_alias"
    if key in SUPPORTED_MODELS:
        return key, "supported"
    return None


def _config_fallback_model() -> tuple[str, bool, str]:
    """配置兜底模型：DEEPSEEK_MODEL > AI_MODEL > OPENAI_MODEL > 内置默认。

    旧别名同样会被映射；未识别的配置值不下发，回退默认 flash（非思考）。
    """
    raw = (
        settings.deepseek_model
        or settings.ai_model
        or settings.openai_model
        or ""
    )
    raw = str(raw).strip()
    if raw:
        classified = classify_model_name(raw)
        if classified is not None:
            model, source = classified
            thinking = bool(LEGACY_MODEL_ALIASES[raw.lower()].thinking) if source == "legacy_alias" else False
            note = f"配置模型 {raw!r}（{source}）归一为 {model}"
            return model, thinking, note
        logger.warning(
            "model_policy: 配置模型 %r 既非实测模型也非已知旧别名，已回退默认模型 %s",
            raw,
            MODEL_FLASH,
        )
        return MODEL_FLASH, False, f"配置模型 {raw!r} 不受支持，已回退默认模型 {MODEL_FLASH}"
    default_model = str(getattr(settings, "default_task_model", MODEL_FLASH) or MODEL_FLASH).strip()
    classified = classify_model_name(default_model)
    if classified is not None:
        default_model = classified[0]
    else:
        default_model = MODEL_FLASH
    return default_model, False, f"未配置模型，使用默认模型 {default_model}"


def _coerce_temperature(value: Any) -> float | None:
    """温度归一：None / 非法 / 越界（<0 或 >2）一律返回 None 并记日志。"""
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning("model_policy: temperature=%r 非法（布尔），已忽略", value)
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        logger.warning("model_policy: temperature=%r 无法解析为数值，已忽略", value)
        return None
    if temperature < 0 or temperature > 2:
        logger.warning("model_policy: temperature=%s 超出 0~2 合理区间，已忽略", temperature)
        return None
    return temperature


def _coerce_positive_int(value: Any) -> int | None:
    """正整数归一：非法 / <=0 返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_effort(effort: str | None, task_type: str | None, model: str) -> tuple[str | None, str | None]:
    """reasoning_effort 归一：仅允许 low / high / max；非法值归一为 high。"""
    if not effort:
        return None, None
    key = str(effort).strip().lower()
    if key in ALLOWED_REASONING_EFFORTS:
        return key, None
    logger.warning(
        "model_policy: 非法 reasoning_effort=%r（task=%s model=%s），已归一为 %s",
        effort,
        task_type,
        model,
        DEFAULT_REASONING_EFFORT,
    )
    return DEFAULT_REASONING_EFFORT, f"非法 reasoning_effort={effort!r}，已归一为 {DEFAULT_REASONING_EFFORT}"


def resolve_model_policy(
    *,
    task_type: str | None = None,
    override: dict | None = None,
    max_tokens: int | None = None,
    default_max_tokens: int | None = None,
    json_output: bool = False,
    timeout_seconds: int | None = None,
) -> ResolvedModelPolicy:
    """把调用方入参归一化为最终请求策略（唯一解析入口）。

    参数：
      task_type         任务类型（TaskType 值或业务别名）
      override          调用方 override，只识别 model_name / temperature / max_tokens
      max_tokens        显式输出预算
      default_max_tokens 业务默认输出预算（如正文按目标字数换算），优先级低于显式参数
      json_output       调用方是否要求 JSON 输出（仅非思考任务下发 response_format）
      timeout_seconds   显式超时（秒）
    """
    ov = dict(override or {})
    task = normalize_task_type(task_type)
    policy = TASK_POLICIES.get(task) if task else None
    notes: list[str] = []

    requested_raw = ov.get("model_name")
    requested = str(requested_raw).strip() if requested_raw is not None else ""

    model: str | None = None
    thinking: bool | None = None
    effort: str | None = None
    model_source = "task_policy"

    # 1) 调用方显式指定的模型名：先查旧别名，再查实测模型，其余一律忽略
    if requested:
        alias = LEGACY_MODEL_ALIASES.get(requested.lower())
        if alias is not None:
            model = alias.model
            thinking = alias.thinking
            effort = alias.reasoning_effort
            model_source = "legacy_alias"
            notes.append(f"旧模型名 {requested!r} 兼容映射为 {model}（{alias.note}）")
            logger.info(
                "model_policy: 旧模型名兼容映射 task=%s requested=%s -> model=%s thinking=%s effort=%s",
                task,
                requested,
                model,
                thinking,
                effort,
            )
        elif requested.lower() in SUPPORTED_MODELS:
            model = requested.lower()
            model_source = "request_override"
            if policy is not None:
                thinking = policy.thinking
                effort = policy.reasoning_effort
            else:
                thinking = False
            notes.append(f"调用方显式指定受支持模型 {model}")
        else:
            notes.append(f"忽略不受支持的 model_name={requested!r}，回退任务策略 / 配置兜底模型")
            logger.warning(
                "model_policy: 忽略不受支持的 model_name=%r（task=%s），已回退任务策略 / 配置兜底模型",
                requested,
                task,
            )

    # 2) 未指定 / 被忽略：走任务策略，再走配置兜底
    if model is None:
        if policy is not None:
            model = policy.model
            thinking = policy.thinking
            effort = policy.reasoning_effort
            model_source = "task_policy"
        else:
            model, thinking, note = _config_fallback_model()
            model_source = "config_fallback"
            notes.append(note)

    if thinking is None:
        thinking = False

    # 3) effort 归一：思考任务必须有 effort；非思考任务不带 effort
    if thinking:
        effort, effort_note = _normalize_effort(effort or DEFAULT_REASONING_EFFORT, task, model)
        if effort_note:
            notes.append(effort_note)
    else:
        if effort:
            notes.append(f"非思考任务不下发 reasoning_effort（原 {effort!r} 已忽略）")
        effort = None

    # 4) 温度：思考任务不下发；非思考任务按 override > 任务策略 > 默认
    dropped_temperature: float | None = None
    if thinking:
        candidate = _coerce_temperature(ov.get("temperature"))
        if candidate is not None:
            dropped_temperature = candidate
            notes.append(f"thinking=enabled 时不下发 temperature（调用方传入 {candidate} 已忽略）")
            logger.info(
                "model_policy: thinking enabled，丢弃 temperature=%s（task=%s model=%s）",
                candidate,
                task,
                model,
            )
        temperature: float | None = None
    else:
        temperature = _coerce_temperature(ov.get("temperature"))
        if temperature is None:
            if policy is not None and policy.temperature is not None:
                temperature = policy.temperature
            else:
                temperature = DEFAULT_TEMPERATURE
        elif ov.get("temperature") is not None:
            notes.append(f"采用调用方 temperature={temperature}（非思考任务允许）")

    # 5) 输出预算：override.max_tokens > 显式 max_tokens > 业务默认 > 策略 > 全局默认
    max_output = (
        _coerce_positive_int(ov.get("max_tokens"))
        or _coerce_positive_int(max_tokens)
        or _coerce_positive_int(default_max_tokens)
        or (policy.max_tokens if policy is not None else None)
        or _coerce_positive_int(getattr(settings, "ai_max_output_tokens", None))
        or 4000
    )
    # 2026-11-16 修正：思考模型的 reasoning tokens 计入 max_tokens
    # （实测 8192 配额下思考吃掉 2200 token，正文仅剩约 5000 必然截断）。
    # thinking=True 时追加思考余量 4000 token，上限 32000（v4-pro 实测可接受）。
    if thinking and max_output < 32000:
        max_output = min(max_output + 4000, 32000)
        notes.append(f"thinking 任务输出预算追加思考余量 -> {max_output}")

    # 6) JSON 输出：仅非思考任务下发 response_format
    want_json = bool(json_output or (policy.json_output if policy is not None else False))
    response_format: str | None = None
    if want_json:
        if thinking:
            notes.append("thinking=enabled 任务不下发 response_format，JSON 由提示词约束")
        else:
            response_format = RESPONSE_FORMAT_JSON

    # 7) 超时：显式 > 策略 > 全局。
    # 2026-11-16 修正：思考模型单次响应可达 3~7 分钟，全局 120 秒会导致连接中途
    # 断开重试、总耗时翻倍（实测 444 秒）；thinking=True 时兜底提到 600 秒。
    default_timeout = (
        _coerce_positive_int(getattr(settings, "ai_timeout_seconds", None)) or 120
    )
    if thinking and default_timeout < 600:
        default_timeout = 600
    timeout = (
        _coerce_positive_int(timeout_seconds)
        or (policy.timeout_seconds if policy is not None else None)
        or default_timeout
    )

    resolved = ResolvedModelPolicy(
        task_type=task,
        model=model,
        thinking=thinking,
        reasoning_effort=effort,
        temperature=temperature,
        max_tokens=int(max_output),
        response_format=response_format,
        timeout_seconds=int(timeout),
        model_source=model_source,
        requested_model=requested or None,
        dropped_temperature=dropped_temperature,
        notes=tuple(notes),
    )
    logger.info(
        "model_policy: task=%s requested=%s -> model=%s thinking=%s effort=%s temperature=%s "
        "max_tokens=%s response_format=%s timeout=%s source=%s notes=%s",
        resolved.task_type,
        resolved.requested_model,
        resolved.model,
        resolved.thinking_type,
        resolved.reasoning_effort,
        resolved.temperature,
        resolved.max_tokens,
        resolved.response_format,
        resolved.timeout_seconds,
        resolved.model_source,
        list(resolved.notes),
    )
    return resolved


# ---------------------------------------------------------------------------
# 五、请求体构造（字段封闭）与元数据
# ---------------------------------------------------------------------------

_ALLOWED_PAYLOAD_KEYS = {
    "model",
    "messages",
    "max_tokens",
    "temperature",
    "thinking",
    "reasoning_effort",
    "response_format",
}


def build_chat_payload(
    policy: ResolvedModelPolicy,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """按策略构造 Chat Completions 请求体（唯一出口，字段封闭）。

    - thinking 始终显式下发（enabled / disabled），避免推理模式语义随服务端默认漂移；
    - reasoning_effort 仅在思考任务下发；
    - temperature 仅在非思考任务下发；
    - response_format=json_object 仅在非思考任务且提示词含 "json" 关键字时下发
      （供应商要求提示词包含 json 字样，否则可能 400，本层做兜底守卫）。
    """
    payload: dict[str, Any] = {
        "model": policy.model,
        "max_tokens": policy.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": user_prompt or ""},
        ],
        "thinking": policy.thinking_param,
    }
    if policy.reasoning_effort:
        payload["reasoning_effort"] = policy.reasoning_effort
    if policy.temperature is not None:
        payload["temperature"] = policy.temperature
    if policy.response_format:
        prompt_text = f"{system_prompt or ''}\n{user_prompt or ''}".lower()
        if "json" in prompt_text:
            payload["response_format"] = {"type": policy.response_format}
        else:
            logger.info(
                "model_policy: 提示词未包含 json 关键字，跳过 response_format=%s（task=%s model=%s）",
                policy.response_format,
                policy.task_type,
                policy.model,
            )

    unexpected = set(payload) - _ALLOWED_PAYLOAD_KEYS
    if unexpected:
        # 防御性断言：任何非白名单字段都不得出现在请求体中
        for key in unexpected:
            payload.pop(key, None)
        logger.error("model_policy: 请求体出现非白名单字段 %s，已剔除", sorted(unexpected))
    return payload


def policy_metadata(policy: ResolvedModelPolicy) -> dict[str, Any]:
    """策略元数据（供结构化日志与生成记录快照使用，不含任何密钥）。"""
    return {
        "task_type": policy.task_type,
        "requested_model": policy.requested_model,
        "model": policy.model,
        "model_source": policy.model_source,
        "thinking": policy.thinking_type,
        "reasoning_effort": policy.reasoning_effort,
        "temperature": policy.temperature,
        "max_tokens": policy.max_tokens,
        "response_format": policy.response_format,
        "timeout_seconds": policy.timeout_seconds,
        "dropped_temperature": policy.dropped_temperature,
        "notes": list(policy.notes),
    }


def resolve_provider_name() -> str:
    """当前生效的 provider 名称（不返回任何密钥）。"""
    if settings.deepseek_api_key:
        return "deepseek"
    if settings.openai_api_key and not settings.ai_api_key:
        return "openai"
    if settings.ai_api_key or settings.openai_api_key:
        return "custom"
    return "mock"


def describe_policy_table() -> list[dict[str, Any]]:
    """导出任务策略表（供自测 / 文档 / 只读核查接口使用）。"""
    rows: list[dict[str, Any]] = []
    for task_type, policy in TASK_POLICIES.items():
        rows.append(
            {
                "task_type": task_type,
                "model": policy.model,
                "thinking": "enabled" if policy.thinking else "disabled",
                "reasoning_effort": policy.reasoning_effort,
                "temperature": policy.temperature,
                "json_output": policy.json_output,
                "description": policy.description,
            }
        )
    return rows
