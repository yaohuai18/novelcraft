# -*- coding: utf-8 -*-
"""B-2 逐次 token 账目服务（cost_budget 业务层）。

单一权威账目 = ai_task_token_usage 表（每次供应商调用一行）；ai_tasks 表零变更，
其 input_tokens/output_tokens 仅作为历史字段保留，账目下界取「账簿已知」与
「历史字段已知」二者较大值（max），既不重复计数也不低估已消费量。

本模块提供四件事：
1. 状态机：pending_dispatch -> complete / partial / unverifiable / request_uncertain；
2. 调用前闸门（pre-dispatch gate）：先落库占用、再发请求；存在未结清账目一律阻断；
3. 预算闸门：每次调用前按「已知下界 >= cost_budget」判定，不得在未知费用下继续；
4. resume / retry 的 409 / 422 契约（账目未结清 → 409；在途未决 → 422）。

失败口径（B-2 裁定，禁止静默吞掉）：
  - 供应商 HTTP 错误 / 超时 / 网络失败            -> request_uncertain（阻断）
  - 2xx 但无 usage 字段                          -> unverifiable（阻断）
  - 只有 total_tokens 或只有单侧细分              -> partial（阻断）
  - total_tokens != prompt + completion（计费口径差异） -> partial（阻断）
  - 已取得响应体之后的 JSON / 结构后处理失败       -> 已落定账目保留原结算状态
                                                    （complete / partial /
                                                    request_uncertain），仅告警；
                                                    已下发但未落定的行结清为
                                                    unverifiable（token 保持 NULL，
                                                    绝不遗留 pending 行）
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_migrations import (
    PHASE22_ALLOWED_USAGE_SOURCE,
    PHASE22_ALLOWED_USAGE_STATUS,
)
from app.models.ai_task import AITask
from app.models.ai_task_token_usage import AITaskTokenUsage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态 / 来源常量（值与迁移层允许枚举严格一致，漂移则导入即失败）
# ---------------------------------------------------------------------------

USAGE_STATUS_PENDING = "pending_dispatch"
USAGE_STATUS_REQUEST_UNCERTAIN = "request_uncertain"
USAGE_STATUS_COMPLETE = "complete"
USAGE_STATUS_PARTIAL = "partial"
USAGE_STATUS_UNVERIFIABLE = "unverifiable"

USAGE_SOURCE_NOT_DISPATCHED = "not_dispatched"
USAGE_SOURCE_PROMPT_COMPLETION = "provider_prompt_completion"
USAGE_SOURCE_TOTAL = "provider_total"
USAGE_SOURCE_MISSING = "missing"
USAGE_SOURCE_REQUEST_UNCERTAIN = "request_uncertain"

ALL_USAGE_STATUSES = (
    USAGE_STATUS_PENDING,
    USAGE_STATUS_REQUEST_UNCERTAIN,
    USAGE_STATUS_COMPLETE,
    USAGE_STATUS_PARTIAL,
    USAGE_STATUS_UNVERIFIABLE,
)
ALL_USAGE_SOURCES = (
    USAGE_SOURCE_NOT_DISPATCHED,
    USAGE_SOURCE_PROMPT_COMPLETION,
    USAGE_SOURCE_TOTAL,
    USAGE_SOURCE_MISSING,
    USAGE_SOURCE_REQUEST_UNCERTAIN,
)

_ENUM_DRIFT = sorted(
    (set(ALL_USAGE_STATUSES) - set(PHASE22_ALLOWED_USAGE_STATUS))
    | (set(ALL_USAGE_SOURCES) - set(PHASE22_ALLOWED_USAGE_SOURCE))
)
if _ENUM_DRIFT:  # pragma: no cover - 仅在有人改动枚举时触发，快速失败优于静默写脏值
    raise RuntimeError(
        f"usage_ledger_enum_drift: 业务层取值超出迁移层允许枚举：{_ENUM_DRIFT}"
    )

# 阻断态（已下发且无法结清）；pending_dispatch + 已下发 = 在途未决。
TERMINAL_BLOCKING_STATUSES = (
    USAGE_STATUS_REQUEST_UNCERTAIN,
    USAGE_STATUS_PARTIAL,
    USAGE_STATUS_UNVERIFIABLE,
)

# A-3 / A-5 固定语义（不设可切换开关，避免误把已确认账目改写成不确定）：
#   后处理失败时「已落定账目保留原结算状态；已下发但未落定的行结清为 unverifiable」。

HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422

CODE_LEDGER_UNSETTLED = "usage_ledger_unsettled"
CODE_LEDGER_IN_FLIGHT = "usage_ledger_in_flight"
CODE_BUDGET_EXCEEDED = "usage_budget_exceeded"
CODE_PRICING_UNAVAILABLE = "usage_pricing_unavailable"

_API_CODE_BY_HTTP = {400: "bad_request", 409: "conflict", 422: "validation_failed"}


class UsageLedgerError(Exception):
    """账目闸门异常：调用前阻断，以及 resume / retry 的 409 / 422 契约载体。"""

    # 供 ai_service 钩子包装层识别：此类异常必须原样上抛，不得被包成 RuntimeError
    usage_ledger_blocked = True

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = dict(details or {})

    @property
    def api_code(self) -> str:
        """映射到统一错误码（api_errors.ApiError.code）。"""
        return _API_CODE_BY_HTTP.get(self.http_status, "invalid_request")


# ---------------------------------------------------------------------------
# 供应商能力声明（capability）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderUsageCapability:
    """供应商 usage 能力：显式声明「能给什么、不能给什么」，禁止用 0 / 空值兜底。"""

    provider: str
    usage_reported: bool
    prompt_completion_breakdown: bool
    total_only: bool
    billing_breakdown_confirmed: bool
    pricing_known: bool
    note: str


def provider_usage_capability(provider: str | None) -> ProviderUsageCapability:
    """按供应商给出 usage 能力声明。

    现状（B-2 现场核对）：已接入供应商只返回 OpenAI 兼容的 status_code + body，
    无法从响应判定其计费口径（是否含缓存命中 token / thinking token），
    也无法确认单价。因此：
      - 预算只按 token 口径核算（与既有 cost_budget 语义一致）；
      - 任何「按金额核算预算」的诉求必须显式拒绝（422），不得折算成 0。
    """
    label = (provider or "unknown").strip().lower() or "unknown"
    return ProviderUsageCapability(
        provider=label,
        usage_reported=True,
        prompt_completion_breakdown=True,
        total_only=False,
        billing_breakdown_confirmed=False,
        pricing_known=False,
        note="仅能依据响应体 usage 字段核算 token 用量；计费口径与单价无法从响应确认",
    )


# ---------------------------------------------------------------------------
# 响应分类（状态机入口）
# ---------------------------------------------------------------------------


@dataclass
class UsageClassification:
    usage_status: str
    usage_source: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    blocking: bool = False
    detail: str = ""


def _as_int(value) -> int | None:
    """严格整型解析：无法确认的一律返回 None（绝不折算为 0）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _body_get(body, key):
    return body.get(key) if isinstance(body, dict) else None


