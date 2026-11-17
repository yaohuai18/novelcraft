# 审计日志服务（第十七批「统计分析与运营后台」规格十七 + 第二十四批「审计日志」规格十四）
# 必须记录：登录登出 / 修改权限 / 删除内容 / 恢复备份 / 下架作品 / 处理举报 /
#           修改系统配置 / 查看受保护内容 / 报表导出。
# 第二十四批：登录/邀请/改角色/查看敏感/导出/下载/删除/恢复/提交审核/通过/发布/下架/改安全策略，
#           普通成员不可删日志，管理员查日志本身也应被记录。
# IP 只保存哈希（ip_hash），不保存原始 IP。
#
# 阶段 J「审计补齐 + 备份调度」：新增内容级可追溯审计（补充 10 / R13）——
#   action 扩展 content_save / content_restore / ai_apply / backfill，
#   记录「谁、在什么版本、改了哪些字段（字段级 diff）」；
#   正文全文不入审计表（只存哈希/长度/摘要 + diff），审计只增不改、绝不删除。
import hashlib
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_log import AuditLog


async def record(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    actor_type: str = "user",
    target_type: str | None = None,
    target_id: str | None = None,
    reason: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
    organization_id: uuid.UUID | None = None,
    before_value: str | None = None,
    after_value: str | None = None,
) -> AuditLog:
    """写入一条审计日志（第二十四批支持组织范围与变更前后值）。"""
    import json

    log = AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        ip_hash=ip_hash,
        user_agent=user_agent,
        metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
        organization_id=organization_id,
        before_value=before_value,
        after_value=after_value,
    )
    db.add(log)
    await db.flush()
    return log


