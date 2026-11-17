# Context Builder（文26 P3「统一上下文构建器」规格实现）
# 唯一入口 build_writing_context：所有正文生成/续写/改写/一致性检查统一调用，
# 按文26 WritingContext 组装：
#   writingRules → currentVolumeOutline → currentChapterOutline → sceneGoal
#   → recentChapterMemories → activeCharacters → timelineConstraints
#   → activeForeshadows → previousEnding → userInstruction
# 组装顺序对应文26：①项目写作规则与禁忌 ②当前卷目标 ③当前章节/场景细纲
#   ④已确认人物资料与动态状态 ⑤当前时间、地点和势力约束 ⑥应埋设/允许回收/禁止揭露的伏笔
#   ⑦最近章节摘要 ⑧上章末尾原文 ⑨用户本次要求。
# 三层上下文策略：
#   长期（项目规则/世界观/人物固定信息/核心谜案/已确认关系）→ writingRules
#   中期（当前卷目标/未回收伏笔/当前人物状态/当前时间线）→ currentVolumeOutline / activeForeshadows / activeCharacters / timelineConstraints
#   短期（最近2~3章摘要/上章末尾800~1500字/当前章细纲）→ recentChapterMemories / previousEnding / currentChapterOutline / sceneGoal
# build_continuation_context 保留为提示词组装外壳（续写/改写/润色路由继续使用），内部统一走 build_writing_context。
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChapterContent,
    ChapterMemory,
    Character,
    CharacterRelationship,
    CharacterStateSnapshot,
    Faction,
    Foreshadow,
    Location,
    Outline,
    Project,
    TimelineEvent,
    WorldSetting,
)
from app.services.chapter_plan_service import (
    chapter_number_of,
    load_chapter_plan,
    load_following_plans,
)


def _following_plans_from_snapshot(
    chapter_plans_snapshot: dict, chapter_id: str
) -> list[dict]:
    """从已确认版本快照中按 chapterNumber 取当前章之后的章节计划（稳定排序）。"""
    plans = []
    for cid, plan in (chapter_plans_snapshot or {}).items():
        if not isinstance(plan, dict) or str(cid) == str(chapter_id):
            continue
        plans.append(plan)
    plans.sort(key=lambda p: (p.get("chapterNumber") or 0))
    return plans

# 前章末尾回退片段长度（短期记忆，文26 要求 800~1500 字，取 1200）
PREV_TAIL_LENGTH = 1200
# 长短期分段限长保护
MAX_LONG_TERM_CHARS = 3000
MAX_MEDIUM_TERM_CHARS = 2500
MAX_SHORT_TERM_CHARS = 1800
# 最近章节记忆数量（文26 短期：最近 2~3 章摘要）
RECENT_MEMORY_COUNT = 3


def _join_text(rows) -> str:
    """把 [(label, text), ...] 拼成可读文本，过滤空值。"""
    lines = []
    for label, text in rows:
        if text and str(text).strip():
            lines.append(f"- {label}：{str(text).strip()}")
    return "\n".join(lines)


def _parse_json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:  # noqa: BLE001
        return []