def classify_response(
    status_code: int | None,
    body=None,
    *,
    provider: str | None = None,
) -> UsageClassification:
    """把一次供应商响应归类为终态账目（纯函数，便于离线穷举用例）。"""
    request_id = _body_get(body, "id")
    response_model = _body_get(body, "model")
    choices = _body_get(body, "choices")
    finish_reason = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")

    # ① 非 2xx / 无状态码：请求是否被计费无法确认 -> request_uncertain（阻断）
    if status_code is None or not (200 <= int(status_code) < 300):
        return UsageClassification(
            usage_status=USAGE_STATUS_REQUEST_UNCERTAIN,
            usage_source=USAGE_SOURCE_REQUEST_UNCERTAIN,
            provider_request_id=request_id,
            blocking=True,
            detail=f"供应商返回 HTTP {status_code}，请求是否被计费无法确认",
        )

    usage = _body_get(body, "usage")

    # ② 2xx 但无 usage 字段：用量不可核验 -> unverifiable（阻断）
    if not isinstance(usage, dict):
        return UsageClassification(
            usage_status=USAGE_STATUS_UNVERIFIABLE,
            usage_source=USAGE_SOURCE_MISSING,
            response_model=response_model,
            finish_reason=finish_reason,
            provider_request_id=request_id,
            blocking=True,
            detail="响应体缺少 usage 字段，无法核验本次 token 用量",
        )

    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    total = _as_int(usage.get("total_tokens"))

    # ③ usage 存在但无任何可用数字 -> unverifiable（阻断）
    if prompt is None and completion is None and total is None:
        return UsageClassification(
            usage_status=USAGE_STATUS_UNVERIFIABLE,
            usage_source=USAGE_SOURCE_MISSING,
            response_model=response_model,
            finish_reason=finish_reason,
            provider_request_id=request_id,
            blocking=True,
            detail="usage 字段存在但无可用 token 数值",
        )

    base = dict(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        response_model=response_model,
        finish_reason=finish_reason,
        provider_request_id=request_id,
    )

    # ④ 细分齐全：口径一致则 complete
    if prompt is not None and completion is not None:
        if total is not None and total != prompt + completion:
            # 口径差异（可能含缓存命中 / thinking token）-> partial（阻断）
            return UsageClassification(
                usage_status=USAGE_STATUS_PARTIAL,
                usage_source=USAGE_SOURCE_TOTAL,
                blocking=True,
                detail=(
                    f"total_tokens({total}) != prompt+completion({prompt + completion})，"
                    "计费口径未确认"
                ),
                **base,
            )
        return UsageClassification(
            usage_status=USAGE_STATUS_COMPLETE,
            usage_source=USAGE_SOURCE_PROMPT_COMPLETION,
            blocking=False,
            detail="usage 细分与总量自洽",
            **base,
        )

    # ⑤ 只有 total 或单侧细分：部分可核验 -> partial（阻断）
    return UsageClassification(
        usage_status=USAGE_STATUS_PARTIAL,
        usage_source=USAGE_SOURCE_TOTAL if total is not None else USAGE_SOURCE_PROMPT_COMPLETION,
        blocking=True,
        detail="usage 不完整（缺 prompt/completion 细分），无法核验完整用量",
        **base,
    )


