# AI 服务：封装大模型对话调用（第六批规格实现，httpx 直连 OpenAI 兼容接口）
import contextvars
import json
import logging
import math
import os
import random
import re
import time

import httpx
from pydantic import BaseModel

from app.core.config import settings
# 阶段 1：模型策略集中解析器（任务分流 / 旧别名兼容 / thinking / effort / 温度唯一维护点）
from app.services.model_policy import (
    LEGACY_MODEL_ALIASES,
    MODEL_FLASH,
    MODEL_V4_PRO,
    REASONING_MODEL_NAMES,
    TaskType,
    build_chat_payload,
    describe_policy_table,
    policy_metadata,
    resolve_model_policy,
)
from app.schemas.ai_novel import (
    AiConfigProvider,
    CharacterProfile,
    ForeshadowPlan,
    NovelChapterPlan,
    NovelPlan,
    NovelVolumePlan,
    PlanUpdateAIResult,
)


_PLACEHOLDER_KEYS = {
    "",
    "sk-demo-local-no-op-2026",
    "your-api-key",
    "sk-xxx",
    "replace-me",
    "replace-this-with-a-long-random-string",
}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 阶段 E：strict_entity_check 开关（实体引用校验）
# 默认 True（写路径启用实体引用校验）；可通过环境变量关闭：
#   NOVELCRAFT_STRICT_ENTITY_CHECK=0|false|off|no
# 校验失败仅丢弃模型候选（可重试），不影响已批准正文，开关可随时关闭。
# ---------------------------------------------------------------------------

_DEFAULT_STRICT_ENTITY_CHECK = True


def strict_entity_check_enabled() -> bool:
    """写路径实体引用校验开关：默认 True，环境变量可关。"""
    env = os.getenv("NOVELCRAFT_STRICT_ENTITY_CHECK")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "off", "no"}
    return _DEFAULT_STRICT_ENTITY_CHECK
# 最近一次 AI 调用的 token 用量（contextvar 隔离并发请求；由 chat_with_ai /
# chat_with_ai_novel 写入，generation_service.record_generation 统一取用后
# 落 generation_records.input_tokens / output_tokens）。
_last_ai_usage: "contextvars.ContextVar[tuple[int | None, int | None]]" = contextvars.ContextVar(
    "last_ai_usage", default=(None, None)
)
# 最近一次 AI 调用的完成原因与用量（finish_reason / prompt_tokens / completion_tokens）。
# 由 chat_with_ai / chat_with_ai_novel 写入，record_generation 统一取用后
# 落 generation_records.finish_reason / prompt_tokens / completion_tokens。
_last_ai_meta: "contextvars.ContextVar[tuple[str | None, int | None, int | None]]" = contextvars.ContextVar(
    "last_ai_meta", default=(None, None, None)
)


def take_last_ai_usage() -> tuple[int | None, int | None]:
    """读取并重置最近一次 AI 调用的 token 用量。"""
    value = _last_ai_usage.get()
    _last_ai_usage.set((None, None))
    return value


def take_last_ai_meta() -> tuple[str | None, int | None, int | None]:
    """读取并重置最近一次 AI 调用的 (finish_reason, prompt_tokens, completion_tokens)。"""
    value = _last_ai_meta.get()
    _last_ai_meta.set((None, None, None))
    return value


def _set_last_ai_usage(data: dict) -> None:
    """从 OpenAI 兼容响应提取 usage 与 finish_reason 写入 contextvar（缺失时置 None）。"""
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    _last_ai_usage.set((prompt_tokens, completion_tokens))
    try:
        finish_reason = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        finish_reason = None
    _last_ai_meta.set((finish_reason, prompt_tokens, completion_tokens))


# ---------------------------------------------------------------------------
# B-2：逐次 token 账目钩子（cost_budget 业务层）
# 由 app.services.token_usage_service 安装；AI 服务只负责在「每次供应商调用」的
# 四个时点回调，不反向依赖业务层（未安装钩子 = 关闭，阶段 1 行为不变）：
#   before_dispatch   发请求前（账目闸门 + 预算闸门 + 先落库占用）
#   response          收到 2xx 响应体后（按 usage 结清）
#   error             请求失败（超时 / 网络 / 非 2xx，结清为不确定）
#   postprocess_error 已取得 usage 之后的 JSON 后处理失败
# ---------------------------------------------------------------------------

_usage_hook = contextvars.ContextVar("usage_hook", default=None)


def install_usage_hook(hook) -> None:
    """安装账目钩子（hook 为 async callable(stage, payload)）。"""
    _usage_hook.set(hook)


def uninstall_usage_hook() -> None:
    _usage_hook.set(None)


def usage_hook_installed() -> bool:
    return _usage_hook.get() is not None


def _usage_provider_label() -> str | None:
    """供应商标签（与 generation_service.resolve_ai_provider 同规则，避免循环依赖）。"""
    if settings.deepseek_api_key:
        return "deepseek"
    if settings.ai_api_key:
        return "openai"
    if settings.openai_api_key:
        return "custom"
    return None


async def _call_usage_hook(stage: str, **payload) -> None:
    """回调账目钩子；钩子异常必须显式上抛（禁止静默吞掉导致漏账）。"""
    hook = _usage_hook.get()
    if hook is None:
        return
    try:
        await hook(stage, payload)
    except Exception as error:  # noqa: BLE001
        if getattr(error, "usage_ledger_blocked", False):
            raise
        raise RuntimeError(f"usage_ledger_hook_failed: {stage}: {error}") from error


async def _notify_usage_error(
    error_type: str, detail: str, *, status_code: int | None = None
) -> None:
    """失败路径通知账目钩子：钩子自身异常不得覆盖原始失败原因，只记 ERROR 日志。"""
    try:
        await _call_usage_hook(
            "error", error_type=error_type, detail=detail, status_code=status_code
        )
    except Exception as hook_error:  # noqa: BLE001
        logger.error("账目钩子失败路径落账异常（原始错误继续上抛）：%s", hook_error)


async def _notify_usage_postprocess_error(
    error_type: str, detail: str, *, status_code: int | None = None
) -> None:
    """后处理失败通知账目钩子（A-3 / A-5）。

    与 _notify_usage_error 同理：钩子自身异常不得覆盖原始失败原因，只记 ERROR 日志。
    区别在阶段语义：error 会把账目结清为 request_uncertain（阻断）；postprocess_error
    只处理「响应已到手之后」的 JSON / 结构失败——已落定的 usage 保留原结算状态，
    已下发但未落定的行结清为 unverifiable（不遗留 pending 行）。
    """
    try:
        await _call_usage_hook(
            "postprocess_error",
            error_type=error_type,
            detail=detail,
            status_code=status_code,
        )
    except Exception as hook_error:  # noqa: BLE001
        logger.error("账目钩子后处理失败路径落账异常（原始错误继续上抛）：%s", hook_error)


# 最近一次 AI 调用的实际提示词（system/user），由 chat_with_ai / chat_with_ai_novel
# 发送前写入，record_generation 在未显式传 prompt 时取用落库，保证每条生成记录可复盘。
_last_ai_prompt: "contextvars.ContextVar[tuple[str, str]]" = contextvars.ContextVar(
    "last_ai_prompt", default=("", "")
)


def take_last_ai_prompt() -> tuple[str, str]:
    """读取并重置最近一次 AI 调用的提示词（system, user）。"""
    value = _last_ai_prompt.get()
    _last_ai_prompt.set(("", ""))
    return value


def _set_last_ai_prompt(system_prompt: str, user_prompt: str) -> None:
    _last_ai_prompt.set((system_prompt or "", user_prompt or ""))
# 阶段 1：最近一次 AI 调用实际生效的模型策略（contextvar 隔离并发请求）。
# 由 chat_with_ai / chat_with_ai_novel 写入，generation_service.record_generation
# 取用后写入生成记录快照（不改历史数据表结构），保证「原始请求模型 vs 实际请求模型」
# 两条信息都可追溯。
_last_ai_policy: "contextvars.ContextVar[dict]" = contextvars.ContextVar(
    "last_ai_policy", default={}
)


def take_last_ai_policy() -> dict:
    """读取并重置最近一次 AI 调用生效的模型策略元数据（无调用时返回 {}）。"""
    value = _last_ai_policy.get()
    _last_ai_policy.set({})
    return dict(value or {})


def _set_last_ai_policy(policy, request_id: str | None = None) -> None:
    """记录本次实际下发的模型策略（模型 / thinking / effort / 温度 / 来源）。"""
    meta = policy_metadata(policy)
    if request_id:
        meta["request_id"] = request_id
    _last_ai_policy.set(meta)


def _response_request_id(response) -> str | None:
    """从响应头提取服务端 request id（仅用于排查，不参与业务逻辑）。"""
    for header in ("x-request-id", "x-ds-request-id", "request-id"):
        try:
            value = response.headers.get(header)
        except Exception:  # noqa: BLE001 - 响应头异常不应影响主流程
            value = None
        if value:
            return str(value)
    return None


def get_effective_ai_config() -> tuple[str, str, str]:
    """返回生效的 AI 配置：优先级 DEEPSEEK_* > AI_* > OPENAI_*。"""
    key = (
        settings.deepseek_api_key
        or settings.ai_api_key
        or settings.openai_api_key
        or ""
    )
    base = (
        settings.deepseek_base_url
        or settings.ai_base_url
        or settings.openai_base_url
        or ""
    ).rstrip("/")
    model = (
        settings.deepseek_model
        or settings.ai_model
        or settings.openai_model
        or ""
    )
    return key, base, model



def _is_real_key(key: str) -> bool:
    # 判断 key 是否为真实密钥（占位/示例视为未配置）。
    if not key or key in _PLACEHOLDER_KEYS:
        return False
    if (
        key.startswith("sk-demo")
        or key.startswith("your-")
        or key.startswith("replace-")
    ):
        return False
    return True


def build_provider_options() -> list[AiConfigProvider]:
    # 构建可选的模型提供方列表（第六批遗留：模型切换入口）。
    # 只返回可展示信息（provider / label / base_url / models / 是否支持思考模型），
    # 任何 API Key 都不出现在返回值中——密钥仅存于服务端环境变量。
    # 阶段 1：是否支持思考由 thinking 参数与任务策略决定，v4-pro 支持思考；
    # 旧推理模型名保留在能力集合中仅用于兼容识别旧配置（集合定义集中在 model_policy）。
    reasoning_models = set(REASONING_MODEL_NAMES)

    def _provider(provider: str, label: str, base_url: str, models: list[str]) -> AiConfigProvider:
        return AiConfigProvider(
            provider=provider,
            label=label,
            base_url=(base_url or "").rstrip("/"),
            models=models,
            support_reasoning=any(
                m.lower() in reasoning_models
                or "reason" in m.lower()
                or m.lower().startswith("o")
                for m in models
            ),
        )

    providers: list[AiConfigProvider] = []
    # deepseek：有 deepseek 配置或 AI_* 默认（AI_BASE_URL 未配时补默认 deepseek 地址）
    ds_base = settings.deepseek_base_url or settings.ai_base_url or "https://api.deepseek.com"
    # 阶段 1：新任务实际请求模型只有 deepseek-flash / deepseek-v4-pro；
    # 若环境变量仍配置旧别名，仅作为「当前配置值」列出，请求时由 model_policy 映射为新模型。
    ds_models = [MODEL_FLASH, MODEL_V4_PRO]
    if settings.deepseek_model:
        ds_models = list(dict.fromkeys(ds_models + [settings.deepseek_model]))
    providers.append(_provider("deepseek", "DeepSeek 深度求索", ds_base, ds_models))

    # openai：有 openai 配置或 AI_* 指向 openai 时列出
    if settings.openai_api_key or settings.openai_base_url or (settings.ai_base_url and "openai" in settings.ai_base_url.lower()):
        oa_base = settings.openai_base_url or settings.ai_base_url or "https://api.openai.com/v1"
        oa_models = list(dict.fromkeys([
            settings.openai_model or "gpt-4o-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
        ]))
        providers.append(_provider("openai", "OpenAI", oa_base, oa_models))

    # custom：有自定义配置（AI_* 或 OPENAI_* 均可承载）时列出
    if settings.ai_api_key or settings.openai_api_key or settings.ai_base_url or settings.openai_base_url:
        cu_base = settings.ai_base_url or settings.openai_base_url or "https://api.openai.com/v1"
        cu_models = list(dict.fromkeys([
            settings.ai_model or settings.openai_model or "custom-model",
            "custom-model",
        ]))
        providers.append(_provider("custom", "自定义兼容接口", cu_base, cu_models))

    return providers


