# 章节生命周期与可追溯元数据服务（第一阶段改造）
#
# 设计目标：小改动、可回滚、不实现全自动 AI 重写。
# 职责：
#   1. 生命周期状态机：draft → generated → validation_failed / pending_review
#      → approved → published。生成失败/截断不使用 lifecycle_status 伪装，
#      独立记录在 generation_records（incomplete/failed/postprocess_failed）。
#   2. 可追溯元数据：generation_meta / validation_report / repair_records /
#      outline_version_id / facts_version（JSON 以文本落库，兼容 SQLite）。
#   3. 保存/审批前确定性校验（复用 fact_validator）：有 blocking → validation_failed；
#      有 warning → pending_review；否则保持 generated（AI 来源）或原状态。
#      校验报告写入 outline.validation_report，并真实落库 chapter_contents
#      validation_summary / validation_updated_at（修复审查 R4）。
#   4. 编辑/重写/续写/恢复/删除后，后续章节标记 needs_recheck（pending_review），
#      不静默假设既有事实仍有效。
#
# 状态转换最小实现：approve/publish 仅写入状态并校验前置条件，不新增审批流。
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.chapter_fact_dependency import ChapterFactDependency
from app.models.generation import GenerationRecord
from app.models.outline import Outline
from app.models.outline_plan_version import OutlinePlanVersion
from app.services import audit_service
from app.services.fact_validator import (
    BLOCKING,
    WARNING,
    build_fact_summary,
    run_fact_validation,
)

logger = logging.getLogger(__name__)

# 生命周期状态常量
STATUS_DRAFT = "draft"
STATUS_GENERATED = "generated"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_PUBLISHED = "published"

LIFECYCLE_STATES = [
    STATUS_DRAFT,
    STATUS_GENERATED,
    STATUS_VALIDATION_FAILED,
    STATUS_PENDING_REVIEW,
    STATUS_APPROVED,
    STATUS_PUBLISHED,
]

# 允许保存为正式章节的 AI 生成状态（未完成/失败一律拒绝，见 PUT /content 校验）
# succeeded：continue/rewrite/polish 预览在 2026-11-12 之前一律按 succeeded 落库，
#   这些历史记录仍带着用户预览过的完整正文，继续允许接受，避免旧数据永久无法采用。
SAVEABLE_GENERATION_STATUSES = ("completed", "over_target", "succeeded")

# 可进入 approved 的来源状态（D2 状态机）
# validation_failed 不在其中：必须重新保存/校验通过后才可审批，禁止失败态直接批准。
APPROVE_FROM_STATUSES = {
    STATUS_DRAFT,
    STATUS_GENERATED,
    STATUS_PENDING_REVIEW,
}

# 可进入 published 的来源状态（必须先审批通过）
PUBLISH_FROM_STATUSES = {STATUS_APPROVED}

# 兼容旧名（历史引用，语义已收敛为 APPROVE_FROM_STATUSES）
APPROVABLE_STATUSES = APPROVE_FROM_STATUSES

# 资源状态（outline.status）：与 lifecycle_status 正交，发布前置门禁需同时判定。
# taken_down 不是 lifecycle 枚举值（U1a 前提确认）。
RESOURCE_STATUS_ARCHIVED = "archived"
RESOURCE_STATUS_TAKEN_DOWN = "taken_down"

# set_lifecycle_status 允许直写的目标状态。
# approved / published 属于审批与发布通道专属，不得经通用写入器绕过门禁。
EXPLICIT_WRITE_TARGETS = {
    STATUS_DRAFT,
    STATUS_GENERATED,
    STATUS_PENDING_REVIEW,
    STATUS_VALIDATION_FAILED,
}