# ---------------------------------------------------------------------------
# 账簿读取与状态
# ---------------------------------------------------------------------------


def row_known_tokens(row: AITaskTokenUsage) -> int | None:
    """单行已知 token 数：优先 total；否则 prompt+completion 齐全才算已知。"""
    if row.total_tokens is not None:
        return int(row.total_tokens)
    if row.prompt_tokens is not None and row.completion_tokens is not None:
        return int(row.prompt_tokens) + int(row.completion_tokens)
    return None


def legacy_token_floor(task: AITask) -> tuple[int, bool]:
    """历史字段下界：只用已取得的真实值累加，未知侧不折算为 0。"""
    floor = 0
    unknown = False
    for value in (task.input_tokens, task.output_tokens):
        if value is None:
            unknown = True
        else:
            floor += int(value)
    return floor, unknown


@dataclass
class LedgerState:
    rows: list[AITaskTokenUsage] = field(default_factory=list)
    conflict_rows: list[AITaskTokenUsage] = field(default_factory=list)
    in_flight_rows: list[AITaskTokenUsage] = field(default_factory=list)
    complete_rows: list[AITaskTokenUsage] = field(default_factory=list)
    known_tokens: int = 0
    last_attempt_no: int | None = None
    last_call_no: int | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.conflict_rows or self.in_flight_rows)

    @property
    def dispatched_calls(self) -> int:
        return sum(1 for row in self.rows if row.dispatched_at is not None)

    def summary(self) -> dict:
        return {
            "rows": len(self.rows),
            "dispatched_calls": self.dispatched_calls,
            "complete": len(self.complete_rows),
            "unsettled": len(self.conflict_rows),
            "in_flight": len(self.in_flight_rows),
            "known_tokens": self.known_tokens,
            "last_attempt_no": self.last_attempt_no,
            "last_call_no": self.last_call_no,
        }

    def unsettled_detail(self) -> list[dict]:
        return [
            {
                "attempt_no": row.attempt_no,
                "call_no": row.call_no,
                "usage_status": row.usage_status,
                "usage_source": row.usage_source,
                "dispatched_at": row.dispatched_at.isoformat() if row.dispatched_at else None,
            }
            for row in (*self.conflict_rows, *self.in_flight_rows)
        ]