async def list_logs(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AuditLog], int]:
    """审计日志查询（auditor/super_admin 使用），支持多维过滤与分页。"""
    filters = []
    if actor_id is not None:
        filters.append(AuditLog.actor_id == actor_id)
    if action:
        filters.append(AuditLog.action == action)
    if target_type:
        filters.append(AuditLog.target_type == target_type)
    if from_dt is not None:
        filters.append(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        filters.append(AuditLog.created_at <= to_dt)

    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


# =====================================================================
# 阶段 J：内容级可追溯审计（补充 10 / R13）
# =====================================================================

# 内容级审计 action（audit_logs.action 扩展取值）
ACTION_CONTENT_SAVE = "content_save"        # 正文保存 / 生成结果接受为新版本
ACTION_CONTENT_RESTORE = "content_restore"  # 恢复到历史版本
ACTION_AI_APPLY = "ai_apply"                # AI 覆盖 / 插入 / 逐段采纳 / 接受候选
ACTION_BACKFILL = "backfill"                # 存量章节事实回填（阶段 K 使用）

CONTENT_AUDIT_ACTIONS = (
    ACTION_CONTENT_SAVE,
    ACTION_CONTENT_RESTORE,
    ACTION_AI_APPLY,
    ACTION_BACKFILL,
)

# 内容级审计统一挂在章节正文上，target_id = outlines.id
CONTENT_AUDIT_TARGET_TYPE = "chapter_content"

ACTION_LABELS = {
    ACTION_CONTENT_SAVE: "保存正文",
    ACTION_CONTENT_RESTORE: "恢复正文版本",
    ACTION_AI_APPLY: "应用 AI 修改",
    ACTION_BACKFILL: "存量事实回填",
}

# ===== A2：生命周期流转审计（与内容审计分域，互不污染） =====
# 边界（刻意保守）：
#   - 生命周期事件 action 以 lifecycle_ 前缀独立命名，绝不加入 CONTENT_AUDIT_ACTIONS，
#     因此 record_content_audit_result 仍拒绝这些 action；
#   - target_type 固定 chapter_lifecycle，与 CONTENT_AUDIT_TARGET_TYPE(chapter_content) 分离，
#     因此不会成为正文 diff 的 before 基准（_last_known_state 只认 chapter_content）；
#   - 本批不传递操作人：actor_id 固定 None、actor_type 固定 system。

LIFECYCLE_AUDIT_TARGET_TYPE = "chapter_lifecycle"

ACTION_LIFECYCLE_APPROVE = "lifecycle_approve"
ACTION_LIFECYCLE_APPROVE_REJECTED = "lifecycle_approve_rejected"
ACTION_LIFECYCLE_PUBLISH = "lifecycle_publish"
ACTION_LIFECYCLE_PUBLISH_REJECTED = "lifecycle_publish_rejected"
ACTION_LIFECYCLE_STATUS = "lifecycle_status"
ACTION_LIFECYCLE_STATUS_REJECTED = "lifecycle_status_rejected"

LIFECYCLE_AUDIT_ACTIONS = (
    ACTION_LIFECYCLE_APPROVE,
    ACTION_LIFECYCLE_APPROVE_REJECTED,
    ACTION_LIFECYCLE_PUBLISH,
    ACTION_LIFECYCLE_PUBLISH_REJECTED,
    ACTION_LIFECYCLE_STATUS,
    ACTION_LIFECYCLE_STATUS_REJECTED,
)

# 拒绝原因码（写审计 metadata.reason_code，供排障与统计；不替代 reason 文本）
LIFECYCLE_REJECT_STATE_NOT_ALLOWED = "state_not_allowed"
LIFECYCLE_REJECT_VALIDATION_REPORT_MISSING = "validation_report_missing"
LIFECYCLE_REJECT_ANCHOR_MISSING = "validation_anchor_missing"
LIFECYCLE_REJECT_ANCHOR_STALE = "validation_anchor_stale"
LIFECYCLE_REJECT_CONTENT_VERSION_UNKNOWN = "content_version_unknown"
LIFECYCLE_REJECT_BLOCKING = "blocking_issues"
LIFECYCLE_REJECT_TARGET_NOT_WRITABLE = "target_not_writable"
LIFECYCLE_REJECT_ALREADY_PUBLISHED = "already_published"
# B1/U1a：资源状态门禁（outline.status）拒绝码，与生命周期状态机拒绝区分
LIFECYCLE_REJECT_ARCHIVED = "archived"
LIFECYCLE_REJECT_TAKEN_DOWN = "taken_down"

# 参与字段级 diff 的字段
DIFF_FIELDS = (
    "content",
    "word_count",
    "version",
    "lifecycle_status",
    "title",
    "outline_version_id",
    "generation_record_id",
)

_PREVIEW_LIMIT = 120


def _text(value) -> str:
    """任意值转文本（None → 空串），用于哈希与摘要。"""
    return "" if value is None else str(value)


def _jsonable(value):
    """把 uuid/datetime 等转成可 JSON 序列化的值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def content_hash(content) -> str:
    """正文哈希（sha256）。审计只存哈希，不复制全文。"""
    return hashlib.sha256(_text(content).encode("utf-8")).hexdigest()


def _preview(text, limit: int = _PREVIEW_LIMIT) -> str:
    flat = _text(text).replace("\r\n", "\n").replace("\n", " ")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def snapshot_content_state(chapter_content, outline=None) -> dict:
    """抓取正文状态快照（只读），供 before/after 字段级 diff 使用。

    调用方必须在修改正文「之前」调用一次，修改「之后」再调用一次。
    """
    if chapter_content is None:
        return {}
    content = _text(getattr(chapter_content, "content", ""))
    outline_version_id = getattr(chapter_content, "outline_version_id", None)
    generation_record_id = getattr(chapter_content, "generation_record_id", None)
    return {
        "content": content,
        "content_hash": content_hash(content),
        "content_length": len(content),
        "word_count": _jsonable(getattr(chapter_content, "word_count", None)),
        "version": _jsonable(getattr(chapter_content, "version", None)),
        "lifecycle_status": getattr(chapter_content, "lifecycle_status", None),
        "title": getattr(outline, "title", None) if outline is not None else None,
        "outline_version_id": (
            str(outline_version_id) if outline_version_id else None
        ),
        "generation_record_id": (
            str(generation_record_id) if generation_record_id else None
        ),
    }


def build_field_diff(before: dict | None, after: dict | None, *, action: str | None = None) -> dict:
    """构造字段级 diff 明细（写入 audit_logs.diff_json）。

    正文（content）不落原文，只落：变更前后哈希、长度、截断摘要。
    """
    before = before or {}
    after = after or {}
    fields: dict = {}
    changed: list[str] = []

    for name in DIFF_FIELDS:
        if name not in before and name not in after:
            continue
        old = before.get(name)
        new = after.get(name)
        if name == "content":
            # 正文按哈希比对（before 可能来自历史审计摘要，不含全文）
            old_hash = before.get("content_hash") or content_hash(old)
            new_hash = after.get("content_hash") or content_hash(new)
            if old_hash == new_hash:
                continue
            changed.append(name)
            fields["content"] = {
                "changed": True,
                "before_hash": old_hash,
                "after_hash": new_hash,
                "before_length": before.get("content_length", len(_text(old))),
                "after_length": after.get("content_length", len(_text(new))),
                "before_preview": _preview(old),
                "after_preview": _preview(new),
            }
            continue
        if old == new:
            continue
        changed.append(name)
        fields[name] = {"changed": True, "before": old, "after": new}

    return {
        "action": action,
        "changed_fields": changed,
        "changed_count": len(changed),
        "content_changed": "content" in changed,
        "fields": fields,
    }


def _value_summary(state: dict | None) -> dict | None:
    """before_value / after_value 落库存量摘要（不含正文全文）。"""
    if not state:
        return None
    return {
        "content_hash": state.get("content_hash"),
        "content_length": state.get("content_length"),
        "content_preview": _preview(state.get("content")),
        "word_count": state.get("word_count"),
        "version": state.get("version"),
        "lifecycle_status": state.get("lifecycle_status"),
        "title": state.get("title"),
        "outline_version_id": state.get("outline_version_id"),
        "generation_record_id": state.get("generation_record_id"),
    }


logger = logging.getLogger(__name__)

# ===== A1：内容审计写入结果与失败可观察性 =====
# 审计写入是「旁路可追溯」而不是「主链路前置条件」：写失败不得让正文保存失败。
# 因此写入放在独立 savepoint 内；失败只回滚 savepoint，并在进程内登记一条失败记录。
#
# 重要边界（勿误解）：
#   - audit_failures 是进程内有界的「可观察性缓冲」，不是审计证据本身，
#     进程重启即丢失、多 worker 各存各的，绝不等价于持久化审计；
#   - 持久化的唯一事实来源仍是 audit_logs 表。

AUDIT_STATUS_WRITTEN = "written"
AUDIT_STATUS_SKIPPED_DISABLED = "skipped_disabled"
AUDIT_STATUS_SKIPPED_UNCHANGED = "skipped_unchanged"
AUDIT_STATUS_FAILED = "failed"

# 失败缓冲容量上限（FIFO 覆盖最旧记录，保证不会无限增长）
AUDIT_FAILURE_LIMIT = 200
AUDIT_FAILURE_ERROR_SUMMARY_LIMIT = 200

audit_failures: "deque[dict[str, Any]]" = deque(maxlen=AUDIT_FAILURE_LIMIT)
_audit_failure_total = 0


@dataclass(frozen=True)
class AuditWriteResult:
    """一次内容审计写入的结果（可区分「写成功 / 开关关闭 / 无变更 / 写失败」）。"""

    status: str
    log: AuditLog | None = None
    failure: dict[str, Any] | None = None

    @property
    def written(self) -> bool:
        return self.status == AUDIT_STATUS_WRITTEN


def record_audit_failure(
    *,
    action: str | None,
    project_id: Any = None,
    target_id: Any = None,
    target_type: str | None = None,
    phase: str = "record_content_audit",
    error: BaseException | str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记一次审计写入失败（有界、进程内、非持久化），并保留 logger 记录。

    并发说明：仅在对同一 asyncio 事件循环内使用；单次 `deque.append` 在 CPython 下
    是原子的，读取用 `list()` 快照，不提供跨进程/跨线程的聚合保证。
    """
    global _audit_failure_total

    error_type = type(error).__name__ if isinstance(error, BaseException) else "UnknownError"
    error_summary = "" if error is None else str(error)
    error_summary = error_summary.replace("\n", " ")[:AUDIT_FAILURE_ERROR_SUMMARY_LIMIT]

    entry: dict[str, Any] = {
        "action": action,
        "project_id": str(project_id) if project_id else None,
        "target_id": str(target_id) if target_id else None,
        "target_type": target_type or CONTENT_AUDIT_TARGET_TYPE,
        "phase": phase,
        "error_type": error_type,
        "error_summary": error_summary,
        "at": datetime.utcnow().isoformat() + "Z",
    }
    if extra:
        entry["extra"] = {k: _jsonable(v) for k, v in extra.items()}

    audit_failures.append(entry)
    _audit_failure_total += 1
    logger.warning(
        "content audit write failed action=%s target=%s phase=%s error=%s: %s",
        action,
        entry["target_id"],
        phase,
        error_type,
        error_summary,
    )
    return entry


def list_audit_failures(limit: int | None = None) -> list[dict[str, Any]]:
    """读取最近的审计失败记录（新的在前）。仅进程内、非持久化。"""
    entries = list(audit_failures)
    entries.reverse()
    if limit is not None:
        return entries[: max(0, int(limit))]
    return entries


def audit_failure_count() -> int:
    """自进程启动以来（或上次 reset）的审计失败累计次数（不受缓冲上限截断）。"""
    return _audit_failure_total


def reset_audit_failures() -> None:
    """清空失败缓冲与累计计数（仅测试/运维手工诊断使用，不影响 audit_logs）。"""
    global _audit_failure_total
    audit_failures.clear()
    _audit_failure_total = 0


async def _materialize_outer_transaction(db: AsyncSession) -> None:
    """确保「savepoint 之外」真的有一层数据库事务（SQLite 专有陷阱）。

    SQLite 语义：若当前没有任何事务，`SAVEPOINT` 会自己开一个事务，
    而最外层 SAVEPOINT 的 `RELEASE` 等价于 COMMIT。这会让「只写审计、默认不提交」
    在空会话里被悄悄提交，破坏与调用方事务的绑定关系。
    因此这里在检测到驱动层尚无事务时显式 BEGIN，把审计 savepoint 压到内层。
    """
    try:
        conn = await db.connection()
        raw = await conn.get_raw_connection()
        driver_conn = getattr(raw, "driver_connection", None)
        if getattr(driver_conn, "in_transaction", False):
            return
        if conn.dialect.name != "sqlite":
            # 其它方言：驱动在需要时自动开事务，无需兜底
            return
        await conn.exec_driver_sql("BEGIN")
    except Exception:  # noqa: BLE001 — 兜底失败不阻断审计写入尝试
        logger.debug("materialize outer transaction skipped", exc_info=True)


async def _flush_log_in_savepoint(db: AsyncSession, log: AuditLog) -> None:
    """在独立 savepoint 内 add+flush 一条审计日志。

    失败时异常会穿过 savepoint 上下文（由调用方在上下文之外处理），
    savepoint 回滚不会污染调用方的外层业务事务。
    """
    await _materialize_outer_transaction(db)
    async with db.begin_nested():
        db.add(log)
        await db.flush()


async def _discard_failed_log(db: AsyncSession, log: AuditLog) -> None:
    """把写失败的审计实例从 Session 中摘除，避免残留状态影响后续 flush/commit。"""
    try:
        if log in db:
            db.expunge(log)
    except Exception:  # noqa: BLE001 — 清理失败不应覆盖原始失败
        logger.debug("expunge failed audit log skipped", exc_info=True)


async def record_content_audit_result(
    db: AsyncSession,
    *,
    action: str,
    project_id,
    target_id,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    before: dict | None = None,
    after: dict | None = None,
    operation: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
    organization_id: uuid.UUID | None = None,
    commit: bool = False,
) -> AuditWriteResult:
    """写入内容级审计（只增不改），返回可区分的写入结果。

    返回 status 取值：written / skipped_disabled / skipped_unchanged / failed。
    A1 起默认 commit=False：只 flush 不提交，由调用方随业务事务一起提交；
    需要自行提交的调用方必须显式传 commit=True。

    savepoint 隔离：add/flush 在 `begin_nested()` 内执行，失败只回滚该 savepoint，
    外层业务事务仍然可用；异常在 savepoint 上下文之外捕获，不会向调用方抛出。

    正文全文不写入审计表：before_value/after_value 存摘要，diff_json 存字段级 diff。
    """
    if action not in CONTENT_AUDIT_ACTIONS:
        raise ValueError(
            f"不支持的内容审计 action：{action}（可选 {'/'.join(CONTENT_AUDIT_ACTIONS)}）"
        )
    if not getattr(settings, "content_audit_enabled", True):
        return AuditWriteResult(status=AUDIT_STATUS_SKIPPED_DISABLED)

    diff = build_field_diff(before, after, action=action)
    if action != ACTION_BACKFILL and not diff["changed_fields"]:
        return AuditWriteResult(status=AUDIT_STATUS_SKIPPED_UNCHANGED)

    meta = {
        "project_id": str(project_id) if project_id else None,
        "operation": operation,
        "source": source,
        "changed_fields": diff["changed_fields"],
        "changed_count": diff["changed_count"],
        "content_changed": diff["content_changed"],
        "content_version": (after or {}).get("version") or (before or {}).get("version"),
        "content_hash_before": (before or {}).get("content_hash"),
        "content_hash_after": (after or {}).get("content_hash"),
    }
    if metadata:
        meta.update(metadata)

    log = AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=CONTENT_AUDIT_TARGET_TYPE,
        target_id=str(target_id) if target_id else None,
        reason=reason,
        ip_hash=ip_hash,
        user_agent=user_agent,
        metadata_json=json.dumps(meta, ensure_ascii=False),
        organization_id=organization_id,
        before_value=(
            json.dumps(_value_summary(before), ensure_ascii=False) if before else None
        ),
        after_value=(
            json.dumps(_value_summary(after), ensure_ascii=False) if after else None
        ),
        project_id=str(project_id) if project_id else None,
        diff_json=json.dumps(diff, ensure_ascii=False),
        content_hash_before=(before or {}).get("content_hash"),
        content_hash_after=(after or {}).get("content_hash"),
        content_version=(after or {}).get("version") or (before or {}).get("version"),
        change_source=source or operation or action,
    )
    # A1：savepoint 隔离——失败只回滚审计写入本身，外层业务事务继续可用。
    # 异常必须在 begin_nested() 上下文之外处理，不能在上下文内部吞掉后继续使用
    # 已失败的 savepoint。
    try:
        await _flush_log_in_savepoint(db, log)
    except Exception as exc:  # noqa: BLE001 — 审计失败不得传染业务事务
        await _discard_failed_log(db, log)
        failure = record_audit_failure(
            action=action,
            project_id=project_id,
            target_id=target_id,
            phase="record_content_audit",
            error=exc,
            extra={
                "content_version": meta.get("content_version"),
                "changed_count": meta.get("changed_count"),
            },
        )
        return AuditWriteResult(status=AUDIT_STATUS_FAILED, failure=failure)
    if commit:
        await db.commit()
    return AuditWriteResult(status=AUDIT_STATUS_WRITTEN, log=log)


