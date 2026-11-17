# -*- coding: utf-8 -*-
"""阶段I「独立 LLM 审稿 + 补丁闭环」核心服务（只读审稿 / 只读候选 / 局部复检）。

三块能力：
1. 独立 LLM 连续性审稿（run_llm_consistency_review）
   - 独立于静态规则与既有 AI 一致性检查，对章节正文做叙事层审稿
     （叙事逻辑 / 人物动机 / 人物一致性 / 时间线 / 伏笔呼应 / 设定一致性）；
   - **只读**：不修改正文、不修改事实库，仅在 consistency_review 落只读报告；
   - 审稿问题只入建议队列（change_suggestions，category=review_issue，
     status=pending），人工确认后才可能被应用（无自动应用通道）；
   - 由 ai_tasks（task_type=consistency_review）异步调度，可取消、可断点续跑。

2. 修复补丁候选生成（build_repair_candidate）
   - **纯只读**：返回候选补丁（location/original/suggested/reason），
     applied=False / requires_confirmation=True，不落正文、不写任何表；
   - 由 ai_tasks（task_type=repair_candidate）异步调度，创建时走阶段D
     AUTO_REPAIR_MAX_PER_ISSUE（2 次/问题）限次守卫；
   - 应用环节不在本模块：由人工确认后经既有正文修改链路处理。

3. 补丁应用后的局部复检（run_partial_recheck）
   - **只读**：仅重跑「该问题关联事实域」（同规则域的确定性规则校验，
     且截止到问题所在章节），不整库全量重校验、不触碰未关联章节；
   - 返回 resolved/remaining 与建议处置（suggested_disposition=fixed），
     由人工经既有事实问题处置接口落库，本模块不写库。

全局约束：不覆盖已批准章节与人工设定、不重写旧正文、审稿与候选只读、
应用前强制人工确认；功能开关关闭时上述接口全部拒绝。
"""
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.change_suggestion import ChangeSuggestion
from app.models.chapter_content import ChapterContent
from app.models.consistency_review import (
    REVIEW_ISSUE_TYPES,
    REVIEW_SEVERITIES,
    ConsistencyReview,
)
from app.models.outline import Outline
from app.services.ai_service import chat_with_ai_json_dict
from app.services.fact_validator import FactIssue, build_fact_summary, run_fact_validation
from app.services.model_policy import TaskType

# 审稿问题入建议队列使用的 category（change_suggestions.category 为 String(20)）
REVIEW_SUGGESTION_CATEGORY = "review_issue"

# 审稿提示词版本（与 ai_tasks.prompt_version 口径一致，便于追溯）
REVIEW_PROMPT_VERSION = "phase_i_llm_review_v1"
CANDIDATE_PROMPT_VERSION = "phase_i_repair_candidate_v1"

# 事实规则域（规则名 "域.规则" 的前缀）→ 该域确定性规则清单（用于局部复检范围展示）。
# 复检过滤以「前缀匹配」为准（对未知/新增规则同样生效），本表仅用于范围说明。
DOMAIN_RULES: dict[str, tuple[str, ...]] = {
    "timeline": (
        "timeline.age",
        "timeline.event_order",
        "timeline.travel_time",
        "timeline.date_order",
    ),
    "state": (
        "state.death_reappear",
        "state.teleport",
        "state.identity",
    ),
    "possession": ("possession.conflict",),
    "secret": ("secret.early_knowledge",),
    "chapter_meta": ("chapter_meta.continuity",),
    "contract": (
        "contract.state_change",
        "contract.forbidden_reveal",
    ),
}

UNKNOWN_DOMAIN = "general"

REVIEW_SYSTEM_PROMPT = (
    "你是一名独立的小说连续性审稿编辑（独立于作者与生成模型）。"
    "你的职责是**只读审稿**：只输出审稿报告，绝不改写、不重写任何正文，"
    "也不修改任何设定或事实数据。\n"
    "请通读给定章节正文，结合大纲与上下文信息，检查以下维度：\n"
    "1) 叙事逻辑：情节因果是否自洽、有无逻辑断裂或前后矛盾；\n"
    "2) 人物动机：角色行为是否有充分动机支撑；\n"
    "3) 人物一致性：性格、能力、称谓、身份在各处是否一致；\n"
    "4) 时间线：事件先后顺序、时距、时间表述是否冲突；\n"
    "5) 伏笔呼应：伏笔是否被无理由遗忘或提前揭露；\n"
    "6) 设定一致性：世界规则、地点、物品、秘密认知是否与既有设定冲突；\n"
    "7) 文风与表达：明显的重复、断裂或口吻不一致。\n"
    "只报告**有依据的问题**，不要臆造；没有问题就返回空列表。\n"
    "必须只输出合法 JSON，结构如下（不要输出任何解释文字或代码块标记）：\n"
    '{"score": 0-100 的整数（本章连续性质量评分）, '
    '"issues": [{"severity": "critical|warning|suggestion", '
    '"type": "narrative_logic|motivation|character_consistency|timeline|'
    'foreshadow|setting|style", '
    '"location": "问题定位（原文片段或段落序号）", '
    '"message": "问题描述", '
    '"suggestion": "修改建议（供人工参考，不代表已应用）"}]}'
)

