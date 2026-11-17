# -*- coding: utf-8 -*-
"""文26 P4 一致性检查引擎（静态数据层）。

基于已落地的 characters / character_state_snapshots / character_relationships /
timeline_events / foreshadows / chapter_memories / generation_records 数据，
扫描并报告以下异常：

- dangling_refs     引用不存在的章节（外键悬空）或生成记录
- character_state   角色状态快照与当前资料冲突
- relationships     关系状态与章节区间矛盾
- foreshadows       伏笔 planted 长期未 resolve / 无回收计划
- appearance        角色出场章节矛盾 / 最后出现章节滞后 / 重复别名
- memory            章节记忆 / 生成记录引用悬空

章节实体统一指 outlines 表 outline_type='chapter' 的行（chapter 级外键指向 outlines.id）。
"""
import json
import uuid
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.character_relationship import CharacterRelationship
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.chapter_memory import ChapterMemory
from app.models.foreshadow import Foreshadow
from app.models.generation import GenerationRecord
from app.models.outline import Outline
from app.models.timeline_event import TimelineEvent
from app.schemas.consistency import DataConsistencyIssue, DataConsistencySummary

CONFIRMED_SNAPSHOT_STATUSES = ("confirmed",)


def _new_issue(
    dimension: str,
    issue_type: str,
    severity: str,
    location: str,
    message: str,
    suggestion: str,
    related_ids: Iterable | None = None,
) -> DataConsistencyIssue:
    return DataConsistencyIssue(
        id=str(uuid.uuid4()),
        dimension=dimension,
        type=issue_type,
        severity=severity,
        location=location,
        message=message,
        suggestion=suggestion,
        related_ids=[str(x) for x in (related_ids or [])],
    )


def _ch_ref(issue_type: str = "dangling_chapter_ref", severity: str = "error") -> str:
    return issue_type


def _build_summary(issues: list[DataConsistencyIssue]) -> DataConsistencySummary:
    by_type: dict[str, int] = {}
    errors = warnings = info = 0
    for it in issues:
        by_type[it.type] = by_type.get(it.type, 0) + 1
        if it.severity == "error":
            errors += 1
        elif it.severity == "warning":
            warnings += 1
        else:
            info += 1
    return DataConsistencySummary(
        total=len(issues),
        errors=errors,
        warnings=warnings,
        info=info,
        by_type=by_type,
    )


async def _load_chapter_index(
    db: AsyncSession, project_id: uuid.UUID
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, str]]:
    """返回 (chapter_id -> 排序序号, chapter_id -> 标题)。"""
    rows = (
        await db.execute(
            select(Outline)
            .where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            )
            .order_by(Outline.sort_order, Outline.created_at)
        )
    ).scalars().all()
    order: dict[uuid.UUID, int] = {}
    titles: dict[uuid.UUID, str] = {}
    for idx, row in enumerate(rows):
        order[row.id] = idx
        titles[row.id] = row.title or f"章节{idx + 1}"
    return order, titles


def _fmt_chapter(ref_id: uuid.UUID | None, titles: dict[uuid.UUID, str]) -> str:
    if ref_id is None:
        return "（未设置）"
    return f"{titles.get(ref_id, str(ref_id)[:8])}({str(ref_id)[:8]})"


