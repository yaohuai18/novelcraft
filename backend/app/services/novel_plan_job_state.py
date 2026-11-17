"""规划任务状态机（唯一真源）。

第二批之前，状态字符串散落在 router / service / 前端，且 `_finalize` 只接受
`status='running'`，导致「启动恢复把任务标成 interrupted 后，旧 worker 的完整结果
被静默丢弃」。本模块把状态、合法转换、可重试判定集中到一处。

状态（**只描述任务如何结束**）：
    queued              已持久化，尚未被有效 worker 领取
    running             已由持有有效租约的 worker 领取
    succeeded           结果已通过最终 CAS 写回
    failed              确定失败，保留错误信息
    cancel_requested    用户请求取消，worker 应在安全点停止
    cancelled           已停止，未写入结果
    interrupted         执行者消失 / 服务终止 / 租约过期

晚到结果**不是状态**（2026-11-12 修正）：
    若把 interrupted/cancelled 改写成 result_pending_review，就等于抹掉了
    「任务为何结束」这一事实，审计与运维将无法区分「被取消」和「中断」。
    因此晚到结果用独立字段表达，见 RESULT_REVIEW_*：
        result_review_status  none / pending_review / accepted / discarded
        late_result_json      晚到的成功结果（不写入 result_json）
        late_result_reason    lease_lost / cancelled / service_shutdown / interrupted

设计约束：
- 任何写状态的代码都必须经过 `ensure_transition()` 或等价 CAS 条件，禁止手写字符串比较；
- 终态（succeeded / failed / cancelled / interrupted）不可再被改写；
- 晚到结果只能人工处置，系统不得自动把它当作成功。
"""
from __future__ import annotations

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

# 晚到结果的独立状态轴（与 status 正交，互不覆盖）
RESULT_REVIEW_NONE = "none"
RESULT_REVIEW_PENDING = "pending_review"
RESULT_REVIEW_ACCEPTED = "accepted"
RESULT_REVIEW_DISCARDED = "discarded"

RESULT_REVIEW_STATUSES: tuple[str, ...] = (
    RESULT_REVIEW_NONE,
    RESULT_REVIEW_PENDING,
    RESULT_REVIEW_ACCEPTED,
    RESULT_REVIEW_DISCARDED,
)

# 只有这些状态可能收到「晚到的成功结果」。
# running 也在内：_finalize 因租约过期失败时任务仍停在 running，
# 晚到结果必须可落库（store_late_result 仅在 _finalize 失败路径调用，无副作用面扩大）
LATE_RESULT_ELIGIBLE_STATUSES: tuple[str, ...] = (
    STATUS_INTERRUPTED,
    STATUS_CANCELLED,
    STATUS_CANCEL_REQUESTED,
    STATUS_RUNNING,
)

# 仍占用「同一作用域只能有一个活跃任务」的额度
ACTIVE_STATUSES: tuple[str, ...] = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_CANCEL_REQUESTED,
)

# 不会再有系统写入的状态
TERMINAL_STATUSES: tuple[str, ...] = (
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

# 允许用户重新提交的状态
RETRYABLE_STATUSES: tuple[str, ...] = (
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_RUNNING, STATUS_CANCELLED, STATUS_INTERRUPTED}),
    STATUS_RUNNING: frozenset(
        {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCEL_REQUESTED, STATUS_CANCELLED, STATUS_INTERRUPTED}
    ),
    STATUS_CANCEL_REQUESTED: frozenset({STATUS_CANCELLED, STATUS_FAILED}),
    STATUS_SUCCEEDED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_INTERRUPTED: frozenset(),
}

# 稳定错误码（前端按 code 分支，不再解析中文）
ERR_SERVICE_RESTART = "service_restart"
ERR_LEASE_EXPIRED = "lease_expired"
ERR_SERVICE_SHUTDOWN = "service_shutdown"
ERR_TIMEOUT = "timeout"
ERR_CANCELLED_BY_USER = "cancelled_by_user"
ERR_LATE_RESULT = "late_result"
ERR_LEASE_LOST = "lease_lost"
ERR_EMPTY_RESULT = "empty_result"


class IllegalTransition(RuntimeError):
    """在应用层发现非法状态转换（数据库 CAS 之外的兜底断言）。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法状态转换：{current} -> {target}")
        self.current = current
        self.target = target


def is_known(status: str) -> bool:
    return status in ALLOWED_TRANSITIONS


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


def is_active(status: str) -> bool:
    return status in ACTIVE_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def can_retry(status: str) -> bool:
    return status in RETRYABLE_STATUSES


def accepts_late_result(status: str) -> bool:
    return status in LATE_RESULT_ELIGIBLE_STATUSES


def transition_table() -> list[dict[str, list[str]]]:
    """给文档 / 测试用：导出可读的转换表。"""
    return [
        {"from": status, "to": sorted(ALLOWED_TRANSITIONS[status])}
        for status in ALL_STATUSES
    ]