CANDIDATE_SYSTEM_PROMPT = (
    "你是一名小说连续性修复助手。请针对给定的一个连续性问题，提出**局部最小修改**"
    "的修复补丁候选，供人工审阅后决定是否应用。\n"
    "硬性要求：\n"
    "1) 只做局部修改，禁止整章重写、禁止改变剧情走向与人物设定；\n"
    "2) 每条补丁必须定位到具体原文片段（original 为原文精确片段，可为空表示新增）；\n"
    "3) 保持原文文风与叙事人称；\n"
    "4) 你只产出候选，不执行修改。\n"
    "必须只输出合法 JSON（不要输出代码块标记）：\n"
    '{"summary": "补丁总体说明", '
    '"patch": [{"location": "定位说明", "original": "待替换原文片段", '
    '"suggested": "建议替换为的文本", "reason": "修改理由"}]}'
)


def _as_uuid(value: Any) -> uuid.UUID | None:
    """把字符串/UUID 统一成 UUID（无法解析返回 None）。"""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def rule_domain(rule: str | None) -> str:
    """由规则名解析事实域（"secret.early_knowledge" -> "secret"）。"""
    text = (rule or "").strip()
    if not text:
        return UNKNOWN_DOMAIN
    head = text.split(".", 1)[0].strip()
    return head or UNKNOWN_DOMAIN


def domain_rules(domain: str) -> list[str]:
    """该事实域的确定性规则清单（未知域返回空列表，过滤仍按前缀生效）。"""
    return list(DOMAIN_RULES.get(domain, ()))


def rule_in_domain(rule: str | None, domain: str) -> bool:
    """规则是否属于该事实域：优先精确/前缀匹配，未知域退化为规则名前缀比较。"""
    text = (rule or "").strip()
    if not text:
        return False
    if text in DOMAIN_RULES.get(domain, ()):
        return True
    return rule_domain(text) == domain


# ---------------------------------------------------------------------------
# 1. 独立 LLM 连续性审稿（只读报告）
# ---------------------------------------------------------------------------
def _normalize_review_issues(raw_issues: Any) -> list[dict[str, Any]]:
    """规整模型输出的问题清单：过滤无描述项、枚举归一、字段裁剪。"""
    if not isinstance(raw_issues, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        severity = str(item.get("severity") or "suggestion").strip().lower()
        if severity not in REVIEW_SEVERITIES:
            severity = "suggestion"
        issue_type = str(item.get("type") or "").strip().lower()
        if issue_type not in REVIEW_ISSUE_TYPES:
            issue_type = "narrative_logic"
        normalized.append(
            {
                "severity": severity,
                "type": issue_type,
                "location": str(item.get("location") or "").strip(),
                "message": message[:1000],
                "suggestion": str(item.get("suggestion") or "").strip()[:1000],
            }
        )
    return normalized


def _severity_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in REVIEW_SEVERITIES}
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    return counts


def _coerce_score(raw: Any) -> float | None:
    try:
        score = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if score < 0 or score > 100:
        return None
    return score


def _review_suggestion_title(issue: dict[str, Any]) -> str:
    prefix = "[审稿]"
    text = f"{prefix}{issue['type']}：{issue['message']}"
    return text[:255]


def _review_suggestion_payload(
    review_id: uuid.UUID | None, issue: dict[str, Any]
) -> str:
    return json.dumps(
        {
            "source": "llm_consistency_review",
            "review_id": str(review_id) if review_id else None,
            "report_only": True,
            "severity": issue["severity"],
            "type": issue["type"],
            "location": issue["location"],
            "message": issue["message"],
            "suggestion": issue["suggestion"],
            "requires_confirmation": True,
        },
        ensure_ascii=False,
    )