async def _check_dangling_chapter_refs(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: set[uuid.UUID],
    chapter_titles: dict[uuid.UUID, str],
    issues: list[DataConsistencyIssue],
) -> None:
    """外键悬空：角色 / 伏笔 / 时间线 / 关系 / 快照 / 章节记忆引用不存在的章节。"""
    dim = "dangling_refs"

    chars = (await db.execute(
        select(Character).where(Character.project_id == project_id)
    )).scalars().all()
    for c in chars:
        loc = f"角色:{c.name}"
        for field, ref_id in (
            ("first_chapter_id", c.first_chapter_id),
            ("last_chapter_id", c.last_chapter_id),
        ):
            if ref_id is not None and uuid.UUID(str(ref_id)) not in chapter_ids:
                issues.append(_new_issue(
                    dim, "dangling_chapter_ref", "error", loc,
                    f"{loc} 的 {field} 引用了不存在的章节（outlines 中无此 id）",
                    f"请到人物库修正该角色的{field}，或删除该引用",
                    [str(c.id), str(ref_id)],
                ))

    fshs = (await db.execute(
        select(Foreshadow).where(Foreshadow.project_id == project_id)
    )).scalars().all()
    for f in fshs:
        loc = f"伏笔:{f.title or f.content[:24]}"
        for field, ref_id in (
            ("planted_chapter_id", f.planted_chapter_id),
            ("planned_payoff_chapter_id", f.planned_payoff_chapter_id),
            ("resolved_chapter_id", f.resolved_chapter_id),
            ("paid_off_chapter_id", f.paid_off_chapter_id),
        ):
            if ref_id is not None and uuid.UUID(str(ref_id)) not in chapter_ids:
                issues.append(_new_issue(
                    dim, "dangling_chapter_ref", "error", loc,
                    f"{loc} 的 {field} 引用了不存在的章节",
                    f"请到伏笔管理修正 {field}，或删除该引用",
                    [str(f.id), str(ref_id)],
                ))

    evts = (await db.execute(
        select(TimelineEvent).where(TimelineEvent.project_id == project_id)
    )).scalars().all()
    for e in evts:
        if e.chapter_id is not None and uuid.UUID(str(e.chapter_id)) not in chapter_ids:
            issues.append(_new_issue(
                dim, "dangling_chapter_ref", "error", f"时间线事件:{e.title}",
                f"时间线事件「{e.title}」的 chapter_id 引用了不存在的章节",
                "请到时间线修正该事件的所属章节",
                [str(e.id), str(e.chapter_id)],
            ))

    rels = (await db.execute(
        select(CharacterRelationship).where(CharacterRelationship.project_id == project_id)
    )).scalars().all()
    for r in rels:
        loc = f"关系:{r.relation_type}"
        for field, ref_id in (
            ("start_chapter_id", r.start_chapter_id),
            ("end_chapter_id", r.end_chapter_id),
        ):
            if ref_id is not None and uuid.UUID(str(ref_id)) not in chapter_ids:
                issues.append(_new_issue(
                    dim, "dangling_chapter_ref", "error", loc,
                    f"{loc} 的 {field} 引用了不存在的章节",
                    "请到关系页修正该关系的起止章节，或删除该引用",
                    [str(r.id), str(ref_id)],
                ))

    snaps = (await db.execute(
        select(CharacterStateSnapshot).where(
            CharacterStateSnapshot.character_id.in_(
                select(Character.id).where(Character.project_id == project_id)
            )
        )
    )).scalars().all()
    for s in snaps:
        if uuid.UUID(str(s.chapter_id)) not in chapter_ids:
            issues.append(_new_issue(
                dim, "dangling_chapter_ref", "error", f"角色状态快照:{str(s.id)[:8]}",
                "角色状态快照的 chapter_id 引用了不存在的章节",
                "请删除或修正该快照的所属章节",
                [str(s.id), str(s.character_id), str(s.chapter_id)],
            ))


async def _check_memory_refs(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: set[uuid.UUID],
    generation_ids: set[uuid.UUID],
    issues: list[DataConsistencyIssue],
) -> None:
    """章节记忆 / 生成记录引用悬空。"""
    dim = "memory"
    mems = (await db.execute(
        select(ChapterMemory).where(ChapterMemory.project_id == project_id)
    )).scalars().all()
    for m in mems:
        if uuid.UUID(str(m.chapter_id)) not in chapter_ids:
            issues.append(_new_issue(
                dim, "dangling_chapter_ref", "error", f"章节记忆:{str(m.id)[:8]}",
                "章节记忆的 chapter_id 引用了不存在的章节",
                "请删除或修正该章节记忆",
                [str(m.id), str(m.chapter_id)],
            ))
        if m.generation_record_id is not None and uuid.UUID(str(m.generation_record_id)) not in generation_ids:
            issues.append(_new_issue(
                dim, "dangling_generation_record", "warning", f"章节记忆:{str(m.id)[:8]}",
                "章节记忆的 generation_record_id 引用了不存在的生成记录",
                "请重新生成摘要或在数据库中清理该引用",
                [str(m.id), str(m.generation_record_id)],
            ))

    recs = (await db.execute(
        select(GenerationRecord).where(GenerationRecord.project_id == project_id)
    )).scalars().all()
    for r in recs:
        for field, ref_id in (
            ("volume_id", r.volume_id),
            ("chapter_id", r.chapter_id),
            ("scene_id", r.scene_id),
        ):
            if ref_id is not None and uuid.UUID(str(ref_id)) not in chapter_ids:
                issues.append(_new_issue(
                    dim, "dangling_outline_ref", "warning", f"生成记录:{str(r.id)[:8]}",
                    f"生成记录(action={r.action_type})的 {field} 引用了不存在的章节/卷/场景",
                    "请检查该生成记录对应的章节是否已删除",
                    [str(r.id), str(ref_id)],
                ))