def _state_from_rows(rows) -> LedgerState:
    state = LedgerState(rows=list(rows))
    for row in state.rows:
        known = row_known_tokens(row)
        if known is not None:
            state.known_tokens += known
        if row.usage_status == USAGE_STATUS_COMPLETE:
            state.complete_rows.append(row)
        elif row.dispatched_at is not None and row.usage_status in TERMINAL_BLOCKING_STATUSES:
            state.conflict_rows.append(row)
        elif row.dispatched_at is not None and row.usage_status == USAGE_STATUS_PENDING:
            state.in_flight_rows.append(row)
    if state.rows:
        last = max(state.rows, key=lambda item: (item.attempt_no, item.call_no))
        state.last_attempt_no = int(last.attempt_no)
        state.last_call_no = int(last.call_no)
        # 下一次调用号专职 helper：next_call_no()
    return state


async def ledger_rows(db: AsyncSession, task_id: uuid.UUID) -> list[AITaskTokenUsage]:
    """读取某任务的全部账目行（按 attempt_no / call_no 升序）。"""
    result = await db.execute(
        select(AITaskTokenUsage)
        .where(AITaskTokenUsage.task_id == task_id)
        .order_by(AITaskTokenUsage.attempt_no, AITaskTokenUsage.call_no)
    )
    return list(result.scalars().all())


async def load_ledger_state(db: AsyncSession, task_id: uuid.UUID) -> LedgerState:
    return _state_from_rows(await ledger_rows(db, task_id))


def confirmed_attempt_no(task: AITask, rows) -> int:
    """可确认的 attempt 号。

    - 账簿已有行：沿用账簿中最大的 attempt_no（resume 在同一 attempt 内继续追加 call）；
    - 账簿为空：retry_count + 1（新任务为 1，手动重试的新任务为 2、3 …）。
    """
    if rows:
        return max(int(row.attempt_no) for row in rows)
    return int(task.retry_count or 0) + 1


async def next_call_no(db: AsyncSession, task_id: uuid.UUID, attempt_no: int) -> int:
    """同一 attempt 内单调递增的下一次调用号。"""
    rows = await ledger_rows(db, task_id)
    same = [int(row.call_no) for row in rows if int(row.attempt_no) == int(attempt_no)]
    return (max(same) + 1) if same else 1


async def known_consumed_tokens(
    db: AsyncSession, task: AITask, *, state: LedgerState | None = None
) -> tuple[int, str]:
    """已知消费下界 = max(账簿已知, 历史字段已知)，避免重复计数又不低估。"""
    state = state if state is not None else await load_ledger_state(db, task.id)
    legacy_floor, _legacy_unknown = legacy_token_floor(task)
    basis = max(state.known_tokens, legacy_floor)
    if state.rows and state.known_tokens >= legacy_floor:
        source = "ledger"
    elif state.rows:
        source = "mixed"
    elif legacy_floor:
        source = "legacy"
    else:
        source = "none"
    return basis, source


# ---------------------------------------------------------------------------
# 闸门一：账目未结清 / 在途未决 -> 禁止派发
# ---------------------------------------------------------------------------


