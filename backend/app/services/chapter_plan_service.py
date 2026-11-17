# 章节计划服务：结构化 ChapterPlan 的解析、旧数据兼容回退、后续章节禁止揭露聚合，
# 以及「计划符合度检查」（AI 生成后不修改正文，只返回结构化问题）。
# 设计约束：
#   - 章节标题单一事实来源 = outlines.title（chapter 行），计划标题只用于比对/提示；
#   - 旧数据（chapter_plan 为 NULL）回退为 legacy 计划，保证历史项目可继续生成；
#   - 生成接口只接受 chapterId，服务端按 chapterId 反查该章计划，绝不取项目第一章/最近一章。
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outline import Outline
from app.schemas.chapter_plan import (
    ChapterPlanData,
    PlanComplianceIssue,
)

PLAN_FIELDS = [
    "title",
    "summary",
    "sceneGoals",
    "requiredCharacters",
    "requiredLocations",
    "requiredEvents",
    "expectedOutcome",
    "forbiddenReveals",
    "status",
    # 阶段F「场景合同」扩展字段
    "sceneTime",
    "allowedReveals",
    "expectedStateChanges",
]

PLAN_STATUSES = {"draft", "confirmed", "archived"}


class TitleConflictError(ValueError):
    """章节标题与已确认大纲计划标题冲突（禁止静默覆盖）。"""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_str_list(value) -> list[str]:
    """把任意输入规范为字符串列表（兼容逗号分隔文本 / JSON 数组 / 单项字符串）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except Exception:  # noqa: BLE001
                pass
        return [v.strip() for v in re.split(r"[\n，,；;、]+", text) if v.strip()]
    return []


def _clean_scene_time(value) -> dict | None:
    """清洗 sceneTime：仅接受 {type: exact/range/relative, value: 非空文本}。"""
    if not isinstance(value, dict):
        return None
    stype = str(value.get("type") or "").strip().lower()
    if stype not in ("exact", "range", "relative"):
        return None
    sval = str(value.get("value") or "").strip()
    if not sval:
        return None
    return {"type": stype, "value": sval}


def _clean_state_change(value) -> dict | None:
    """清洗 expectedStateChanges：保留 characters/locations/items/secrets 四类。

    每项只保留 {target, before, after}，要求 target 与 after 非空（before 可空，
    表示「无明确变化前状态」）；全部为空时返回 None（等价于未设置）。
    """
    if not isinstance(value, dict):
        return None
    out: dict = {}
    for key in ("characters", "locations", "items", "secrets"):
        items = []
        raw = value.get(key)
        if isinstance(raw, list):
            for it in raw:
                if not isinstance(it, dict):
                    continue
                target = str(it.get("target") or "").strip()
                after = str(it.get("after") or "").strip()
                if not target or not after:
                    continue
                before = str(it.get("before") or "").strip()
                items.append({"target": target, "before": before, "after": after})
        if items:
            out[key] = items
    if not out:
        return None
    return out


def _clean_plan(plan: dict) -> dict:
    """清洗并规范化计划 dict：只保留合法字段、去空、status 归一到合法枚举。"""
    out: dict = {}
    for field in PLAN_FIELDS:
        if field in ("title", "summary", "expectedOutcome", "status"):
            value = plan.get(field)
            out[field] = (
                str(value).strip() if value is not None and str(value).strip() else None
            )
        elif field == "sceneTime":
            out[field] = _clean_scene_time(plan.get(field))
        elif field == "expectedStateChanges":
            out[field] = _clean_state_change(plan.get(field))
        else:
            out[field] = _as_str_list(plan.get(field))
    status = out.get("status") or "draft"
    out["status"] = status if status in PLAN_STATUSES else "draft"
    return out


def parse_chapter_plan(raw: str | None) -> dict | None:
    """解析 outlines.chapter_plan JSON 文本；非法/为空返回 None。"""
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = json.loads(str(raw))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    return _clean_plan(parsed)


def build_legacy_plan(chapter: Outline) -> dict:
    """旧数据兼容：由 outlines.title/summary/content 构建等价计划（legacy）。"""
    required_events = []
    if chapter.content and str(chapter.content).strip():
        required_events = [
            v.strip()
            for v in re.split(r"[\n，,；;、]+", str(chapter.content))
            if v.strip()
        ]
    return _clean_plan(
        {
            "title": chapter.title,
            "summary": chapter.summary,
            "sceneGoals": [],
            "requiredCharacters": [],
            "requiredLocations": [],
            "requiredEvents": required_events,
            "expectedOutcome": None,
            "forbiddenReveals": [],
            "status": "legacy",
        }
    )


def chapter_has_legacy(chapter: Outline) -> bool:
    """旧数据是否有可回退内容（标题 + 摘要/细纲任一非空）。"""
    return bool(
        chapter.title
        or (chapter.summary and str(chapter.summary).strip())
        or (chapter.content and str(chapter.content).strip())
    )


async def load_chapter_plan(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> tuple[dict | None, str]:
    """按 chapterId 读取该章计划。

    返回 (plan, source)：plan 为规范化 dict（含 legacy 回退），
    source 为 manual / legacy / none。
    仅读取当前章节，绝不使用项目内其他章节计划。
    """
    chapter = (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter_id, Outline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if chapter is None:
        return None, "none"

    parsed = parse_chapter_plan(chapter.chapter_plan)
    if parsed is not None:
        return parsed, "manual"
    if chapter_has_legacy(chapter):
        return build_legacy_plan(chapter), "legacy"
    return None, "none"


async def save_chapter_plan(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    plan: ChapterPlanData,
    *,
    sync_title: bool = False,
) -> dict:
    """保存结构化计划。

    sync_title=True 时把计划标题同步到 outlines.title（显式用户确认后）。
    「已确认大纲版本化」约束：
      - 若项目存在 active confirmed 版本且该 chapterId 已绑定 confirmed 计划，
        计划标题与 confirmed 标题不一致时禁止静默同步标题（抛 TitleConflictError）；
      - 保存后自动同步 draft 版本（人工编辑章节计划产生 draft，草稿不覆盖 confirmed）。
    """
    from app.services.outline_plan_version_service import (
        get_confirmed_chapter_plan,
        NoConfirmedVersionError,
        ChapterNotBoundError,
        sync_draft_version,
    )

    chapter = (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter_id, Outline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if chapter is None:
        raise ValueError("chapter_not_found")

    payload = plan.model_dump(exclude_none=True)
    chapter.chapter_plan = json.dumps(_clean_plan(payload), ensure_ascii=False)

    if sync_title and plan.title and str(plan.title).strip():
        confirmed, confirmed_plan = None, None
        try:
            confirmed, confirmed_plan = await get_confirmed_chapter_plan(
                db, project_id, chapter_id
            )
        except (NoConfirmedVersionError, ChapterNotBoundError):
            confirmed = None
        if confirmed is not None:
            confirmed_title = (confirmed_plan or {}).get("title") or ""
            if confirmed_title and confirmed_title != str(plan.title).strip():
                raise TitleConflictError(
                    f"章节《{chapter.title or ''}》的已确认大纲标题为《{confirmed_title}》，"
                    f"与本次计划标题《{str(plan.title).strip()}》不一致；"
                    f"请先确认新大纲版本，或改回已确认标题，禁止静默覆盖"
                )
        chapter.title = str(plan.title).strip()

    await db.commit()
    await db.refresh(chapter)
    # 人工编辑章节计划 → 同步/创建 draft 版本（幂等；内容与 confirmed 一致时不产生草稿）
    try:
        await sync_draft_version(db, project_id)
    except Exception:  # noqa: BLE001
        # 草稿同步失败不影响计划保存结果（仅影响版本化状态提示）
        pass
    return parse_chapter_plan(chapter.chapter_plan) or {}


async def load_following_plans(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter: Outline,
) -> list[dict]:
    """当前章之后（同卷内 sort_order 更大的）章节计划列表。

    用途：仅构造 forbiddenReveals —— 后续章节的 title / requiredEvents /
    expectedOutcome 作为「禁止提前揭露/禁止推进到后续剧情」素材，
    绝不作为当前章应生成剧情。
    """
    if chapter.parent_id:
        siblings = (
            await db.execute(
                select(Outline)
                .where(
                    Outline.project_id == project_id,
                    Outline.parent_id == chapter.parent_id,
                    Outline.outline_type == "chapter",
                    Outline.id != chapter.id,
                )
                .order_by(Outline.sort_order.asc(), Outline.created_at.asc())
                .limit(50)
            )
        ).scalars().all()
    else:
        siblings = (
            await db.execute(
                select(Outline)
                .where(
                    Outline.project_id == project_id,
                    Outline.outline_type == "chapter",
                    Outline.id != chapter.id,
                )
                .order_by(Outline.sort_order.asc(), Outline.created_at.asc())
                .limit(50)
            )
        ).scalars().all()
    following = [s for s in siblings if s.sort_order > chapter.sort_order]
    following.sort(key=lambda s: (s.sort_order, s.created_at))

    out: list[dict] = []
    for row in following[:10]:
        plan, source = await load_chapter_plan(db, project_id, row.id)
        out.append(
            {
                "chapterId": str(row.id),
                "chapterNumber": _chapter_number(row),
                "title": row.title or "",
                "planSource": source,
                "requiredEvents": (plan or {}).get("requiredEvents") or [],
                "expectedOutcome": (plan or {}).get("expectedOutcome") or "",
                "forbiddenReveals": (plan or {}).get("forbiddenReveals") or [],
            }
        )
    return out


def _chapter_number(chapter: Outline) -> int:
    """章节序号：按项目内 chapter 类型 sort_order 升序的 1-based 序号。"""
    return max(int(chapter.sort_order or 0), 0)


async def chapter_number_of(
    db: AsyncSession, project_id: uuid.UUID, chapter: Outline
) -> int:
    """当前章在项目内 chapter 列表中的 1-based 序号（排序：sort_order, created_at）。"""
    rows = (
        await db.execute(
            select(Outline.id)
            .where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            )
            .order_by(Outline.sort_order.asc(), Outline.created_at.asc())
        )
    ).scalars().all()
    ids = [str(r) for r in rows]
    try:
        return ids.index(str(chapter.id)) + 1
    except ValueError:
        return _chapter_number(chapter)


# ---------------------------------------------------------------------------
# 计划符合度检查（本地规则，不调用 AI；只提示，不修改正文）
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _covered(text_norm: str, keyword: str) -> bool:
    keyword_norm = _normalize(keyword)
    if not keyword_norm:
        return True
    if len(keyword_norm) == 1:
        return keyword_norm in text_norm
    return keyword_norm in text_norm


def run_plan_compliance_check(
    chapter: Outline,
    content: str,
    plan: dict | None,
    following_plans: list[dict],
) -> dict:
    """检查正文是否符合当前章节计划。

    检查维度：
      1. 章节标题/核心情境是否体现（title/summary 关键词命中正文）；
      2. requiredEvents 是否被覆盖；
      3. expectedOutcome 是否达成；
      4. forbiddenReveals 是否被违反（正文命中禁止揭露项 → error）；
      5. 是否明显推进到后续章节剧情（正文命中后续章节 requiredEvents / 标题 → warning）。
    返回 {score, issues[]}，issues 为 PlanComplianceIssue 结构化问题。
    """
    issues: list[PlanComplianceIssue] = []
    content_norm = _normalize(content or "")
    plan = plan or {}
    title = chapter.title or plan.get("title") or ""
    summary = plan.get("summary") or chapter.summary or ""
    required_events = plan.get("requiredEvents") or []
    expected_outcome = plan.get("expectedOutcome") or ""
    forbidden_reveals = plan.get("forbiddenReveals") or []

    # 1. 标题/核心情境
    if title and not _covered(content_norm, title):
        issues.append(
            PlanComplianceIssue(
                severity="warning",
                type="title_not_covered",
                message=f"正文未明显体现章节标题《{title}》的核心情境",
                evidence=title,
                suggestion="调整正文开头或关键段落，使章节标题对应的情境成为本章焦点",
            )
        )
    if summary:
        summary_hits = [
            s for s in re.split(r"[\n，,；;、。]+", summary) if len(_normalize(s)) >= 3
        ]
        covered_count = sum(1 for s in summary_hits if _covered(content_norm, s))
        if summary_hits and covered_count < max(1, (len(summary_hits) + 1) // 2):
            issues.append(
                PlanComplianceIssue(
                    severity="warning",
                    type="title_not_covered",
                    message="正文未充分覆盖章节摘要/目标所述核心情境",
                    evidence=f"命中 {covered_count}/{len(summary_hits)} 个摘要要点",
                    suggestion="补充摘要中要求的情境、人物或场景，确保本章目标达成",
                )
            )

    # 2. 必须发生事件
    missing_events = [
        ev for ev in required_events if not _covered(content_norm, ev)
    ]
    for ev in missing_events:
        issues.append(
            PlanComplianceIssue(
                severity="error",
                type="event_missing",
                message=f"必须发生事件未在正文中体现：{ev}",
                evidence=ev,
                suggestion="补充该事件或调整情节使其发生；若确实不再需要，请先修改章节计划",
            )
        )

    # 3. 结束状态
    if expected_outcome and not _covered(content_norm, expected_outcome):
        issues.append(
            PlanComplianceIssue(
                severity="warning",
                type="outcome_not_met",
                message="正文未明显达成章节计划的结束状态",
                evidence=expected_outcome,
                suggestion="在章节结尾补足预期收束，或按计划修订后重新生成",
            )
        )

    # 4. 禁止提前揭露
    for item in forbidden_reveals:
        if _covered(content_norm, item):
            issues.append(
                PlanComplianceIssue(
                    severity="error",
                    type="forbidden_reveal",
                    message=f"正文出现计划禁止提前揭露的内容：{item}",
                    evidence=item,
                    suggestion="删除/弱化该揭露，或将其留到计划允许回收的章节",
                )
            )

    # 5. 明显推进到后续剧情
    for fp in following_plans:
        fp_title = fp.get("title") or ""
        for ev in fp.get("requiredEvents") or []:
            if _covered(content_norm, ev):
                issues.append(
                    PlanComplianceIssue(
                        severity="warning",
                        type="advanced_plot",
                        message=f"正文可能推进到了后续章节《{fp_title}》的计划事件：{ev}",
                        evidence=ev,
                        suggestion="后续章节事件不得在本章发生，请移除或改为本章计划的对应情节",
                    )
                )
        fp_outcome = fp.get("expectedOutcome") or ""
        if fp_outcome and _covered(content_norm, fp_outcome):
            issues.append(
                PlanComplianceIssue(
                    severity="warning",
                    type="advanced_plot",
                    message=f"正文可能推进到了后续章节《{fp_title}》的结束状态",
                    evidence=fp_outcome,
                    suggestion="本章应止步于当前章节计划的结束状态，请收缩剧情范围",
                )
            )

    error_count = sum(1 for i in issues if i.severity == "error")
    warning_count = sum(1 for i in issues if i.severity == "warning")
    score = max(0, 100 - error_count * 20 - warning_count * 5)
    if not issues:
        score = 100
    if not plan and not following_plans:
        score = 100
    return {"score": score, "issues": issues}


async def plan_compliance_of(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    content: str,
) -> dict:
    """服务入口：按 chapterId 读当前章计划 + 后续章节计划，执行符合度检查。

    「已确认大纲版本化」约束：优先基于 active confirmed 版本下绑定该 chapterId
    的 ChapterPlan 快照执行检查（标题/核心情境/requiredEvents/expectedOutcome/
    forbiddenReveals/后续章节剧情）；项目尚无 confirmed 版本或章节未绑定时，
    回退读取 outlines.chapter_plan（manual/legacy），并在返回中标注 plan_source，
    不自动改写正文。
    """
    from app.services.outline_plan_version_service import (
        ChapterNotBoundError,
        NoConfirmedVersionError,
        get_confirmed_chapter_plan,
    )

    chapter = (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter_id, Outline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if chapter is None:
        raise ValueError("chapter_not_found")

    plan: dict | None = None
    following: list[dict] = []
    plan_source = "none"
    try:
        active, confirmed_plan = await get_confirmed_chapter_plan(
            db, project_id, chapter_id
        )
        plan = confirmed_plan
        plan_source = "confirmed"
        try:
            plans = json.loads(active.chapter_plans_snapshot or "{}")
        except Exception:  # noqa: BLE001
            plans = {}
        cur_num = (plan or {}).get("chapterNumber")
        if isinstance(cur_num, int):
            following = [
                p
                for p in plans.values()
                if isinstance(p, dict)
                and isinstance(p.get("chapterNumber"), int)
                and p["chapterNumber"] > cur_num
            ]
            following.sort(key=lambda p: p["chapterNumber"])
    except (NoConfirmedVersionError, ChapterNotBoundError):
        plan, _source = await load_chapter_plan(db, project_id, chapter_id)
        plan_source = _source
        following = await load_following_plans(db, project_id, chapter)

    result = run_plan_compliance_check(chapter, content, plan, following)
    result["plan"] = plan
    result["following_plans"] = following
    result["plan_source"] = plan_source
    return result


async def generate_chapter_plan_batch(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: list[uuid.UUID],
) -> list[dict]:
    """按批生成章节计划（场景合同），2026-11-15 第二档升级。

    每章一次 AI 调用（chat_with_ai_json，自带截断重试与 Pydantic 校验），
    生成结果写入 outlines.chapter_plan，并回填 chapter_goal（summary）与
    chapter_outcome（expectedOutcome）两个规划列。
    单章失败不影响后续章节（结果列表逐章带 error）。
    """
    from app.models.project import Project
    from app.schemas.chapter_plan import ChapterPlanData
    from app.services.ai_service import chat_with_ai_json, is_ai_configured
    from app.services.model_policy import TaskType

    if not is_ai_configured():
        return [
            {"chapter_id": str(cid), "error": "ai_not_configured"}
            for cid in chapter_ids
        ]

    project = await db.get(Project, project_id)
    if project is None:
        raise ValueError("project_not_found")
    overview = (project.background or "")[:3000]

    system_prompt = (
        "你是长篇小说的章节规划师。根据作品设定与章节位置，为指定章节输出「场景合同」：\n"
        "summary（一句话概括本章内容）、本章目标（sceneGoals，1-3 条）、"
        "必须出场的人物（requiredCharacters）、必须出现的地点（requiredLocations）、"
        "必须发生的事件（requiredEvents，2-5 条）、本章结束时的状态（expectedOutcome）、"
        "本章禁止提前揭示的秘密（forbiddenReveals）。\n"
        "要求：summary 必填；目标具体可验证；事件推动主线或伏笔；"
        "出场人物与地点只写作品里已有的；本章位置靠前时不要提前揭晓后期秘密。"
        "只输出严格 JSON，字段与给定结构一致。"
    )

    results: list[dict] = []
    for cid in chapter_ids:
        chapter = (
            await db.execute(
                select(Outline).where(
                    Outline.id == cid, Outline.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        if chapter is None:
            results.append({"chapter_id": str(cid), "error": "chapter_not_found"})
            continue

        # 前两章已生成的计划作为连贯性上下文
        siblings = (
            await db.execute(
                select(Outline)
                .where(
                    Outline.project_id == project_id,
                    Outline.outline_type == "chapter",
                    Outline.sort_order < chapter.sort_order,
                    Outline.chapter_plan.is_not(None),
                )
                .order_by(Outline.sort_order.desc())
                .limit(2)
            )
        ).scalars().all()
        prev_ctx = ""
        for sib in reversed(siblings):
            prev_ctx += f"\n- 第{sib.sort_order}章《{sib.title or ''}》计划摘要：{(parse_chapter_plan(sib.chapter_plan) or {}).get('summary') or ''}"

        user_prompt = (
            f"作品概要：\n{overview}\n\n"
            f"本章标题：{chapter.title or ''}\n"
            f"章节排序：第 {chapter.sort_order} 章\n"
            f"细纲备注：{(chapter.summary or '')[:600] or '（无）'}\n"
            f"前文章节计划（供连贯参考，勿照搬）：{prev_ctx or '（无）'}"
        )

        try:
            plan = await chat_with_ai_json(
                system_prompt,
                user_prompt,
                ChapterPlanData,
                task_type=TaskType.SCENE_CONTRACT.value,
            )
            await save_chapter_plan(db, project_id, cid, plan, sync_title=False)
            # 回填逐章规划列（此前 48 章全空）；summary 缺省时退用第一条目标
            chapter.chapter_goal = plan.summary or (
                plan.sceneGoals[0] if plan.sceneGoals else ""
            )
            chapter.chapter_outcome = plan.expectedOutcome or ""
            await db.commit()
            results.append(
                {
                    "chapter_id": str(cid),
                    "ok": True,
                    "plan": plan.model_dump(exclude_none=True),
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 单章失败不阻断整批
            await db.rollback()
            results.append({"chapter_id": str(cid), "error": str(exc)[:300]})

    return results