# ===== A2：生命周期流转审计 =====
class LifecycleRejected(ValueError):
    """生命周期门禁拒绝（带「拒绝审计是否已落库待提交」标记）。

    两种形态必须区分，否则路由会漏提交或多提交：
      - audit_pending=True  ：审计行只 flush 未提交，由路由在 409 之前提交一次；
      - audit_pending=False ：service 已在既有业务事务内提交（blocking 拒绝与
                              validation_failed 同事务），路由不得再次提交。
    继承 ValueError，保证既有 `except ValueError → 409` 语义不破。
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        gate: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        blocking_count: int | None = None,
        content_version: int | None = None,
        audit_pending: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.gate = gate
        self.from_status = from_status
        self.to_status = to_status
        self.blocking_count = blocking_count
        self.content_version = content_version
        self.audit_pending = audit_pending


async def _record_lifecycle_event(
    db: AsyncSession,
    outline: Outline,
    *,
    action: str,
    from_status: str | None,
    to_status: str | None,
    reason: str,
    reason_code: str | None = None,
    gate: str | None = None,
    content_version: int | None = None,
    blocking_count: int | None = None,
) -> bool:
    """写一条生命周期审计，返回「是否还有未提交的审计行」。

    - 只 flush 不提交（record_lifecycle_audit_result(commit=False)）；
    - 审计失败/开关关闭都不抛异常、不改变门禁结果，仅返回 False（无可提交内容）；
    - 开关关闭时返回 False，路由因此不会产生空提交。
    """
    from app.services import audit_service

    result = await audit_service.record_lifecycle_audit_result(
        db,
        action=action,
        project_id=getattr(outline, "project_id", None),
        target_id=outline.id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        reason_code=reason_code,
        gate=gate,
        content_version=content_version,
        blocking_count=blocking_count,
    )
    if result.written:
        return True
    if result.status == audit_service.AUDIT_STATUS_FAILED:
        logger.warning(
            "lifecycle audit write failed action=%s chapter=%s gate=%s",
            action,
            outline.id,
            gate,
        )
    return False


def _anchor_reject_code(
    report: dict | None, content: ChapterContent | None
) -> str:
    """把锚点门禁的三类拒绝映射到原因码（供审计与排障区分）。"""
    from app.services import audit_service

    anchor = _read_validation_anchor(report)
    if anchor is None or _as_int(anchor.get("content_version")) is None:
        # 缺锚点 / 锚点结构不完整：历史报告，需重新校验
        return audit_service.LIFECYCLE_REJECT_ANCHOR_MISSING
    if _as_int(getattr(content, "version", None)) is None:
        # 锚点存在但当前正文版本不可解析：按校验门禁拒绝，绝不视为「无版本信息可放行」
        return audit_service.LIFECYCLE_REJECT_CONTENT_VERSION_UNKNOWN
    return audit_service.LIFECYCLE_REJECT_ANCHOR_STALE


# 编辑/重写/续写/恢复/删除后，后续章节被标记的状态
RECHECK_STATUS = STATUS_PENDING_REVIEW

VALIDATION_OK_STATUS = "generated"

# D3：校验报告的正文版本锚点键名。
# 报告只在「校验当时的正文版本」上成立；正文被任何路径改动（保存/恢复/
# AI 采纳/同步）后 version 递增，旧报告的结论即失效，必须重新校验。
ANCHOR_REPORT_KEY = "validation_anchor"


def _json_text(value: Any) -> str | None:
    """任意可 JSON 化的对象 → 文本（空 dict/list/None 返回 None）。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _load_json_text(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        return {"raw": value}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _collect_blocking_issues(report: dict | None) -> list[dict]:
    """统一阻断判定（D2）：severity=blocking 且未被豁免（exempted=True）。

    approve 与 publish 必须走同一判定，避免“审批能过、发布被拦”或反向的分叉。
    """
    issues = (report or {}).get("issues") or []
    return [
        i
        for i in issues
        if isinstance(i, dict)
        and i.get("severity") == BLOCKING
        and not i.get("exempted", False)
    ]


def _assert_transition(current: str, allowed_from: set[str], action: str) -> None:
    """状态机守卫（D2）：来源状态不在白名单即拒绝，拒绝分支不写库、不提交。"""
    if current not in allowed_from:
        allowed = "、".join(sorted(allowed_from))
        raise ValueError(
            f"章节当前状态为 {current}，不能执行{action}（允许来源：{allowed}）"
        )


def _assert_validation_report(report: dict | None, action: str) -> None:
    """校验门禁（D1）：报告必须来自一次成功的确定性校验（validated is True）。"""
    if not isinstance(report, dict) or report.get("validated") is not True:
        raise ValueError(
            f"章节确定性校验未完成或校验异常，不能执行{action}，"
            "请重新保存/校验生成有效报告后再试"
        )


def _as_int(value: Any) -> int | None:
    """版本号归一：None / 非法值返回 None（不抛异常，由调用方判定）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_validation_anchor(
    outline: Outline, chapter_content: ChapterContent
) -> dict[str, Any]:
    """组装校验锚点（D3）：报告绑定到「本次校验所依据的正文版本」。

    content_version 是硬锚点：正文版本在保存/恢复/AI 采纳路径中同步递增，
    因此可作为“报告是否仍然适用”的确定性判据。
    facts_version 仅做记录：事实抽取在后台独立 session 异步 bump，
    不能作为等值阻断条件（会误伤刚保存的章节）。
    """
    facts_version = _as_int(getattr(outline, "facts_version", None))
    if facts_version is None:
        facts_version = _as_int(getattr(chapter_content, "facts_version", None))
    outline_version_id = getattr(outline, "outline_version_id", None)
    return {
        "content_version": _as_int(
            getattr(chapter_content, "version", None)
        ),
        "facts_version": facts_version,
        "outline_version_id": str(outline_version_id) if outline_version_id else None,
        "anchored_at": _now_iso(),
    }


def _read_validation_anchor(report: dict | None) -> dict | None:
    """读取报告锚点；缺失或结构异常返回 None（按“缺锚点”处理）。"""
    anchor = (report or {}).get(ANCHOR_REPORT_KEY)
    return anchor if isinstance(anchor, dict) else None


def _assert_validation_anchor(
    report: dict | None,
    content: ChapterContent | None,
    action: str,
) -> dict[str, Any]:
    """锚点门禁（D3）：缺锚点或正文版本已变化的报告一律拒绝。

    拒绝分支不写库（调用方不提交），避免“过期报告换来一次状态跃迁”。
    """
    anchor = _read_validation_anchor(report)
    if anchor is None:
        raise ValueError(
            f"校验报告缺少正文版本锚点（历史报告），不能执行{action}，"
            "请重新保存/校验生成带锚点的报告后再试"
        )
    anchored_version = _as_int(anchor.get("content_version"))
    if anchored_version is None:
        raise ValueError(
            f"校验报告的正文版本锚点不完整，不能执行{action}，"
            "请重新保存/校验后再试"
        )
    current_version = _as_int(getattr(content, "version", None))
    if current_version is None:
        raise ValueError(
            f"章节正文版本缺失，无法确认{action}所依据的正文，请重新保存后再试"
        )
    if anchored_version != current_version:
        raise ValueError(
            f"校验报告锚定的正文版本为 v{anchored_version}，"
            f"当前正文已是 v{current_version}，报告已过期，不能执行{action}，"
            "请重新保存/校验后再试"
        )
    return anchor


def build_facts_version_hint(
    anchor: dict[str, Any] | None,
    outline: Outline,
    content: ChapterContent | None,
) -> dict[str, Any] | None:
    """事实版本提示（D3，非阻断）：锚点后 facts_version 变化只提示不拦。

    事实抽取在保存后由后台异步任务 bump facts_version，故其变化不代表
    正文被改动；这里只作为“建议复核设定一致性”的软提示。
    """
    anchored = _as_int((anchor or {}).get("facts_version"))
    current = _as_int(getattr(outline, "facts_version", None))
    if current is None:
        current = _as_int(getattr(content, "facts_version", None))
    if anchored is None or current is None or anchored == current:
        return None
    return {
        "blocking": False,
        "anchored_facts_version": anchored,
        "current_facts_version": current,
        "message": (
            f"事实版本已从 v{anchored} 变为 v{current}（后台事实抽取异步更新），"
            "不阻断本次操作，建议复核设定一致性"
        ),
    }


def build_generation_meta(record: GenerationRecord) -> dict[str, Any]:
    """由生成记录组装可追溯元数据（不落库的中间结构）。"""
    meta: dict[str, Any] = {
        "generation_record_id": str(record.id),
        "action_type": record.action_type,
        "model_provider": record.model_provider,
        "model_name": record.model_name,
        "temperature": record.temperature,
        "max_tokens": record.max_tokens,
        "status": record.status,
        "finish_reason": record.finish_reason,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "was_truncated": record.was_truncated,
        "truncation_reason": record.truncation_reason,
        "review_flags": _load_json_text(record.review_flags),
        "generated_at": (
            record.created_at.isoformat() if record.created_at else None
        ),
        "outline_plan_version_id": (
            str(record.outline_plan_version_id)
            if record.outline_plan_version_id
            else None
        ),
    }
    return meta


async def get_outline_plan_version(
    db: AsyncSession, record: GenerationRecord
) -> OutlinePlanVersion | None:
    """按生成记录溯源已确认大纲版本（供 outline_version_id 回填）。"""
    if record.outline_plan_version_id is None:
        return None
    return await db.get(OutlinePlanVersion, record.outline_plan_version_id)


def _apply_recheck_to_outline(
    ch: Outline, changed_outline: Outline
) -> None:
    """将单个章节标记为需要重新校验（pending_review + needs_recheck）。

    只改生命周期状态与校验报告标记，不自动改正文（阶段H约束）。
    """
    report = _load_json_text(ch.validation_report) or {}
    report["needs_recheck"] = True
    report["recheck_reason"] = (
        f"前置章节 {changed_outline.title or str(changed_outline.id)} 已变更，"
        f"后续事实/设定可能失效"
    )
    report["recheck_marked_at"] = _now_iso()
    ch.validation_report = _json_text(report)
    if ch.lifecycle_status in (
        STATUS_DRAFT,
        STATUS_GENERATED,
        STATUS_APPROVED,
        STATUS_PUBLISHED,
    ):
        ch.lifecycle_status = STATUS_PENDING_REVIEW


async def _collect_later_chapters(
    db: AsyncSession, changed_outline: Outline
) -> list[Outline]:
    """收集同项目 sort_order 更大的后续章节（status != deleted）。"""
    result = await db.execute(
        select(Outline).where(
            Outline.project_id == changed_outline.project_id,
            Outline.outline_type == "chapter",
            Outline.status != "deleted",
        )
    )
    changed_order = changed_outline.sort_order or 0
    changed_id = str(changed_outline.id)
    return [
        ch
        for ch in result.scalars().all()
        if (ch.sort_order or 0) > changed_order and str(ch.id) != changed_id
    ]


async def _mark_recheck_all_later(
    db: AsyncSession, changed_outline: Outline
) -> int:
    """回退路径：全量标记后续章节（旧行为，依赖未知时兼容）。"""
    marked = 0
    for ch in await _collect_later_chapters(db, changed_outline):
        _apply_recheck_to_outline(ch, changed_outline)
        marked += 1
    return marked


async def compute_dependency_impact_set(
    db: AsyncSession, changed_outline: Outline
) -> list[Outline] | None:
    """阶段H「依赖图增量回归」：按依赖图计算「受变更影响的后续章节」集合。

    归一化依赖键：
      - 实体级键 ("e", "entity_type:entity_id")：只要后续章依赖同一实体即命中；
      - 名字级键 ("k", "fact_key")：实体匹配不到时的兜底键（fact_key 内嵌名字）。

    返回：
      - None：变更章无任何依赖记录（依赖未知），调用方应回退为全量标记；
      - []：有依赖记录但无后续章受影响；
      - [Outline]：受影响章节列表。
    只读计算，不修改任何章节。
    """
    changed_deps = (
        await db.execute(
            select(ChapterFactDependency).where(
                ChapterFactDependency.chapter_id == changed_outline.id
            )
        )
    ).scalars().all()
    if not changed_deps:
        return None

    changed_keys: set[tuple[str, str]] = set()
    for d in changed_deps:
        if d.entity_id is not None:
            changed_keys.add(("e", f"{d.entity_type}:{d.entity_id}"))
        changed_keys.add(("k", d.fact_key))

    later = await _collect_later_chapters(db, changed_outline)
    if not later:
        return []

    # 一次性拉取所有后续章的依赖行，避免 N+1
    later_deps = (
        await db.execute(
            select(ChapterFactDependency).where(
                ChapterFactDependency.chapter_id.in_([ch.id for ch in later])
            )
        )
    ).scalars().all()
    deps_by_chapter: dict[str, set[tuple[str, str]]] = {}
    for d in later_deps:
        key_set = deps_by_chapter.setdefault(str(d.chapter_id), set())
        if d.entity_id is not None:
            key_set.add(("e", f"{d.entity_type}:{d.entity_id}"))
        key_set.add(("k", d.fact_key))

    impacted = [
        ch
        for ch in later
        if deps_by_chapter.get(str(ch.id), set()) & changed_keys
    ]
    return impacted


async def mark_later_chapters_needs_recheck(
    db: AsyncSession, changed_outline: Outline
) -> int:
    """编辑/重写/续写/恢复/删除章节后，将受影响的后续章节标记为需要重新校验。

    阶段H「依赖图增量回归」：优先按依赖图计算受影响集合，仅标记与本章依赖键
    有交集的后续章节（pending_review + validation_report.needs_recheck）；
    变更章无依赖记录（依赖未知）时回退为标记全部后续章节（兼容旧行为）。

    只改生命周期状态与校验报告标记，不自动改正文。
    返回被标记的章节数。
    """
    if changed_outline.outline_type != "chapter":
        return 0
    impacted = await compute_dependency_impact_set(db, changed_outline)
    if impacted is None:
        # 依赖未知 → 回退旧行为：全量标记后续章
        return await _mark_recheck_all_later(db, changed_outline)
    marked = 0
    for ch in impacted:
        _apply_recheck_to_outline(ch, changed_outline)
        marked += 1
    return marked


async def after_content_saved(
    db: AsyncSession,
    outline: Outline,
    chapter_content: ChapterContent,
    record: GenerationRecord | None = None,
    *,
    mark_following: bool = True,
    audit_action: str | None = None,
    audit_source: str | None = None,
    audit_before: dict | None = None,
    audit_actor_id: Any | None = None,
    audit_actor_type: str = "user",
    audit_operation: str | None = None,
    audit_reason: str | None = None,
    audit_metadata: dict | None = None,
) -> dict[str, Any]:
    """保存正文后统一执行的「元数据 + 确定性校验 + 生命周期」收尾。

    调用时机：PUT /content 保存、restore、ai_replace/ai_insert/ai_apply_blocks、
    删除等一切改变正文/设定的路径（E1/E3 治理）。

    行为：
      - AI 来源且记录可保存：写 generation_meta / outline_version_id /
        lifecycle_status=generated（同步正文表冗余字段）；
      - 手工来源：不置 generated，但编辑会触发后续重校验；
      - 确定性校验（复用 fact_validator）：blocking → validation_failed，
        warning → pending_review，干净 → 保持 generated/原状态；
        校验报告写入 outline.validation_report 并真实落库
        chapter_content.validation_summary / validation_updated_at（R4）；
      - 默认把后续章节标记 needs_recheck。

    返回 summary 结构（供路由响应附加，不阻塞保存）。
    """
    now = datetime.utcnow()
    changed = False

    # 1) 生成元数据与版本溯源（仅 AI 来源）
    if record is not None:
        meta = build_generation_meta(record)
        outline.generation_meta = _json_text(meta)
        outline.outline_version_id = record.outline_plan_version_id
        if outline.lifecycle_status in (None, STATUS_DRAFT, ""):
            outline.lifecycle_status = STATUS_GENERATED
        elif outline.lifecycle_status in (
            STATUS_VALIDATION_FAILED,
            STATUS_PENDING_REVIEW,
        ):
            # 重新生成后清掉旧校验失败标记，等待本轮校验结果
            outline.lifecycle_status = STATUS_GENERATED
        chapter_content.generation_meta = _json_text(meta)
        chapter_content.outline_version_id = record.outline_plan_version_id
        changed = True
    elif outline.lifecycle_status in (
        STATUS_GENERATED,
        STATUS_APPROVED,
        STATUS_PUBLISHED,
    ):
        # 手工编辑正式内容：清空“未经验证的生成”假设，等待校验
        outline.lifecycle_status = STATUS_GENERATED
        changed = True

    # 2) 确定性校验（只读；单条规则内部异常不阻断整体）
    issues = []
    validation_error: dict[str, Any] | None = None
    try:
        issues = await run_fact_validation(
            db, outline.project_id, chapter_id=outline.id
        )
    except Exception as exc:  # noqa: BLE001
        # D1：校验器异常不能被等价成“零问题通过”，必须留下可判定痕迹并阻断门禁
        validation_error = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
        logger.exception("lifecycle validation failed for chapter %s", outline.id)

    summary = build_fact_summary(issues)
    issue_list = [i.to_dict() for i in issues]
    blocking_count = summary["counts"].get("blocking", 0)
    warning_count = summary["counts"].get("warning", 0)
    validated = validation_error is None

    report: dict[str, Any] = {
        "issues": issue_list,
        "summary": summary,
        "needs_recheck": False,
        "validated_at": _now_iso(),
        "validated": validated,
        "validation_error": validation_error,
        # D3：把报告绑定到本次校验所依据的正文版本（硬锚点），
        # 供 approve/publish 判定“报告是否仍然适用于当前正文”。
        ANCHOR_REPORT_KEY: build_validation_anchor(outline, chapter_content),
    }
    outline.validation_report = _json_text(report)
    chapter_content.validation_summary = _json_text(summary)
    chapter_content.validation_updated_at = now

    if not validated:
        # 校验异常 = 校验未通过（D1），不得落到 generated 造成“看起来已校验”
        outline.lifecycle_status = STATUS_VALIDATION_FAILED
    elif blocking_count > 0:
        outline.lifecycle_status = STATUS_VALIDATION_FAILED
    elif warning_count > 0:
        outline.lifecycle_status = STATUS_PENDING_REVIEW
    else:
        outline.lifecycle_status = STATUS_GENERATED

    # 同步正文表冗余生命周期
    chapter_content.lifecycle_status = outline.lifecycle_status

    # 3) 后续章节重校验标记
    if mark_following:
        try:
            await mark_later_chapters_needs_recheck(db, outline)
        except Exception:  # noqa: BLE001
            logger.exception("mark following chapters recheck failed")

    changed = changed or True

    # 4) 阶段J「内容级可追溯审计」：记录本次正文/元数据变更的字段级 diff。
    #    审计只增不改；失败不阻断保存；开关关闭时静默跳过（返回 None）。
    try:
        from app.services import audit_service

        await audit_service.record_content_change(
            db,
            outline=outline,
            chapter_content=chapter_content,
            action=audit_action or audit_service.ACTION_CONTENT_SAVE,
            source=audit_source or ("ai" if record is not None else "manual"),
            actor_id=audit_actor_id,
            actor_type=audit_actor_type,
            before=audit_before,
            operation=audit_operation,
            reason=audit_reason,
            metadata=audit_metadata,
        )
    except Exception:  # noqa: BLE001 — 审计写入失败不得影响保存主链路
        logger.exception("content audit failed for chapter %s", outline.id)

    if changed:
        await db.commit()
        try:
            await db.refresh(chapter_content)
            await db.refresh(outline)
        except Exception:  # noqa: BLE001
            pass

    return {
        "valid": summary["valid"],
        "blocking": blocking_count,
        "warning": warning_count,
        "lifecycle_status": outline.lifecycle_status,
        "validated": validated,
        "validation_error": validation_error,
    }


async def approve_chapter(
    db: AsyncSession,
    outline: Outline,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """审批通过（最小状态转换，不新增审批流）。

    前置：存在正文；存在 blocking 问题（validation_failed）时拒绝批准，
    要求先修复/重写（第一阶段只做状态阻断，不自动修复）。
    批准后置 approved，并把 blocking 级问题写入 repair_records 记录。
    """
    content = (
        await db.execute(
            select(ChapterContent).where(
                ChapterContent.outline_id == outline.id
            )
        )
    ).scalar_one_or_none()
    if content is None or not (content.content or "").strip():
        raise ValueError("章节无正文，不能审批通过")

    # A2：拒绝事件需要「拒绝前的来源状态」与「所依据的正文版本」，在任何写入前固化
    from_status = outline.lifecycle_status
    content_version = _as_int(getattr(content, "version", None))

    report = _load_json_text(outline.validation_report)
    if not isinstance(report, dict):
        report = {}
    # D2：状态机（来源白名单）与校验门禁（validated is True）前置，拒绝分支不写库
    try:
        _assert_transition(from_status, APPROVE_FROM_STATUSES, "审批通过")
    except ValueError as exc:
        pending = await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
            from_status=from_status,
            to_status=from_status,
            reason=str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED,
            gate="state_machine",
            content_version=content_version,
        )
        raise LifecycleRejected(
            str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED,
            gate="state_machine",
            from_status=from_status,
            to_status=from_status,
            content_version=content_version,
            audit_pending=pending,
        ) from exc

    try:
        _assert_validation_report(report, "审批通过")
    except ValueError as exc:
        pending = await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
            from_status=from_status,
            to_status=from_status,
            reason=str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_VALIDATION_REPORT_MISSING,
            gate="validation_report",
            content_version=content_version,
        )
        raise LifecycleRejected(
            str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_VALIDATION_REPORT_MISSING,
            gate="validation_report",
            from_status=from_status,
            to_status=from_status,
            content_version=content_version,
            audit_pending=pending,
        ) from exc

    # D3：报告必须锚定当前正文版本（缺锚点 / 正文已变一律拒绝，拒绝分支不写库）
    try:
        anchor = _assert_validation_anchor(report, content, "审批通过")
    except ValueError as exc:
        reason_code = _anchor_reject_code(report, content)
        pending = await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
            from_status=from_status,
            to_status=from_status,
            reason=str(exc),
            reason_code=reason_code,
            gate="validation_anchor",
            content_version=content_version,
        )
        raise LifecycleRejected(
            str(exc),
            reason_code=reason_code,
            gate="validation_anchor",
            from_status=from_status,
            to_status=from_status,
            content_version=content_version,
            audit_pending=pending,
        ) from exc
    facts_hint = build_facts_version_hint(anchor, outline, content)
    if facts_hint is not None:
        report["facts_version_hint"] = facts_hint
        outline.validation_report = _json_text(report)
    blocking = _collect_blocking_issues(report)
    if blocking:
        # 状态阻断 + 可审计记录：问题清单写入 repair_records（本阶段不自动修复）
        repair: dict[str, Any] = {
            "pending_blocking_issues": blocking,
            "mode": "blocked_await_manual_or_rewrite",
            "created_at": _now_iso(),
        }
        outline.repair_records = _json_text(repair)
        outline.lifecycle_status = STATUS_VALIDATION_FAILED
        # A2：拒绝事件与本次 validation_failed 变更同一事务提交（commit 紧随其后）
        await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
            from_status=from_status,
            to_status=STATUS_VALIDATION_FAILED,
            reason=f"存在 {len(blocking)} 个阻断性问题，审批被拒绝",
            reason_code=audit_service.LIFECYCLE_REJECT_BLOCKING,
            gate="blocking_issues",
            content_version=content_version,
            blocking_count=len(blocking),
        )
        await db.commit()
        raise LifecycleRejected(
            f"章节存在 {len(blocking)} 个阻断性问题（validation_failed），"
            "请先重写/修复后再审批",
            reason_code=audit_service.LIFECYCLE_REJECT_BLOCKING,
            gate="blocking_issues",
            from_status=from_status,
            to_status=STATUS_VALIDATION_FAILED,
            blocking_count=len(blocking),
            content_version=content_version,
            audit_pending=False,
        ) from None

    outline.lifecycle_status = STATUS_APPROVED
    outline.repair_records = None
    if actor_id:
        report["approved_by"] = str(actor_id)
        report["approved_at"] = _now_iso()
        outline.validation_report = _json_text(report)
    # A2：成功事件在既有业务 commit 之前写入，随后随业务事务一次提交
    await _record_lifecycle_event(
        db,
        outline,
        action=audit_service.ACTION_LIFECYCLE_APPROVE,
        from_status=from_status,
        to_status=STATUS_APPROVED,
        reason=f"审批通过：{from_status} → {STATUS_APPROVED}",
        gate="approve",
        content_version=content_version,
    )
    await db.commit()
    return {
        "lifecycle_status": STATUS_APPROVED,
        "approved": True,
        "blocking": 0,
        "message": facts_hint["message"] if facts_hint else None,
        "warnings": [facts_hint["message"]] if facts_hint else [],
        "facts_version_hint": facts_hint,
    }


async def _publish_state_rejection(
    db: AsyncSession,
    outline: Outline,
    *,
    reason: str,
    reason_code: str,
    gate: str,
    from_status: str | None,
    content_version: int | None = None,
) -> LifecycleRejected:
    """写一条发布拒绝审计（仅 flush），返回待抛出的 LifecycleRejected。

    返回异常对象而不是内部 raise，便于调用点保留 ``raise ... from exc`` 语义。
    """
    pending = await _record_lifecycle_event(
        db,
        outline,
        action=audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED,
        from_status=from_status,
        to_status=from_status,
        reason=reason,
        reason_code=reason_code,
        gate=gate,
        content_version=content_version,
    )
    return LifecycleRejected(
        reason,
        reason_code=reason_code,
        gate=gate,
        from_status=from_status,
        to_status=from_status,
        content_version=content_version,
        audit_pending=pending,
    )


async def assert_publish_state(
    db: AsyncSession,
    outline: Outline,
    content: ChapterContent | None = None,
) -> None:
    """发布前置状态门禁（B1/U1a）：只读判定，通过时不写任何列。

    判定顺序（R-2：状态门禁优先于正文/内容体检的 400/422）：
      1. ``outline.status == archived``   → reason_code=archived（gate=resource_archived）
      2. ``outline.status == taken_down`` → reason_code=taken_down（gate=resource_taken_down）
      3. ``lifecycle_status`` 不在 PUBLISH_FROM_STATUSES → state_not_allowed（gate=state_machine）

    拒绝时写一条 ``lifecycle_publish_rejected`` 审计（flush 未提交，是否提交由调用方
    依据 ``LifecycleRejected.audit_pending`` 决定），并抛 LifecycleRejected；
    通过时既不写审计也不改列，可安全重复调用（外部通道与路由两侧都调用也不会重复记账）。

    visibility 不在本门禁判定范围内（R-5：继续由快照/Publication 通道维护，语义守卫归 B4）。
    """
    from_status = outline.lifecycle_status
    content_version = _as_int(getattr(content, "version", None))
    resource_status = getattr(outline, "status", None)

    if resource_status == RESOURCE_STATUS_ARCHIVED:
        raise await _publish_state_rejection(
            db,
            outline,
            reason="章节已归档，不能发布；请先恢复章节",
            reason_code=audit_service.LIFECYCLE_REJECT_ARCHIVED,
            gate="resource_archived",
            from_status=from_status,
            content_version=content_version,
        )
    if resource_status == RESOURCE_STATUS_TAKEN_DOWN:
        raise await _publish_state_rejection(
            db,
            outline,
            reason="章节已下架，不能直接发布；请先走恢复/重发流程",
            reason_code=audit_service.LIFECYCLE_REJECT_TAKEN_DOWN,
            gate="resource_taken_down",
            from_status=from_status,
            content_version=content_version,
        )
    try:
        _assert_transition(from_status, PUBLISH_FROM_STATUSES, "发布")
    except ValueError as exc:
        raise await _publish_state_rejection(
            db,
            outline,
            reason=str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED,
            gate="state_machine",
            from_status=from_status,
            content_version=content_version,
        ) from exc


@dataclass(slots=True)
class PublishFacts:
    """发布门禁通过后的事实快照（U2a）。

    只承载「写透点与编排器需要共享的只读事实」，不携带任何待提交写入；
    ``facts_version_hint`` 为 D3 既有语义（报告与正文版本不一致时的提示）。
    """

    from_status: str | None
    content_version: int | None
    facts_version_hint: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


async def _load_publish_content(
    db: AsyncSession, outline: Outline
) -> ChapterContent | None:
    return (
        await db.execute(
            select(ChapterContent).where(ChapterContent.outline_id == outline.id)
        )
    ).scalar_one_or_none()


async def _publish_validation_rejection(
    db: AsyncSession,
    outline: Outline,
    *,
    reason: str,
    reason_code: str,
    gate: str,
    from_status: str | None,
    content_version: int | None,
    to_status: str | None = None,
    blocking_count: int | None = None,
) -> LifecycleRejected:
    """写一条发布拒绝审计（仅 flush），返回待抛出的 LifecycleRejected（U2a）。

    与 ``_publish_state_rejection`` 的区别：本函数服务于「内容/校验层」拒绝，
    允许携带 ``to_status``（blocking 拒绝会先置 validation_failed）；
    是否提交由调用方依据 ``audit_pending`` 决定，service 自身不提交。
    """
    target_status = to_status or from_status
    pending = await _record_lifecycle_event(
        db,
        outline,
        action=audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED,
        from_status=from_status,
        to_status=target_status,
        reason=reason,
        reason_code=reason_code,
        gate=gate,
        content_version=content_version,
        blocking_count=blocking_count,
    )
    return LifecycleRejected(
        reason,
        reason_code=reason_code,
        gate=gate,
        from_status=from_status,
        to_status=target_status,
        blocking_count=blocking_count,
        content_version=content_version,
        audit_pending=pending,
    )


async def validate_publish(
    db: AsyncSession,
    outline: Outline,
    *,
    require_validation_evidence: bool = True,
) -> PublishFacts:
    """发布门禁（U2a）：状态门禁 → 正文 → 校验报告 → 锚点 → 阻断判定。

    只读语义为主：通过时返回 PublishFacts，**不提交、不改发布三列**；
    唯一写入是 D3 既有的 facts_version_hint 回写 validation_report（随业务事务落库）。
    拒绝时写一条拒绝审计（flush 未提交）后抛 LifecycleRejected，
    由路由依据 ``audit_pending`` 决定是否提交一次，service 绝不自行提交。

    ``require_validation_evidence=False``：仅执行状态门禁与正文存在性判定，
    供 E1/E2 沿用各自既有内容体检，不扩大拒绝面。
    """
    content = await _load_publish_content(db, outline)

    # A2：拒绝事件所需的来源状态与所依据正文版本，在任何写入前固化
    from_status = outline.lifecycle_status
    content_version = _as_int(getattr(content, "version", None))

    # B1/U1a：状态门禁前置（R-2）——已归档 / 已下架 / 非 approved 先拒绝，
    # 不因正文为空等体检问题降级为误导性的 400/422。
    await assert_publish_state(db, outline, content)

    if content is None or not (content.content or "").strip():
        raise ValueError("章节无正文，不能发布")

    facts = PublishFacts(from_status=from_status, content_version=content_version)
    if not require_validation_evidence:
        return facts

    report = _load_json_text(outline.validation_report)
    if not isinstance(report, dict):
        report = {}
    # D2：来源状态已由 assert_publish_state 卡过，这里继续卡校验报告与阻断判定
    try:
        _assert_validation_report(report, "发布")
    except ValueError as exc:
        raise await _publish_validation_rejection(
            db,
            outline,
            reason=str(exc),
            reason_code=audit_service.LIFECYCLE_REJECT_VALIDATION_REPORT_MISSING,
            gate="validation_report",
            from_status=from_status,
            content_version=content_version,
        ) from exc

    # D3：发布同样要求报告锚定当前正文版本（审批后正文若被改动则必须重校验）
    try:
        anchor = _assert_validation_anchor(report, content, "发布")
    except ValueError as exc:
        raise await _publish_validation_rejection(
            db,
            outline,
            reason=str(exc),
            reason_code=_anchor_reject_code(report, content),
            gate="validation_anchor",
            from_status=from_status,
            content_version=content_version,
        ) from exc

    facts_hint = build_facts_version_hint(anchor, outline, content)
    if facts_hint is not None:
        report["facts_version_hint"] = facts_hint
        outline.validation_report = _json_text(report)
        facts.facts_version_hint = facts_hint
        facts.warnings = [facts_hint.get("message")]

    blocking = _collect_blocking_issues(report)
    if blocking:
        # A2/U2a：validation_failed 与拒绝审计同一事务，由路由在 409 前提交一次
        outline.lifecycle_status = STATUS_VALIDATION_FAILED
        raise await _publish_validation_rejection(
            db,
            outline,
            reason="章节仍存在阻断性问题，发布被拒绝",
            reason_code=audit_service.LIFECYCLE_REJECT_BLOCKING,
            gate="blocking_issues",
            from_status=from_status,
            content_version=content_version,
            to_status=STATUS_VALIDATION_FAILED,
            blocking_count=len(blocking),
        )
    return facts


async def mark_published(
    db: AsyncSession,
    outline: Outline,
    facts: PublishFacts,
    *,
    actor_id: str | None = None,
) -> None:
    """发布写透（U2a 唯一发布写入点）：发布三列 + published_by + 成功审计。

    - 只 flush，不提交：调用方（编排器）把发布状态、Publication、索引/统计
      合并进同一次业务提交；
    - ``outline.lifecycle_status`` / ``outline.status`` / ``outline.published_at``
      仅在本函数写入，路由与 publication_service 一律禁写；
    - 成功审计动作固定 ``lifecycle_publish``，target_type 由
      ``record_lifecycle_audit_result`` 统一写为 ``chapter_lifecycle``。
    """
    outline.lifecycle_status = STATUS_PUBLISHED
    outline.status = "published"
    # 与 publication_service 一致：历史已有 published_at 时保留首次发布时间
    if outline.published_at is None:
        outline.published_at = datetime.utcnow()
    if actor_id:
        report = _load_json_text(outline.validation_report)
        if not isinstance(report, dict):
            report = {}
        report["published_by"] = str(actor_id)
        report["published_at"] = _now_iso()
        outline.validation_report = _json_text(report)
    # A2：成功事件在业务 commit 之前写入，随业务事务一次提交
    await _record_lifecycle_event(
        db,
        outline,
        action=audit_service.ACTION_LIFECYCLE_PUBLISH,
        from_status=facts.from_status,
        to_status=STATUS_PUBLISHED,
        reason=f"发布成功：{facts.from_status} → {STATUS_PUBLISHED}",
        gate="publish",
        content_version=facts.content_version,
    )


async def publish_chapter(
    db: AsyncSession,
    outline: Outline,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """发布兼容包装（D4 提交语义保持）：门禁 → 写透 → 唯一一次提交。

    U2a 起三个发布入口统一经 ``publish_orchestrator``，路由不得直接调用本函数；
    保留本包装仅为历史调用方与既有测试的兼容入口。
    blocking 拒绝仍沿用旧语义：validation_failed 与拒绝审计在本函数内提交一次，
    重抛时 ``audit_pending=False``，路由不得重复提交。
    """
    try:
        facts = await validate_publish(db, outline, require_validation_evidence=True)
    except LifecycleRejected as exc:
        if exc.to_status != STATUS_VALIDATION_FAILED:
            raise
        await db.commit()
        raise LifecycleRejected(
            str(exc),
            reason_code=exc.reason_code,
            gate=exc.gate,
            from_status=exc.from_status,
            to_status=exc.to_status,
            blocking_count=exc.blocking_count,
            content_version=exc.content_version,
            audit_pending=False,
        ) from exc

    await mark_published(db, outline, facts, actor_id=actor_id)
    await db.commit()
    return {
        "lifecycle_status": STATUS_PUBLISHED,
        "approved": True,
        "blocking": 0,
        "message": facts.facts_version_hint.get("message") if facts.facts_version_hint else None,
        "warnings": list(facts.warnings),
        "facts_version_hint": facts.facts_version_hint,
    }

async def set_lifecycle_status(
    db: AsyncSession,
    outline: Outline,
    target: str,
) -> dict[str, Any]:
    """通用状态写入（approve/publish 之外的显式状态调整，最小转换）。

    D2：不得作为绕过审批/发布门禁的后门——approved / published 仅由
    approve_chapter / publish_chapter 写入；已发布章节不得直接改写状态。
    """
    if target not in LIFECYCLE_STATES:
        # A2：非法状态值是入参错误，不是「流转被拒绝」，因此不写生命周期审计
        raise ValueError(f"未知生命周期状态: {target}")

    from_status = outline.lifecycle_status
    if target not in EXPLICIT_WRITE_TARGETS:
        reason = f"状态 {target} 只能通过审批/发布接口写入，不能直接设置"
        pending = await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
            from_status=from_status,
            to_status=target,
            reason=reason,
            reason_code=audit_service.LIFECYCLE_REJECT_TARGET_NOT_WRITABLE,
            gate="explicit_write_targets",
        )
        raise LifecycleRejected(
            reason,
            reason_code=audit_service.LIFECYCLE_REJECT_TARGET_NOT_WRITABLE,
            gate="explicit_write_targets",
            from_status=from_status,
            to_status=target,
            audit_pending=pending,
        ) from None
    if outline.lifecycle_status == STATUS_PUBLISHED:
        reason = "章节已发布，不能直接改写生命周期状态"
        pending = await _record_lifecycle_event(
            db,
            outline,
            action=audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
            from_status=from_status,
            to_status=target,
            reason=reason,
            reason_code=audit_service.LIFECYCLE_REJECT_ALREADY_PUBLISHED,
            gate="published_guard",
        )
        raise LifecycleRejected(
            reason,
            reason_code=audit_service.LIFECYCLE_REJECT_ALREADY_PUBLISHED,
            gate="published_guard",
            from_status=from_status,
            to_status=target,
            audit_pending=pending,
        ) from None
    outline.lifecycle_status = target
    # A2：成功事件在既有业务 commit 之前写入，随业务事务一次提交
    await _record_lifecycle_event(
        db,
        outline,
        action=audit_service.ACTION_LIFECYCLE_STATUS,
        from_status=from_status,
        to_status=target,
        reason=f"生命周期状态直写：{from_status} → {target}",
        gate="explicit_write_targets",
    )
    await db.commit()
    return {"lifecycle_status": target}


async def get_effective_generation_record(
    db: AsyncSession,
    project_id: Any,
    record_id: Any,
    expected_statuses: tuple[str, ...] = SAVEABLE_GENERATION_STATUSES,
) -> GenerationRecord | None:
    """按“可保存为正式章节”白名单读取生成记录；不存在/不属于本项目/状态不在
    白名单（incomplete/failed/postprocess_failed 等）返回 None。"""
    if record_id is None:
        return None
    record = await db.get(GenerationRecord, record_id)
    if record is None or str(record.project_id) != str(project_id):
        return None
    if record.status not in expected_statuses:
        return None
    return record