def _blocked_error(state: LedgerState, *, action: str) -> UsageLedgerError:
    detail = state.unsettled_detail()
    summary = state.summary()
    if state.conflict_rows:
        return UsageLedgerError(
            CODE_LEDGER_UNSETTLED,
            (
                f"{action}被拒绝：任务存在未结清的 token 账目（{CODE_LEDGER_UNSETTLED}），"
                f"未结清调用数={len(state.conflict_rows)}；"
                "未知费用口径下禁止继续派发调用"
            ),
            http_status=HTTP_CONFLICT,
            details={"usage_ledger": summary, "unsettled": detail},
        )
    return UsageLedgerError(
        CODE_LEDGER_IN_FLIGHT,
        (
            f"{action}被拒绝：存在在途未结清的调用占用（{CODE_LEDGER_IN_FLIGHT}），"
            f"在途调用数={len(state.in_flight_rows)}；"
            "该调用的费用口径尚未确定，禁止继续派发"
        ),
        http_status=HTTP_UNPROCESSABLE,
        details={"usage_ledger": summary, "in_flight": detail},
    )


async def assert_dispatch_allowed(
    db: AsyncSession,
    task: AITask,
    *,
    action: str = "派发调用",
    state: LedgerState | None = None,
) -> LedgerState:
    """调用前账目闸门：任何未结清 / 在途账目都存在即阻断（不得继续烧钱）。"""
    state = state if state is not None else await load_ledger_state(db, task.id)
    if state.blocked:
        raise _blocked_error(state, action=action)
    return state


# ---------------------------------------------------------------------------
# 闸门二：预算（每次调用前判定）
# ---------------------------------------------------------------------------


@dataclass
class BudgetDecision:
    allowed: bool
    code: str | None
    message: str
    basis: int
    budget: int | None
    basis_source: str
    http_status: int | None = None


async def evaluate_budget(
    db: AsyncSession,
    task: AITask,
    *,
    pricing_required: bool = False,
    provider: str | None = None,
    state: LedgerState | None = None,
) -> BudgetDecision:
    """预算闸门判定（纯判定，不抛异常）。

    - cost_budget 为 NULL：预算关闭（与既有语义一致，不拦截）；
    - 已知下界 >= cost_budget：拦截（口径与既有预检 `>=` 一致）；
    - pricing_required=True 且单价未知：拦截（422，禁止用 0 折算）。
    """
    budget = task.cost_budget
    basis, source = await known_consumed_tokens(db, task, state=state)
    if pricing_required and not provider_usage_capability(provider).pricing_known:
        return BudgetDecision(
            allowed=False,
            code=CODE_PRICING_UNAVAILABLE,
            message=(
                f"按金额核算预算被拒绝（{CODE_PRICING_UNAVAILABLE}）："
                "供应商单价未知，无法把 token 折算为费用，禁止按 0 折算继续调用"
            ),
            basis=basis,
            budget=budget,
            basis_source=source,
            http_status=HTTP_UNPROCESSABLE,
        )
    if budget is None:
        return BudgetDecision(True, None, "未设置 cost_budget，预算闸门关闭", basis, budget, source)
    if basis >= int(budget):
        return BudgetDecision(
            allowed=False,
            code=CODE_BUDGET_EXCEEDED,
            message=(
                f"调用前预算闸门拦截（{CODE_BUDGET_EXCEEDED}）："
                f"cost_budget={budget}，已知累计 token={basis}（来源 {source}）"
            ),
            basis=basis,
            budget=int(budget),
            basis_source=source,
            http_status=HTTP_CONFLICT,
        )
    return BudgetDecision(True, None, "预算未超限", basis, budget, source)


async def assert_budget_allowed(
    db: AsyncSession,
    task: AITask,
    *,
    pricing_required: bool = False,
    provider: str | None = None,
    state: LedgerState | None = None,
) -> BudgetDecision:
    decision = await evaluate_budget(
        db, task, pricing_required=pricing_required, provider=provider, state=state
    )
    if not decision.allowed:
        raise UsageLedgerError(
            decision.code or CODE_BUDGET_EXCEEDED,
            decision.message,
            http_status=decision.http_status or HTTP_CONFLICT,
            details={
                "basis": decision.basis,
                "basis_source": decision.basis_source,
                "cost_budget": decision.budget,
            },
        )
    return decision