def build_ai_config_payload() -> dict:
    # GET /api/ai/config 响应体：当前生效配置 + 可选提供方（不含任何密钥）。
    key, base, configured_model = get_effective_ai_config()
    providers = build_provider_options()
    reasoning_models = [
        m for p in providers for m in p.models
        if "reason" in m.lower() or m.lower().startswith("o") or m.lower() in REASONING_MODEL_NAMES
    ]
    # 阶段 1：model 字段返回「无任务类型时的兜底实际请求模型」（旧别名会被映射），
    # 并附任务策略表与旧别名映射表，便于前端 / 运维只读核对（不涉及 UI 重构）。
    fallback_policy = resolve_model_policy()
    # 当前生效提供方判定
    if _is_real_key(key):
        if settings.deepseek_api_key:
            provider = "deepseek"
        elif settings.openai_api_key and not settings.ai_api_key:
            provider = "openai"
        else:
            provider = "custom"
    else:
        provider = "mock"
    return {
        "configured": _is_real_key(key),
        "mode": "real" if _is_real_key(key) else "mock",
        "provider": provider,
        "model": fallback_policy.model,
        "configured_model": configured_model or "",
        "model_source": fallback_policy.model_source,
        "thinking": fallback_policy.thinking_type,
        "default_task_model": getattr(settings, "default_task_model", MODEL_FLASH),
        "temperature": 0.7,
        "temperature_note": "temperature 已由 model_policy 按任务类型集中决定；thinking=enabled 的任务不发送 temperature",
        "max_tokens": settings.ai_max_output_tokens,
        "base_url": base or "",
        "providers": [p.model_dump() for p in providers],
        "reasoning_models": list(dict.fromkeys(reasoning_models)),
        "task_policies": describe_policy_table(),
        "legacy_model_aliases": {
            alias: {
                "model": item.model,
                "thinking": "enabled" if item.thinking else "disabled",
                "reasoning_effort": item.reasoning_effort,
            }
            for alias, item in LEGACY_MODEL_ALIASES.items()
        },
    }