async def enqueue_review_suggestions(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    review_id: uuid.UUID | None,
    issues: list[dict[str, Any]],
) -> int:
    """把审稿问题写入人工确认队列（change_suggestions，pending）。

    只入队、不落正文；已存在同章同标题的 pending 建议时跳过（去重幂等）。
    返回本次新增条数。
    """
    if not issues:
        return 0
    existing = (await db.execute(
        select(ChangeSuggestion.title).where(
            ChangeSuggestion.project_id == project_id,
            ChangeSuggestion.chapter_id == chapter_id,
            ChangeSuggestion.category == REVIEW_SUGGESTION_CATEGORY,
            ChangeSuggestion.status == "pending",
        )
    )).scalars().all()
    existing_titles = {str(title) for title in existing}

    created = 0
    for issue in issues:
        title = _review_suggestion_title(issue)
        if title in existing_titles:
            continue
        existing_titles.add(title)
        db.add(
            ChangeSuggestion(
                project_id=project_id,
                chapter_id=chapter_id,
                category=REVIEW_SUGGESTION_CATEGORY,
                title=title,
                description=issue["message"],
                payload=_review_suggestion_payload(review_id, issue),
                status="pending",
            )
        )
        created += 1
    await db.flush()
    return created


async def get_or_create_review(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID | None,
    chapter_title: str | None = None,
    review_id: uuid.UUID | None = None,
    trigger: str = "manual",
) -> ConsistencyReview:
    """创建或载入审稿报告行（报告本身只读，可重复覆盖式更新）。"""
    review: ConsistencyReview | None = None
    if review_id is not None:
        review = await db.get(ConsistencyReview, review_id)
    if review is None:
        review = ConsistencyReview(
            project_id=project_id,
            chapter_id=chapter_id,
            chapter_title=chapter_title,
            scope="chapter",
            status="pending",
            trigger=trigger,
        )
        db.add(review)
        await db.flush()
    return review