# ---------------------------------------------------------------------------
# 写入：占用行 / 结清
# ---------------------------------------------------------------------------


async def open_call(
    db: AsyncSession,
    task: AITask,
    *,
    attempt_no: int,
    call_no: int | None = None,
    provider: str | None = None,
    requested_model: str | None = None,
    normalized_model: str | None = None,
    dispatched_at: datetime | None = None,
) -> AITaskTokenUsage:
    """写入一次调用的占用行（先落库、后发请求；唯一键确定性：task+attempt+call）。"""
    now = datetime.utcnow()
    row = AITaskTokenUsage(
        task_id=task.id,
        attempt_no=int(attempt_no),
        call_no=int(call_no) if call_no is not None else await next_call_no(db, task.id, attempt_no),
        usage_status=USAGE_STATUS_PENDING,
        usage_source=USAGE_SOURCE_NOT_DISPATCHED,
        provider=provider,
        requested_model=requested_model,
        normalized_model=normalized_model,
        dispatched_at=dispatched_at or now,
        created_at=now,
    )
    db.add(row)
    return row


def apply_classification(
    row: AITaskTokenUsage,
    classification: UsageClassification,
    *,
    settled_at: datetime | None = None,
) -> AITaskTokenUsage:
    """把分类结果写入账目行（终态 + settled_at + updated_at）。"""
    now = settled_at or datetime.utcnow()
    row.usage_status = classification.usage_status
    row.usage_source = classification.usage_source
    if classification.prompt_tokens is not None:
        row.prompt_tokens = classification.prompt_tokens
    if classification.completion_tokens is not None:
        row.completion_tokens = classification.completion_tokens
    if classification.total_tokens is not None:
        row.total_tokens = classification.total_tokens
    if classification.response_model:
        row.response_model = classification.response_model
    if classification.finish_reason:
        row.finish_reason = classification.finish_reason
    if classification.provider_request_id:
        row.provider_request_id = classification.provider_request_id
    row.settled_at = now
    row.updated_at = now
    return row


# ---------------------------------------------------------------------------
# 钩子：挂在 ai_service 每次供应商调用前后
# ---------------------------------------------------------------------------