def normalize_chat_completions_url(base: str) -> str:
    """规范化 Chat Completions URL：兼容 base 是否带 /v1 或 /chat/completions 后缀。

    - https://api.deepseek.com            -> https://api.deepseek.com/chat/completions
    - https://api.deepseek.com/v1         -> https://api.deepseek.com/v1/chat/completions
    - https://api.deepseek.com/v1/        -> 同上（去尾斜杠）
    - https://api.deepseek.com/chat/completions -> 原样返回
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("AI_BASE_URL 未配置")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def is_ai_configured() -> bool:
    """是否已配置真实 AI 密钥（占位/示例值视为未配置 → 走 mock）。"""
    key, _, _ = get_effective_ai_config()
    if not key or key in _PLACEHOLDER_KEYS:
        return False
    if (
        key.startswith("sk-demo")
        or key.startswith("your-")
        or key.startswith("replace-")
    ):
        return False
    return True


async def chat_with_ai(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None = None,
    override: dict | None = None,
    task_type: str | None = None,
) -> str:
    """通用 AI 对话。

    阶段 1 起：真实请求模型 / thinking / reasoning_effort / temperature /
    response_format 全部由 app.services.model_policy 集中解析，调用方只声明
    任务类型 task_type（可选 override 仅作文本级覆盖，且受策略表约束）。
    """
    key, base, _configured_model = get_effective_ai_config()
    if not key:
        raise RuntimeError("ai_not_configured: 尚未配置 AI 服务")
    url = normalize_chat_completions_url(base)

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    # 唯一模型策略解析点（旧别名兼容映射、thinking/effort/温度规则集中于此）
    policy = resolve_model_policy(
        task_type=task_type,
        override=override,
        max_tokens=max_tokens,
    )
    payload = build_chat_payload(policy, system_prompt, user_prompt)
    _provider = _usage_provider_label()
    # B-2：发请求前先过账目闸门 / 预算闸门，并先落库占用（禁止无账目下发）
    await _call_usage_hook(
        "before_dispatch",
        provider=_provider,
        requested_model=getattr(policy, "requested_model", None),
        normalized_model=getattr(policy, "model", None),
        task_type=task_type,
    )

    try:
        async with httpx.AsyncClient(timeout=policy.timeout_seconds) as client:
            _set_last_ai_prompt(system_prompt, user_prompt)
            _set_last_ai_policy(policy)
            response = await client.post(
                url,
                headers=headers,
                json=payload
            )
    except httpx.TimeoutException:
        await _notify_usage_error("timeout", "ai_provider_timeout")
        raise RuntimeError(
            "ai_provider_timeout: 模型响应超时，请稍后重试或调大 AI_TIMEOUT_SECONDS"
        )
    except httpx.HTTPError as error:
        await _notify_usage_error("network", str(error))
        raise RuntimeError(f"ai_provider_network: 网络请求失败：{error}")

    if response.status_code == 429:
        await _notify_usage_error(
            "http", "ai_provider_rate_limited", status_code=response.status_code
        )
        raise RuntimeError(
            "ai_provider_rate_limited: 模型服务限流（429），请稍后再试"
        )
    if response.is_error:
        await _notify_usage_error(
            "http", response.text[:300], status_code=response.status_code
        )
        raise RuntimeError(
            f"ai_provider_error: 模型服务返回错误（{response.status_code}）：{response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as error:
        # A-5：2xx 但响应体不是合法 JSON -> 请求已下发且 usage 不可验证；
        #       必须走 postprocess_error 结清为 unverifiable（禁止永久遗留 pending 行）
        await _notify_usage_postprocess_error(
            "bad_response",
            f"ai_provider_bad_response: 响应体不是合法 JSON（{type(error).__name__}）",
            status_code=response.status_code,
        )
        raise RuntimeError(
            "ai_provider_bad_response: AI 返回内容不是合法 JSON"
        ) from error

    # B-2：已取得响应体，立即按 usage 结清本次调用的账目
    await _call_usage_hook(
        "response",
        status_code=response.status_code,
        body=data,
        provider=_provider,
    )

    try:
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason")
        content = choice["message"]["content"]
        _set_last_ai_usage(data)
        _set_last_ai_policy(policy, _response_request_id(response))
        if finish_reason == "length":
            raise RuntimeError(
                "ai_output_truncated: 模型输出达到 max_tokens 上限被截断，内容不完整"
            )
        return content
    except RuntimeError:
        raise
    except (KeyError, IndexError, TypeError):
        # B-2 / A-3：usage 已取得但响应体结构异常 -> 走 postprocess_error 语义
        # （已落定账目保留 complete / partial，不改判为不确定）
        await _notify_usage_postprocess_error(
            "bad_response", "ai_provider_bad_response", status_code=response.status_code
        )
        raise RuntimeError("ai_provider_bad_response: AI 返回格式异常")


async def _chat_with_ai_retry_on_truncation(
    system_prompt: str,
    user_prompt: str,
    override: dict | None = None,
    task_type: str | None = None,
) -> str:
    """调用 chat_with_ai；若因 max_tokens 截断（finish_reason=length），
    放宽上限自动重试一次，提升 JSON 完整输出成功率。

    注意：chat_with_ai 内部 override.max_tokens 优先于显式参数，
    故重试时必须把新的上限写回 override，否则会被请求携带的较小值覆盖。
    """
    try:
        return await chat_with_ai(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            override=override,
            task_type=task_type,
        )
    except RuntimeError as error:
        if "ai_output_truncated" not in str(error):
            raise
        # 2026-11-16 修正：DeepSeek 单次输出硬上限 8192 tokens，
        # 原 2 倍上限（16384）会被 API 拒绝；重试配额提到 8192 封顶，
        # 若当前配额已到上限则重试无意义，直接抛出原错误。
        boosted = min(max(int(settings.ai_max_output_tokens * 2), 8192), 8192)
        current = int((override or {}).get("max_tokens") or settings.ai_max_output_tokens)
        if boosted <= current:
            raise
        retry_override = dict(override or {})
        retry_override["max_tokens"] = boosted
        return await chat_with_ai(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            override=retry_override,
            task_type=task_type,
        )


def _extract_json_obj(text: str):
    """从模型原始输出中提取 JSON 对象：优先直接解析；失败则剥离 Markdown 代码块、
    截取首个 { 到末个 } 的子串后再解析。全部失败返回 None。"""
    if not text:
        return None
    candidates = []
    candidates.append(text)
    # 剥离 ```json / ``` 代码块围栏
    stripped = re.sub(r"```(?:json|JSON)?\s*", "", text).replace("```", "")
    if stripped.strip() != text.strip():
        candidates.append(stripped)
    # 截取首个 { 到末个 } 之间的内容
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


async def chat_with_ai_json(
    system_prompt: str,
    user_prompt: str,
    response_model: type[BaseModel],
    override: dict | None = None,
    task_type: str | None = None,
) -> BaseModel:
    """调用 AI 并要求返回严格 JSON，解析后按 Pydantic 模型校验。

    解析失败或校验失败时抛 RuntimeError，由调用方决定重试或返回 502。
    JSON 输出格式由 model_policy 按任务类型集中决定（非思考任务下发
    response_format=json_object，思考任务由提示词约束）。
    """
    text = await _chat_with_ai_retry_on_truncation(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        override=override,
        task_type=task_type,
    )

    data = _extract_json_obj(text)
    if data is None:
        raise RuntimeError("AI 返回的不是有效 JSON")

    try:
        return response_model.model_validate(data)
    except Exception as error:
        raise RuntimeError(f"AI 返回内容不符合要求：{error}")


async def chat_with_ai_json_dict(
    system_prompt: str,
    user_prompt: str,
    override: dict | None = None,
    task_type: str | None = None,
) -> dict:
    """调用 AI 并要求返回严格 JSON，解析后返回原始 dict（不做 Pydantic 校验）。

    供需要先宽松规整再实例化模型的场景使用（如规划接口的数组字段兜底）。
    """
    text = await _chat_with_ai_retry_on_truncation(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        override=override,
        task_type=task_type,
    )
    data = _extract_json_obj(text)
    if data is None:
        raise RuntimeError("AI 返回的不是有效 JSON")
    if not isinstance(data, dict):
        raise RuntimeError("AI 返回内容不是 JSON 对象")
    return data


# ---------------------------------------------------------------------------
# AI 小说生成（端到端自动创作）
# ---------------------------------------------------------------------------

def _locked_block(setting: "NovelGenerateRequest") -> str:
    """把锁定设定拼成提示词段落：生成正文时必须遵守、不得改变。"""
    locked = [s for s in (setting.locked or []) if s and s.strip()]
    if not locked:
        return ""
    lines = "\n".join(f"- {s}" for s in locked)
    return (
        "\n\n【已锁定设定（不可改变）】\n"
        "以下设定已经用户确认并锁定，本次生成正文必须严格遵循，禁止修改、推翻或自相矛盾：\n"
        f"{lines}"
    )


def count_words(text: str | None) -> int:
    """统计正文有效字数：去除所有空白字符（含换行/空格）后的字符数，中文按字计。"""
    if not text:
        return 0
    return len(re.sub(r"\s+", "", text))


def _words_target_tokens(words: int) -> int:
    """根据目标字数估算需要的 max_tokens。

    2026-11-16 实测修正：中文字符与 token 近似 1:1.5~1.8，1.6 倍系数在 800 字目标下
    配额 1280 必然触顶截断（实测 output_tokens=1280 失败）；系数提到 2.5 留安全余量。
    """
    if words <= 0:
        return settings.ai_max_output_tokens
    estimated = int(words * 2.5)
    return max(128, min(estimated, settings.ai_max_output_tokens))


def _words_constraint_block(target_words: int) -> str:
    """生成正文时的字数强约束提示词段落（正文不含标题、不计空白）。

    第一阶段修正：后端已关闭静默字符截断（allow_truncate=False），不再存在
    「超出 20% 被截断」的行为。提示词明确告知模型正文会完整保留，避免模型
    因害怕被截断而提前收尾；若因单次 max_tokens 上限未写完，引导其完整输出
    并提示可续写，由元数据（finish_reason/status）标记为未完成，不伪装正式章节。
    """
    low = max(1, int(target_words * 0.85))
    high = int(target_words * 1.15)
    return (
        f"\n字数强约束：正文应控制在目标字数附近（即 {low}~{high} 字），"
        f"不得大幅超长或过短；请在收尾时自行核对篇幅。后端不会按字数截断正文，"
        f"超出或不足的内容都会完整保留并标记。若因单次输出长度上限（max_tokens）"
        f"尚未写完，请勿强行压缩结尾，保持叙事完整，稍后可通过续写补齐。"
    )


def _smart_truncate(text: str, target: int) -> str:
    """按目标字数智能截断正文：优先保持段落边界，其次句子边界（句号/感叹/问号/分号），
    最后才允许在字符级截断，并保证返回内容以完整句子/段落结尾。"""
    if count_words(text) <= target:
        return text
    paragraphs = text.split("\n")
    kept: list[str] = []
    acc = 0
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        w = count_words(p)
        if acc + w <= target:
            kept.append(p)
            acc += w
            continue
        # 当前段落放不下：先在句子边界切分
        if acc < target:
            remaining = target - acc
            sentences = re.split(r"(?<=[。！？；])", p)
            partial = ""
            for s in sentences:
                if not s:
                    continue
                sw = count_words(s)
                if acc + sw <= target:
                    partial += s
                    acc += sw
                else:
                    # 字符级兜底：截到目标附近；绝不在词中间截断——
                    # 2026-11-17 修复：回退到预算内最后一个标点/引号处，
                    # 保证返回内容以完整句子结尾（用户看到「章节不全」的直接原因）。
                    need = target - acc
                    if need > 0:
                        cut = s[:need]
                        m = re.search(r"[。！？；，、：”’…][^。！？；，、：”’…]*$", cut)
                        if m:
                            cut = cut[: m.start() + 1]
                        else:
                            cut = ""
                        if cut.strip():
                            partial += cut
                            acc += count_words(cut)
                    break
            if partial.strip():
                kept.append(partial.strip())
        break
    return "\n\n".join(kept).strip()


def assemble_stream_chunks(chunks: list[str] | None) -> str:
    """按序拼接流式分片为完整文本。

    流式链路必须在分片完整拼接后才能进入 _finalize_chapter /
    evaluate_chapter_result 等后处理，避免对半截文本做标题提取、字数评估
    或完成性标记，防止把正常生成误判为残缺。
    """
    return "".join(chunks or [])


_SENTENCE_END_CHARS = "。！？…；"

# 2026-11-17：章末允许出现的结尾字符（其余一律视为「没写完」）。
# 「…」是合法的悬念式结尾；引号/括号结尾视为完整。
_CHAPTER_END_OK_CHARS = "。！？…”\'」』…"


def _ends_incomplete(text: str) -> bool:
    """末尾未落在句末标点/引号上 → 判定为未写完（模型或网关误停）。"""
    s = (text or "").rstrip()
    if not s:
        return False
    return s[-1] not in _CHAPTER_END_OK_CHARS


async def _complete_unfinished_ending(content: str, override: dict | None) -> str:
    """末尾明显没写完时，追加一次小续写把结尾补齐并给出章末钩子。

    2026-11-17 修复「章节不全」：模型输出被中途截断（finish_reason 却报 stop）
    时，此前会原样返回残缺正文；现在自动补写一次。失败时保留原文，不阻断。
    """
    tail = (content or "")[-600:]
    sys_p = (
        "你是一名资深网络小说作家。用户提供了一段尚未写完的小说章节，"
        "最后一句没有写完。请从最后一句之后继续写，把本章收尾写完整，"
        "并给出章末钩子。要求：只输出续写部分（150~400 字），"
        "不要输出标题或解释，不要重复已写内容，使用简体中文。"
    )
    usr_p = (
        f"章节正文末尾（接续依据）：\n{tail}\n\n"
        "请从最后一句之后继续写，写完本章并给出章末钩子。"
    )
    try:
        extra = await chat_with_ai(
            system_prompt=sys_p,
            user_prompt=usr_p,
            max_tokens=_words_target_tokens(400),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
    except RuntimeError:
        return content
    extra = (extra or "").strip()
    if not extra:
        return content
    return f"{content.rstrip()}\n\n{extra}"


async def _maybe_complete_ending(content: str, override: dict | None) -> str:
    if _ends_incomplete(content):
        return await _complete_unfinished_ending(content, override)
    return content


def _tail_quality_flags(body: str) -> list[str]:
    """完成性风险标记（只标记不删除内容）：末尾未闭合引号/括号、非句末标点。"""
    flags: list[str] = []
    stripped = (body or "").rstrip()
    if not stripped:
        return flags
    pairs = (
        ("“", "”"), ("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"),
        ("《", "》"), ("【", "】"), ("[", "]"), ("{", "}"),
        ("‘", "’"), ("\"", "\""),
    )
    for open_q, close_q in pairs:
        if open_q == close_q:
            if stripped.count(open_q) % 2 == 1:
                flags.append(f"unclosed_delimiter:{open_q}")
        else:
            if stripped.count(open_q) > stripped.count(close_q):
                flags.append(f"unclosed_delimiter:{open_q}")
            if stripped.count(close_q) > stripped.count(open_q):
                flags.append(f"extra_closing_delimiter:{close_q}")
    last_char = stripped[-1]
    if last_char not in _SENTENCE_END_CHARS:
        flags.append("non_sentence_end")
    return flags


def evaluate_chapter_result(
    *,
    raw_content: str,
    title: str,
    body: str,
    target_words: int | None = None,
    finish_reason: str | None = None,
    stream_completed: bool | None = None,
) -> dict:
    """生成完成性校验 + 结果状态评估（纯函数，不改写正文）。

    判定优先级：
      1. finish_reason == "length"（触达 max_tokens）→ status=incomplete；
      2. 流式链路无明确结束事件（stream_completed=False）→ status=incomplete；
      3. 正文为空或低于最小阈值（< target*0.7*0.3，或正文为空）→ status=failed；
      4. 正文超过 target*1.2 → status=over_target（保留完整正文，不截断）；
      5. 其余 → status=completed。

    needs_review 为独立风险标记（存 review_flags），不改变 status：
      - 末尾未闭合引号/括号 → unclosed_delimiter:* / extra_closing_delimiter:*
      - 末尾非句末标点 → non_sentence_end
      - 字数低于最小目标（< target*0.7）→ below_minimum

    返回 dict 字段与 generation_records 新列一一对应：
      status / review_flags / postprocess_actions / was_truncated /
      truncation_reason / final_content_length / raw_content_length。
    """
    raw_len = count_words(raw_content)
    final_len = count_words(body)
    actions: list[str] = ["normalize_title"]
    flags: list[str] = []
    status = "completed"
    truncation_reason: str | None = None
    was_truncated = False

    if finish_reason == "length":
        status = "incomplete"
        truncation_reason = "truncated_by_max_tokens"
        flags.append("truncated_by_max_tokens")
    elif stream_completed is False:
        status = "incomplete"
        truncation_reason = "stream_not_completed"
        flags.append("stream_not_completed")

    if final_len <= 0 or (
        target_words and final_len < max(1, int(target_words * 0.7 * 0.3))
    ):
        status = "failed"
        truncation_reason = (
            truncation_reason or "response_below_minimum"
        )
        flags.append("below_minimum")
    elif target_words and final_len > target_words * 1.2:
        status = "over_target"
        flags.append("over_target")
        actions.append("keep_full_text_no_truncate")

    flags.extend(_tail_quality_flags(body))
    if target_words and final_len < target_words * 0.7 and status not in ("failed", "incomplete"):
        flags.append("below_minimum")

    if status == "incomplete":
        actions.append("flagged_incomplete")
    actions.append("no_truncate")

    return {
        "status": status,
        "review_flags": flags,
        "postprocess_actions": actions,
        "was_truncated": was_truncated,
        "truncation_reason": truncation_reason,
        "final_content_length": final_len,
        "raw_content_length": raw_len,
    }


async def chat_with_ai_novel(
    setting: "NovelGenerateRequest",
    index: int,
    total: int,
    previous_chapter: str | None = None,
    override: dict | None = None,
    task_type: str = TaskType.NOVEL_BODY.value,
) -> str:
    """真实模式下生成一章：模型策略由 model_policy 集中解析，超时/失败抛 RuntimeError。"""
    key, base, _configured_model = get_effective_ai_config()
    if not key:
        raise RuntimeError("ai_not_configured: 尚未配置 AI 服务")
    if not base:
        raise RuntimeError("ai_base_url_not_configured: AI_BASE_URL 未配置")

    url = normalize_chat_completions_url(base)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "你是一名资深网络小说作家。请严格按照用户提供的创作设定，"
        "写出一章完整的、可直接阅读的小说正文。\n"
        "要求：\n"
        "1. 输出格式：第一行必须是本章标题，严格采用「第X章：简短标题」形式"
        "（X 为本章序号，标题本体 2~20 个字的名词性短语，不得是完整句子或描述句，"
        "正确示例「第1章：风声渐起」，错误示例「第一章 主角在调查中发现线索并决定跟踪」）；"
        "从第二行开始才是正文内容，正文不要重复标题。\n"
        "2. 只输出小说正文本身，不要输出标题以外的解释、大纲或元信息。\n"
        "3. 开头不要重复复述设定，直接从情节切入。\n"
        "4. 每章要有明确推进：场景、人物互动、冲突或变化、结尾留钩子。\n"
        "5. 严格遵守叙事视角与文风要求。\n"
        "6. 正文使用简体中文，按段落换行。\n"
        "7. 用户标注为「已锁定设定」的内容是最高优先级约束：不得改变、不得推翻、"
        "不得出现与锁定设定矛盾的人物身份、人物关系、结局走向、反派身份或关键秘密。\n"
        "8. 字数强约束：本章正文必须严格控制在目标字数附近，波动不得超过 ±15%，"
        "不得大幅超长或过短；正文总字数（不含标题、不计空白）以用户给出的目标字数为准，"
        "请在写作过程中自行把握篇幅并在收尾时确认字数落在目标范围内；"
        "若超出目标 20% 以上，后端不会截断正文，将保留完整内容并标记为超长（over_target）。"
    )

    user_prompt = (
        f"作品标题：{setting.title}\n"
        f"小说类型：{setting.novel_type or '未设定'}\n"
        f"题材：{setting.genre or '未设定'}\n"
        f"世界观：{setting.world_view or '未设定'}\n"
        f"时代背景：{setting.era or '未设定'}\n"
        f"主要人物：{setting.characters or '未设定'}\n"
        f"人物关系：{setting.relationships or '未设定'}\n"
        f"故事主线：{setting.main_plot or '未设定'}\n"
        f"叙事视角：{setting.perspective or '未设定'}\n"
        f"文风：{setting.writing_style or '未设定'}\n"
        f"其他创作要求：{setting.requirements or '无'}"
        f"{_locked_block(setting)}\n\n"
        f"本次要写第 {index} 章，共 {total} 章，本章目标字数：{setting.words_per_chapter} 字"
        f"（正文尽量落在 {int(setting.words_per_chapter * 0.85)}~{int(setting.words_per_chapter * 1.15)} 字之间，"
        f"以目标值为准自行收尾，超长不会被截断）。\n"
        f"之前章节正文（供承接，不要复述）：\n{(previous_chapter or '无')[:6000]}\n\n"
        "请开始写正文。"
    )

    # 唯一模型策略解析点：正文默认 deepseek-v4-pro（非思考），输出预算按目标字数换算
    policy = resolve_model_policy(
        task_type=task_type or TaskType.NOVEL_BODY.value,
        override=override,
        default_max_tokens=_words_target_tokens(setting.words_per_chapter),
    )
    payload = build_chat_payload(policy, system_prompt, user_prompt)
    _provider = _usage_provider_label()
    # B-2：发请求前先过账目闸门 / 预算闸门，并先落库占用（禁止无账目下发）
    await _call_usage_hook(
        "before_dispatch",
        provider=_provider,
        requested_model=getattr(policy, "requested_model", None),
        normalized_model=getattr(policy, "model", None),
        task_type=task_type,
    )

    try:
        async with httpx.AsyncClient(timeout=policy.timeout_seconds) as client:
            _set_last_ai_prompt(system_prompt, user_prompt)
            _set_last_ai_policy(policy)
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        await _notify_usage_error("timeout", "ai_provider_timeout")
        raise RuntimeError(
            "ai_provider_timeout: 模型响应超时，请稍后重试或调大 AI_TIMEOUT_SECONDS"
        )
    except httpx.HTTPError as error:
        await _notify_usage_error("network", str(error))
        raise RuntimeError(f"ai_provider_network: 网络请求失败：{error}")

    if response.status_code == 429:
        await _notify_usage_error(
            "http", "ai_provider_rate_limited", status_code=response.status_code
        )
        raise RuntimeError(
            "ai_provider_rate_limited: 模型服务限流（429），请稍后再试"
        )
    if response.is_error:
        await _notify_usage_error(
            "http", response.text[:300], status_code=response.status_code
        )
        raise RuntimeError(
            f"ai_provider_error: 模型服务返回错误（{response.status_code}）：{response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as error:
        # A-5：2xx 但响应体不是合法 JSON -> 请求已下发且 usage 不可验证；
        #       必须走 postprocess_error 结清为 unverifiable（禁止永久遗留 pending 行）
        await _notify_usage_postprocess_error(
            "bad_response",
            f"ai_provider_bad_response: 响应体不是合法 JSON（{type(error).__name__}）",
            status_code=response.status_code,
        )
        raise RuntimeError(
            "ai_provider_bad_response: 模型返回内容不是合法 JSON"
        ) from error
    try:
        # B-2：已取得响应体，立即按 usage 结清本次调用的账目
        await _call_usage_hook(
            "response",
            status_code=response.status_code,
            body=data,
            provider=_provider,
        )
        content = data["choices"][0]["message"]["content"]
        _set_last_ai_usage(data)
        _set_last_ai_policy(policy, _response_request_id(response))
        return content
    except (KeyError, IndexError, TypeError):
        # B-2 / A-3：usage 已取得但响应体结构异常 -> 走 postprocess_error 语义
        # （已落定账目保留 complete / partial，不改判为不确定）
        await _notify_usage_postprocess_error(
            "bad_response", "ai_provider_bad_response", status_code=response.status_code
        )
        raise RuntimeError("ai_provider_bad_response: 模型返回格式异常")


# ---------------------------------------------------------------------------
# 章节标题规范化（统一为「第X章：简短标题」形式，供所有生成入口兜底清洗）
# ---------------------------------------------------------------------------


def _strip_chapter_prefix(raw: str) -> str:
    """剥离 Markdown 标记与「第X章/第X回/第X节/3. /Chapter 3」等前缀，返回纯标题文本。"""
    s = raw.strip()
    # 剥离 Markdown 标题/列表/加粗/引用标记（## / # / * / - / > 等），模型可能叠加输出
    s = re.sub(r"^[#*>\-]+\s*", "", s)
    s = re.sub(
        r"^第\s*[0-9０-９一二三四五六七八九十百千零〇]+\s*(章|回|节|卷)\s*[:：\-—]?\s*",
        "",
        s,
    )
    s = re.sub(r"^[0-9０-９]+\s*[.、:：]\s*", "", s)
    s = re.sub(r"^[Cc]hapter\s*[0-9]+\s*[:：\-—]?\s*", "", s)
    # 再剥离一次 Markdown 标记，兜底「## 第1章：标题」这类叠加输出
    s = re.sub(r"^[#*>\-]+\s*", "", s)
    return s.strip()


def _clean_title_text(raw: str) -> str:
    """清洗标题本体：去外壳引用/书名号、去句末句子标点、超长截断（≤20 字）。"""
    s = raw.strip()
    s = s.strip("《》「」『』“”‘’\"' 　")
    s = re.sub(r"[。！？；，、,.!?;:]+$", "", s).strip()
    if len(s) > 20:
        # 优先取第一个逗号/顿号前的片段（更接近标题形态），否则硬截断
        seg = re.split(r"[，,、]", s, maxsplit=1)[0].strip()
        if 2 <= len(seg) <= 20:
            s = seg
        else:
            s = s[:20]
        s = re.sub(r"[。！？；，、,.!?;:]+$", "", s).strip()
    return s


def normalize_chapter_title(raw: str | None, index: int) -> str:
    """把任意来源的章节标题规范为「第X章：简短标题」。

    - 剥离已有「第X章 / 3.」前缀，只保留标题本体；
    - 去句末句子标点、超长截断（≤20 字），长句优先截到第一个逗号前；
    - 空值 / 纯标点回退「未命名」；
    - 统一前缀「第{index}章：」（index 为全局章节序号，从 1 起）。
    """
    fallback = "未命名"
    s = _strip_chapter_prefix(raw or "")
    if not s:
        return f"第{index}章：{fallback}"
    s = _clean_title_text(s)
    if not s:
        return f"第{index}章：{fallback}"
    return f"第{index}章：{s}"


def _normalize_plan_titles(plan: "NovelPlan") -> "NovelPlan":
    """对规划内所有章节标题做「第X章：简短标题」兜底清洗（保留 index 与其余字段）。"""
    for vol in plan.volumes:
        for ch in vol.chapters:
            ch.title = normalize_chapter_title(ch.title, ch.index)
    return plan


def _fix_foreshadow_order(plan: "NovelPlan") -> "NovelPlan":
    """伏笔顺序兜底：回收章号必须晚于埋设章号（2026-11-16 实测模型会偶发倒挂）。

    payoff <= planted 时把回收章修正为 planted + 10（不超过 400 章上限）。
    """
    for f in plan.foreshadowing:
        if f.payoff_chapter <= f.planted_chapter:
            f.payoff_chapter = min(f.planted_chapter + 10, 400)
    return plan


def _coerce_plan_dict(plan: dict) -> dict:
    """宽松规整模型返回的规划 JSON：数组类字段合并为字符串，避免 schema 校验失败。

    DeepSeek 等模型偶发把 key_events / goal / conflict 生成为数组，schema 要求字符串；
    这里统一 join 后再实例化 NovelPlan，保证真实模式 plan 接口稳定返回。
    """
    plan = dict(plan or {})
    for vol in plan.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        for ch in vol.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            for field in ("title", "goal", "conflict", "key_events", "ending_hook"):
                val = ch.get(field)
                if isinstance(val, list):
                    parts = [str(x).strip() for x in val if str(x).strip()]
                    ch[field] = "；".join(parts) if parts else ""
    return plan


# ---- mock 动态生成器：根据用户设定构造结构完整、每次不同的小说文本 ----

_CN_NUMS = "一二三四五六七八九十"


def _cn_num(n: int) -> str:
    if n <= 10:
        return _CN_NUMS[n - 1]
    return str(n)


def _pick(rng: random.Random, options: list[str]) -> str:
    return options[rng.randrange(len(options))]


def _mock_chapter_title(rng: random.Random, index: int) -> str:
    titles = [
        "风声渐起", "暗流涌动", "初遇", "抉择", "旧日残影", "夜色之下",
        "无声的约定", "裂痕", "重逢", "转折点", "险境", "真相一角",
        "远行", "约定", "风暴前夜", "回声", "破局", "代价", "曙光", "归途",
    ]
    a = _pick(rng, titles)
    if index <= len(titles):
        a = titles[(index - 1) % len(titles)]
    return normalize_chapter_title(a, index)


def _split_names(s: str) -> list[str]:
    """按逗号/顿号拆分人名，但保持括号（身份描述）内完整。"""
    out: list[str] = []
    buf = ""
    depth = 0
    for ch in s:
        if ch in "（(":
            depth += 1
            buf += ch
        elif ch in "）)":
            depth -= 1
            buf += ch
        elif ch in ",，、;；" and depth == 0:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _mock_paragraphs(
    rng: random.Random,
    setting: "NovelGenerateRequest",
    index: int,
    total: int,
    previous_chapter: str | None,
    target_chars: int,
) -> list[str]:
    title = setting.title or "无名之作"
    genre = setting.genre or "故事"
    world_view = setting.world_view or "一个独特的世界"
    era = setting.era or "当代"
    characters = setting.characters or "主角"
    relationships = setting.relationships or "人物之间关系复杂"
    main_plot = setting.main_plot or "一段跌宕起伏的冒险"
    perspective = setting.perspective or "第三人称"
    writing_style = setting.writing_style or "平实流畅"
    requirements = setting.requirements or ""

    c = _split_names(characters)
    hero = c[0] if c else "主角"
    peer = c[1] if len(c) > 1 else "同伴"
    foe = c[-1] if len(c) > 1 else "对手"

    paras: list[str] = []

    if previous_chapter:
        tail = previous_chapter.strip().splitlines()[-1][:40]
        paras.append(
            _pick(rng, [
                f"上一章的尾声仍萦绕在{hero}心头——“{tail}”——但此刻已没有时间多想。",
                f"“{tail}”这句话在{hero}脑中反复回响，{hero}深吸一口气，推开了面前的门。",
            ])
        )
    else:
        paras.append(
            _pick(rng, [
                f"《{title}》的故事，从{era}的一个寻常日子开始。",
                f"在{era}，{hero}第一次意识到，自己正站在一个巨大转折的起点。",
                f"风吹过{world_view}的街巷，{hero}停下脚步，心里有个念头越来越清晰。",
            ])
        )

    paras.append(
        _pick(rng, [
            f"{hero}与{peer}走在{world_view}之中，周围的一切都在提醒他们：{main_plot}。",
            f"{hero}知道，要解开{main_plot}这条线，必须先面对{foe}。",
            f"{era}的风物在眼前铺开，{hero}却只记得{main_plot}带来的不安。",
        ])
    )

    paras.append(
        _pick(rng, [
            f"“{peer}，”{hero}压低声音，“{relationships}，这句话我们不能再装作没听见。”",
            f"{foe}的出现让气氛骤然凝固。{hero}想起{relationships}，一时竟不知该先开口还是先出手。",
            f"{hero}翻开{peer}留下的字条，上面的信息与{relationships}不谋而合，脊背泛起一阵寒意。",
        ])
    )

    paras.append(
        _pick(rng, [
            f"接下来的半天里，{hero}不断试探、求证，把散落的线索一一拼合。{main_plot}的全貌逐渐清晰，代价却是失去了一个安稳的退路。",
            f"{hero}与{foe}的第一次正面交锋来得比预想更快。刀光与言语交错之间，{main_plot}露出了它真实的獠牙。",
            f"没有人注意到{hero}悄悄调转了方向。真正要紧的不是眼前的争吵，而是{main_plot}背后那双看不见的手。",
        ])
    )

    paras.append(
        _pick(rng, [
            f"以{perspective}视角望去，这一切不过是序章：更大的风暴正在{era}的尽头酝酿。",
            f"入夜后，{hero}独自坐在灯下，把今天的经历写进笔记。{writing_style}的字句下，藏着不敢说出口的预感。",
            f"远处传来一声异响。{hero}回头，只见{foe}的身影没入黑暗，而{main_plot}的谜底，又远了一步。",
        ])
    )

    if requirements:
        paras.append(
            _pick(rng, [
                f"按照约定，这段叙事还需顾及：{requirements}。{hero}默默记下，决定在下一章给读者一个交代。",
                f"{requirements}的要求像一根无形的线，把{hero}的选择拉回正轨。",
            ])
        )

    paras.append(
        _pick(rng, [
            f"这一章在此处告一段落，但{hero}知道，真正的{main_plot}才刚刚开始。",
            f"夜色更深，{hero}合上眼，脑中却全是{peer}最后那句话。故事，还远未结束。",
        ])
    )

    # 按目标字数调整：不足时插入补充段落直到接近目标，超出时压缩中间段
    text = "\n\n".join(paras)
    if len(text) > target_chars:
        keep = [paras[0], paras[-1]]
        mid = paras[1:-1]
        total_over = len(text) - target_chars
        while mid and total_over > 120:
            removed = mid.pop()
            total_over -= len(removed)
        paras = [paras[0]] + mid + [paras[-1]]
    else:
        # 目标字数较大时插入扩充段，使演示模式同样贴近目标（目标 ±5% 内）
        while True:
            text_now = "\n\n".join(paras)
            if len(text_now) >= target_chars * 0.95:
                break
            extra = _mock_extra_paragraph(
                rng, hero, peer, foe, world_view, main_plot,
                perspective, writing_style, era,
            )
            paras.insert(len(paras) - 1, extra)
    return paras


def _mock_extra_paragraph(
    rng: random.Random,
    hero: str,
    peer: str,
    foe: str,
    world_view: str,
    main_plot: str,
    perspective: str,
    writing_style: str,
    era: str,
) -> str:
    """生成一段 60~120 字的 mock 扩充段落，用于把演示正文字数补到目标附近。"""
    templates = [
        f"{hero}在{world_view}中走走停停，把刚刚得到的线索反复咀嚼。{main_plot}像一张越收越紧的网，"
        f"每走一步都离真相更近，也离危险更近。以{perspective}视角看来，此刻的平静不过是风暴前的短暂沉默。",
        f"{peer}低声提醒道：“{main_plot}这件事，牵涉的远比我们想象的深。”{hero}没有立刻回答，"
        f"只是望着远处{era}的天际线，心里盘算着下一步棋。风从街角吹来，带着一丝若有若无的腥气。",
        f"旧日的记忆在这一刻涌上心头。{hero}想起自己为何会走到今天这一步，想起与{foe}之间那段被刻意掩埋的往事，"
        f"指尖微微发凉。{writing_style}的笔调下，往事与眼前的危机重叠成同一个阴影。",
        f"{hero}蹲下身，拾起一片被风吹落的纸页，上面的字迹潦草却触目惊心。{main_plot}的关键一环，"
        f"似乎就藏在这几行字里。他/她抬起头，{peer}的目光里同样写满了不安与期待。",
        f"街道另一头忽然亮起车灯，刺目的光把{hero}的影子拉得很长。{foe}的人已经跟到这里了吗？"
        f"{hero}没有回头，只是加快脚步。在这个{world_view}里，慢一步，就可能是万劫不复。",
        f"深夜的房间里只有笔尖摩擦纸面的声响。{hero}把今天所见整理成册，{main_plot}的脉络在纸上逐渐成形，"
        f"却有一个关键节点怎么也对不上。他/她揉着眉心，决定明天再去一次那个被遗忘的地方。",
        f"“你确定要继续查下去？”{peer}的声音带着迟疑。“已经走到这一步了，”{hero}说，"
        f"“{main_plot}的答案，我必须亲眼看到。”两人的对话在{era}的风声里显得格外清晰，也格外沉重。",
        f"{hero}忽然想起一个细节——此前被忽略的角落里，曾有{foe}留下的痕迹。若真是如此，"
        f"整件事的走向就完全不同了。他/她立刻转身，向{peer}说了自己的想法，两人连夜重新梳理起所有线索。",
    ]
    return _pick(rng, templates)


def generate_mock_novel(
    setting: "NovelGenerateRequest",
    index: int,
    total: int,
    previous_chapter: str | None = None,
    locked: list[str] | None = None,
) -> str:
    """根据设定生成一章 mock 文本：每次调用结果不同（时间种子）。"""
    # 演示模式同样写入等效提示词，使 generation_records.prompt / input_tokens 可复盘
    _set_last_ai_prompt(
        (
            "你是资深网文作家。请根据创作设定与上下文，续写小说正文。\n"
            "要求：第一行必须是本章标题（第X章：简短标题），从第二行开始才是正文内容，正文不要重复标题；"
            "只输出小说正文本身，不要输出解释、大纲或元信息；正文使用简体中文，按段落换行；"
            + _words_constraint_block(setting.words_per_chapter)
        ),
        (
            f"{_setting_brief(setting)}\n"
            f"本章为第 {index} 章（共 {total} 章），目标字数约 {setting.words_per_chapter} 字。\n"
            + (f"上一章末尾：\n{previous_chapter[-1200:]}\n" if previous_chapter else "")
            + "请开始续写正文。"
        ),
    )
    rng = random.Random(
        (hash((setting.title, setting.main_plot, setting.characters)) & 0xFFFF)
        ^ (index * 7919)
        ^ int(time.time() * 1000) % 999983
    )
    paras = _mock_paragraphs(
        rng, setting, index, total, previous_chapter, setting.words_per_chapter
    )
    title = _mock_chapter_title(rng, index)
    body = "\n\n".join(paras)
    lock_items = [s for s in (locked or setting.locked or []) if s and s.strip()]
    lock_note = ""
    if lock_items:
        lock_note = "\n\n（本演示正文已遵循锁定设定：" + "；".join(lock_items[:5]) + "）"
    return f"{title}\n\n{body}{lock_note}\n\n（以上为演示生成内容，配置 DEEPSEEK_API_KEY 后可切换真实 AI 模式）"


# ---------------------------------------------------------------------------
# AI 调用限流：每用户每分钟 N 次（内存实现，窗口与上限读 settings）
# ---------------------------------------------------------------------------

_ai_rate_buckets: dict[str, list[float]] = {}


def check_ai_rate_limit(user_id: str) -> tuple[bool, int]:
    """检查用户 AI 调用是否超限。返回 (allowed, retry_after_seconds)。"""
    window = settings.rate_limit_window_seconds
    limit = settings.rate_limit_defaults.get("ai", 20)
    now = time.time()
    bucket = [t for t in _ai_rate_buckets.get(user_id, []) if now - t < window]
    if len(bucket) >= limit:
        _ai_rate_buckets[user_id] = bucket
        retry_after = int(window - (now - bucket[0])) + 1
        return False, retry_after
    bucket.append(now)
    _ai_rate_buckets[user_id] = bucket
    return True, 0


# ---------------------------------------------------------------------------
# 全书大纲 + 第一章 / 生成下一章 / 重新生成 / 扩写 / 润色（真实 + mock 双模式）
# ---------------------------------------------------------------------------


def _setting_brief(setting: "NovelGenerateRequest") -> str:
    """把创作设定拼成一段文本，供 prompt 复用。"""
    return (
        f"作品标题：{setting.title}\n"
        f"小说类型：{setting.novel_type or '未设定'}\n"
        f"题材：{setting.genre or '未设定'}\n"
        f"世界观：{setting.world_view or '未设定'}\n"
        f"时代背景：{setting.era or '未设定'}\n"
        f"主要人物：{setting.characters or '未设定'}\n"
        f"人物关系：{setting.relationships or '未设定'}\n"
        f"故事主线：{setting.main_plot or '未设定'}\n"
        f"叙事视角：{setting.perspective or '未设定'}\n"
        f"文风：{setting.writing_style or '未设定'}\n"
        f"其他创作要求：{setting.requirements or '无'}"
    )


async def ai_generate_outline_first(
    setting: "NovelGenerateRequest",
    real: bool,
    outline: str = "",
    override: dict | None = None
) -> tuple[str, str]:
    """生成「全书大纲 + 第一章」。返回 (outline, first_chapter_text)。

    若用户已提供大纲（setting.outline / outline 参数），必须以该大纲为骨架生成
    分卷与章节大纲，禁止脱离用户大纲自由发挥。
    """
    user_outline = (outline or (setting.outline or "")).strip()
    outline_guide = (
        f"\n\n【用户已提供的大纲（必须严格遵守）】\n{user_outline[:8000]}\n"
        "以上是用户已确定的全书大纲：生成分卷与章节大纲时必须逐条保留其情节走向、"
        "章节顺序与关键事件，只能补充细化，禁止改写、删减或另起炉灶。"
        if user_outline else ""
    )
    if real:
        # 2026-11-16 卷级骨架：章节数超阈值时大纲只出卷级结构，
        # 否则 400 章大纲文本必然触顶截断（实测 502）。
        skeleton_mode = setting.chapter_count > _PLAN_SKELETON_CHAPTER_LIMIT
        skeleton_note = (
            "\n6. 本次只生成卷级大纲：每卷给出卷名、覆盖章节范围（如「第 1~80 章」）"
            "与卷内核心事件链、卷末钩子；严禁逐章列出标题与要点（章节明细将分批生成）。"
            if skeleton_mode else ""
        )
        outline = await chat_with_ai(
            system_prompt=(
                "你是一名资深网络小说策划编辑。请根据用户的创作设定，生成全书大纲。\n"
                "要求：\n"
                "1. 输出结构清晰的分卷大纲：每卷给出卷名与核心内容，卷下列出各章标题与本章要点。\n"
                "2. 只输出大纲本身，不要输出解释或元信息。\n"
                "3. 章节数参考用户设定的章节数量，可适当分卷。\n"
                "4. 大纲必须贴合用户提供的主线、人物与世界观，禁止脱离设定自由发挥。\n"
                "5. 若用户已提供大纲，必须严格按其情节走向与章节顺序展开，禁止改变用户大纲。"
                + skeleton_note
            ),
            user_prompt=(
                f"{_setting_brief(setting)}\n"
                f"全书计划章节数：约 {setting.chapter_count} 章，每章 {setting.words_per_chapter} 字左右。\n"
                f"{outline_guide}\n\n"
                "请生成全书大纲。"
            ),
            override=override,
            task_type=TaskType.OUTLINE_PLAN.value,
        )
        first = await chat_with_ai_novel(setting, index=1, total=setting.chapter_count, override=override)
    else:
        rng = random.Random(
            (hash((setting.title, setting.main_plot, setting.characters)) & 0xFFFF)
            ^ int(time.time() * 1000) % 999983
        )
        genre = setting.genre or "故事"
        main_plot = setting.main_plot or "一段跌宕起伏的冒险"
        characters = setting.characters or "主角"
        world_view = setting.world_view or "一个独特的世界"
        c = _split_names(characters)
        hero = c[0] if c else "主角"
        total_chapters = max(1, setting.chapter_count)
        vol_count = max(1, (total_chapters + 2) // 3)
        vol_lines: list[str] = []
        for v in range(1, vol_count + 1):
            ch_start = (v - 1) * 3 + 1
            ch_end = min(v * 3, total_chapters)
            vol_lines.append(
                f"第{_cn_num(v)}卷：{_pick(rng, ['开端', '转折', '高潮', '收束', '余波'])}（第{ch_start}-{ch_end}章）\n"
                f"  本卷核心：围绕{main_plot}推进，{hero}与对手的冲突逐步升级，{world_view}的规则逐渐展开。"
            )
        outline = (
            f"《{setting.title}》全书大纲（演示生成，共约 {total_chapters} 章）\n\n"
            + "\n\n".join(vol_lines)
            + "\n\n（以上为演示生成内容，配置 DEEPSEEK_API_KEY 后可切换真实 AI 模式）"
        )
        first = generate_mock_novel(setting, index=1, total=total_chapters)
    # 生成记录复盘：大纲提示词在返回前统一写入 contextvar（真实/演示均生效），
    # 保证 outline 记录记录的是「大纲生成」的提示词，而非随后章节生成覆盖的提示词。
    _set_last_ai_prompt(
        (
            "你是一名资深网络小说策划编辑。请根据用户的创作设定，生成全书大纲。\n"
            "要求：\n"
            "1. 输出结构清晰的分卷大纲：每卷给出卷名与核心内容，卷下列出各章标题与本章要点。\n"
            "2. 只输出大纲本身，不要输出解释或元信息。\n"
            "3. 章节数参考用户设定的章节数量，可适当分卷。\n"
            "4. 大纲必须贴合用户提供的主线、人物与世界观，禁止脱离设定自由发挥。\n"
            "5. 若用户已提供大纲，必须严格按其情节走向与章节顺序展开，禁止改变用户大纲。"
        ),
        (
            f"{_setting_brief(setting)}\n"
            f"全书计划章节数：约 {setting.chapter_count} 章，每章 {setting.words_per_chapter} 字左右。\n"
            f"{outline_guide}\n\n"
            "请生成全书大纲。"
        ),
    )
    return outline, first


# 单次输出目标字数超过该阈值时自动分段生成。
# 2026-11-16 实测：DeepSeek max output 硬上限 8192 tokens，中文约 1 字 2.5 token，
# 3500+ 字一次生成必然触顶截断（502 ai_output_truncated），必须分段续写再拼接。
_SEGMENT_MIN_WORDS = 3000
# 单段最大目标字数：2800×2.5=7000 tokens，留 8192 上限约 15% 安全余量；
# 目标字数更大时自动切成 N 段（8000 字 = 3 段）。
_SEGMENT_MAX_WORDS = 2800
# 规划章节数超过该值时，plan-novel 只生成书级+卷级骨架（章节明细走批量生成）。
# 倒算：骨架约 2000 字 + 每章明细约 60 字，20 章 ≈ 3200 字 ≈ 8000 tokens，已达单次安全上限。
_PLAN_SKELETON_CHAPTER_LIMIT = 20


def _next_chapter_default_prompts(
    setting: "NovelGenerateRequest",
    target_words: int,
    *,
    outline: str,
    previous_summary: str,
    character_state: str,
    chapter_index: int,
    previous_chapter: str,
    continuation_tail: str | None = None,
    written_words: int = 0,
    hard_limit: int | None = None,
    final_segment: bool = True,
) -> tuple[str, str]:
    """构造「生成下一章」的默认 system/user 提示词。

    continuation_tail 非空表示分段生成的后续段：提示词切换为「接续已写内容」，
    要求不输出标题、不重复已写内容。final_segment=True（末段）要求写完本章并
    给出章末钩子；False（中间段）要求写到自然停顿处即停止。
    written_words / hard_limit 携带已写字数与全章硬上限，用于收敛总字数。
    """
    if continuation_tail:
        limit_note = (
            f"全章总字数严禁超过 {hard_limit} 字，写满必须立即收尾。"
            if hard_limit else ""
        )
        close_rule = (
            "4. 必须严格按【全书大纲】推进本章情节，写完本章并给出章末钩子。\n"
            if final_segment else
            "4. 必须严格按【全书大纲】推进本章情节，写到剧情中段自然停顿处即停止，"
            "不要写本章结尾、不要写章末悬念钩子，最后一句不要是收束语。\n"
        )
        system_prompt = (
            "你是一名资深网络小说作家。请接续已写内容，续写本章。\n"
            "要求：\n"
            "1. 只输出小说正文本身，不要输出标题、解释或元信息（标题已给出）。\n"
            "2. 从断点处自然接续，严禁重复或改写已写内容，严禁重述已写情节；"
            "绝对禁止从本章开头重新写起，禁止输出「第X章」标题行。\n"
            "3. 保持人物性格、叙事视角与文风一致，与前文浑然一体。\n"
            + close_rule
            + "5. 正文使用简体中文，按段落换行。"
            + _words_constraint_block(target_words)
            + (f"\n{limit_note}" if limit_note else "")
        )
        user_prompt = (
            f"{_setting_brief(setting)}\n\n"
            f"全书大纲（必须严格遵守，本章只写大纲中当前章对应的情节）：\n{outline[:8000] or '未提供'}\n\n"
            f"前文摘要：\n{previous_summary[:3000] or '未提供'}\n\n"
            f"人物当前状态：\n{character_state[:3000] or '未提供'}\n\n"
            f"已写好的本章上半段末尾（接续依据）：\n{continuation_tail}\n\n"
            f"本章已写 {written_words} 字，本次续写剩余部分，目标字数：{target_words} 字"
            f"（正文必须落在目标 ±15% 内，以目标值为准收尾）。{limit_note}\n"
            "请从上半段最后一句之后继续写，严禁重复最后一段。"
        )
        return system_prompt, user_prompt

    system_prompt = (
        "你是一名资深网络小说作家。请根据创作设定与上下文，续写下一章小说正文。\n"
        "要求：\n"
        "1. 输出格式：第一行必须是本章标题，严格采用「第X章：简短标题」形式"
        "（X 为本章序号，标题本体 2~20 个字的名词性短语，不得是完整句子或描述句，"
        "正确示例「第5章：暗流涌动」，错误示例「第五章 主角继续调查发现事情并不简单」）；"
        "从第二行开始才是正文内容，正文不要重复标题。\n"
        "2. 只输出小说正文本身，不要输出解释、大纲或元信息。\n"
        "3. 承接前文摘要与人物状态，保持人物性格、叙事视角与文风一致。\n"
        "4. 必须严格按【全书大纲】推进本章情节，本章事件、冲突与结尾悬念必须对应当前章在大纲中的安排，禁止偏离大纲另起情节。\n"
        "5. 正文使用简体中文，按段落换行。"
        + _words_constraint_block(target_words)
    )
    user_prompt = (
        f"{_setting_brief(setting)}\n\n"
        f"全书大纲（必须严格遵守，本章只写大纲中当前章对应的情节）：\n{outline[:8000] or '未提供'}\n\n"
        f"前文摘要：\n{previous_summary[:3000] or '未提供'}\n\n"
        f"人物当前状态：\n{character_state[:3000] or '未提供'}\n\n"
        f"上一章结尾：\n{previous_chapter[-1200:]}\n\n"
        f"本次要写第 {chapter_index} 章，本章目标字数：{target_words} 字"
        f"（正文必须落在目标 ±15% 内，以目标值为准收尾）。\n"
        "请开始续写正文。"
    )
    return system_prompt, user_prompt


async def _generate_next_chapter_segmented(
    setting: "NovelGenerateRequest",
    *,
    outline: str,
    previous_summary: str,
    character_state: str,
    chapter_index: int,
    previous_chapter: str,
    system_prompt: str | None,
    user_prompt: str | None,
    override: dict | None,
) -> str:
    """长章节分段生成（2026-11-16 改版：固定两段 → 自动 N 段循环）。

    按单段安全上限把目标字数切成 N 段依次生成：首段写至自然停顿，中间段接续至
    自然停顿，末段收尾并给章末钩子；每段目标按「剩余字数 ÷ 剩余段数」动态均分，
    全章总字数收敛在 hard_limit 内，拼接后另有智能截断兜底。
    标题只在首段出现（默认路径含「第X章」首行；ctx 路径本来就不输出标题）。
    """
    total = max(1, setting.words_per_chapter)
    n_segs = max(2, math.ceil(total / _SEGMENT_MAX_WORDS))
    hard_limit = int(total * 1.15)
    texts: list[str] = []

    for seg in range(n_segs):
        is_last = seg == n_segs - 1
        written = sum(count_words(t) for t in texts)
        segs_left = n_segs - seg
        seg_target = max(1, (total - written) // segs_left)
        tail = texts[-1][-900:] if texts else None

        if seg == 0:
            pause_note = (
                f"\n分段生成指示（第 1/{n_segs} 段）：本次只写本章第 1 段，目标 {seg_target} 字。"
                "写到剧情中段一个自然的停顿处（如场景转换前、对话告一段落）即停止，"
                "不要写本章结尾、不要写章末悬念钩子，最后一句不要是收束语。"
            )
            if system_prompt and user_prompt:
                sys_i = system_prompt + pause_note
                usr_i = user_prompt
            else:
                sys_i, usr_i = _next_chapter_default_prompts(
                    setting,
                    seg_target,
                    outline=outline,
                    previous_summary=previous_summary,
                    character_state=character_state,
                    chapter_index=chapter_index,
                    previous_chapter=previous_chapter,
                )
                sys_i += pause_note
        elif is_last:
            final_note = (
                f"\n分段生成指示（最后一段）：本章已写 {written} 字，本次续写剩余部分，目标 {seg_target} 字。"
                "从断点处自然接续，严禁重复或改写已写内容，写完本章并给出章末钩子。"
                f"全章总字数严禁超过 {hard_limit} 字，写满必须立即收尾。"
            )
            if system_prompt and user_prompt:
                sys_i = system_prompt + final_note
                usr_i = user_prompt + (
                    f"\n\n已写内容末尾（接续依据）：\n{tail}\n\n"
                    f"本章已写 {written} 字，剩余目标 {seg_target} 字，"
                    f"全章总字数严禁超过 {hard_limit} 字。"
                    "请从最后一句之后继续写，严禁重复最后一段。"
                )
            else:
                sys_i, usr_i = _next_chapter_default_prompts(
                    setting,
                    seg_target,
                    outline=outline,
                    previous_summary=previous_summary,
                    character_state=character_state,
                    chapter_index=chapter_index,
                    previous_chapter=previous_chapter,
                    continuation_tail=tail,
                    written_words=written,
                    hard_limit=hard_limit,
                    final_segment=True,
                )
        else:
            mid_note = (
                f"\n分段生成指示（第 {seg + 1}/{n_segs} 段）：本章已写 {written} 字，"
                f"本次续写第 {seg + 1} 段，目标 {seg_target} 字。"
                "从断点处自然接续，严禁重复或改写已写内容，写到剧情中段自然停顿处即停止，"
                "不要写本章结尾、不要写章末悬念钩子，最后一句不要是收束语。"
            )
            if system_prompt and user_prompt:
                sys_i = system_prompt + mid_note
                usr_i = user_prompt + (
                    f"\n\n已写内容末尾（接续依据）：\n{tail}\n\n"
                    "请从最后一句之后继续写，严禁重复最后一段。"
                )
            else:
                sys_i, usr_i = _next_chapter_default_prompts(
                    setting,
                    seg_target,
                    outline=outline,
                    previous_summary=previous_summary,
                    character_state=character_state,
                    chapter_index=chapter_index,
                    previous_chapter=previous_chapter,
                    continuation_tail=tail,
                    written_words=written,
                    hard_limit=hard_limit,
                    final_segment=False,
                )

        text_i = await chat_with_ai(
            system_prompt=sys_i,
            user_prompt=usr_i,
            max_tokens=_words_target_tokens(seg_target),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        texts.append(text_i)

    # ---- 拼接 + 智能截断兜底：超长时按段落/句子边界收敛到目标上限 ----
    merged = "\n\n".join(t.rstrip().lstrip() for t in texts)
    head, _, body = merged.partition("\n")
    if re.search(r"第\s*\d+\s*章", head):
        if count_words(body) > hard_limit:
            body = _smart_truncate(body, hard_limit)
        merged = head + "\n\n" + body
    elif count_words(merged) > hard_limit:
        merged = _smart_truncate(merged, hard_limit)
    # 末尾没写完（如模型/网关误停）→ 自动补写一次收尾（2026-11-17「章节不全」修复）
    return await _maybe_complete_ending(merged, override)


async def ai_generate_next_chapter(
    setting: "NovelGenerateRequest",
    real: bool,
    *,
    outline: str = "",
    previous_summary: str = "",
    character_state: str = "",
    chapter_index: int = 1,
    previous_chapter: str = "",
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    override: dict | None = None
) -> str:
    """生成下一章：携带全书大纲、前文摘要、人物状态与上一章结尾，基于上下文续写。

    第二期「章节能被记住」：路由层可用 Context Builder 组装好的 system_prompt /
    user_prompt 覆盖默认提示词（真实模式生效），使续写上下文优先引用已确认章节记忆。
    """
    if real:
        target = max(1, setting.words_per_chapter)
        if target > _SEGMENT_MIN_WORDS:
            return await _generate_next_chapter_segmented(
                setting,
                outline=outline,
                previous_summary=previous_summary,
                character_state=character_state,
                chapter_index=chapter_index,
                previous_chapter=previous_chapter,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                override=override,
            )
        if system_prompt and user_prompt:
            content = await chat_with_ai(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=_words_target_tokens(setting.words_per_chapter),
                override=override,
                task_type=TaskType.NOVEL_BODY.value,
            )
            return await _maybe_complete_ending(content, override)
        sys_prompt, usr_prompt = _next_chapter_default_prompts(
            setting,
            target,
            outline=outline,
            previous_summary=previous_summary,
            character_state=character_state,
            chapter_index=chapter_index,
            previous_chapter=previous_chapter,
        )
        content = await chat_with_ai(
            system_prompt=sys_prompt,
            user_prompt=usr_prompt,
            max_tokens=_words_target_tokens(setting.words_per_chapter),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        return await _maybe_complete_ending(content, override)
    total = max(1, setting.chapter_count)
    return generate_mock_novel(
        setting,
        index=max(1, chapter_index),
        total=total,
        previous_chapter=previous_chapter or None,
    )


async def ai_regenerate_chapter(
    setting: "NovelGenerateRequest",
    real: bool,
    *,
    chapter_title: str = "",
    chapter_content: str = "",
    instruction: str = "",
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    override: dict | None = None,
    outline: str = "",
    previous_chapter_tail: str = "",
    next_chapter_head: str = "",
) -> str:
    """重新生成本章：基于设定重写该章正文（可携带用户额外指令）。

    第二期「章节能被记住」：路由层可用 Context Builder 组装好的 system_prompt /
    user_prompt 覆盖默认提示词（真实模式生效），使重写承接已确认人物状态与伏笔约束。
    2026-11-17：新增 outline / previous_chapter_tail / next_chapter_head——
    重写必须与上一章结尾、本章大纲、下一章开头衔接（此前重写与前后章完全脱节）。
    """
    # 前后章衔接约束块：默认路径与 ctx 路径共用（ctx 提示词末尾追加）
    continuity_block = ""
    if (previous_chapter_tail or "").strip():
        continuity_block += (
            f"\n\n上一章结尾（本章第一段必须从这里无缝承接，场景/情绪/事件不得冲突）：\n"
            f"{previous_chapter_tail.strip()[-2000:]}"
        )
    if (outline or "").strip():
        continuity_block += (
            f"\n\n本章大纲（重写必须严格按大纲推进本章情节，不得另起情节）：\n{outline.strip()[:6000]}"
        )
    if (next_chapter_head or "").strip():
        continuity_block += (
            f"\n\n下一章开头（本章结尾与章末钩子必须为它做好铺垫，直接引向该情节）：\n"
            f"{next_chapter_head.strip()[:2000]}"
        )
    if real:
        # 2026-11-16 修正：重写输出长度应接近原文，配额按「设定字数与原文长度取大者」计算；
        # 否则设定 800 字但原文 3500+ 字时必然截断（实测 502 ai_output_truncated）。
        rewrite_target = max(setting.words_per_chapter, count_words(chapter_content))
        if system_prompt and user_prompt:
            content = await chat_with_ai(
                system_prompt=system_prompt,
                user_prompt=user_prompt + continuity_block,
                max_tokens=_words_target_tokens(rewrite_target),
                override=override,
                task_type=TaskType.NOVEL_BODY.value,
            )
            return await _maybe_complete_ending(content, override)
        content = await chat_with_ai(
            system_prompt=(
                "你是一名资深网络小说作家。请根据创作设定，重新生成指定章节的正文。\n"
                "要求：\n"
                "1. 输出格式：第一行必须是本章标题，严格采用「第X章：简短标题」形式"
                "（X 为本章序号，标题本体 2~20 个字的名词性短语，不得是完整句子或描述句，"
                "正确示例「第3章：裂痕」，错误示例「第三章 她在雨夜里独自前行想起了很多往事」）；"
                "从第二行开始才是正文内容，正文不要重复标题。\n"
                "2. 只输出小说正文本身，不要输出解释或元信息。\n"
                "3. 尊重原章节标题与剧情位置，但正文应重新创作，不要照抄原文。\n"
                "4. 保持人物性格、叙事视角与文风一致，结尾留钩子。\n"
                "5. 正文使用简体中文，按段落换行。\n"
                "6. 前后章衔接硬约束（2026-11-17）：若提供了上一章结尾，本章第一段必须"
                "从上一章结尾无缝承接（延续同一场景或立即交代后续动作），不得凭空开场；"
                "若提供了下一章开头，本章结尾的钩子必须直接为下一章开头的情节做铺垫；"
                "若提供了本章大纲，必须严格按大纲写本章情节。"
                + _words_constraint_block(setting.words_per_chapter)
            ),
            user_prompt=(
                f"{_setting_brief(setting)}{_locked_block(setting)}\n\n"
                f"章节标题：{chapter_title or '未提供'}\n"
                f"原正文（仅作情节参考，不要照抄）：\n{chapter_content[-6000:] or '未提供'}\n\n"
                f"重新生成要求：{instruction or '保持原有情节走向，写得更有张力'}\n"
                f"本章目标字数：{rewrite_target} 字"
                f"（正文必须落在目标 ±15% 内，以目标值为准收尾）。"
                + continuity_block
                + "\n\n请开始重新生成正文。"
            ),
            max_tokens=_words_target_tokens(rewrite_target),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
        return await _maybe_complete_ending(content, override)
    total = max(1, setting.chapter_count)
    return generate_mock_novel(setting, index=1, total=total)


async def ai_expand_chapter(
    setting: "NovelGenerateRequest",
    real: bool,
    *,
    chapter_title: str = "",
    chapter_content: str = "",
    instruction: str = "",
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    override: dict | None = None,
    outline: str = "",
    previous_chapter_tail: str = "",
    next_chapter_head: str = "",
) -> str:
    """扩写本章：保留原文情节，扩充细节，返回扩写后的完整章节正文。

    第二期「章节能被记住」：路由层可用 Context Builder 组装好的 system_prompt /
    user_prompt 覆盖默认提示词（真实模式生效），使扩写不偏离已确认人物状态与伏笔约束。
    2026-11-17：新增前后章衔接字段——扩写不得改变本章开头承接与结尾铺垫。
    """
    continuity_block = ""
    if (previous_chapter_tail or "").strip():
        continuity_block += (
            f"\n\n上一章结尾（扩写不得改变本章开头的承接关系）：\n{previous_chapter_tail.strip()[-2000:]}"
        )
    if (outline or "").strip():
        continuity_block += f"\n\n本章大纲（扩写不得偏离大纲情节）：\n{outline.strip()[:6000]}"
    if (next_chapter_head or "").strip():
        continuity_block += (
            f"\n\n下一章开头（扩写不得改变本章结尾为其铺垫的关系）：\n{next_chapter_head.strip()[:2000]}"
        )
    if real:
        if system_prompt and user_prompt:
            return await chat_with_ai(
                system_prompt=system_prompt,
                user_prompt=user_prompt + continuity_block,
                max_tokens=_words_target_tokens(
                    max(setting.words_per_chapter, count_words(chapter_content))
                ),
                override=override,
                task_type=TaskType.NOVEL_BODY.value,
            )
        return await chat_with_ai(
            system_prompt=(
                "你是一名资深网络小说作家。请对用户提供的章节正文进行扩写。\n"
                "要求：\n"
                "1. 输出格式：第一行必须是本章标题，严格采用「第X章：简短标题」形式"
                "（X 为本章序号，标题本体 2~20 个字的名词性短语，不得是完整句子或描述句，"
                "正确示例「第4章：险境」，错误示例「第四章 他们来到那座废弃工厂发现里面藏着惊天秘密」）；"
                "从第二行开始才是扩写后的完整正文，正文不要重复标题。\n"
                "2. 只输出扩写后的完整章节正文，不要输出解释或元信息。\n"
                "3. 保留原文全部关键情节、对话与设定，不改变剧情走向。\n"
                "4. 扩充环境描写、动作细节、心理活动与对话，使内容更丰满。\n"
                "5. 保持人物性格、叙事视角与文风一致，正文使用简体中文按段落换行。"
            ),
            user_prompt=(
                f"{_setting_brief(setting)}{_locked_block(setting)}\n\n"
                f"章节标题：{chapter_title or '未提供'}\n"
                f"需要扩写的原文：\n{chapter_content[:10000] or '未提供'}\n\n"
                f"扩写要求：{instruction or f'扩写到约 {setting.words_per_chapter} 字'}"
                + continuity_block
                + "\n请输出扩写后的完整章节正文。"
            ),
            max_tokens=_words_target_tokens(
                max(setting.words_per_chapter, count_words(chapter_content))
            ),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
    body = chapter_content.strip()
    title = chapter_title.strip() or f"第1章"
    rng = random.Random(int(time.time() * 1000) % 999983)
    genre = setting.genre or "故事"
    world_view = setting.world_view or "一个独特的世界"
    c = _split_names(setting.characters or "主角")
    hero = c[0] if c else "主角"
    extra = (
        f"\n\n{hero}环顾四周，{world_view}的风物在眼前铺展。关于{genre}的一切似乎都比想象中更复杂，"
        f"他/她定了定神，决定先把眼前的线索整理清楚。"
        f"\n\n（以上为演示扩写内容，配置 DEEPSEEK_API_KEY 后可切换真实 AI 模式）"
    )
    return f"{title}\n\n{body}{extra if body else ''}"


async def ai_polish_chapter(
    setting: "NovelGenerateRequest",
    real: bool,
    *,
    chapter_title: str = "",
    chapter_content: str = "",
    style: str = "",
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    override: dict | None = None
) -> str:
    """润色本章：按目标风格优化语言表达，返回润色后的完整章节正文。

    第二期「章节能被记住」：路由层可用 Context Builder 组装好的 system_prompt /
    user_prompt 覆盖默认提示词（真实模式生效），使润色保持人物性格与叙事视角一致。
    """
    if real:
        if system_prompt and user_prompt:
            return await chat_with_ai(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=_words_target_tokens(max(1, count_words(chapter_content))),
                override=override,
                task_type=TaskType.NOVEL_BODY.value,
            )
        return await chat_with_ai(
            system_prompt=(
                "你是一名资深小说编辑。请对用户提供的章节正文进行润色。\n"
                "要求：\n"
                "1. 输出格式：第一行必须是本章标题，严格采用「第X章：简短标题」形式"
                "（X 为本章序号，标题本体 2~20 个字的名词性短语，不得是完整句子或描述句，"
                "正确示例「第7章：风暴前夜」，错误示例「第七章 所有人的命运在今晚迎来转折谁都没想到」）；"
                "从第二行开始才是润色后的完整正文，正文不要重复标题。\n"
                "2. 只输出润色后的完整章节正文，不要输出解释或元信息。\n"
                "3. 保留原文情节、对话与设定，只优化语言表达与节奏。\n"
                "4. 严格按照用户指定的风格润色，保持人物性格与叙事视角一致。\n"
                "5. 正文使用简体中文，按段落换行。\n"
                "6. 字数约束：润色必须保持字数与原文接近，不得大幅扩写或缩写，"
                "正文总字数波动控制在 ±15% 以内。"
            ),
            user_prompt=(
                f"{_setting_brief(setting)}{_locked_block(setting)}\n\n"
                f"章节标题：{chapter_title or '未提供'}\n"
                f"需要润色的原文：\n{chapter_content[:10000] or '未提供'}\n\n"
                f"原文正文约 {count_words(chapter_content)} 字，润色后请保持字数接近（波动 ±15% 内）。\n"
                f"目标风格：{style or '语言流畅、表达精准、富有画面感'}\n"
                "请输出润色后的完整章节正文。"
            ),
            max_tokens=_words_target_tokens(max(1, count_words(chapter_content))),
            override=override,
            task_type=TaskType.NOVEL_BODY.value,
        )
    body = chapter_content.strip()
    title = chapter_title.strip() or f"第1章"
    return (
        f"{title}\n\n{body}\n\n"
        f"（以上为演示润色内容，配置 DEEPSEEK_API_KEY 后可切换真实 AI 模式）"
    )


# ---------------------------------------------------------------------------
# 灵感 → AI 规划 → 确认锁定 → 逐章生成正文（规划 / 局部修改，真实 + mock 双模式）
# ---------------------------------------------------------------------------


def _idea_fragment(idea: str, length: int = 10) -> str:
    """从灵感文本提取核心片段（去标点空白），供 mock 规划填充。"""
    cleaned = "".join(ch for ch in idea if ch not in "，。！？、；：""''《》【】\n\t ")
    cleaned = cleaned.strip()
    if not cleaned:
        return "无名灵感"
    return cleaned[:length]


def _cn_number(n: int) -> str:
    """数字转中文（用于 mock 章节/分卷名）。"""
    if n <= 0:
        return str(n)
    if n <= 10:
        return "一二三四五六七八九十"[n - 1]
    if n < 20:
        return "十" + ("一二三四五六七八九"[n - 11] if n > 10 else "")
    return str(n)


def _flatten_chapters(plan: "NovelPlan") -> list["NovelChapterPlan"]:
    """把分卷内章节展平为顺序列表（全局 index 从 1 开始）。"""
    out: list["NovelChapterPlan"] = []
    for vol in plan.volumes:
        for ch in vol.chapters:
            out.append(ch)
    return out


def _mock_plan_from_idea(
    *,
    idea: str,
    genre: str,
    writing_style: str,
    perspective: str,
    chapter_count: int,
    words_per_chapter: int,
    ending_type: str,
    has_romance: bool,
    content_restrictions: str,
    must_keep_settings: str,
    existing_settings: dict | None,
) -> "NovelPlan":
    """演示模式：根据灵感与可选设置构造完整规划（每次结果随灵感/设置/时间变化）。"""
    total = max(1, chapter_count)
    rng = random.Random(
        (hash((idea, genre, ending_type, str(has_romance), str(total))) & 0xFFFFF)
        ^ int(time.time() * 1000) % 999983
    )
    frag = _idea_fragment(idea, 12)
    genre_text = genre or "都市悬疑"
    perspective_text = perspective or "第三人称"
    style_text = writing_style or "冷峻写实，富有画面感"
    ending_text = ending_type or "开放式结局"
    romance_text = "，并埋设一条贯穿全程的感情线" if has_romance else ""
    keep_text = must_keep_settings or "无特别必须保留项"
    restrict_text = content_restrictions or "无特别内容限制"

    # 书名候选
    title_candidates = [
        f"《{frag}》",
        f"《{frag}：记忆迷宫》",
        f"《{genre_text}档案·{frag}》",
        f"《藏在{frag}背后的真相》",
    ]

    hero = "主角"
    if existing_settings and isinstance(existing_settings, dict):
        hero = str(existing_settings.get("protagonist_name") or existing_settings.get("hero") or hero)
    support_names = ["挚友", "搭档", "对手"]
    foe = "神秘对手"

    selling_point = f"以「{frag}」为引，讲述一段{genre_text}故事：开局即悬念，{ending_text}，全程高能反转{romance_text}。"

    synopsis = (
        f"灵感源于：{idea.strip()[:80]}。在{perspective_text}视角下，{hero}无意间卷入与「{frag}」有关的漩涡，"
        f"必须{('与' + foe + '周旋') if foe else '查明真相'}。随着调查深入，表面的线索都指向一个更深的秘密，"
        f"而{hero}的每一次选择都在改写结局。{ending_text}。"
    )

    world_view = (
        f"世界观：以{genre_text}为基调的现实/架空混合世界。核心规则围绕「{frag}」展开："
        f"{must_keep_settings or '该设定由灵感自然生长，后续章节不得推翻已确认部分'}。"
        f"{'限定内容：' + content_restrictions if content_restrictions else ''}"
    )

    era = "当代都市"
    core_conflict = f"围绕「{frag}」的真相之争：{hero}追寻答案，而{foe}试图让秘密永远沉默，两者正面交锋。"

    protagonist = (
        f"{hero}：故事的绝对视角人物，性格敏锐、执拗，对「{frag}」有着无法解释的执念。"
        f"小传：表面过着普通生活，实则背负一段被抹去的记忆，一切从某个雨夜开始改变。"
    )

    supporting_characters = [
        {"name": "苏婉", "role": "配角", "description": f"{hero}的多年好友兼线人，掌握关键线索却语焉不详，立场成谜。"},
        {"name": "老周", "role": "配角", "description": "刑警队长，表面中立，暗中对「{frag}」的调查多次出手干预。"},
        {"name": foe, "role": "反派配角", "description": f"与「{frag}」直接相关，身份隐藏极深，直到后期才逐步浮出水面。"},
    ]

    relationships = (
        f"{hero} 与 苏婉：信任与试探并存；{hero} 与 老周：互相利用的微妙关系；"
        f"{hero} 与 {foe}：追查与被追查的宿命对手。"
    )

    main_plot = (
        f"主线：{hero}追查「{frag}」背后的真相，先后遭遇线索被毁、证人失踪、记忆干扰等障碍，"
        f"最终与{foe}正面对决，揭开被抹去的记忆与整件事的真相。{ending_text}。"
    )

    # 分卷与章节大纲
    vol_count = max(1, (total + 3) // 4)
    vol_names = ["缘起", "迷雾", "交锋", "真相", "终章", "余波"]
    volumes: list[dict] = []
    chapter_flat: list[dict] = []
    idx = 0
    for v in range(1, vol_count + 1):
        ch_start = idx + 1
        ch_end = min(v * 4, total)
        if ch_end < ch_start:
            ch_end = ch_start
        vol_chapters: list[dict] = []
        for i in range(ch_start, ch_end + 1):
            idx += 1
            phase = (
                "铺垫" if i <= max(1, total // 4)
                else "推进" if i <= total * 3 // 4
                else "高潮收束"
            )
            chapter = {
                "index": idx,
                "title": normalize_chapter_title(_pick(rng, ['风声', '线索', '缺口', '试探', '谜团', '阴影', '裂痕', '倒计时']), idx),
                "goal": f"本章目标：推进「{frag}」主线，{hero}获得一条关键线索并锁定下一步行动（{phase}）。",
                "conflict": f"冲突：{hero}的调查触碰{foe}的防线，遭遇阻挠或误导，双方首次/再次正面交锋。",
                "key_events": f"关键事件：{_pick(rng, ['发现匿名纸条', '证人临时改口', '旧档案突然消失', '收到一封警告信', '记忆片段闪回', '尾随者现身'])}；{hero}与{('苏婉' if idx % 2 else '老周')}就线索产生分歧。",
                "cliffhanger": f"结尾悬念：{_pick(rng, ['门外响起三下敲门声', '手机收到一条只有坐标的短信', '黑暗中有人叫出了{hero}的真名', '档案末尾出现{hero}自己的照片', '电话那头传来熟悉又陌生的声音'])}",
            }
            vol_chapters.append(chapter)
            chapter_flat.append(chapter)
        volumes.append({
            "volume_index": v,
            "title": f"第{_cn_number(v)}卷 {_pick(rng, vol_names)}",
            "summary": f"本卷核心：{hero}围绕「{frag}」的追查进入{'铺陈' if v == 1 else '白热化'}阶段，冲突升级，关键人物接连登场。",
            "chapters": vol_chapters,
        })

    # 伏笔与回收
    foreshadowing: list[dict] = []
    for f_idx in range(1, min(5, total) + 1):
        planted = max(1, min(total - 1, f_idx * 2 - 1))
        payoff = max(planted + 1, min(total, planted + 3))
        foreshadowing.append({
            "item": f"伏笔{f_idx}：{'被抹去的记忆碎片' if f_idx == 1 else ('苏婉的立场' if f_idx == 2 else ('老周的干预动机' if f_idx == 3 else '「' + frag + '」的真正含义'))}",
            "planted_chapter": planted,
            "payoff_chapter": payoff,
            "note": f"第{planted}章埋设，第{payoff}章回收，用于支撑主线反转。",
        })

    return NovelPlan(
        title_candidates=title_candidates,
        selling_point=selling_point,
        synopsis=synopsis,
        world_view=world_view,
        era=era,
        core_conflict=core_conflict,
        protagonist=protagonist,
        supporting_characters=[CharacterProfile(**c) for c in supporting_characters],
        relationships=relationships,
        main_plot=main_plot,
        volumes=[NovelVolumePlan(**v) for v in volumes],
        foreshadowing=[ForeshadowPlan(**f) for f in foreshadowing],
        ending_type=ending_text,
    )


_PLAN_SYSTEM_PROMPT = (
    "你是一名资深网络小说策划编辑与世界观架构师。请根据用户的灵感与可选设置，"
    "生成一份完整、可执行的创作规划。\n"
    "要求：\n"
    "1. 只输出一个合法的 JSON 对象，不要输出任何解释、注释或 Markdown 代码块标记。\n"
    "2. JSON 结构必须严格符合给定 Schema："
    "{title_candidates: string[]（3-4个书名候选）, selling_point: string（一句话卖点）, "
    "synopsis: string（故事简介）, world_view: string（世界观）, era: string（时代背景）, "
    "core_conflict: string（核心冲突）, protagonist: string（主角小传）, "
    "supporting_characters: [{name, role, description}]（配角小传）, "
    "relationships: string（人物关系）, main_plot: string（故事主线）, "
    "volumes: [{volume_index: number, title: string, summary: string, "
    "chapters: [{index: number（全局顺序从1开始）, title, goal, conflict, key_events, cliffhanger}]}], "
    "foreshadowing: [{item, planted_chapter: number, payoff_chapter: number, note}], "
    "ending_type: string}。\n"
    "3. 章节大纲必须覆盖用户要求的目标章节数，每章都要给出目标、冲突、关键事件、结尾悬念。\n"
    "4. 每个章节 title 必须严格采用「第X章：简短标题」形式：X 为该章全局序号（从 1 起递增），"
    "冒号后为 2~20 个字的名词性短语标题；严禁写成完整句子、描述句或带句号/逗号/问号/感叹号的长句。\n"
    "   正确示例：「第1章：风声渐起」「第5章：旧日残影」「第12章：真相一角」\n"
    "   错误示例：「第1章：主角在调查中发现线索并决定跟踪」（描述句）、"
    "「夜色越来越深，她决定明天再查」（句子且无章节号）、"
    "「第一章 讲述了女主来到新城市开始新生活的种种遭遇」（冗长句子）。\n"
    "5. 规划必须严格贴合灵感，禁止偏离用户明确给出的设定；用户已填写的详细设定只允许补空白，不允许改写。\n"
    "6. 伏笔必须「先埋后收」：foreshadowing 中每条 payoff_chapter 必须大于 planted_chapter，"
    "严禁回收章号早于或等于埋设章号。"
)


async def ai_plan_novel(
    *,
    idea: str,
    genre: str,
    writing_style: str,
    perspective: str,
    chapter_count: int,
    words_per_chapter: int,
    ending_type: str,
    has_romance: bool,
    content_restrictions: str,
    must_keep_settings: str,
    existing_settings: dict | None,
    real: bool,
    override: dict | None = None
) -> "NovelPlan":
    """根据灵感生成完整创作规划（real=真实模型，mock=本地动态模拟）。

    2026-11-16 两级生成：章节数超过 _PLAN_SKELETON_CHAPTER_LIMIT 时只生成
    书级+卷级骨架（chapters 空数组），章节明细由「章节计划批量生成」逐批补全——
    否则 400 章的规划 JSON 约 6-10 万字，单次 max_tokens 必然截断。
    """
    if real:
        skeleton_mode = chapter_count > _PLAN_SKELETON_CHAPTER_LIMIT
        settings_brief = ""
        if existing_settings and isinstance(existing_settings, dict):
            settings_brief = "\n".join(f"{k}：{v}" for k, v in existing_settings.items() if v)
        user_prompt = (
            f"灵感（核心）：{idea}\n\n"
            f"可选设置：\n"
            f"- 题材：{genre or '未指定'}\n"
            f"- 文风：{writing_style or '未指定'}\n"
            f"- 叙事视角：{perspective or '未指定'}\n"
            f"- 目标章节数：{chapter_count} 章\n"
            f"- 每章字数：约 {words_per_chapter} 字\n"
            f"- 结局类型：{ending_type or '未指定'}\n"
            f"- 感情线：{'需要' if has_romance else '不需要'}\n"
            f"- 内容限制：{content_restrictions or '无'}\n"
            f"- 必须保留设定：{must_keep_settings or '无'}\n"
            f"- 已填详细设定（只补空白，不得改写）：\n{settings_brief or '无'}\n\n"
            "请生成完整创作规划（JSON）。"
        )
        if skeleton_mode:
            user_prompt += (
                f"\n\n【重要】全书共 {chapter_count} 章，单次无法输出全部章节明细。"
                "本次只生成「卷级结构」：\n"
                "1. 输出除 volumes[].chapters 之外的完整规划（书名候选/卖点/简介/世界观/时代背景/"
                "核心冲突/主角小传/配角小传/人物关系/主线/伏笔回收/结局类型）；\n"
                f"2. 将全书 {chapter_count} 章合理划分为若干卷（建议 4~8 卷），"
                "每个卷的 chapters 必须输出空数组 []；\n"
                "3. 每个卷的 title 只写卷名本体（2~10 字名词短语，如「归京」「毒案」「公审」），"
                "严禁带「第N卷」前缀（前端会自行渲染卷序号）；"
                "summary 必须写清：本卷章节起止序号（如「第 41~80 章」）、卷内核心事件链、卷末钩子。"
            )
        plan_dict = await chat_with_ai_json_dict(
            _PLAN_SYSTEM_PROMPT,
            user_prompt,
            override=override,
            task_type=TaskType.OUTLINE_PLAN.value,
        )
        plan = NovelPlan(**_coerce_plan_dict(plan_dict))
        return _fix_foreshadow_order(_normalize_plan_titles(plan))
    return _mock_plan_from_idea(
        idea=idea,
        genre=genre,
        writing_style=writing_style,
        perspective=perspective,
        chapter_count=chapter_count,
        words_per_chapter=words_per_chapter,
        ending_type=ending_type,
        has_romance=has_romance,
        content_restrictions=content_restrictions,
        must_keep_settings=must_keep_settings,
        existing_settings=existing_settings,
    )


async def ai_plan_update(
    *,
    instruction: str,
    current_plan: "NovelPlan",
    locked: list[str],
    real: bool,
    override: dict | None = None
) -> tuple["NovelPlan", list[int]]:
    """局部修改规划：AI 只更新受影响的章节并返回 changed_chapter_ids，不得重置整本。"""
    if real:
        locked_text = "\n".join(f"- {s}" for s in locked) if locked else "无"
        user_prompt = (
            f"用户修改指令：{instruction}\n\n"
            f"当前完整规划（JSON）：\n{json.dumps(current_plan.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"已锁定设定（不得改变）：\n{locked_text}\n\n"
            "请只修改与指令相关的部分（优先只动受影响章节），其余内容原样保留，"
            "并返回 JSON 对象 {plan: <更新后的完整规划，结构与原规划一致>, "
            "changed_chapter_ids: <受影响的章节全局 index 数组，没改到的章节不要列入>}。\n"
            "只输出一个合法 JSON 对象，不要输出解释或 Markdown 代码块标记。"
        )
        result_dict = await chat_with_ai_json_dict(
            _PLAN_SYSTEM_PROMPT,
            user_prompt,
            override=override,
            task_type=TaskType.OUTLINE_PLAN.value,
        )
        if isinstance(result_dict.get("plan"), dict):
            result_dict["plan"] = _coerce_plan_dict(result_dict["plan"])
        result = PlanUpdateAIResult(**result_dict)
        return _normalize_plan_titles(result.plan), result.changed_chapter_ids

    # mock 启发式局部修改：解析「第N章」只刷新对应章；「增加/删除」调整章节；全局指令只改概要字段
    import re
    plan = current_plan.model_copy(deep=True)
    changed: list[int] = []
    flat = _flatten_chapters(plan)
    total = len(flat)
    m = re.search(r"第\s*([0-9一二三四五六七八九十百]+)\s*章", instruction)
    target: int | None = None
    if m:
        token = m.group(1)
        if token.isdigit():
            target = int(token)
        else:
            cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            target = sum(cn_map.get(ch, 0) for ch in token) or None
    if target and 1 <= target <= total:
        ch = flat[target - 1]
        note = f"（已按修改指令调整：{instruction.strip()[:60]}）"
        ch.goal = (ch.goal or "") + note
        ch.key_events = (ch.key_events or "") + note
        changed.append(target)
    elif re.search(r"(增加|新增|加).{0,4}(章|章节)|加.{0,4}章", instruction):
        # 追加章节
        new_idx = total + 1
        new_ch = NovelChapterPlan(
            index=new_idx,
            title=normalize_chapter_title("新线索", new_idx),
            goal=f"本章目标：按修改指令「{instruction.strip()[:40]}」扩展调查过程，引入新证据。",
            conflict=f"冲突：{plan.protagonist or '主角'}的新发现引来更多阻力，局势进一步复杂。",
            key_events=f"关键事件：依据指令「{instruction.strip()[:40]}」新增关键事件。",
            cliffhanger="结尾悬念：新线索指向一个此前从未提及的名字。",
        )
        flat.append(new_ch)
        if plan.volumes:
            plan.volumes[-1].chapters.append(new_ch)
        else:
            plan.volumes.append(NovelVolumePlan(volume_index=1, title="第1卷", summary="", chapters=[new_ch]))
        changed.append(new_idx)
    else:
        # 全局概要调整：不涉及具体章节
        tag = f"（已按修改指令调整：{instruction.strip()[:60]}）"
        plan.selling_point = (plan.selling_point or "") + tag
        plan.synopsis = (plan.synopsis or "") + tag
    return plan, changed