async def record_content_audit(
    db: AsyncSession,
    *,
    action: str,
    project_id,
    target_id,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    before: dict | None = None,
    after: dict | None = None,
    operation: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
    organization_id: uuid.UUID | None = None,
    commit: bool = False,
) -> AuditLog | None:
    """写入内容级审计（既有契约：成功返回 AuditLog，跳过/失败返回 None）。

    需要区分「开关关闭 / 无变更 / 写失败」的调用方请改用
    `record_content_audit_result()`。失败详情见 `list_audit_failures()`。
    """
    result = await record_content_audit_result(
        db,
        action=action,
        project_id=project_id,
        target_id=target_id,
        actor_id=actor_id,
        actor_type=actor_type,
        before=before,
        after=after,
        operation=operation,
        source=source,
        reason=reason,
        ip_hash=ip_hash,
        user_agent=user_agent,
        metadata=metadata,
        organization_id=organization_id,
        commit=commit,
    )
    return result.log


async def record_lifecycle_audit_result(
    db: AsyncSession,
    *,
    action: str,
    project_id,
    target_id,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    reason_code: str | None = None,
    gate: str | None = None,
    content_version: int | None = None,
    blocking_count: int | None = None,
    commit: bool = False,
) -> AuditWriteResult:
    """写入生命周期流转审计（成功 / 拒绝），返回可区分的写入结果。

    设计约束（A2）：
    - 复用 A1 的 savepoint 隔离 + 进程内失败登记，审计失败绝不改变业务/门禁结果；
    - 默认 commit=False：只 flush 不提交，由调用方随业务事务一并提交；
    - actor_id 固定 None、actor_type 固定 "system"（本批不传递操作人）；
    - 开关沿用 content_audit_enabled，不新增配置项；
    - 不写正文 diff 字段（content_hash_* 保持空），diff_json 仅描述生命周期状态变化，
      且 target_type 固定 chapter_lifecycle，不会成为正文 _last_known_state 的基准。
    """
    if action not in LIFECYCLE_AUDIT_ACTIONS:
        raise ValueError(
            f"不支持的生命周期审计 action：{action}"
            f"（可选 {'/'.join(LIFECYCLE_AUDIT_ACTIONS)}）"
        )
    if not getattr(settings, "content_audit_enabled", True):
        return AuditWriteResult(status=AUDIT_STATUS_SKIPPED_DISABLED)

    changed: list[str] = []
    fields: dict[str, Any] = {}
    if from_status != to_status:
        changed.append("lifecycle_status")
        fields["lifecycle_status"] = {
            "changed": True,
            "before": from_status,
            "after": to_status,
        }
    diff = {
        "action": action,
        "changed_fields": changed,
        "changed_count": len(changed),
        "content_changed": False,
        "fields": fields,
    }

    meta = {
        "project_id": str(project_id) if project_id else None,
        "from_status": from_status,
        "to_status": to_status,
        "reason_code": reason_code,
        "gate": gate,
        "content_version": content_version,
        "blocking_count": blocking_count,
        "source": "chapter_lifecycle",
        # 本批未接入操作人来源，显式标注「操作人未知」而不是伪造 actor
        "actor_known": False,
    }

    log = AuditLog(
        actor_id=None,
        actor_type="system",
        action=action,
        target_type=LIFECYCLE_AUDIT_TARGET_TYPE,
        target_id=str(target_id) if target_id else None,
        reason=reason,
        metadata_json=json.dumps(meta, ensure_ascii=False),
        project_id=str(project_id) if project_id else None,
        before_value=json.dumps({"lifecycle_status": from_status}, ensure_ascii=False),
        after_value=json.dumps({"lifecycle_status": to_status}, ensure_ascii=False),
        diff_json=json.dumps(diff, ensure_ascii=False),
        content_version=content_version,
        change_source=f"lifecycle:{gate}" if gate else "lifecycle",
    )
    try:
        await _flush_log_in_savepoint(db, log)
    except Exception as exc:  # noqa: BLE001 — 生命周期审计失败不得改变业务/门禁结果
        await _discard_failed_log(db, log)
        failure = record_audit_failure(
            action=action,
            project_id=project_id,
            target_id=target_id,
            target_type=LIFECYCLE_AUDIT_TARGET_TYPE,
            phase="record_lifecycle_audit",
            error=exc,
            extra={
                "reason_code": reason_code,
                "gate": gate,
                "from_status": from_status,
                "to_status": to_status,
            },
        )
        return AuditWriteResult(status=AUDIT_STATUS_FAILED, failure=failure)
    if commit:
        await db.commit()
    return AuditWriteResult(status=AUDIT_STATUS_WRITTEN, log=log)


