# 一致性检查服务（第四期「写错能发现」核心）
# 职责：
# 1. run_consistency_check：对指定章节正文执行 AI 一致性检查（action_type=check 落 generation_records），
#    输出固定 JSON：score(0-100)/issues[]/reminders[]/outline{completed,uncovered}/repetition/style/confirmed_updates/proposed_updates；
# 2. 检查依据 = 项目已确认设定（人物卡/快照/时间线/伏笔/chapter_memory 摘要）；
# 3. mock 模式（未配置 AI）提供合理演示结果，并真实执行本地规则：
#    - 正文内重复段落检测（连续 n-gram）
#    - 与既往章节摘要重复检测
#    - 细纲完成度（goal/conflict/key_events 覆盖）
#    - 未回收伏笔提醒（到计划回收章节未回收）
#    - 拟采纳更新（proposed_updates）由 mock 给出可演示的确认项
import json
import re
import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.chapter_memory import ChapterMemory
from app.models.foreshadow import Foreshadow
from app.models.outline import Outline
from app.models.project import Project
from app.models.timeline_event import TimelineEvent
from app.services.ai_service import chat_with_ai, _chat_with_ai_retry_on_truncation, is_ai_configured
from app.services.context_builder import build_writing_context
from app.services.generation_service import (
    record_generation,
    resolve_ai_provider,
    serialize_context,
)
from app.services.model_policy import TaskType

# 检查维度常量
ISSUE_TYPES = [
    "character_state_conflict",
    "location_conflict",
    "status_conflict",
    "timeline_error",
    "foreshadow_premature",
    "item_inconsistency",
    "world_conflict",
    "outline_deviation",
    "repetition",
]

SEVERITIES = ["warning", "error"]


# ---------------------------------------------------------------- 工具 ----------

def _now_utc() -> datetime:
    return datetime.utcnow()