async def _check_character_state(
    db: AsyncSession,
    project_id: uuid.UUID,
    issues: list[DataConsistencyIssue],
) -> None:
    """角色状态快照（confirmed）与角色当前资料冲突。"""
    dim = "character_state"
    chars = {
        c.id: c
        for c in (await db.execute(
            select(Character).where(Character.project_id == project_id)
        )).scalars().all()
    }
    if not chars:
        return
    snaps = (await db.execute(
        select(CharacterStateSnapshot).where(
            CharacterStateSnapshot.character_id.in_(list(chars.keys())),
            CharacterStateSnapshot.status.in_(CONFIRMED_SNAPSHOT_STATUSES),
        )
    )).scalars().all()
    for s in snaps:
        c = chars.get(s.character_id)
        if c is None:
            continue
        try:
            state = json.loads(s.state_json or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(state, dict):
            continue
        loc = f"角色:{c.name}"
        snap_status = state.get("status")
        if snap_status and c.status and snap_status != c.status:
            issues.append(_new_issue(
                dim, "character_state_conflict", "warning", loc,
                f"{loc} 状态快照为「{snap_status}」，与人物库当前状态「{c.status}」冲突",
                "请确认以快照为准还是人物库为准，更新其中一方",
                [str(c.id), str(s.id)],
            ))
        snap_loc = state.get("current_location")
        if snap_loc and c.current_location and snap_loc != c.current_location:
            issues.append(_new_issue(
                dim, "character_state_conflict", "warning", loc,
                f"{loc} 快照地点「{snap_loc}」与人物库当前地点「{c.current_location}」冲突",
                "请核对快照章节的时间线，更新人物库当前地点或修正快照",
                [str(c.id), str(s.id)],
            ))


async def _check_relationships(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: set[uuid.UUID],
    chapter_order: dict[uuid.UUID, int],
    chapter_titles: dict[uuid.UUID, str],
    issues: list[DataConsistencyIssue],
) -> None:
    """关系状态与章节区间矛盾。"""
    dim = "relationships"
    rels = (await db.execute(
        select(CharacterRelationship).where(CharacterRelationship.project_id == project_id)
    )).scalars().all()
    for r in rels:
        loc = f"关系:{r.relation_type}"
        start, end = r.start_chapter_id, r.end_chapter_id
        if (
            start is not None
            and end is not None
            and uuid.UUID(str(start)) in chapter_ids
            and uuid.UUID(str(end)) in chapter_ids
        ):
            so, eo = chapter_order[uuid.UUID(str(start))], chapter_order[uuid.UUID(str(end))]
            if so > eo:
                issues.append(_new_issue(
                    dim, "relationship_chapter_order", "warning", loc,
                    f"{loc} 的起始章节「{_fmt_chapter(start, chapter_titles)}」晚于结束章节「{_fmt_chapter(end, chapter_titles)}」",
                    "请调整关系的起止章节，使其符合时间顺序",
                    [str(r.id), str(start), str(end)],
                ))
            if so == eo:
                issues.append(_new_issue(
                    dim, "relationship_chapter_order", "info", loc,
                    f"{loc} 的起始与结束章节「{_fmt_chapter(start, chapter_titles)}」为同一章节，区间可能无效",
                    "请确认该关系是否在同一章节内起止，或删除结束章节",
                    [str(r.id), str(start), str(end)],
                ))
        if r.status == "archived" and end is None:
            issues.append(_new_issue(
                dim, "relationship_archived_no_end", "warning", loc,
                f"{loc} 已归档（archived）但未设置结束章节",
                "请在关系页补充 end_chapter_id 或改为 confirmed 状态",
                [str(r.id)],
            ))


async def _check_foreshadows(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: set[uuid.UUID],
    chapter_order: dict[uuid.UUID, int],
    chapter_titles: dict[uuid.UUID, str],
    issues: list[DataConsistencyIssue],
) -> None:
    """伏笔：planted 长期未 resolve / 无回收计划。"""
    dim = "foreshadows"
    # 已有正文处理的章节（存在章节记忆即视为该章已写/已处理）
    written = {
        m.chapter_id
        for m in (await db.execute(
            select(ChapterMemory).where(ChapterMemory.project_id == project_id)
        )).scalars().all()
    }
    fshs = (await db.execute(
        select(Foreshadow).where(Foreshadow.project_id == project_id)
    )).scalars().all()
    for f in fshs:
        loc = f"伏笔:{f.title or f.content[:24]}"
        if f.status == "planted":
            payoff_id = f.planned_payoff_chapter_id
            if payoff_id is None and not (f.planned_payoff or "").strip():
                issues.append(_new_issue(
                    dim, "foreshadow_no_payoff", "warning", loc,
                    f"{loc} 已 planted 但未设置计划回收章节/回收方式",
                    "请在伏笔管理补充 planned_payoff_chapter_id 或 planned_payoff",
                    [str(f.id)],
                ))
            elif (
                payoff_id is not None
                and uuid.UUID(str(payoff_id)) in chapter_ids
                and uuid.UUID(str(payoff_id)) in written
            ):
                issues.append(_new_issue(
                    dim, "foreshadow_overdue", "warning", loc,
                    f"{loc} 计划在「{_fmt_chapter(uuid.UUID(str(payoff_id)), chapter_titles)}」回收，该章节已写但伏笔仍未 resolve",
                    "请确认是否在该章完成回收，或在伏笔管理标记 resolved/abandoned",
                    [str(f.id), str(payoff_id)],
                ))
            elif f.planted_chapter_id is None:
                issues.append(_new_issue(
                    dim, "foreshadow_planted_no_chapter", "warning", loc,
                    f"{loc} 状态为 planted 但未设置埋设章节",
                    "请补充 planted_chapter_id 或在伏笔管理改回 planned",
                    [str(f.id)],
                ))
        if f.status == "resolved" and f.resolved_chapter_id is None:
            issues.append(_new_issue(
                dim, "foreshadow_resolved_no_chapter", "warning", loc,
                f"{loc} 已 resolve 但未填写回收章节",
                "请补充 resolved_chapter_id 或在伏笔管理修正状态",
                [str(f.id)],
            ))


async def _check_appearance(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: set[uuid.UUID],
    chapter_order: dict[uuid.UUID, int],
    chapter_titles: dict[uuid.UUID, str],
    issues: list[DataConsistencyIssue],
) -> None:
    """角色出场：章节区间矛盾 / 最后出现章节滞后 / 重复别名。"""
    dim = "appearance"
    chars = (await db.execute(
        select(Character).where(Character.project_id == project_id)
    )).scalars().all()

    # 1) first/last 顺序矛盾；last 滞后（以 confirmed 快照推断实际出现）
    snaps = (await db.execute(
        select(CharacterStateSnapshot).where(
            CharacterStateSnapshot.character_id.in_([c.id for c in chars]),
            CharacterStateSnapshot.status.in_(CONFIRMED_SNAPSHOT_STATUSES),
        )
    )).scalars().all()
    by_char: dict[uuid.UUID, list[uuid.UUID]] = {}
    for s in snaps:
        by_char.setdefault(s.character_id, []).append(uuid.UUID(str(s.chapter_id)))

    for c in chars:
        loc = f"角色:{c.name}"
        first, last = c.first_chapter_id, c.last_chapter_id
        if (
            first is not None
            and last is not None
            and uuid.UUID(str(first)) in chapter_order
            and uuid.UUID(str(last)) in chapter_order
            and chapter_order[uuid.UUID(str(first))] > chapter_order[uuid.UUID(str(last))]
        ):
            issues.append(_new_issue(
                dim, "character_chapter_order", "error", loc,
                f"{loc} 的首次出场章节晚于最后出场章节",
                "请到人物库修正 first_chapter_id / last_chapter_id",
                [str(c.id), str(first), str(last)],
            ))

        appear_chapters = [ch for ch in by_char.get(c.id, []) if ch in chapter_order]
        if not appear_chapters:
            continue
        latest = max(appear_chapters, key=lambda ch: chapter_order[ch])
        if last is None:
            issues.append(_new_issue(
                dim, "character_last_appearance_missing", "warning", loc,
                f"{loc} 存在已确认出场快照，但人物库未设置最后出场章节（last_chapter_id）",
                "请到人物库补充 last_chapter_id",
                [str(c.id), str(latest)],
            ))
        elif uuid.UUID(str(last)) not in chapter_order:
            continue  # 悬空已由 dangling_refs 报告
        elif chapter_order[uuid.UUID(str(last))] < chapter_order[latest]:
            issues.append(_new_issue(
                dim, "character_last_appearance_stale", "warning", loc,
                f"{loc} 的 last_chapter_id 为「{_fmt_chapter(uuid.UUID(str(last)), chapter_titles)}」，但该角色在更晚章节「{_fmt_chapter(latest, chapter_titles)}」仍有已确认出场",
                "请更新 last_chapter_id 为最新出现章节",
                [str(c.id), str(last), str(latest)],
            ))

    # 2) 重复别名：跨角色别名冲突 / 别名与角色名冲突 / 单角色别名内部重复
    alias_map: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for c in chars:
        if not c.aliases:
            continue
        try:
            aliases = json.loads(c.aliases or "[]")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(aliases, list):
            continue
        seen_local: set[str] = set()
        for a in aliases:
            if not a or not str(a).strip():
                continue
            key = str(a).strip().lower()
            if key in seen_local:
                issues.append(_new_issue(
                    dim, "duplicate_alias", "info", f"角色:{c.name}",
                    f"{c.name} 的别名「{a}」在本人别名中重复",
                    "请在人物库清理重复别名",
                    [str(c.id)],
                ))
                continue
            seen_local.add(key)
            alias_map.setdefault(key, []).append((c.id, c.name))
        # 别名与本人名字相同（无效别名）
        name_key = c.name.strip().lower()
        if name_key in seen_local:
            issues.append(_new_issue(
                dim, "duplicate_alias", "info", f"角色:{c.name}",
                f"{c.name} 的别名与其姓名完全相同",
                "请在人物库删除该别名",
                [str(c.id)],
            ))
    for key, holders in alias_map.items():
        if len(holders) < 2:
            continue
        names = "、".join(dict.fromkeys(n for _, n in holders))
        issues.append(_new_issue(
            dim, "duplicate_alias", "warning", f"别名:{key}",
            f"别名「{key}」被多个角色使用：{names}",
            "请为其中一方调整别名或改名，避免检索混淆",
            [str(cid) for cid, _ in holders],
        ))


async def run_static_consistency_check(
    db: AsyncSession,
    project_id: uuid.UUID,
    dimension: str = "all",
) -> tuple[list[DataConsistencyIssue], DataConsistencySummary]:
    """执行静态一致性检查，返回 (issues, summary)。

    dimension 取值见 schemas.consistency.DATA_CONSISTENCY_DIMENSIONS。
    """
    if dimension not in (
        "all", "dangling_refs", "character_state",
        "relationships", "foreshadows", "appearance", "memory",
    ):
        dimension = "all"

    chapter_order, chapter_titles = await _load_chapter_index(db, project_id)
    chapter_ids = set(chapter_order.keys())

    gen_ids = {
        g.id for g in (await db.execute(
            select(GenerationRecord).where(GenerationRecord.project_id == project_id)
        )).scalars().all()
    }

    issues: list[DataConsistencyIssue] = []
    if dimension in ("all", "dangling_refs"):
        await _check_dangling_chapter_refs(
            db, project_id, chapter_ids, chapter_titles, issues
        )
    if dimension in ("all", "memory"):
        await _check_memory_refs(db, project_id, chapter_ids, gen_ids, issues)
    if dimension in ("all", "character_state"):
        await _check_character_state(db, project_id, issues)
    if dimension in ("all", "relationships"):
        await _check_relationships(db, project_id, chapter_ids, chapter_order, chapter_titles, issues)
    if dimension in ("all", "foreshadows"):
        await _check_foreshadows(db, project_id, chapter_ids, chapter_order, chapter_titles, issues)
    if dimension in ("all", "appearance"):
        await _check_appearance(db, project_id, chapter_ids, chapter_order, chapter_titles, issues)

    return issues, _build_summary(issues)