async def commit_pending_rejection_audit(db: AsyncSession) -> bool:
    """提交路由侧「已 flush 未提交」的生命周期拒绝审计（B1/U1a 补正）。

    设计约束：
    - 只提交旁路这一条审计，成功返回 True；
    - 提交失败：``logger.exception`` 记录，再尝试 ``rollback`` 清理失败事务；
      rollback 自身失败也只记录，不上抛——调用方必须继续抛出原有的生命周期拒绝
      （409），不得把审计故障降级为 500；
    - 审计未持久化是可接受的降级结果，本批不扩展 A1 的进程内 failure 队列
      （该队列服务于审计写入阶段，不覆盖「审计 commit 阶段」失败）；
    - 只在 ``LifecycleRejected.audit_pending is True`` 时调用；``audit_pending``
      为 False（审计未写入或已随业务事务提交）时不得调用，避免空提交或多余回滚。
    """
    try:
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 — 审计提交失败不得覆盖原有生命周期拒绝
        logger.exception("lifecycle rejection audit commit failed; keeping 409")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 — rollback 失败同样不得覆盖拒绝语义
            logger.exception("lifecycle rejection audit rollback failed; keeping 409")
        return False


async def _last_known_state(db: AsyncSession, outline_id) -> dict | None:
    """取该章节最近一次内容审计的「变更后状态」，作为下一次 diff 的 before。

    用于调用方未显式提供 before 快照时（例如 after_content_saved 的统一收尾），
    保证「字段级 diff」始终相对上一次已知状态，而不是凭空比较。
    """
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.target_type == CONTENT_AUDIT_TARGET_TYPE,
                AuditLog.target_id == str(outline_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    summ = _loads(row.after_value) or {}
    return {
        "content": None,
        "content_hash": row.content_hash_after,
        "content_length": summ.get("content_length"),
        "word_count": summ.get("word_count"),
        "version": summ.get("version"),
        "lifecycle_status": summ.get("lifecycle_status"),
        "title": summ.get("title"),
        "outline_version_id": summ.get("outline_version_id"),
        "generation_record_id": summ.get("generation_record_id"),
    }


async def record_content_change(
    db: AsyncSession,
    *,
    outline,
    chapter_content,
    action: str = ACTION_CONTENT_SAVE,
    source: str | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    before: dict | None = None,
    operation: str | None = None,
    reason: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
    organization_id: uuid.UUID | None = None,
    commit: bool = False,
) -> AuditLog | None:
    """章节正文变更审计的统一入口（读取当前状态 → 与 before 做字段级 diff）。

    调用方在「修改正文之前」调用 snapshot_content_state() 抓取 before；
    未提供时回退为「该章节上一次审计的变更后状态」。
    返回 None 表示未变更 / 开关关闭（不写审计）。
    """
    if outline is None or chapter_content is None:
        return None
    after = snapshot_content_state(chapter_content, outline)
    if before is None:
        before = await _last_known_state(db, outline.id)
    if source is None:
        source = "manual"
    return await record_content_audit(
        db,
        action=action,
        project_id=getattr(outline, "project_id", None),
        target_id=getattr(outline, "id", None),
        actor_id=actor_id,
        actor_type=actor_type,
        before=before,
        after=after,
        operation=operation,
        source=source,
        reason=reason,
        ip_hash=ip_hash,
        user_agent=user_agent,
        metadata=metadata,
        organization_id=organization_id,
        commit=commit,
    )


async def list_content_logs(
    db: AsyncSession,
    *,
    project_id=None,
    outline_id=None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AuditLog], int]:
    """内容级审计查询：按作品 / 章节 / action / 操作人 / 时间范围过滤（只读）。"""
    filters = [AuditLog.target_type == CONTENT_AUDIT_TARGET_TYPE]
    if project_id is not None:
        filters.append(AuditLog.project_id == str(project_id))
    if outline_id is not None:
        filters.append(AuditLog.target_id == str(outline_id))
    if action:
        filters.append(AuditLog.action == action)
    if actor_id is not None:
        filters.append(AuditLog.actor_id == actor_id)
    if from_dt is not None:
        filters.append(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        filters.append(AuditLog.created_at <= to_dt)

    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def record_backfill_audit(
    db: AsyncSession,
    *,
    project_id,
    outline_id,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "system",
    backfill_id: str | None = None,
    dry_run: bool = False,
    summary: dict | None = None,
    reason: str | None = None,
    commit: bool = False,
) -> AuditLog | None:
    """存量章节事实回填审计（action=backfill，阶段 K 回填任务调用）。

    回填不改正文，因此没有正文字段级 diff；此处记录「回填批次 id / dry-run 标记 /
    新增或更新的事实条数」等汇总信息，保证回填动作可追溯、可回滚定位。
    """
    metadata = {
        "backfill_id": backfill_id,
        "dry_run": dry_run,
        "backfill_summary": summary or {},
    }
    return await record_content_audit(
        db,
        action=ACTION_BACKFILL,
        project_id=project_id,
        target_id=outline_id,
        actor_id=actor_id,
        actor_type=actor_type,
        before=None,
        after=None,
        operation="backfill_state_facts",
        source="backfill",
        reason=reason or ("回填预演（dry-run）" if dry_run else "存量章节事实回填"),
        metadata=metadata,
        commit=commit,
    )


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def content_audit_trail(log: AuditLog) -> dict:
    """把一条内容审计日志序列化成 API 输出（字段级 diff 明细一并返回）。"""
    return {
        "id": str(log.id),
        "action": log.action,
        "action_label": ACTION_LABELS.get(log.action, log.action),
        "target_type": log.target_type,
        "outline_id": log.target_id,
        "project_id": log.project_id,
        "actor_id": str(log.actor_id) if log.actor_id else None,
        "actor_type": log.actor_type,
        "change_source": log.change_source,
        "content_version": log.content_version,
        "content_hash_before": log.content_hash_before,
        "content_hash_after": log.content_hash_after,
        "reason": log.reason,
        "diff": _loads(log.diff_json),
        "before_value": _loads(log.before_value),
        "after_value": _loads(log.after_value),
        "metadata": _loads(log.metadata_json),
        "created_at": log.created_at.isoformat() + "Z",
    }