async def run_llm_consistency_review(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    content: str,
    chapter_id: uuid.UUID | None = None,
    chapter_title: str | None = None,
    outline_text: str = "",
    model_name: str | None = None,
    review_id: uuid.UUID | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    """独立 LLM 连续性审稿：产出只读报告 + 问题入建议队列。

    **不修改正文、不修改事实库**；仅写 consistency_review 与 change_suggestions。
    功能开关 llm_consistency_review_enabled=False 时直接拒绝。
    """
    if not bool(getattr(settings, "llm_consistency_review_enabled", True)):
        raise RuntimeError("独立 LLM 审稿功能已关闭（llm_consistency_review_enabled=False）")

    review = await get_or_create_review(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        chapter_title=chapter_title,
        review_id=review_id,
        trigger=trigger,
    )
    now = datetime.utcnow()
    review.status = "running"
    review.model_name = model_name
    review.prompt_version = REVIEW_PROMPT_VERSION
    await db.flush()

    body = (content or "").strip()
    if not body:
        # 无正文：不调用模型，直接出空报告（仍为只读报告）
        review.status = "succeeded"
        review.issue_count = 0
        review.suggestion_count = 0
        review.severity_counts = json.dumps(_severity_counts([]), ensure_ascii=False)
        review.issues = json.dumps([], ensure_ascii=False)
        review.note = "章节暂无正文，未执行独立审稿"
        review.completed_at = now
        await db.flush()
        return _review_result(review, [], 0, note=review.note)

    user_prompt = (
        f"章节标题：{chapter_title or '（未命名）'}\n"
        f"大纲/上下文信息：{outline_text.strip() or '（无）'}\n"
        f"----- 章节正文开始 -----\n{body}\n----- 章节正文结束 -----\n"
        "请按系统提示的 JSON 结构输出独立审稿报告。"
    )
    # 阶段 1：审校任务由 model_policy 统一分流为 deepseek-v4-pro + thinking(high)，
    # 原「temperature=0.2」差异化手段已被思考模式取代（思考任务不下发 temperature）。
    raw = await chat_with_ai_json_dict(
        REVIEW_SYSTEM_PROMPT,
        user_prompt,
        task_type=TaskType.CONSISTENCY_REVIEW.value,
    )
    issues = _normalize_review_issues(raw.get("issues") if isinstance(raw, dict) else None)
    counts = _severity_counts(issues)

    suggestion_count = 0
    if chapter_id is not None:
        suggestion_count = await enqueue_review_suggestions(
            db,
            project_id=project_id,
            chapter_id=_as_uuid(chapter_id),
            review_id=review.id,
            issues=issues,
        )

    review.status = "succeeded"
    review.score = _coerce_score(raw.get("score") if isinstance(raw, dict) else None)
    review.issue_count = len(issues)
    review.severity_counts = json.dumps(counts, ensure_ascii=False)
    review.issues = json.dumps(issues, ensure_ascii=False)
    review.suggestion_count = suggestion_count
    review.raw_response = json.dumps(raw, ensure_ascii=False)[:20000]
    review.note = (
        "独立审稿报告为只读产物：未修改正文与事实库；"
        "问题已进入建议队列（change_suggestions），人工确认后才可应用"
    )
    review.completed_at = datetime.utcnow()
    await db.flush()
    return _review_result(review, issues, suggestion_count, note=review.note)


def _review_result(
    review: ConsistencyReview,
    issues: list[dict[str, Any]],
    suggestion_count: int,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "review_id": str(review.id),
        "project_id": str(review.project_id),
        "chapter_id": str(review.chapter_id) if review.chapter_id else None,
        "chapter_title": review.chapter_title,
        "status": review.status,
        "score": review.score,
        "issue_count": review.issue_count,
        "severity_counts": _severity_counts(issues),
        "suggestion_count": suggestion_count,
        "issues": issues,
        "report_only": True,
        "content_modified": False,
        "requires_confirmation": True,
        "prompt_version": review.prompt_version,
        "model_name": review.model_name,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 2. 修复补丁候选生成（纯只读，不落正文）
# ---------------------------------------------------------------------------
async def build_repair_candidate(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    issue: dict[str, Any],
    chapter_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """针对单个校验问题生成修复补丁候选（**只读、不落正文、不写库**）。

    返回候选补丁；applied=False / requires_confirmation=True 明确表示未应用，
    应用必须由人工确认后走既有正文修改链路（阶段D 限次守卫在任务创建侧生效）。
    """
    if not bool(getattr(settings, "repair_candidate_enabled", True)):
        raise RuntimeError("补丁候选生成功能已关闭（repair_candidate_enabled=False）")

    issue_id = str(issue.get("issue_id") or issue.get("id") or "").strip()
    rule = str(issue.get("rule") or "").strip()
    message = str(issue.get("message") or "").strip()
    suggestion = str(issue.get("suggestion") or "").strip()
    related_ids = issue.get("related_ids") or []

    chapter_title = ""
    chapter_snippet = ""
    target_chapter_id = _as_uuid(chapter_id) or _as_uuid(issue.get("chapter_id"))
    if target_chapter_id is not None:
        outline = await db.get(Outline, target_chapter_id)
        if outline is not None:
            chapter_title = outline.title or ""
        row = (await db.execute(
            select(ChapterContent).where(ChapterContent.outline_id == target_chapter_id)
        )).scalar_one_or_none()
        if row is not None and row.content:
            chapter_snippet = row.content[:6000]

    user_prompt = (
        f"问题编号：{issue_id or '（未知）'}\n"
        f"所属章节：{chapter_title or '（未知）'}\n"
        f"违反规则：{rule or '（未知）'}\n"
        f"严重度：{issue.get('severity') or '（未知）'}\n"
        f"问题描述：{message or '（无）'}\n"
        f"既有建议：{suggestion or '（无）'}\n"
        f"关联实体：{json.dumps(related_ids, ensure_ascii=False)}\n"
        f"----- 章节正文片段（仅供定位，请勿整体重写）-----\n"
        f"{chapter_snippet or '（无正文片段）'}\n"
        "请输出局部修复补丁候选 JSON。"
    )
    raw: dict[str, Any] = {}
    try:
        # 阶段 1：修复补丁候选走 model_policy 分流（deepseek-v4-pro + thinking(high)）
        raw = await chat_with_ai_json_dict(
            CANDIDATE_SYSTEM_PROMPT,
            user_prompt,
            task_type=TaskType.REPAIR_CANDIDATE.value,
        )
    except Exception:
        raw = {}

    patch = _normalize_patch(raw.get("patch") if isinstance(raw, dict) else None)
    summary = str(raw.get("summary") or "").strip() if isinstance(raw, dict) else ""
    if not patch:
        # 确定性兜底候选：不调用模型也能给出人工可审阅的候选（仍为只读）
        patch = [
            {
                "location": str(issue.get("location") or "") or "（定位由人工确认）",
                "original": "",
                "suggested": suggestion or "请人工确认修改方案",
                "reason": f"确定性兜底候选：{message or '问题未提供描述'}",
            }
        ]
        summary = summary or "模型未返回可用补丁，已给出确定性兜底候选"

    return {
        "candidate_id": str(uuid.uuid4()),
        "project_id": str(project_id),
        "chapter_id": str(target_chapter_id) if target_chapter_id else None,
        "chapter_title": chapter_title or None,
        "issue_id": issue_id or None,
        "issue_key": issue.get("issue_key") or None,
        "rule": rule or None,
        "domain": rule_domain(rule),
        "severity": issue.get("severity") or None,
        "summary": summary,
        "patch": patch,
        "applied": False,
        "read_only": True,
        "requires_confirmation": True,
        "prompt_version": CANDIDATE_PROMPT_VERSION,
        "model_name": model_name,
        "generated_at": datetime.utcnow().isoformat(),
        "write_guard": (
            "候选仅只读生成：不落正文、不写库；应用需人工确认后走既有正文修改链路，"
            "且受自动修复次数上限（2 次/问题）约束"
        ),
    }


def _normalize_patch(raw_patch: Any) -> list[dict[str, Any]]:
    """规整模型补丁候选：过滤空项、裁剪长度。"""
    if not isinstance(raw_patch, list):
        return []
    patch: list[dict[str, Any]] = []
    for item in raw_patch:
        if not isinstance(item, dict):
            continue
        suggested = str(item.get("suggested") or "").strip()
        if not suggested:
            continue
        patch.append(
            {
                "location": str(item.get("location") or "").strip()[:500],
                "original": str(item.get("original") or "").strip()[:2000],
                "suggested": suggested[:2000],
                "reason": str(item.get("reason") or "").strip()[:500],
            }
        )
    return patch


# ---------------------------------------------------------------------------
# 3. 补丁应用后的局部复检（只读，仅重跑关联事实域）
# ---------------------------------------------------------------------------
async def run_partial_recheck(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    rule: str,
    issue_key: str | None = None,
    related_ids: list[Any] | None = None,
) -> dict[str, Any]:
    """补丁应用后的局部复检：**仅重跑该问题关联事实域**（只读）。

    - 范围裁剪：仅运行该问题所属规则域（rule 前缀）的确定性校验，且
      run_fact_validation 以 chapter_id 为界（向前包含历史章，不触后续章）；
    - 不触碰未关联章节与正文，不写库；
    - 命中同一 issue_key 视为「仍存在」，未命中视为「已解决」，
      建议处置 disposition=fixed 供人工经处置接口落库。
    """
    domain = rule_domain(rule)
    scoped_rules = domain_rules(domain)
    related = [str(item) for item in (related_ids or []) if item]

    all_issues: list[FactIssue] = await run_fact_validation(
        db, project_id, chapter_id=chapter_id
    )
    domain_issues = [i for i in all_issues if rule_in_domain(i.rule, domain)]

    target_present = False
    if issue_key:
        target_present = any(i.issue_key == issue_key for i in domain_issues)
    else:
        target_present = any(i.rule == rule for i in domain_issues)

    remaining = [i.to_dict() for i in domain_issues]
    related_remaining = [
        i.to_dict()
        for i in domain_issues
        if related and set(str(x) for x in (i.related_ids or [])) & set(related)
    ]
    observed_rules = sorted({i.rule for i in domain_issues})

    return {
        "project_id": str(project_id),
        "chapter_id": str(chapter_id),
        "domain": domain,
        "rule": rule,
        "issue_key": issue_key,
        "resolved": not target_present,
        "scope": {
            "mode": "fact_domain",
            "chapter_id": str(chapter_id),
            "domain": domain,
            "rules": observed_rules or scoped_rules,
            "registered_rules": scoped_rules,
            "related_ids": related,
            "note": "仅重跑该问题关联事实域（同域确定性规则，截止到问题所在章节）",
        },
        "summary": build_fact_summary(domain_issues),
        "remaining_count": len(domain_issues),
        "remaining": remaining,
        "related_remaining": related_remaining,
        "suggested_disposition": None if target_present else "fixed",
        "read_only": True,
        "content_modified": False,
        "checked_at": datetime.utcnow().isoformat(),
        "note": (
            "局部复检为只读结果：不修改正文、不写库；"
            "如需落库处置（fixed/deferred/wont_fix 等）请走人工处置接口"
        ),
    }