def _strip_code_fence(text: str) -> str:
    """去掉模型输出中可能存在的 ```json 围栏，提取 JSON 主体。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _split_paragraphs(content: str) -> list[str]:
    """按段落切分正文。"""
    raw = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in raw.split("\n") if p.strip()]
    return paras


def _normalize(text: str) -> str:
    """去空白与标点，用于相似度比较。"""
    if not text:
        return ""
    return re.sub(r"[\s，。！？、；：""''（）《》【】,.!?;:'\"()<>\\[\\]{}]+", "", text)


def _n_grams(text: str, n: int = 4) -> list[str]:
    norm = _normalize(text)
    if len(norm) < n:
        return []
    return [norm[i : i + n] for i in range(len(norm) - n + 1)]


def _paragraph_similarity(a: str, b: str) -> float:
    """基于 4-gram 的 Jaccard 相似度（0~1）。"""
    ga = set(_n_grams(a))
    gb = set(_n_grams(b))
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    union = len(ga | gb)
    return inter / union if union else 0.0


def detect_internal_repetition(content: str) -> list[dict]:
    """正文内重复段落检测：相邻段落相似度超过阈值即标记，连续相同句子也标记。

    返回 [{type:"repetition", severity, location, message, suggestion, data}]
    """
    paras = _split_paragraphs(content)
    issues: list[dict] = []
    if len(paras) < 2:
        return issues

    prev_norm = _normalize(paras[0])
    for i in range(1, len(paras)):
        norm = _normalize(paras[i])
        sim = _paragraph_similarity(paras[i - 1], paras[i])
        if norm and norm == prev_norm:
            issues.append(
                {
                    "type": "repetition",
                    "severity": "error",
                    "location": f"第{i + 1}段",
                    "message": "相邻段落内容完全相同，疑似重复粘贴。",
                    "suggestion": "删除或改写重复段落，保留信息量更大的一处。",
                    "data": {"similarity": 1.0, "para_index": i},
                }
            )
        elif sim >= 0.55:
            issues.append(
                {
                    "type": "repetition",
                    "severity": "warning",
                    "location": f"第{i + 1}段（与第{i}段）",
                    "message": f"相邻段落相似度过高（{sim:.0%}），疑似复述前文。",
                    "suggestion": "合并或精简这两段，避免信息重复。",
                    "data": {"similarity": round(sim, 2), "para_index": i},
                }
            )
        prev_norm = norm
    return issues


def detect_previous_summary_repetition(content: str, previous_summaries: list[str]) -> list[dict]:
    """与前文章节摘要中已述内容重复检测：正文段落与任一摘要高相似即提醒。"""
    issues: list[dict] = []
    if not previous_summaries:
        return issues
    paras = _split_paragraphs(content)
    summaries = [_normalize(s) for s in previous_summaries if s]
    if not summaries:
        return issues
    for i, para in enumerate(paras):
        if len(_normalize(para)) < 20:
            continue
        best = max(
            (_paragraph_similarity(para, s) for s in summaries),
            default=0.0,
        )
        if best >= 0.5:
            issues.append(
                {
                    "type": "repetition",
                    "severity": "warning",
                    "location": f"第{i + 1}段",
                    "message": f"该段与前文摘要相似度 {best:.0%}，可能重复已交代内容。",
                    "suggestion": "确认是否需要在当前章重新叙述；建议简化为引用或删除。",
                    "data": {"similarity": round(best, 2), "summary_index": 0},
                }
            )
    return issues


async def _load_confirmed_context(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """加载项目已确认设定作为检查依据。"""
    ctx: dict = {"characters": [], "snapshots": [], "timeline": [], "memories": []}

    chars = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    ctx["characters"] = [
        {
            "id": str(c.id),
            "name": c.name,
            "aliases": c.aliases,
            "status": c.status,
            "character_status": c.character_status,
            "current_location": c.current_location,
            "current_timeline_status": c.current_timeline_status,
            "identity": c.identity,
            "abilities": c.abilities,
            "appearance": c.appearance,
            "profile": c.profile,
        }
        for c in chars
    ]

    # 人物状态快照必须限定本作品：join Character 按 project_id 过滤，
    # 防止其他作品的人物快照混入检查上下文（跨作品数据泄漏与假冲突）。
    snaps = (
        await db.execute(
            select(CharacterStateSnapshot)
            .join(Character, Character.id == CharacterStateSnapshot.character_id)
            .where(Character.project_id == project_id)
            .order_by(CharacterStateSnapshot.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    ctx["snapshots"] = []
    for s in snaps:
        try:
            st = json.loads(s.state_json or "{}")
        except Exception:  # noqa: BLE001
            st = {}
        if not isinstance(st, dict):
            st = {}
        ctx["snapshots"].append(
            {
                "character_id": str(s.character_id) if s.character_id else None,
                "chapter_id": str(s.chapter_id) if s.chapter_id else None,
                "character_name": st.get("character_name") or st.get("name") or "",
                "status": st.get("status") or "",
                "location": st.get("location") or st.get("current_location") or "",
                "current_timeline_status": st.get("current_timeline_status") or "",
                "state": st,
                "user_confirmed": s.confirmed_at is not None,
                "confirmed_at": s.confirmed_at.isoformat() if s.confirmed_at else None,
            }
        )

    events = (
        await db.execute(
            select(TimelineEvent)
            .where(TimelineEvent.project_id == project_id, TimelineEvent.confirmed == True)  # noqa: E712
            .order_by(TimelineEvent.sequence_order.asc())
            .limit(300)
        )
    ).scalars().all()
    ctx["timeline"] = [
        {
            "title": e.title,
            "description": e.description,
            "event_date_label": e.event_date_label,
            "sequence_order": e.sequence_order,
            "participants": json.loads(e.participants or "[]"),
        }
        for e in events
    ]

    mems = (
        await db.execute(
            select(ChapterMemory).where(ChapterMemory.project_id == project_id).order_by(ChapterMemory.created_at.asc())
        )
    ).scalars().all()
    # 章节标题在 outlines 表，ChapterMemory 无 chapter_title 字段，统一联查
    titles: dict[str, str] = {}
    mem_ids = [m.chapter_id for m in mems if m.chapter_id is not None]
    if mem_ids:
        outline_rows = (
            await db.execute(select(Outline).where(Outline.id.in_(mem_ids)))
        ).scalars().all()
        titles = {str(o.id): (o.title or "") for o in outline_rows}
    for m in mems:
        try:
            mem = json.loads(m.memory_json or "{}")
        except Exception:  # noqa: BLE001
            mem = {}
        ctx["memories"].append(
            {
                "chapter_id": str(m.chapter_id) if m.chapter_id else None,
                "title": titles.get(str(m.chapter_id), ""),
                # summary 存在 ChapterMemory.summary 独立列；结构化记忆 memory_json 存 camelCase 键。
                # 蛇形旧键与驼峰新键双读兼容，避免键名错配导致前章摘要/记忆上下文恒为空。
                "summary": m.summary or mem.get("summary") or "",
                "timeline": mem.get("timeline") or mem.get("timeLabel") or [],
                "characters_present": mem.get("characters_present") or mem.get("charactersPresent") or [],
                "character_updates": mem.get("character_updates") or mem.get("characterChanges") or [],
                "foreshadows_added": mem.get("foreshadows_added") or mem.get("foreshadowsAdded") or [],
                "open_threads": mem.get("open_threads") or mem.get("openThreads") or [],
                "important_items": mem.get("important_items") or mem.get("importantItems") or [],
            }
        )
    return ctx


async def _load_outline(db: AsyncSession, project_id: uuid.UUID, chapter_id: uuid.UUID) -> dict | None:
    """加载当前章细纲（outlines 表 chapter 类型，content 为细纲文本）。"""
    outline = (
        await db.execute(select(Outline).where(Outline.id == chapter_id, Outline.project_id == project_id))
    ).scalar_one_or_none()
    if outline is None:
        return None
    return {
        "id": str(outline.id),
        "title": outline.title,
        "summary": outline.summary,
        "content": outline.content,
        "outline_type": outline.outline_type,
    }


def _split_outline_items(outline_text: str) -> list[str]:
    """把细纲文本切成可核对条目（按行/句号/分号/编号）。"""
    if not outline_text:
        return []
    text = outline_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) > 1:
        return lines
    # 单行时按句号、分号切
    parts = re.split(r"[。；;\n]", text)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def detect_outline_completion(content: str, outline_text: str) -> dict:
    """细纲完成度检查：goal/conflict/key_events 逐项覆盖（关键词匹配，供 mock 与演示）。"""
    result = {"completed": [], "uncovered": [], "score": 0}
    norm_content = _normalize(content)
    items = _split_outline_items(outline_text)
    if not items:
        return result
    for item in items:
        norm_item = _normalize(item)
        if not norm_item or len(norm_item) < 3:
            continue
        # 用条目中较有区分度的词检查覆盖：整段包含或关键名词包含
        hit = norm_item in norm_content
        if not hit and len(norm_item) >= 8:
            # 拆出 2+ 字符词，命中 60% 以上视为覆盖
            segs = [norm_item[i : i + 3] for i in range(0, len(norm_item) - 2)]
            hit_cnt = sum(1 for s in set(segs) if s in norm_content)
            hit = hit_cnt >= max(1, int(len(set(segs)) * 0.6))
        if hit:
            result["completed"].append(item)
        else:
            result["uncovered"].append(item)
    total = len(result["completed"]) + len(result["uncovered"])
    result["score"] = int(total and len(result["completed"]) / total * 100)
    return result


async def _collect_unpaid_foreshadows(
    db: AsyncSession, project_id: uuid.UUID, current_chapter_id: uuid.UUID
) -> list[dict]:
    """收集未回收伏笔，对已到计划回收章节仍未回收的给出提醒。

    status in (planned|planted) 视为未回收；planned_payoff_chapter_id 对应章节
    已存在（章节排序在计划回收章节之前/等于）且当前章不是埋设章时提醒。
    """
    rows = (
        await db.execute(
            select(Foreshadow).where(
                Foreshadow.project_id == project_id,
                Foreshadow.status.in_(["planned", "planted"]),
            )
        )
    ).scalars().all()
    reminders: list[dict] = []
    for f in rows:
        item = {
            "id": str(f.id),
            "content": f.content,
            "planted_chapter_id": str(f.planted_chapter_id) if f.planted_chapter_id else None,
            "planned_payoff_chapter_id": str(f.planned_payoff_chapter_id) if f.planned_payoff_chapter_id else None,
            "planned_payoff": f.planned_payoff or "",
            "status": f.status,
            "due": False,
        }
        if f.planned_payoff_chapter_id and str(f.planned_payoff_chapter_id) != str(current_chapter_id):
            # 计划回收章节 <= 当前章节序号 → 已到期
            current = (
                await db.execute(select(Outline).where(Outline.id == current_chapter_id))
            ).scalar_one_or_none()
            target = (
                await db.execute(select(Outline).where(Outline.id == f.planned_payoff_chapter_id))
            ).scalar_one_or_none()
            if (
                current is not None
                and target is not None
                and (target.sort_order or 0) <= (current.sort_order or 0)
            ):
                item["due"] = True
        reminders.append(item)
    return reminders


# ---------------------------------------------------------------- mock ----------

def build_mock_check(
    chapter_title: str,
    content: str,
    outline: dict | None,
    unpaid: list[dict],
    repetition_issues: list[dict],
    prev_summary_issues: list[dict],
) -> dict:
    """演示模式的合理 mock 检查结果：本地规则真实执行 + 固定演示 issue。"""
    clean = (content or "").replace("\n", " ").replace("\r", " ").strip()
    seg = clean[:80].strip()

    issues: list[dict] = []
    issues.extend(repetition_issues)
    issues.extend(prev_summary_issues)

    # 细纲完成度（本地规则）
    outline_result = {
        "completed": [],
        "uncovered": [],
        "score": 100,
    }
    if outline and outline.get("content"):
        outline_result = detect_outline_completion(content, outline["content"])
        for item in outline_result["uncovered"]:
            issues.append(
                {
                    "type": "outline_deviation",
                    "severity": "warning",
                    "location": "本章",
                    "message": f"细纲条目未在正文中找到对应内容：{item}",
                    "suggestion": "补充该情节，或在细纲中调整删除该条目并说明理由。",
                    "data": {"outline_item": item},
                }
            )
    elif not (outline and outline.get("content")):
        outline_result = {
            "completed": [],
            "uncovered": [],
            "score": 100,
        }
    if not outline_result["uncovered"]:
        issues.append(
            {
                "type": "outline_deviation",
                "severity": "warning",
                "location": "本章",
                "message": "演示提示：细纲条目「矿洞中火光来历揭示」未在正文中找到对应内容，请确认是否已覆盖。",
                "suggestion": "补充该情节，或在细纲中调整删除该条目并说明理由。",
                "data": {"outline_item": "矿洞中火光来历揭示"},
            }
        )

    # 演示 issue：人物状态冲突（呼应正文开头）
    if seg:
        issues.append(
            {
                "type": "character_state_conflict",
                "severity": "error",
                "location": "第1段",
                "message": f"正文称「{seg[:16]}…」，与已确认人物卡状态不一致。",
                "suggestion": "确认人物当前状态后修改正文，或更新人物卡。",
                "data": {"character": "陈九", "expected_status": "在世"},
            }
        )
        issues.append(
            {
                "type": "location_conflict",
                "severity": "warning",
                "location": "第2段",
                "message": "陈九当前应位于边军大营，正文出现了北关城场景描述。",
                "suggestion": "检查是否已离开边军大营，补充过渡情节。",
                "data": {"character": "陈九", "expected_location": "边军大营"},
            }
        )
        issues.append(
            {
                "type": "timeline_error",
                "severity": "warning",
                "location": "第3段",
                "message": "时间线事件顺序与已确认时间线冲突（应晚于「陈九加入边军大营」）。",
                "suggestion": "调整事件顺序或补充时间过渡。",
                "data": {"event": "北关城旧事"},
            }
        )
        issues.append(
            {
                "type": "world_conflict",
                "severity": "warning",
                "location": "第4段",
                "message": "称谓「都尉大人」与世界设定中「边军大营称谓体系」不符。",
                "suggestion": "按世界设定使用规范称谓。",
                "data": {"world_item": "称谓体系"},
            }
        )

    # 未回收伏笔提醒（本地规则）
    reminders: list[dict] = []
    for u in unpaid:
        if u.get("due"):
            reminders.append(
                {
                    "id": u["id"],
                    "content": u["content"],
                    "planned_payoff": u.get("planned_payoff") or "",
                    "status": u.get("status") or "planted",
                    "type": "foreshadow_due",
                    "message": "伏笔已到计划回收章节仍未回收，请在后续剧情中安排回收或明确延后。",
                    "suggestion": "在最近章节安排回收，或将计划回收章节顺延。",
                }
            )
        else:
            reminders.append(
                {
                    "id": u["id"],
                    "content": u["content"],
                    "planned_payoff": u.get("planned_payoff") or "",
                    "status": u.get("status") or "planted",
                    "type": "foreshadow_open",
                    "message": "该伏笔仍在埋设/未回收状态，续写时注意保持线索连贯。",
                    "suggestion": "如已在当前章回收，请将伏笔状态更新为 resolved。",
                }
            )

    # 拟采纳更新（采纳后进入第三期确认流程）
    proposed_updates = [
        {
            "category": "character_state",
            "title": "确认人物状态：陈九当前状态",
            "description": "检查发现正文人物状态与人物卡存在冲突，建议确认后更新人物卡状态。",
            "payload": {"character": "陈九", "changes": ["确认当前状态为「在世」"], "current_location": "边军大营", "current_timeline_status": "加入边军大营", "status": "在世"},
        },
        {
            "category": "timeline",
            "title": "时间线事件确认：陈九离开北关城",
            "description": "建议把正文事件补充进已确认时间线。",
            "payload": {"title": "陈九离开北关城", "event_date_label": "景历二十三年秋", "sequence_order": 2, "participants": ["陈九"], "description": "陈九在边军大营期间回忆北关城旧事，正式告别旧身份。"},
        },
    ]

    score = 100
    error_cnt = sum(1 for i in issues if i.get("severity") == "error")
    warn_cnt = sum(1 for i in issues if i.get("severity") == "warning")
    score -= error_cnt * 12 + warn_cnt * 4
    score = max(0, min(100, score))

    return {
        "mode": "mock",
        "score": score,
        "issues": issues,
        "confirmed_updates": [],
        "proposed_updates": proposed_updates,
        "outline": outline_result,
        "repetition": {
            "internal": repetition_issues,
            "against_previous_summary": prev_summary_issues,
        },
        "style": {
            "score": 88,
            "evaluation": "整体文风与既往章节一致；个别段落用词偏现代，建议微调。（演示模式固定评分）",
        },
        "reminders": reminders,
    }


# ---------------------------------------------------------------- 主流程 ----------

async def run_consistency_check(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
    outline_text: str | None = None,
) -> dict:
    """对指定章节正文执行一致性检查。

    返回固定 JSON：score/issues[]/confirmed_updates[]/proposed_updates[]/reminders[]/
    outline{completed,uncovered,score}/repetition/style/mode。
    """
    provider, model = resolve_ai_provider()
    real = is_ai_configured()

    # 本地规则（两种模式都执行）
    repetition_issues = detect_internal_repetition(content)
    ctx = await _load_confirmed_context(db, project_id)
    previous_summaries = [
        m.get("summary") or ""
        for m in ctx["memories"]
        if m.get("chapter_id") != str(chapter_id) and m.get("summary")
    ]
    prev_summary_issues = detect_previous_summary_repetition(content, previous_summaries)

    # 细纲：优先请求体传入，否则读 outlines 当前章节 content
    outline = await _load_outline(db, project_id, chapter_id)
    if not outline_text and outline:
        outline_text = outline.get("content") or ""
    outline_result = (
        detect_outline_completion(content, outline_text)
        if outline_text
        else {"completed": [], "uncovered": [], "score": 0}
    )
    for item in outline_result.get("uncovered") or []:
        prev_summary_issues.append(
            {
                "type": "outline_deviation",
                "severity": "warning",
                "location": "本章",
                "message": f"细纲条目未在正文中找到对应内容：{item}",
                "suggestion": "补充该情节，或在细纲中调整删除该条目并说明理由。",
                "data": {"outline_item": item},
            }
        )

    unpaid = await _collect_unpaid_foreshadows(db, project_id, chapter_id)

    system_prompt = (
        "你是小说一致性检查器。请阅读章节正文与项目已确认设定，检查以下维度："
        "人物身份/年龄/能力冲突、人物不应出现的地点、死亡/失踪/受伤状态矛盾、时间顺序错误、"
        "伏笔过早揭露、关键物品凭空出现或消失、世界观/称谓/势力归属冲突、当前章偏离细纲、重复段落或明显复述。"
        "必须只输出 JSON，不要任何多余文字。"
    )

    context_json = json.dumps(
        {
            "chapter_id": str(chapter_id),
            "chapter_title": chapter_title or "",
            "content": content or "",
            "outline": outline_text or "",
            "confirmed_context": ctx,
            "unpaid_foreshadows": unpaid,
            "local_repetition": repetition_issues,
            "local_outline": outline_result,
        },
        ensure_ascii=False,
    )
    user_prompt = (
        "检查对象：\n" + context_json + "\n\n"
        "输出 JSON 格式：\n"
        '{"score": 0-100 整数, '
        '"issues": [{"severity": "warning|error", "type": "character_state_conflict|location_conflict|status_conflict|timeline_error|foreshadow_premature|item_inconsistency|world_conflict|outline_deviation|repetition", '
        '"location": "如 第4段", "message": "问题描述", "suggestion": "修改建议"}], '
        '"confirmed_updates": [{"category": "character_state|relationship|timeline|foreshadow", "title": "标题", "description": "说明", "payload": {}}], '
        '"proposed_updates": [{"category": "character_state|relationship|timeline|foreshadow", "title": "标题", "description": "说明", "payload": {}}], '
        '"reminders": [{"type": "foreshadow_due|foreshadow_open", "content": "伏笔内容", "planned_payoff": "计划回收", "status": "planned|planted|resolved|abandoned", "message": "提醒", "suggestion": "建议"}], '
        '"outline": {"completed": ["已覆盖细纲条目"], "uncovered": ["未覆盖细纲条目"], "score": 0-100}, '
        '"repetition": {"internal": [], "against_previous_summary": []}, '
        '"style": {"score": 0-100, "evaluation": "文风一致性评估"}}'
    )

    raw = None
    status = "succeeded"
    error_message: str | None = None

    if real:
        try:
            raw = await _chat_with_ai_retry_on_truncation(
                    system_prompt,
                    user_prompt,
                    task_type=TaskType.CONSISTENCY_REVIEW.value,
                )
            raw = _strip_code_fence(raw)
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error_message = str(exc)[:500]
            parsed = None
    else:
        parsed = build_mock_check(
            chapter_title,
            content,
            outline,
            unpaid,
            repetition_issues,
            prev_summary_issues,
        )

    if status == "failed":
        # AI 调用失败时如实返回失败信号（score=-1 / mode=failed），
        # 不得归一化成 score=100、mode=real 的“正常结果”误导调用方。
        return {
            "status": "failed",
            "mode": "failed",
            "score": -1,
            "issues": [],
            "confirmed_updates": [],
            "proposed_updates": [],
            "reminders": [],
            "outline": {"completed": [], "uncovered": [], "score": 0},
            "repetition": {"internal": [], "against_previous_summary": []},
            "style": {"score": 0, "evaluation": ""},
            "error_message": error_message,
        }

    result = _normalize_check_result(parsed or {}, outline_result, repetition_issues, prev_summary_issues)

    # P3 统一上下文构建器：一致性检查统一使用 build_writing_context 组装检查依据，
    # 并将 WritingContext 原样写入 generation_records.context_snapshot。
    writing_context: dict | None = None
    try:
        writing_context = await build_writing_context(
            db,
            project_id,
            chapter_id,
            instruction="对以下正文执行一致性检查，报告与已确认设定的冲突，不修改正文。",
        )
    except Exception:  # noqa: BLE001
        writing_context = None

    try:
        snapshot_payload = {
            "chapter_title": chapter_title,
            "content_length": len(content or ""),
            "outline_length": len(outline_text or ""),
            "trigger": "manual_or_auto",
            "score": result.get("score"),
            "issue_count": len(result.get("issues") or []),
        }
        if writing_context is not None:
            snapshot_payload["writing_context"] = writing_context
        await record_generation(
            db,
            attribution_status="project_bound",
            project_id=project_id,
            chapter_id=chapter_id,
            action_type="check",
            model_provider=(provider or "mock") if real else "mock",
            model_name=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=serialize_context(snapshot_payload),
            response_content=json.dumps(result, ensure_ascii=False),
            status=status,
            error_message=error_message,
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
    return result


def _normalize_check_result(
    parsed: dict,
    outline_result: dict,
    repetition_issues: list[dict],
    prev_summary_issues: list[dict],
) -> dict:
    """归一化检查结果，保证字段齐全且类型正确。"""
    issues = parsed.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    clean_issues = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        itype = str(it.get("type") or "")
        if itype not in ISSUE_TYPES:
            itype = "character_state_conflict"
        sev = str(it.get("severity") or "warning")
        if sev not in SEVERITIES:
            sev = "warning"
        clean_issues.append(
            {
                "severity": sev,
                "type": itype,
                "location": str(it.get("location") or ""),
                "message": str(it.get("message") or ""),
                "suggestion": str(it.get("suggestion") or ""),
                "data": it.get("data") or {},
            }
        )
    if not clean_issues and (repetition_issues or prev_summary_issues):
        clean_issues = [dict(i) for i in repetition_issues + prev_summary_issues]

    def _clean_updates(key: str) -> list[dict]:
        ups = parsed.get(key) or []
        if not isinstance(ups, list):
            return []
        out = []
        for u in ups:
            if not isinstance(u, dict):
                continue
            out.append(
                {
                    "category": str(u.get("category") or "character_state"),
                    "title": str(u.get("title") or "未命名建议"),
                    "description": str(u.get("description") or ""),
                    "payload": u.get("payload") or {},
                }
            )
        return out

    reminders = parsed.get("reminders") or []
    if not isinstance(reminders, list):
        reminders = []

    outline = parsed.get("outline")
    if not isinstance(outline, dict):
        outline = outline_result

    repetition = parsed.get("repetition")
    if not isinstance(repetition, dict):
        repetition = {
            "internal": repetition_issues,
            "against_previous_summary": prev_summary_issues,
        }

    style = parsed.get("style")
    if not isinstance(style, dict):
        style = {"score": 0, "evaluation": ""}

    score = parsed.get("score")
    try:
        score = max(0, min(100, int(score)))
    except Exception:  # noqa: BLE001
        score = 100 - sum(12 if i["severity"] == "error" else 4 for i in clean_issues)
        score = max(0, min(100, score))

    return {
        "mode": str(parsed.get("mode") or ("real" if is_ai_configured() else "mock")),
        "score": score,
        "issues": clean_issues,
        "confirmed_updates": _clean_updates("confirmed_updates"),
        "proposed_updates": _clean_updates("proposed_updates"),
        "reminders": reminders,
        "outline": outline,
        "repetition": repetition,
        "style": style,
    }


async def trigger_auto_check(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> dict | None:
    """生成正文后自动一致性检查入口（受项目设置 auto_check 开关控制，失败不阻断保存）。"""
    try:
        from app.services.chapter_memory_service import get_or_create_project_setting

        setting = await get_or_create_project_setting(db, project_id)
        if not getattr(setting, "auto_check", False):
            return None
        return await run_consistency_check(db, project_id, chapter_id, chapter_title, content)
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
        return None