async def _latest_chapter_content(
    db: AsyncSession, chapter_id: uuid.UUID
) -> str:
    row = (
        await db.execute(
            select(ChapterContent)
            .where(ChapterContent.outline_id == chapter_id)
            .order_by(ChapterContent.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (row.content if row and row.content else "").strip()


async def _chapter_memory_of(
    db: AsyncSession, chapter_id: uuid.UUID
) -> ChapterMemory | None:
    row = (
        await db.execute(
            select(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id)
        )
    ).scalar_one_or_none()
    return row


def _chapter_memory_payload(mem: ChapterMemory) -> dict:
    """文26 ChapterMemory 结构（含 chapterTitle 供提示词渲染与前端展示）。"""
    return {
        "id": str(mem.id),
        "chapterId": str(mem.chapter_id),
        "summary": mem.summary or "",
        "structuredMemory": mem.structured_memory,
        "source": mem.source,
        "status": mem.status,
        "generationRecordId": (
            str(mem.generation_record_id) if mem.generation_record_id else None
        ),
        "updatedAt": mem.updated_at.isoformat() if mem.updated_at else None,
    }


async def _collect_volume_summary(
    db: AsyncSession, chapter: Outline, volume: Outline | None
) -> str:
    """卷内剧情脉络（2026-11-16 第三档）：当前章之前各章目标拼接，帮生成跨章承接。

    数据源优先 volume.summary（人工/规划写好的卷摘要）；否则用卷内前文章节的
    chapter_goal 拼接（最多 20 条、每条截 80 字）。零 API 成本。
    """
    if volume is not None and (volume.summary or "").strip():
        return (volume.summary or "").strip()[:3000]
    stmt = select(Outline).where(
        Outline.project_id == chapter.project_id,
        Outline.outline_type == "chapter",
        Outline.sort_order < chapter.sort_order,
    ).order_by(Outline.sort_order.asc()).limit(20)
    rows = (await db.execute(stmt)).scalars().all()
    lines = []
    for r in rows:
        goal = (r.chapter_goal or "").strip() or (
            (parse_chapter_plan(r.chapter_plan) or {}).get("summary") or ""
        ).strip()
        if goal:
            lines.append(f"第{r.sort_order}章《{r.title or ''}》：{goal[:80]}")
    return "\n".join(lines)[:3000]


async def _collect_related_past(
    db: AsyncSession, project_id: uuid.UUID, chapter: Outline
) -> str:
    """相关往事（2026-11-16 第三档）：按本章计划的关键人物/地点做项目内关键词检索，
    取历史原文片段注入（带章题出处）。无计划或索引缺失时返回空串。
    """
    try:
        from app.services import fts_search, search_service

        plan = parse_chapter_plan(chapter.chapter_plan) or {}
        names = (plan.get("requiredCharacters") or [])[:2]
        locations = (plan.get("requiredLocations") or [])[:1]
        terms = [str(n).strip() for n in names + locations if str(n).strip()]
        if not terms:
            return ""
        # 批次 5：优先 FTS5（bm25 相关度排序，零 API 成本）；索引缺失回退旧关键词检索
        items = await fts_search.fts_search_chapters(
            db, project_id, terms, limit=3
        )
        if not items:
            q = " ".join(terms)
            legacy_items, _total = await search_service.search(
                db,
                project_id,
                q,
                target_type="chapter_content",
                page=1,
                page_size=3,
                sort="relevance",
            )
            items = [
                {"title": it.get("title") or "", "text": it.get("text") or it.get("summary") or ""}
                for it in legacy_items
            ]
        blocks = []
        for it in items:
            title = it.get("title") or ""
            snippet = (it.get("text") or "").strip()
            if not snippet:
                continue
            blocks.append(f"《{title}》片段：{snippet[:400]}")
        return "\n".join(blocks)[:1500]
    except Exception:  # noqa: BLE001 —— 检索失败不阻断生成
        return ""


async def _collect_confirmed_foreshadows(
    db: AsyncSession, project_id: uuid.UUID
) -> list[str]:
    """聚合项目内伏笔约束文本。

    数据源（2026-11-15 第二档升级）：
    1) foreshadows 账本表（权威，人工确认/回填后的未回收伏笔优先）；
    2) 已确认章节记忆中的 foreshadowsAdded / openThreads（原始来源兜底）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _append(content: str, payoff: str | None = None) -> None:
        if not content:
            return
        key = content[:40]
        if key in seen:
            return
        seen.add(key)
        out.append(
            f"【伏笔】{content}"
            + (f"（计划回收：{payoff}）" if payoff else "")
        )

    # 1) 伏笔账本：未回收的（planted / planned）优先，按重要性+创建时间
    ledger = (
        await db.execute(
            select(Foreshadow)
            .where(
                Foreshadow.project_id == project_id,
                Foreshadow.status.in_(("planted", "planned")),
            )
            .order_by(
                Foreshadow.importance.desc(), Foreshadow.created_at.asc()
            )
        )
    ).scalars().all()
    for f in ledger:
        _append(f.content or f.title or "", f.planned_payoff)

    # 2) 已确认章节记忆兜底
    rows = (
        await db.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.project_id == project_id,
                ChapterMemory.status == "confirmed",
            )
            .order_by(ChapterMemory.created_at.asc())
        )
    ).scalars().all()
    for row in rows:
        try:
            mem = json.loads(row.memory_json or "{}")
        except Exception:  # noqa: BLE001
            mem = {}
        structured = row.structured_memory
        for f in structured.get("foreshadowsAdded") or []:
            content = f.get("content") if isinstance(f, dict) else ""
            payoff = f.get("plannedPayoff") if isinstance(f, dict) else ""
            _append(content, payoff)
        for t in structured.get("openThreads") or []:
            if t:
                if t[:40] not in seen:
                    seen.add(t[:40])
                    out.append(f"【未解决】{t}")
        # 兼容二期旧结构（memory_json 顶层 foreshadows_added / open_threads）
        for f in mem.get("foreshadows_added") or []:
            content = f.get("content") if isinstance(f, dict) else ""
            payoff = f.get("planned_payoff") if isinstance(f, dict) else ""
            _append(content, payoff)
        for t in mem.get("open_threads") or []:
            if t:
                if t[:40] not in seen:
                    seen.add(t[:40])
                    out.append(f"【未解决】{t}")
    return out[:12]


async def _load_volume(
    db: AsyncSession, chapter: Outline
) -> Outline | None:
    if not chapter.parent_id:
        return None
    return (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter.parent_id,
                Outline.outline_type == "volume",
            )
        )
    ).scalar_one_or_none()


async def _load_sibling_chapters(
    db: AsyncSession, project_id: uuid.UUID, chapter: Outline, volume: Outline | None
) -> list[Outline]:
    """当前章所在卷内按 sort_order 排序的章节列表（不含当前章）。"""
    if volume is not None:
        rows = (
            await db.execute(
                select(Outline)
                .where(
                    Outline.project_id == project_id,
                    Outline.parent_id == volume.id,
                    Outline.outline_type == "chapter",
                    Outline.id != chapter.id,
                )
                .order_by(Outline.sort_order.asc(), Outline.created_at.asc())
                .limit(50)
            )
        ).scalars().all()
    else:
        rows = (
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
    return list(rows)


async def _load_recent_chapter_memories(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter: Outline,
    volume: Outline | None,
    count: int = RECENT_MEMORY_COUNT,
) -> list[dict]:
    """短期上下文：最近 2~3 章章节记忆（不含当前章），按章节顺序升序输出。

    优先已确认记忆；未确认记忆也带回（status=draft 供提示词标注「待确认」）。
    无记忆的最近章节返回占位结构（summary 为空，带章节标题），保证「最近章节摘要」层可见。
    """
    siblings = await _load_sibling_chapters(db, project_id, chapter, volume)
    # 取本章之前的最近 count 章（按 sort_order 升序的末尾段）
    recent = siblings[-count:] if len(siblings) >= count else siblings
    out: list[dict] = []
    for row in recent:
        mem = await _chapter_memory_of(db, row.id)
        if mem is not None:
            payload = _chapter_memory_payload(mem)
        else:
            payload = {
                "id": None,
                "chapterId": str(row.id),
                "chapterTitle": row.title or "",
                "summary": "",
                "structuredMemory": {
                    "timeLabel": None,
                    "locations": [],
                    "charactersPresent": [],
                    "characterChanges": [],
                    "relationshipChanges": [],
                    "timelineEvents": [],
                    "foreshadowsAdded": [],
                    "foreshadowsResolved": [],
                    "importantItems": [],
                    "openThreads": [],
                    "nextChapterRequirements": [],
                },
                "source": "manual",
                "status": "draft",
                "generationRecordId": None,
                "updatedAt": None,
            }
        payload["chapterTitle"] = row.title or ""
        out.append(payload)
    return out


async def _load_active_characters(
    db: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """中期上下文：人物卡 + 最近已确认状态快照（CharacterWithCurrentState）。

    currentState 取该人物最近一条 user_confirmed（confirmed_at 非空）的状态快照；
    无已确认快照时回退 characters 表当前字段。
    """
    chars = (
        await db.execute(
            select(Character)
            .where(Character.project_id == project_id)
            .order_by(Character.created_at.asc())
            .limit(30)
        )
    ).scalars().all()
    out: list[dict] = []
    for c in chars:
        latest = (
            await db.execute(
                select(CharacterStateSnapshot)
                .where(
                    CharacterStateSnapshot.character_id == c.id,
                    CharacterStateSnapshot.confirmed_at.is_not(None),  # noqa: E711
                )
                .order_by(
                    CharacterStateSnapshot.created_at.desc(),
                    CharacterStateSnapshot.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        current_state: dict = {}
        if latest is not None:
            try:
                st = json.loads(latest.state_json or "{}")
                if isinstance(st, dict):
                    current_state = st
            except Exception:  # noqa: BLE001
                current_state = {}
        out.append(
            {
                "id": str(c.id),
                "name": c.name,
                "aliases": _parse_json_list(c.aliases),
                "role": c.role,
                "identity": c.identity,
                "personality": c.personality,
                "profile": c.profile,
                "goals": c.goals or c.goal,
                "secrets": c.secrets,
                "abilities": c.abilities,
                "conflict": c.conflict,
                "status": c.status,
                "currentLocation": c.current_location,
                "currentTimelineStatus": c.current_timeline_status,
                "currentState": current_state,
                "firstChapterId": (
                    str(c.first_chapter_id) if c.first_chapter_id else None
                ),
                "lastChapterId": (
                    str(c.last_chapter_id) if c.last_chapter_id else None
                ),
            }
        )
    return out


async def _load_timeline_constraints(
    db: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """中期上下文：TimelineEvent 数组（按 sort_order/sequence_order 排序，含 confirmed 标记）。"""
    events = (
        await db.execute(
            select(TimelineEvent)
            .where(TimelineEvent.project_id == project_id)
            .order_by(
                TimelineEvent.sort_order.asc(),
                TimelineEvent.sequence_order.asc(),
                TimelineEvent.created_at.asc(),
            )
            .limit(50)
        )
    ).scalars().all()
    return [
        {
            "id": str(e.id),
            "chapterId": str(e.chapter_id) if e.chapter_id else None,
            "title": e.title,
            "description": e.description,
            "eventTime": e.event_time,
            "eventDateLabel": e.event_date_label,
            "locationId": str(e.location_id) if e.location_id else None,
            "participants": _parse_json_list(e.participants),
            "sortOrder": e.sort_order,
            "sequenceOrder": e.sequence_order,
            "confirmed": e.confirmed,
            "source": e.source,
        }
        for e in events
    ]


async def _load_active_foreshadows(
    db: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """中期上下文：伏笔约束数组。

    status 语义（应埋设/允许回收/禁止揭露）：
      planned   → 已登记未埋设，本章应埋设
      planted   → 已埋设，允许回收；未到计划回收章节前禁止提前揭露
      resolved  → 已回收，仅作背景参考
      abandoned → 已弃用，禁止再次使用
    """
    rows = (
        await db.execute(
            select(Foreshadow)
            .where(Foreshadow.project_id == project_id)
            .order_by(Foreshadow.status.asc(), Foreshadow.created_at.asc())
            .limit(40)
        )
    ).scalars().all()
    return [
        {
            "id": str(f.id),
            "title": f.title,
            "content": f.content,
            "status": f.status,
            "plantedChapterId": (
                str(f.planted_chapter_id) if f.planted_chapter_id else None
            ),
            "plannedPayoffChapterId": (
                str(f.planned_payoff_chapter_id)
                if f.planned_payoff_chapter_id
                else None
            ),
            "plannedPayoff": f.planned_payoff,
            "resolvedChapterId": (
                str(f.resolved_chapter_id) if f.resolved_chapter_id else None
            ),
            "paidOffChapterId": (
                str(f.paid_off_chapter_id) if f.paid_off_chapter_id else None
            ),
            "importance": f.importance,
            "source": f.source,
            "note": f.note,
        }
        for f in rows
    ]


async def _load_locations_factions(
    db: AsyncSession, project_id: uuid.UUID
) -> str:
    """地点/势力静态约束文本（并入 writingRules 长期设定，供顺序⑤的「地点和势力约束」）。"""
    locations = (
        await db.execute(
            select(Location)
            .where(Location.project_id == project_id)
            .limit(20)
        )
    ).scalars().all()
    factions = (
        await db.execute(
            select(Faction).where(Faction.project_id == project_id).limit(20)
        )
    ).scalars().all()
    lines = []
    for loc in locations:
        lines.append(
            f"- 地点：{loc.name}（{loc.category or '未分类'}）"
            + (f"：{loc.description}" if loc.description else "")
        )
    for f in factions:
        lines.append(
            f"- 势力：{f.name}"
            + (f"：{f.description}" if f.description else "")
            + (
                f"（领袖：{f.leader}；目标：{f.goal}）"
                if (f.leader or f.goal)
                else ""
            )
        )
    return "\n".join(lines)[:MAX_MEDIUM_TERM_CHARS]


async def _load_writing_rules(
    db: AsyncSession, project_id: uuid.UUID, project: Project | None
) -> str:
    """长期上下文 writingRules：项目规则与禁忌 + 世界观条目 + 地点/势力 + 人物固定信息 + 核心谜案 + 已确认关系。"""
    parts = _join_text(
        [
            ("作品类型", project.genre if project else None),
            ("叙事视角", project.narrative_perspective if project else None),
            ("写作风格", project.writing_style if project else None),
            ("世界观设定", project.world_setting if project else None),
            ("核心冲突", project.main_conflict if project else None),
            ("结局方向", project.ending_direction if project else None),
            ("禁用设定", project.forbidden_settings if project else None),
        ]
    )
    # 世界设定条目（知识库式）
    world_rows = (
        await db.execute(
            select(WorldSetting)
            .where(WorldSetting.project_id == project_id)
            .order_by(WorldSetting.importance.desc())
            .limit(20)
        )
    ).scalars().all()
    if world_rows:
        extra = _join_text(
            [
                (f"{w.name}（{w.category}）", w.content)
                for w in world_rows
            ]
        )
        if extra:
            parts = parts + ("\n" if parts else "") + extra
    # 地点/势力约束
    loc_fac = await _load_locations_factions(db, project_id)
    if loc_fac:
        parts = parts + ("\n" if parts else "") + loc_fac
    # 人物固定信息（长期）
    chars = (
        await db.execute(
            select(Character)
            .where(Character.project_id == project_id)
            .order_by(Character.created_at.asc())
            .limit(30)
        )
    ).scalars().all()
    char_lines = []
    for c in chars:
        fixed_bits = [
            ("身份", c.identity),
            ("性格", c.personality),
            ("外貌", c.appearance),
            ("背景", c.background),
            ("目标", c.goal),
            ("冲突", c.conflict),
            ("能力", c.abilities),
            ("已知秘密", c.secrets),
            ("档案", c.profile),
        ]
        desc = "；".join(
            f"{label}：{val}" for label, val in fixed_bits if val
        )
        if not desc:
            continue
        char_lines.append(f"- {c.name}：{desc}")
    if char_lines:
        parts = parts + ("\n" if parts else "") + "\n".join(char_lines)
    # 核心谜案（main_conflict 已在上面）
    # 已确认关系（长期）
    relations = (
        await db.execute(
            select(CharacterRelationship)
            .where(
                CharacterRelationship.project_id == project_id,
                CharacterRelationship.status == "confirmed",
            )
            .limit(40)
        )
    ).scalars().all()
    if relations:
        rel_lines = []
        for r in relations:
            src_name = r.source_character_id
            tgt_name = r.target_character_id
            # 联查人物名（关系表只存 id）
            src = (
                await db.execute(
                    select(Character.name).where(Character.id == r.source_character_id)
                )
            ).scalar_one_or_none()
            tgt = (
                await db.execute(
                    select(Character.name).where(Character.id == r.target_character_id)
                )
            ).scalar_one_or_none()
            src_name = src or str(r.source_character_id)[:8]
            tgt_name = tgt or str(r.target_character_id)[:8]
            rel_lines.append(
                f"- {src_name} —[{r.relation_type}]— {tgt_name}"
                + (f"：{r.description}" if r.description else "")
            )
        parts = parts + ("\n" if parts else "") + "已确认关系：\n" + "\n".join(rel_lines)
    return parts[:MAX_LONG_TERM_CHARS]


async def build_writing_context(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    *,
    instruction: str = "",
    confirmed_version: dict | None = None,
    confirmed_plan: dict | None = None,
) -> dict:
    """P3 唯一 ContextBuilder 主入口：按文26 WritingContext 组装上下文。

    返回 WritingContext 结构：
      writingRules / currentVolumeOutline / currentChapterOutline / sceneGoal /
      recentChapterMemories / activeCharacters / timelineConstraints /
      activeForeshadows / previousEnding / userInstruction
    附加 memoryTiers（长期/中期/短期标注，供快照复盘与调试）。

    「已确认大纲版本化」：confirmed_version + confirmed_plan 同时传入时，
    当前章节计划与后续章节计划均以 active confirmed 版本快照为唯一事实来源
    （不读 outlines.chapter_plan 草稿，避免草稿覆盖已确认计划）。
    """
    chapter = (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter_id, Outline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if chapter is None:
        raise ValueError("chapter_not_found")

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()

    volume = await _load_volume(db, chapter)
    siblings = await _load_sibling_chapters(db, project_id, chapter, volume)
    prev_1 = siblings[-1] if siblings else None

    # ---------- ⓪ 章节计划（高优先级约束，新增） ----------
    # 章节标题单一事实来源 = outlines.title（编辑预览）或 active confirmed 版本计划标题（正文生成）；
    # 章节序号按项目内 chapter 顺序 1-based；生成时必须基于 confirmed 版本快照。
    chapter_number = await chapter_number_of(db, project_id, chapter)
    if confirmed_plan is not None:
        chapter_number = int(confirmed_plan.get("chapterNumber") or chapter_number)
        current_chapter_plan = {**confirmed_plan, "source": "confirmed"}
        chapter_plans_snapshot = (confirmed_version or {}).get("chapterPlansSnapshot") or {}
        following_chapter_plans = _following_plans_from_snapshot(
            chapter_plans_snapshot, str(chapter.id)
        )
        plan_source = "confirmed"
    else:
        current_chapter_plan, plan_source = await load_chapter_plan(
            db, project_id, chapter.id
        )
        following_chapter_plans = await load_following_plans(db, project_id, chapter)
    chapter_plan_payload = (
        {
            **current_chapter_plan,
            "source": plan_source,
            "chapterNumber": chapter_number,
        }
        if current_chapter_plan
        else None
    )

    # ---------- ① 项目写作规则与禁忌（长期） ----------
    writing_rules = await _load_writing_rules(db, project_id, project)

    # ---------- ② 当前卷目标（中期） ----------
    current_volume_outline = _join_text(
        [
            ("当前卷", volume.title if volume else None),
            ("卷目标", volume.summary if volume else None),
            ("卷细纲", volume.content if volume else None),
        ]
    )

    # ---------- ③ 当前章节/场景细纲（短期） ----------
    current_chapter_outline = _join_text(
        [
            ("本章标题", chapter.title),
            ("本章摘要", chapter.summary),
            ("本章细纲", chapter.content),
        ]
    )
    scenes = (
        await db.execute(
            select(Outline)
            .where(
                Outline.parent_id == chapter_id,
                Outline.outline_type == "scene",
            )
            .order_by(Outline.sort_order.asc())
            .limit(10)
        )
    ).scalars().all()
    scene_goal = _join_text(
        [
            (f"场景 {i + 1}：{s.title}", s.summary or s.content)
            for i, s in enumerate(scenes)
        ]
    )

    # ---------- ⑦ 最近章节摘要（短期） ----------
    recent_chapter_memories = await _load_recent_chapter_memories(
        db, project_id, chapter, volume
    )

    # ---------- ④ 已确认人物资料与动态状态（中期） ----------
    active_characters = await _load_active_characters(db, project_id)

    # ---------- ⑤ 当前时间、地点和势力约束（中期） ----------
    timeline_constraints = await _load_timeline_constraints(db, project_id)

    # ---------- ⑥ 应埋设/允许回收/禁止揭露的伏笔（中期） ----------
    active_foreshadows = await _load_active_foreshadows(db, project_id)

    # ---------- ⑧ 上章末尾原文（短期，800~1500 字） ----------
    previous_ending = ""
    if prev_1 is not None:
        tail = (await _latest_chapter_content(db, prev_1.id))[-PREV_TAIL_LENGTH:]
        previous_ending = (
            f"（前一章《{prev_1.title}》末尾 {len(tail)} 字）\n{tail}"
            if tail
            else ""
        )

    # ---------- ⑨ 用户本次要求 ----------
    user_instruction = (instruction or "").strip()

    # ---------- ⑩ 跨卷记忆（2026-11-16 第三档升级） ----------
    volume_summary = await _collect_volume_summary(db, chapter, volume)
    related_past = await _collect_related_past(db, project_id, chapter)

    writing_context = {
        "writingRules": writing_rules,
        "currentVolumeOutline": current_volume_outline,
        "currentChapterOutline": current_chapter_outline,
        "sceneGoal": scene_goal,
        "recentChapterMemories": recent_chapter_memories,
        "activeCharacters": active_characters,
        "timelineConstraints": timeline_constraints,
        "activeForeshadows": active_foreshadows,
        "previousEnding": previous_ending,
        "userInstruction": user_instruction,
        "currentChapterNumber": chapter_number,
        "currentChapterPlan": chapter_plan_payload,
        "followingChapterPlans": following_chapter_plans,
        "volumeSummary": volume_summary,
        "relatedPastEvents": related_past,
    }

    # 三层上下文策略标注（长期/中期/短期）
    memory_tiers = {
        "长期记忆": [
            f"项目规则/世界观/人物固定/核心谜案/已确认关系：{writing_rules[:1500]}".strip(),
        ],
        "中期记忆": [
            f"当前卷目标：{current_volume_outline[:1000]}".strip(),
            f"未回收伏笔：{len(active_foreshadows)} 条".strip(),
            f"当前人物状态：{len(active_characters)} 人".strip(),
            f"当前时间线：{len(timeline_constraints)} 条事件".strip(),
            f"卷内剧情脉络：{len(volume_summary)} 字".strip(),
            f"相关往事检索：{len(related_past)} 字".strip(),
        ],
        "短期记忆": [
            f"最近章节摘要：{len(recent_chapter_memories)} 章".strip(),
            f"上章末尾：{len(previous_ending)} 字".strip(),
            f"当前章细纲/场景目标：{current_chapter_outline[:800]}".strip(),
        ],
        "章节计划": [
            (
                f"已确认大纲 v{confirmed_version.get('version')} / 第 {chapter_number} 章"
                f"（{confirmed_version.get('status')}，{confirmed_version.get('confirmed_at')}）"
                if confirmed_version
                else f"第 {chapter_number} 章计划（{plan_source}）"
            )
            + f"：{((current_chapter_plan or {}).get('summary') or '')[:1000]}".strip(),
            f"后续 {len(following_chapter_plans)} 章作为禁止提前揭露约束".strip(),
        ],
    }
    writing_context["memoryTiers"] = memory_tiers
    writing_context["planSource"] = plan_source
    writing_context["confirmedVersion"] = confirmed_version
    return writing_context


def _render_plot_summary(recent_memories: list[dict]) -> tuple[str, str]:
    """从 recentChapterMemories 渲染剧情承接摘要文本，返回 (摘要, 来源说明)。

    优先最近一条已确认记忆（summary + 下章承接要求 + 已埋设伏笔 + 未解决冲突）；
    否则回退最近 2~3 章任意状态摘要。
    """
    if not recent_memories:
        return "", "无前章可引用"
    confirmed = [m for m in recent_memories if m.get("status") == "confirmed"]
    if confirmed:
        m = confirmed[-1]
        seg = m.get("summary") or ""
        sm = m.get("structuredMemory") or {}
        reqs = sm.get("nextChapterRequirements") or []
        threads = sm.get("openThreads") or []
        foreshadows = sm.get("foreshadowsAdded") or []
        if reqs:
            seg += "\n- 下章承接：" + "；".join(str(r) for r in reqs)
        if foreshadows:
            seg += "\n- 已埋设伏笔：" + "；".join(
                f.get("content", "") for f in foreshadows if isinstance(f, dict)
            )
        if threads:
            seg += "\n- 未解决冲突：" + "；".join(str(t) for t in threads)
        title = m.get("chapterTitle") or ""
        return seg[:MAX_SHORT_TERM_CHARS], f"已确认章节记忆：第 {title}"
    parts = []
    for m in recent_memories:
        summary = m.get("summary") or ""
        if summary:
            title = m.get("chapterTitle") or ""
            status_flag = "（待确认）" if m.get("status") != "confirmed" else ""
            parts.append(f"《{title}》{status_flag}：{summary}")
    if parts:
        return "\n".join(parts)[:MAX_SHORT_TERM_CHARS], "最近 2~3 章章节记忆摘要（未全部确认）"
    m = recent_memories[-1]
    title = m.get("chapterTitle") or ""
    return "", f"前章《{title}》暂无章节记忆（回退上章末尾原文）"


def _render_character_cards(active_characters: list[dict]) -> str:
    """从 activeCharacters 渲染人物卡文本（含当前状态/位置/时间线状态）。"""
    lines = []
    for c in active_characters:
        st = c.get("currentState") or {}
        status = (
            st.get("status")
            or c.get("status")
            or c.get("character_status")
            or ""
        )
        loc = st.get("location") or st.get("current_location") or c.get("currentLocation") or ""
        tls = st.get("current_timeline_status") or c.get("currentTimelineStatus") or ""
        bits = []
        if c.get("identity"):
            bits.append(f"身份：{c['identity']}")
        if c.get("personality"):
            bits.append(f"性格：{c['personality']}")
        if c.get("goals"):
            bits.append(f"目标：{c['goals']}")
        if c.get("conflict"):
            bits.append(f"冲突：{c['conflict']}")
        if status:
            bits.append(f"当前状态：{status}")
        if loc:
            bits.append(f"所在地：{loc}")
        if tls:
            bits.append(f"时间线状态：{tls}")
        lines.append(f"- {c.get('name', '')}：" + "；".join(bits))
    return "\n".join(lines)[:MAX_LONG_TERM_CHARS]


def _render_timeline_constraints(timeline_constraints: list[dict]) -> str:
    lines = []
    for ev in timeline_constraints:
        marker = "" if ev.get("confirmed") else "（待确认）"
        lines.append(
            f"- 时间线事件{marker}：{ev.get('title')}"
            + (
                f"（{ev.get('eventDateLabel') or ev.get('eventTime')}）"
                if (ev.get("eventDateLabel") or ev.get("eventTime"))
                else ""
            )
            + (f"：{ev.get('description')}" if ev.get("description") else "")
        )
    return "\n".join(lines)[:MAX_MEDIUM_TERM_CHARS]


def _render_foreshadow_constraints(active_foreshadows: list[dict]) -> str:
    lines = []
    for f in active_foreshadows:
        status = f.get("status") or ""
        if status == "abandoned":
            continue
        tag = {
            "planned": "【应埋设】",
            "planted": "【允许回收/禁止提前揭露】",
            "resolved": "【已回收】",
        }.get(status, f"【{status}】")
        lines.append(
            f"{tag}{f.get('content')}"
            + (f"（计划回收：{f.get('plannedPayoff')}）" if f.get("plannedPayoff") else "")
        )
    return "\n".join(lines)[:MAX_MEDIUM_TERM_CHARS]


async def build_continuation_context(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    *,
    max_length: int = 800,
    instruction: str = "",
    confirmed_version: dict | None = None,
    confirmed_plan: dict | None = None,
) -> dict:
    """续写/改写/润色路由的提示词组装外壳（统一调用 build_writing_context）。

    章节计划为高优先级约束：当前章节计划（序号/标题/摘要/必须发生事件/结束状态/
    禁止事项）置于 Prompt 最前方，优先级高于全局大纲、历史章节摘要与用户普通补充指令；
    后续章节计划仅用于构造 forbiddenReveals，不作为当前章应生成剧情。

    「已确认大纲版本化」：confirmed_version + confirmed_plan 同时传入时，
    Prompt 顶部渲染「已确认大纲 vN / 第 N 章《标题》」高优先级约束区，且
    snapshot 携带 outline_plan_version_id / chapter_plan_id / titleSnapshot 等追溯字段。

    返回 dict：parts、memory_tiers、summary_source、system_prompt / user_prompt、
    snapshot（含 writing_context 原样，供 generation_records.context_snapshot 落库）。
    """
    writing_context = await build_writing_context(
        db,
        project_id,
        chapter_id,
        instruction=instruction,
        confirmed_version=confirmed_version,
        confirmed_plan=confirmed_plan,
    )

    chapter_number = writing_context.get("currentChapterNumber") or 0
    current_chapter_plan = writing_context.get("currentChapterPlan") or None
    following_chapter_plans = writing_context.get("followingChapterPlans") or []
    chapter_plan_section = _render_chapter_plan_section(
        chapter_number,
        current_chapter_plan,
        following_chapter_plans,
        confirmed_version=confirmed_version,
    )

    plot_summary, summary_source = _render_plot_summary(
        writing_context.get("recentChapterMemories") or []
    )
    if not plot_summary:
        # 三级回退：前章末尾原文（previousEnding 已含）
        prev_end = (writing_context.get("previousEnding") or "").strip()
        if prev_end:
            plot_summary = prev_end[:MAX_SHORT_TERM_CHARS]
            summary_source = "前章末尾原文回退"

    character_cards = _render_character_cards(
        writing_context.get("activeCharacters") or []
    )
    location_faction_timeline = _render_timeline_constraints(
        writing_context.get("timelineConstraints") or []
    )
    foreshadow_constraints = _render_foreshadow_constraints(
        writing_context.get("activeForeshadows") or []
    )
    # 合并已确认章节记忆里的伏笔约束（文本聚合）
    legacy_constraints = "\n".join(
        await _collect_confirmed_foreshadows(db, project_id)
    )
    if legacy_constraints:
        foreshadow_constraints = (
            (foreshadow_constraints + "\n" + legacy_constraints)
            if foreshadow_constraints
            else legacy_constraints
        )

    user_instruction = (writing_context.get("userInstruction") or "").strip()

    chapter = (
        await db.execute(
            select(Outline).where(
                Outline.id == chapter_id, Outline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    chapter_title = chapter.title if chapter else ""

    if confirmed_version is not None:
        plan_basis_line = (
            f"本次续写的唯一事实来源为已确认大纲版本 v{confirmed_version.get('version')}"
            f"（确认时间：{confirmed_version.get('confirmed_at')}）下的第 {chapter_number} 章计划。"
            "最新草稿、前端传入的大纲文本、历史缓存均不得作为生成依据。"
        )
    else:
        plan_basis_line = ""
    system_prompt = (
        "你是资深网络小说续写助手。你将基于给定的章节计划（最高优先级）、作品写作规范、"
        "卷目标、剧情承接摘要、人物状态、伏笔约束与前一章末尾，续写出风格一致、"
        "承接自然的本章正文。\n"
        "要求：\n"
        "1. 【章节计划】是最高优先级约束：本章只写当前章节计划要求的剧情，"
        "严格遵守章节序号、章节标题、本章目标、必须发生事件与本章结束状态；\n"
        + (f"【依据唯一性】{plan_basis_line}\n" if plan_basis_line else "")
        + "2. 除当前章节计划外，全局大纲、历史章节摘要、后续章节计划与用户普通补充指令"
        "都不得覆盖或替代【章节计划】；后续章节内容只用于避免提前揭露，绝不作为本章剧情；\n"
        "3. 严格遵循【作品写作规范】与【伏笔约束】，不得违背已确认的人物状态与时间线；\n"
        "4. 承接【剧情承接摘要】与【前章末尾】的叙事节奏，不要重复已发生情节；\n"
        "5. 只输出续写正文，不要输出标题、大纲、章节计划或解释性文字。"
    )

    def _section(title: str, body: str) -> str:
        body = body.strip()
        return f"【{title}】\n{body or '（无）'}\n"

    # 章节计划置于最前（优先级最高，先于全局大纲/历史摘要/普通指令）
    user_prompt = (
        _section("当前章节计划（最高优先级约束）", chapter_plan_section)
        + _section("作品写作规范", writing_context.get("writingRules") or "")
        + _section("当前卷目标", writing_context.get("currentVolumeOutline") or "")
        + _section("本卷剧情脉络（前文章节目标回顾）", writing_context.get("volumeSummary") or "")
        + _section("相关往事（历史原文检索，标注出处）", writing_context.get("relatedPastEvents") or "")
        + _section("当前章节细纲", writing_context.get("currentChapterOutline") or "")
        + _section("当前场景目标", writing_context.get("sceneGoal") or "")
        + _section("剧情承接摘要", plot_summary)
        + _section("已确认人物状态", character_cards)
        + _section("地点/势力/时间线约束", location_faction_timeline)
        + _section("伏笔约束", foreshadow_constraints)
        + _section("前章末尾", writing_context.get("previousEnding") or "")
        + _section(
            "本次写作要求",
            (
                (user_instruction + "\n" if user_instruction else "")
                + f"请续写《{chapter_title}》本章正文（当前为第 {chapter_number} 章），"
                f"期望长度约 {max_length} 字。"
            ),
        )
    )

    parts = {
        "chapter_plan": chapter_plan_section,
        "writing_guide": writing_context.get("writingRules") or "",
        "volume_goal": writing_context.get("currentVolumeOutline") or "",
        "chapter_outline": writing_context.get("currentChapterOutline") or "",
        "scene_goal": writing_context.get("sceneGoal") or "",
        "plot_summary": plot_summary,
        "character_cards": character_cards,
        "location_faction_timeline": location_faction_timeline,
        "foreshadow_constraints": foreshadow_constraints,
        "prev_tail": writing_context.get("previousEnding") or "",
        "user_instruction": user_instruction,
    }
    memory_tiers = writing_context.get("memoryTiers") or {}

    snapshot = {
        "project_id": str(project_id),
        "chapter_id": str(chapter_id),
        "chapter_number": chapter_number,
        "chapter_title": chapter_title,
        "chapter_plan": current_chapter_plan,
        "following_chapter_plans": following_chapter_plans,
        "confirmed_plan_version": confirmed_version,
        "confirmed_chapter_plan": confirmed_plan,
        "plan_source": writing_context.get("planSource") or "",
        "writing_context": writing_context,
        "parts": parts,
        "memory_tiers": memory_tiers,
        "summary_source": summary_source,
        "max_length": max_length,
        "user_prompt": user_prompt,
        "system_prompt": system_prompt,
    }

    return {
        "parts": parts,
        "memory_tiers": memory_tiers,
        "summary_source": summary_source,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "snapshot": snapshot,
    }


async def build_confirmed_continuation_context(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    *,
    max_length: int = 800,
    instruction: str = "",
) -> dict:
    """正文生成的唯一事实来源入口：基于 active confirmed OutlinePlanVersion 组装上下文。

    - 无 confirmed 版本、或当前章节未绑定 confirmed ChapterPlan 时抛
      ValueError("no_confirmed_plan")，由生成路由转为显式错误，禁止降级。
    - 返回在 build_continuation_context 基础上附加追溯字段：
      outline_plan_version_id / chapter_plan_id / chapter_plan_snapshot / title_snapshot，
      供 generation_record 落库与预览页「依据：已确认大纲 vN / 第 N 章《标题》」展示。
    """
    from app.services.outline_plan_version_service import get_confirmed_chapter_plan

    version, plan = await get_confirmed_chapter_plan(db, project_id, chapter_id)
    if version is None or plan is None:
        raise ValueError("no_confirmed_plan")
    # ORM 对象不可按 dict 取值，统一转成轻量 dict（version 元数据 + 完整计划快照）
    try:
        chapter_plans_snapshot = json.loads(version.chapter_plans_snapshot or "{}")
    except Exception:  # noqa: BLE001
        chapter_plans_snapshot = {}
    version_dict = {
        "id": str(version.id),
        "version": version.version,
        "status": version.status,
        "confirmed_at": (
            version.confirmed_at.isoformat() if version.confirmed_at else None
        ),
        "confirmed_by": str(version.confirmed_by) if version.confirmed_by else None,
        "chapterPlansSnapshot": chapter_plans_snapshot,
    }
    ctx = await build_continuation_context(
        db,
        project_id,
        chapter_id,
        max_length=max_length,
        instruction=instruction,
        confirmed_version=version_dict,
        confirmed_plan=plan,
    )
    # 追溯字段：chapter_plan_id 取已确认快照中与该 chapterId 稳定绑定的计划 key
    chapter_plan_id = plan.get("chapterId") or str(chapter_id)
    ctx["outline_plan_version_id"] = version_dict["id"]
    ctx["chapter_plan_id"] = chapter_plan_id
    ctx["chapter_plan_snapshot"] = plan
    # 2026-11-16：批量生成的计划 title 字段为空、标题在 outlineTitle 里，取非空者
    ctx["title_snapshot"] = plan.get("title") or plan.get("outlineTitle") or ""
    ctx["snapshot"]["outline_plan_version_id"] = version_dict["id"]
    ctx["snapshot"]["chapter_plan_id"] = chapter_plan_id
    ctx["snapshot"]["chapter_plan_snapshot"] = plan
    ctx["snapshot"]["title_snapshot"] = ctx["title_snapshot"]
    return ctx


def _render_chapter_plan_section(
    chapter_number: int,
    current_chapter_plan: dict | None,
    following_chapter_plans: list[dict],
    *,
    confirmed_version: dict | None = None,
) -> str:
    """渲染「当前章节计划（最高优先级约束）」文本段。

    明确向模型传递：当前章节序号、当前章节标题、本章目标（摘要）、必须发生事件、
    本章结束状态、禁止事项（forbiddenReveals + 后续章节不可提前揭露/推进）、输出边界。

    阶段F「场景合同」扩展：sceneTime（本章场景时间）、allowedReveals（本章允许揭示）、
    expectedStateChanges（结构化预期状态变化：人物/地点/物品/秘密 变化前→变化后）。
    新字段缺失时（legacy 计划）不渲染对应段落，保持旧行为。

    confirmed_version 提供时，头部渲染「依据：已确认大纲 vN / 第 N 章《标题》」
    并附带版本状态与确认时间，使 Prompt 高优先级约束区与已确认版本绑定。
    """
    lines = []
    plan = current_chapter_plan or {}
    title = plan.get("title") or ""
    if confirmed_version is not None:
        lines.append(
            f"依据：已确认大纲 v{confirmed_version.get('version')}"
            f"（{confirmed_version.get('status')}，确认时间：{confirmed_version.get('confirmed_at')}）"
            f" / 第 {chapter_number} 章《{title}》"
        )
    lines.append(f"当前章节序号：第 {chapter_number} 章")
    if title:
        lines.append(f"当前章节标题：《{title}》")
    summary = plan.get("summary") or ""
    if summary:
        lines.append(f"本章目标/摘要：{summary}")
    scene_goals = plan.get("sceneGoals") or []
    if scene_goals:
        lines.append("本章场景目标：" + "；".join(str(g) for g in scene_goals))
    scene_time = plan.get("sceneTime")
    if isinstance(scene_time, dict) and scene_time.get("value"):
        st_type = {"exact": "精确时间", "range": "时间段", "relative": "相对时间"}.get(
            scene_time.get("type"), scene_time.get("type") or "未知类型"
        )
        lines.append(f"本章场景时间（{st_type}）：{scene_time.get('value')}")
    required_chars = plan.get("requiredCharacters") or []
    if required_chars:
        lines.append("本章必须出场人物：" + "；".join(str(c) for c in required_chars))
    required_locs = plan.get("requiredLocations") or []
    if required_locs:
        lines.append("本章必须出现地点：" + "；".join(str(l) for l in required_locs))
    required_events = plan.get("requiredEvents") or []
    if required_events:
        lines.append("本章必须发生事件：" + "；".join(str(e) for e in required_events))
    outcome = plan.get("expectedOutcome") or ""
    if outcome:
        lines.append(f"本章结束状态：{outcome}")
    allowed = plan.get("allowedReveals") or []
    if allowed:
        lines.append("本章允许揭示：" + "；".join(str(a) for a in allowed))
    expected = plan.get("expectedStateChanges")
    if isinstance(expected, dict):
        esc_lines = []
        for key, label in (
            ("characters", "人物"),
            ("locations", "地点"),
            ("items", "物品"),
            ("secrets", "秘密"),
        ):
            for item in expected.get(key) or []:
                if not isinstance(item, dict):
                    continue
                target = item.get("target") or ""
                after = item.get("after") or ""
                if not target or not after:
                    continue
                before = item.get("before") or ""
                if before:
                    esc_lines.append(f"{target}（{label}）：{before} → {after}")
                else:
                    esc_lines.append(f"{target}（{label}）：{after}")
        if esc_lines:
            lines.append("本章预期状态变化：" + "；".join(esc_lines))
    forbidden = plan.get("forbiddenReveals") or []
    if forbidden:
        lines.append("本章禁止事项：" + "；".join(str(f) for f in forbidden))

    # 后续章节仅用于构造禁止提前揭露（不作为本章剧情）
    following_bits = []
    for fp in following_chapter_plans:
        fp_no = fp.get("chapterNumber") or "?"
        fp_title = fp.get("title") or ""
        bits = [f"第 {fp_no} 章"]
        if fp_title:
            bits.append(f"《{fp_title}》")
        for ev in fp.get("requiredEvents") or []:
            bits.append(f"将发生：{ev}")
        if fp.get("expectedOutcome"):
            bits.append(f"结束于：{fp['expectedOutcome']}")
        for fr in fp.get("forbiddenReveals") or []:
            bits.append(f"另有隐藏：{fr}")
        if len(bits) > 1:
            following_bits.append("、".join(bits))
    if following_bits:
        lines.append(
            "禁止提前揭露/禁止推进到后续剧情（仅作约束，不得在本章发生）："
            + "；".join(following_bits)
        )

    if not lines:
        return "（无章节计划：自由创作）"
    lines.append(
        "输出边界：本章只写本计划对应剧情，必须发生事件逐条覆盖，"
        "不得写入后续章节内容，不得提前揭露禁止事项，正文以本章结束状态收束。"
    )
    return "\n".join(lines)