class UsageLedgerHook:
    """逐次账目钩子（每次供应商调用一行）。

    阶段约定（由 ai_service 调用）：
      - before_dispatch：账目闸门 + 预算闸门 + 先落库占用（dispatched_at 非空）
      - response：按响应体结清（complete / partial / unverifiable）
      - error：请求失败结清为 request_uncertain（阻断）
      - postprocess_error：已取得响应体后的 JSON / 结构后处理失败
        （已落定账目保留原结算状态；已下发但未落定的行结清为 unverifiable）
    """

    def __init__(self, db: AsyncSession, task: AITask) -> None:
        self.db = db
        self.task = task
        self.row: AITaskTokenUsage | None = None
        self.blocked_error: UsageLedgerError | None = None

    async def __call__(self, stage: str, payload: dict | None = None) -> None:
        data = dict(payload or {})
        if stage == "before_dispatch":
            await self.before_dispatch(data)
        elif stage == "response":
            await self.settle_from_response(data)
        elif stage == "error":
            await self.settle_from_error(data)
        elif stage == "postprocess_error":
            await self.postprocess_failed(data)
        else:  # pragma: no cover - 防御性
            logger.warning("未知账目钩子阶段：%s", stage)

    # -- 调用前 ------------------------------------------------------------
    async def before_dispatch(self, payload: dict) -> None:
        # ① 账目闸门：未结清 / 在途 -> 直接阻断（不得在未知费用下继续）
        state = await assert_dispatch_allowed(
            self.db, self.task, action="调用前派发"
        )
        # ② 预算闸门：每次调用前按已知下界判定
        await assert_budget_allowed(
            self.db,
            self.task,
            provider=payload.get("provider"),
            state=state,
        )
        # ③ 先落库、后发请求：dispatched_at 非空 = 已向供应商发出请求（崩溃可见）
        attempt_no = confirmed_attempt_no(self.task, state.rows)
        row = await open_call(
            self.db,
            self.task,
            attempt_no=attempt_no,
            provider=payload.get("provider"),
            requested_model=payload.get("requested_model"),
            normalized_model=payload.get("normalized_model"),
        )
        try:
            await self.db.commit()
        except IntegrityError as error:
            await self.db.rollback()
            raise UsageLedgerError(
                CODE_LEDGER_IN_FLIGHT,
                (
                    f"调用前派发被拒绝（{CODE_LEDGER_IN_FLIGHT}）："
                    f"(task, attempt_no={attempt_no}, call_no={row.call_no}) 占用行已存在"
                ),
                http_status=HTTP_UNPROCESSABLE,
                details={"attempt_no": attempt_no, "call_no": row.call_no},
            ) from error
        self.row = row
        logger.debug(
            "账目占用已落库：task=%s attempt=%s call=%s", self.task.id, attempt_no, row.call_no
        )

    # -- 结清 --------------------------------------------------------------
    async def _ensure_row(self) -> AITaskTokenUsage:
        if self.row is not None:
            return self.row
        logger.warning("账目钩子在未占用的情况下收到结清通知，补建占用行：task=%s", self.task.id)
        state = await load_ledger_state(self.db, self.task.id)
        attempt_no = confirmed_attempt_no(self.task, state.rows)
        row = await open_call(self.db, self.task, attempt_no=attempt_no)
        await self.db.commit()
        self.row = row
        return row

    async def settle_from_response(self, payload: dict) -> None:
        row = await self._ensure_row()
        classification = classify_response(
            payload.get("status_code"),
            payload.get("body"),
            provider=payload.get("provider"),
        )
        apply_classification(row, classification)
        await self.db.commit()
        if classification.blocking:
            self.blocked_error = UsageLedgerError(
                CODE_LEDGER_UNSETTLED,
                (
                    f"调用已阻断后续派发（{CODE_LEDGER_UNSETTLED}）："
                    f"attempt={row.attempt_no} call={row.call_no} "
                    f"usage_status={classification.usage_status}（{classification.detail}）"
                ),
                http_status=HTTP_CONFLICT,
                details={
                    "attempt_no": row.attempt_no,
                    "call_no": row.call_no,
                    "usage_status": classification.usage_status,
                    "usage_source": classification.usage_source,
                },
            )
            logger.warning("账目结清为阻断态：%s", self.blocked_error.message)
        else:
            logger.debug(
                "账目结清：task=%s attempt=%s call=%s tokens=%s",
                self.task.id, row.attempt_no, row.call_no, row_known_tokens(row),
            )

    async def settle_from_error(self, payload: dict) -> None:
        """失败路径结清：非 2xx / 超时 / 网络失败 -> request_uncertain（阻断）。

        即便失败响应体里带了 usage，也不得据此确认本次请求的费用口径
        （请求本身未被正常处理），一律按不确定落账，由 409 阻断后续派发。
        """
        row = await self._ensure_row()
        base = classify_response(
            payload.get("status_code"),
            payload.get("body"),
            provider=payload.get("provider"),
        )
        classification = UsageClassification(
            usage_status=USAGE_STATUS_REQUEST_UNCERTAIN,
            usage_source=USAGE_SOURCE_REQUEST_UNCERTAIN,
            provider_request_id=base.provider_request_id,
            response_model=base.response_model,
            finish_reason=base.finish_reason,
            blocking=True,
            detail=(
                f"调用失败（{payload.get('error_type', 'unknown')}）："
                f"{str(payload.get('detail') or '')[:200]}"
            ),
        )
        apply_classification(row, classification)
        await self.db.commit()
        self.blocked_error = UsageLedgerError(
            CODE_LEDGER_UNSETTLED,
            (
                f"调用失败且费用口径不确定（{CODE_LEDGER_UNSETTLED}）："
                f"attempt={row.attempt_no} call={row.call_no} "
                f"usage_status={USAGE_STATUS_REQUEST_UNCERTAIN}"
            ),
            http_status=HTTP_CONFLICT,
            details={
                "attempt_no": row.attempt_no,
                "call_no": row.call_no,
                "usage_status": USAGE_STATUS_REQUEST_UNCERTAIN,
            },
        )
        logger.warning(
            "账目结清为不确定态（后续派发将被阻断）：task=%s attempt=%s call=%s %s",
            self.task.id, row.attempt_no, row.call_no, classification.detail,
        )

    async def postprocess_failed(self, payload: dict) -> None:
        """已取得响应体之后的 JSON / 结构后处理失败（A-3 + A-5 裁定）。

        必须区分两种账目情形，不得一刀切：
        1) 本行已落定（settled_at 非空，或 usage_source 已非 not_dispatched）：
           **保留原结算状态**（complete / partial / request_uncertain），
           不改判为不确定——已确认的账目不得回退成未知；
        2) 本行已下发但尚未落定（pending_dispatch 且 dispatched_at 非空）：
           本次调用费用口径不可验证，必须结清为 unverifiable（token 保持 NULL）；
           绝不把已下发的 pending 行永久遗留（否则后续派发被 422 永久死锁）。
        """
        row = self.row
        detail = str(payload.get("detail") or "后处理失败")
        if row is None:
            logger.warning("后处理失败发生在占用行创建之前：task=%s %s", self.task.id, detail)
            return
        settled = row.settled_at is not None or (
            row.usage_source or USAGE_SOURCE_NOT_DISPATCHED
        ) != USAGE_SOURCE_NOT_DISPATCHED
        if settled:
            await self.db.commit()
            logger.warning(
                "已取得 usage 后的后处理失败（保留已落定账目）：task=%s attempt=%s call=%s "
                "usage_status=%s %s",
                self.task.id, row.attempt_no, row.call_no, row.usage_status, detail,
            )
            return
        classification = UsageClassification(
            usage_status=USAGE_STATUS_UNVERIFIABLE,
            usage_source=USAGE_SOURCE_MISSING,
            blocking=True,
            detail=detail,
        )
        apply_classification(row, classification)
        await self.db.commit()
        logger.warning(
            "已下发但未落定的行因后处理失败结清为不确定（阻断后续派发）："
            "task=%s attempt=%s call=%s %s",
            self.task.id, row.attempt_no, row.call_no, detail,
        )


def install_usage_ledger_hook(db: AsyncSession, task: AITask) -> UsageLedgerHook:
    """安装账目钩子（供 run_ai_task 在每次执行轮内调用）。"""
    from app.services import ai_service

    hook = UsageLedgerHook(db, task)
    ai_service.install_usage_hook(hook)
    return hook


def uninstall_usage_ledger_hook() -> None:
    from app.services import ai_service

    ai_service.uninstall_usage_hook()


# ---------------------------------------------------------------------------
# resume / retry 的 409 / 422 契约
# ---------------------------------------------------------------------------

#: 允许来源字典序固定，便于契约测试断言
RECOVERABLE_ACTIONS = ("resume", "retry")


async def assert_recoverable(db: AsyncSession, task: AITask, *, action: str) -> LedgerState:
    """resume / retry 前置契约：

    - 冲突态（已下发且无法结清：request_uncertain / partial / unverifiable）-> 409；
    - 在途未决（已下发但未结清：pending_dispatch）-> 422；
    - 通过则返回账目状态。
    """
    label = "恢复任务" if action == "resume" else "重试任务"
    return await assert_dispatch_allowed(db, task, action=label)
