# 确定性规则校验器（第一批「事实数据库与规则校验」第四节）
# 纯确定性规则，不调用 LLM，对已落库的结构化事实做五类校验：
#   timeline    - timeline.age（年龄倒退）/ timeline.event_order（事件先后倒挂）/
#                 timeline.travel_time（行程时间存疑）
#   state       - state.death_reappear（死亡重现）/ state.teleport（瞬移）
#   possession  - possession.conflict（物品归属冲突）
#   secret      - secret.early_knowledge（秘密提前知情）
#   chapter_meta- chapter_meta.continuity（前后章节衔接）
# 注：contract.state_change / contract.forbidden_reveal（阶段F「场景合同」）不在
# B3-A 范围内——两条规则依赖 chapter_plan_service 的 allowedReveals /
# expectedStateChanges 字段扩展，随阶段F一并提交。
# 输出结构化问题清单（FactIssue），并生成 validation_summary 摘要（不阻断生成/保存）。
# 规则全部为确定性判断：数据缺失/未知时降级为 warning/info，绝不误报 blocking。
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.item import Item
from app.models.item_possession import ItemPossession
from app.models.location import Location
from app.models.outline import Outline
from app.models.secret_knower import SecretKnower
from app.models.timeline_event import TimelineEvent
from app.models.fact_issue_disposition import FactIssueDisposition

from app.core.config import settings

logger = logging.getLogger(__name__)

# 严重级别：blocking=确定性错误（堵住）；warning=存疑需人工确认；info=提示性
BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"

# 行程时间阈值（小时）：超过视为「跨距过大，可能缺过渡」，仅 warning
TRAVEL_HOURS_WARN = 24 * 7

# 章节衔接文本相似度阈值：低于此值认为上章落点与本章目标完全无关（仅 warning）
CONTINUITY_SIM_THRESHOLD = 0.15


@dataclass
class FactIssue:
    """一条校验问题。related_ids 携带关联实体（如人物/物品/秘密/事件 id）。

    阶段B「人工处置与误报闭环」扩展：
    - issue_key：问题指纹（同项目下同规则同实体同消息的确定性问题共用），
      供人工处置写接口定位；
    - disposition：人工处置状态（confirmed/false_positive/accepted_exception/
      fixed/deferred/wont_fix），来自 fact_issue_dispositions 表；
    - exempted：accepted_exception 豁免命中且豁免开关开启时置 True，
      不计入 blocking（不报错），仍保留在报告中作为「已豁免」记录。
    """
    dimension: str
    rule: str
    severity: str
    message: str
    chapter_id: str | None = None
    character_id: str | None = None
    item_id: str | None = None
    location_id: str | None = None
    suggestion: str | None = None
    related_ids: list[str] = field(default_factory=list)
    issue_key: str = ""
    disposition: str | None = None
    exempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "rule": self.rule,
            "severity": self.severity,
            "chapter_id": self.chapter_id,
            "character_id": self.character_id,
            "item_id": self.item_id,
            "location_id": self.location_id,
            "message": self.message,
            "suggestion": self.suggestion,
            "related_ids": self.related_ids,
            "issue_key": self.issue_key,
            "disposition": self.disposition,
            "exempted": self.exempted,
        }


def _issue_key(issue: FactIssue) -> str:
    """问题指纹：同项目下同一规则对同一实体同一条消息的确定性问题视为同一问题。

    参与指纹的字段与人工处置稳定相关（dimension/rule/severity/实体/消息），
    章节内部结构变化不影响指纹，保证人工处置跨回归稳定命中。
    """
    payload = "|".join([
        issue.dimension or "",
        issue.rule or "",
        issue.severity or "",
        str(issue.chapter_id or ""),
        str(issue.character_id or ""),
        str(issue.item_id or ""),
        str(issue.location_id or ""),
        issue.message or "",
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def build_fact_summary(issues: list[FactIssue]) -> dict[str, Any]:
    """生成 validation_summary：valid 仅由 blocking 决定（有 blocking 即不通过）。
    不阻断生成/保存，只做记录与提示。

    阶段B：accepted_exception 豁免命中的问题标记 exempted，不计入 blocking
    （不报错），仍记录为「已豁免」，随摘要展示给人工确认。
    """
    counts = {"blocking": 0, "warning": 0, "info": 0, "exempted": 0}
    dimensions: dict[str, dict[str, int]] = {}
    for issue in issues:
        if issue.exempted:
            counts["exempted"] += 1
            continue
        counts[issue.severity] += 1
        dim = dimensions.setdefault(
            issue.dimension, {"blocking": 0, "warning": 0, "info": 0}
        )
        dim[issue.severity] += 1
    return {
        "valid": counts["blocking"] == 0,
        "counts": counts,
        "dimensions": dimensions,
        "checked_at": datetime.utcnow().isoformat(),
    }


def _chapter_order_key(ch: Outline) -> int:
    return ch.sort_order or 0


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except Exception:  # noqa: BLE001
        pass
    return []


def _text_similarity(a: str, b: str) -> float:
    """极简字符 bigram Jaccard 相似度，用于章节衔接提示（确定性启发式）。

    中文没有天然分词：按空格 split 会把整句当成单个 token，交集恒为 0。
    这里改为字符二元组（bigram）计算重叠比例，可捕捉人名/短语级局部重合。
    """
    if not a or not b:
        return 0.0

    def _bigrams(s: str) -> set[str]:
        chars = [
            c for c in s
            if not c.isspace() and c not in "，。,.！？!?；;：:、\"'“”‘’"
        ]
        return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}

    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


async def run_fact_validation(
    db: AsyncSession,
    project_id: Any,
    chapter_id: Any = None,
    limit_issues: int = 200,
    exemptions_enabled: bool | None = None,
    cognition_enabled: bool | None = None,
) -> list[FactIssue]:
    """对项目（或指定章节）已落库的结构化事实做确定性校验，返回问题清单。

    规则可组合复用；任何单条规则内部异常都不阻断整体（降级为 info 提示）。

    阶段B「人工处置与误报闭环」：
    - 校验末尾加载本项目 fact_issue_dispositions 人工处置；
    - disposition 状态回填到问题（confirmed/false_positive/...），供前端展示；
    - accepted_exception 命中且豁免开关开启（exemptions_enabled 默认取
      settings.fact_exemption_enabled）时，问题标记 exempted=True：
      不计入 blocking（不报错），仍保留在报告中作为「已豁免」记录。

    阶段G「时间与认知系统」：
    - cognition_enabled 默认取 settings.fact_cognition_enabled（默认 True）；
    - 开启时启用认知类型细化：① secret.early_knowledge 对 belief/rumor/lie/
      uncertain 类快照不按客观知情判定（角色听说≠客观知晓，避免误报）；
      ② 新增 timeline.date_order 规则解析结构化时间（time_type 非 unknown
      且可解析的事件，range 取区间交集判断先后顺序）；
    - 关闭时两类细化完全不生效，belief 快照仍按客观知情判定、date_order 不
      执行，既有规则与旧行为完全保留。
    """
    if exemptions_enabled is None:
        exemptions_enabled = bool(getattr(settings, "fact_exemption_enabled", True))
    if cognition_enabled is None:
        cognition_enabled = bool(getattr(settings, "fact_cognition_enabled", True))
    issues: list[FactIssue] = []

    # 加载章节（按 sort_order 排序；outline_type=chapter）
    chapters_res = await db.execute(
        select(Outline)
        .where(Outline.project_id == project_id, Outline.outline_type == "chapter")
        .order_by(Outline.sort_order, Outline.created_at)
    )
    chapters = list(chapters_res.scalars().all())
    chapter_by_id = {str(ch.id): ch for ch in chapters}
    order_by_id = {
        str(ch.id): idx for idx, ch in enumerate(chapters)
    }

    if chapter_id is not None:
        target = str(chapter_id)
        if target not in chapter_by_id:
            return issues
        # 校验指定章节时，向前包含全部历史章节以支持跨章规则
        target_idx = order_by_id[target]
        chapters = chapters[: target_idx + 1]

    # 快照（含人物名，用于消息可读性）
    snaps_res = await db.execute(
        select(CharacterStateSnapshot)
        .where(CharacterStateSnapshot.chapter_id.in_([ch.id for ch in chapters]))
        .order_by(CharacterStateSnapshot.chapter_id)
    )
    snapshots = list(snaps_res.scalars().all())

    character_names: dict[str, str] = {}
    if snapshots:
        from app.models.character import Character
        cids = {s.character_id for s in snapshots}
        char_res = await db.execute(
            select(Character).where(Character.id.in_(cids))
        )
        character_names = {
            str(c.id): c.name for c in char_res.scalars().all()
        }

    def cname(cid: Any) -> str:
        return character_names.get(str(cid), str(cid)[:8])

    # ---- timeline.age：同一角色年龄倒退（确定性错误）----
    by_char: dict[str, list[CharacterStateSnapshot]] = {}
    for snap in snapshots:
        by_char.setdefault(str(snap.character_id), []).append(snap)
    for char_id, snaps in by_char.items():
        snaps.sort(key=lambda s: order_by_id.get(str(s.chapter_id), 0))
        prev_age: int | None = None
        prev_ch: str | None = None
        for snap in snaps:
            if snap.age is None:
                continue
            if prev_age is not None and snap.age < prev_age:
                issues.append(FactIssue(
                    dimension="timeline",
                    rule="timeline.age",
                    severity=BLOCKING,
                    chapter_id=str(snap.chapter_id),
                    character_id=char_id,
                    message=(
                        f"人物「{cname(char_id)}」年龄倒退：第 "
                        f"{order_by_id.get(str(snap.chapter_id), 0) + 1} 章年龄 {snap.age}，"
                        f"早于前章年龄 {prev_age}"
                    ),
                    suggestion="检查年龄是否误填；年龄只应随章节增长",
                    related_ids=[str(snap.id)],
                ))
            prev_age = snap.age
            prev_ch = str(snap.chapter_id)

    # ---- timeline.event_order：因果事件先后倒挂（确定性错误）----
    events_res = await db.execute(
        select(TimelineEvent)
        .where(TimelineEvent.chapter_id.in_([ch.id for ch in chapters]))
    )
    events = list(events_res.scalars().all())
    event_by_id = {str(e.id): e for e in events}
    for ev in events:
        if ev.cause_event_id is None:
            continue
        cause = event_by_id.get(str(ev.cause_event_id))
        if cause is None:
            continue
        self_order = order_by_id.get(str(ev.chapter_id), 0)
        cause_order = order_by_id.get(str(cause.chapter_id), 0)
        if cause_order > self_order:
            issues.append(FactIssue(
                dimension="timeline",
                rule="timeline.event_order",
                severity=BLOCKING,
                chapter_id=str(ev.chapter_id),
                message=(
                    f"事件「{ev.title or ev.event_date_label or str(ev.id)[:8]}」"
                    f"声明由更晚章节的事件引发（因果先后倒挂）"
                ),
                suggestion="修正 cause_event_id 或调整事件发生章节",
                related_ids=[str(ev.id), str(ev.cause_event_id)],
            ))

    # ---- timeline.date_order：结构化时间先后（阶段G，range 取区间交集判断）----
    # 仅当认知开关开启时生效；仅校验 time_type 非 unknown 且时间可解析的事件：
    #   - exact/relative/approximate：解析为单一时间刻度；
    #   - range：解析 time_start / time_end 为区间，区间整体晚于 effect 区间
    #     （cause_start > effect_end）判定倒挂；
    # 区间相交/嵌套无法判定先后时保守跳过（不误报），解析失败也跳过，
    # 保持旧行为由 timeline.event_order（章节序）兜底。
    # 严重度：exact/range/relative 倒挂为 blocking；涉及 approximate 为 warning。
    if cognition_enabled:
        for ev in events:
            if ev.cause_event_id is None:
                continue
            cause = event_by_id.get(str(ev.cause_event_id))
            if cause is None:
                continue
            c_bounds = _event_time_bounds(cause)
            e_bounds = _event_time_bounds(ev)
            if c_bounds is None or e_bounds is None:
                continue
            c_start, c_end, c_kind = c_bounds
            e_start, e_end, e_kind = e_bounds
            if c_start is None or e_start is None or e_end is None:
                continue
            if c_start > e_end:
                sev = (
                    BLOCKING
                    if c_kind != "approximate" and e_kind != "approximate"
                    else WARNING
                )
                issues.append(FactIssue(
                    dimension="timeline",
                    rule="timeline.date_order",
                    severity=sev,
                    chapter_id=str(ev.chapter_id),
                    message=(
                        f"事件「{ev.title or ev.event_date_label or str(ev.id)[:8]}」"
                        f"的结构化时间早于其引发事件"
                        f"「{cause.title or cause.event_date_label or str(cause.id)[:8]}」"
                        f"（原因事件时间更晚，先后倒挂）"
                    ),
                    suggestion="修正事件 time_start/time_end 或 cause_event_id",
                    related_ids=[str(ev.id), str(ev.cause_event_id)],
                ))

    # ---- timeline.travel_time / state.teleport：跨地点行程（warning）----
    loc_res = await db.execute(
        select(Location)
        .where(Location.project_id == project_id)
    )
    locations = {str(l.id): l for l in loc_res.scalars().all()}

    def travel_hours(a: str, b: str) -> int | None:
        la, lb = locations.get(a), locations.get(b)
        if not la or not lb:
            return None
        if la.parent_location_id and str(la.parent_location_id) == b:
            return la.travel_hours_from
        if lb.parent_location_id and str(lb.parent_location_id) == a:
            return lb.travel_hours_from
        return None

    for char_id, snaps in by_char.items():
        snaps.sort(key=lambda s: order_by_id.get(str(s.chapter_id), 0))
        for i in range(1, len(snaps)):
            prev, cur = snaps[i - 1], snaps[i]
            if not prev.location_id or not cur.location_id:
                continue
            if str(prev.location_id) == str(cur.location_id):
                continue
            prev_ch_idx = order_by_id.get(str(prev.chapter_id), 0)
            cur_ch_idx = order_by_id.get(str(cur.chapter_id), 0)
            adjacent = (cur_ch_idx - prev_ch_idx) <= 1
            loc_a = locations.get(str(prev.location_id))
            loc_b = locations.get(str(cur.location_id))
            name_a = loc_a.name if loc_a else str(prev.location_id)[:8]
            name_b = loc_b.name if loc_b else str(cur.location_id)[:8]
            hours = travel_hours(str(prev.location_id), str(cur.location_id))
            if hours is None:
                issues.append(FactIssue(
                    dimension="timeline" if not adjacent else "state",
                    rule="timeline.travel_time" if not adjacent else "state.teleport",
                    severity=WARNING,
                    chapter_id=str(cur.chapter_id),
                    character_id=char_id,
                    location_id=str(cur.location_id),
                    message=(
                        f"人物「{cname(char_id)}」从「{name_a}」到「{name_b}」"
                        f"（{'相邻章节' if adjacent else '跨章节'}）行程耗时未知，"
                        f"请确认是否合理"
                    ),
                    suggestion="在 locations 表补充 travel_hours_from，或补充过渡章节",
                    related_ids=[str(prev.location_id), str(cur.location_id)],
                ))
            elif hours > TRAVEL_HOURS_WARN:
                issues.append(FactIssue(
                    dimension="timeline",
                    rule="timeline.travel_time",
                    severity=WARNING,
                    chapter_id=str(cur.chapter_id),
                    character_id=char_id,
                    location_id=str(cur.location_id),
                    message=(
                        f"人物「{cname(char_id)}」从「{name_a}」到「{name_b}」"
                        f"行程需约 {hours} 小时，可能缺少过渡描写"
                    ),
                    suggestion="补充途中事件或缩短地理跨度",
                    related_ids=[str(prev.location_id), str(cur.location_id)],
                ))

    # ---- state.death_reappear：死亡重现（确定性错误）----
    for char_id, snaps in by_char.items():
        snaps.sort(key=lambda s: order_by_id.get(str(s.chapter_id), 0))
        dead_chapter: str | None = None
        for snap in snaps:
            if snap.is_alive is False and dead_chapter is None:
                dead_chapter = str(snap.chapter_id)
            if dead_chapter is not None and snap.is_alive is True:
                issues.append(FactIssue(
                    dimension="state",
                    rule="state.death_reappear",
                    severity=BLOCKING,
                    chapter_id=str(snap.chapter_id),
                    character_id=char_id,
                    message=(
                        f"人物「{cname(char_id)}」在第 "
                        f"{order_by_id.get(dead_chapter, 0) + 1} 章已标记死亡/离世，"
                        f"却在第 {order_by_id.get(str(snap.chapter_id), 0) + 1} 章复活"
                    ),
                    suggestion="检查生死标记；死亡后不应再出现在世快照",
                    related_ids=[dead_chapter, str(snap.chapter_id)],
                ))
                break

    # ---- possession.conflict：物品归属冲突 ----
    poss_res = await db.execute(
        select(ItemPossession)
        .where(ItemPossession.item_id.in_(
            select(Item.id).where(Item.project_id == project_id)
        ))
    )
    possessions = list(poss_res.scalars().all())
    item_ids = {p.item_id for p in possessions if p.item_id is not None}
    if item_ids:
        items_res = await db.execute(
            select(Item).where(Item.id.in_(item_ids))
        )
        items_by_id = {i.id: i for i in items_res.scalars().all()}
    else:
        items_by_id = {}
    # 1) 同物品同时两个 active 持有人（区间重叠）
    active = [p for p in possessions if p.status in ("active", None)]
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            if a.item_id != b.item_id:
                continue
            a_until = order_by_id.get(str(a.until_chapter_id), 10**9)
            b_until = order_by_id.get(str(b.until_chapter_id), 10**9)
            a_since = order_by_id.get(str(a.since_chapter_id), -1)
            b_since = order_by_id.get(str(b.since_chapter_id), -1)
            if a_since <= b_until and b_since <= a_until:
                name = items_by_id.get(a.item_id)
                iname = name.name if name else str(a.item_id)[:8]
                issues.append(FactIssue(
                    dimension="possession",
                    rule="possession.conflict",
                    severity=BLOCKING,
                    item_id=str(a.item_id),
                    message=(
                        f"物品「{iname}」在同一时间段同时由「{cname(a.owner_character_id)}」"
                        f"与「{cname(b.owner_character_id)}」持有"
                    ),
                    suggestion="修正 item_possessions 的 since/until 章节区间",
                    related_ids=[str(a.id), str(b.id)],
                ))
    # 2) 快照持有物品与事实库不一致（warning）
    item_name_to_id = {i.name: str(i.id) for i in items_by_id.values()}
    for snap in snapshots:
        held = _parse_json_list(snap.possessions)
        if not held:
            continue
        for name in held:
            item_id = item_name_to_id.get(name)
            if item_id is None:
                continue
            row = next(
                (
                    p for p in active
                    if str(p.item_id) == item_id
                    and str(p.owner_character_id) == str(snap.character_id)
                ),
                None,
            )
            if row is None:
                issues.append(FactIssue(
                    dimension="possession",
                    rule="possession.conflict",
                    severity=WARNING,
                    chapter_id=str(snap.chapter_id),
                    character_id=str(snap.character_id),
                    item_id=item_id,
                    message=(
                        f"快照显示人物「{cname(snap.character_id)}」持有「{name}」，"
                        f"但事实库无对应持有记录"
                    ),
                    suggestion="登记 item_possessions 或修正快照 possessions",
                    related_ids=[str(snap.id)],
                ))

    # ---- secret.early_knowledge：秘密提前知情（确定性错误）----
    secret_items_res = await db.execute(
        select(Item).where(
            Item.project_id == project_id, Item.category == "secret"
        )
    )
    secret_items = {i.name: i for i in secret_items_res.scalars().all()}
    knower_res = await db.execute(
        select(SecretKnower)
        .where(SecretKnower.secret_id.in_(
            [s.id for s in secret_items.values()]
        ))
    )
    knowers = list(knower_res.scalars().all())
    learned_map: dict[tuple[str, str], str | None] = {}
    for k in knowers:
        learned_map[(str(k.secret_id), str(k.character_id))] = (
            str(k.learned_chapter_id) if k.learned_chapter_id else None
        )
    for snap in snapshots:
        # 阶段G「时间与认知系统」：认知类型细化。
        # belief/rumor/lie/uncertain 类快照记录的是「角色相信/听说/被误导」，
        # 并非客观知情事实，不按客观知情判定秘密是否提前知晓（角色听说≠客观
        # 知晓，避免误报）；开关关闭或默认 fact_kind=fact 时完全保持旧行为。
        if (
            cognition_enabled
            and (getattr(snap, "fact_kind", "fact") or "fact") in _NON_FACT_KINDS
        ):
            continue
        secrets = _parse_json_list(snap.known_secrets)
        if not secrets:
            continue
        for name in secrets:
            item = secret_items.get(name)
            if item is None:
                continue
            learned = learned_map.get((str(item.id), str(snap.character_id)))
            snap_idx = order_by_id.get(str(snap.chapter_id), 0)
            if learned is None:
                issues.append(FactIssue(
                    dimension="secret",
                    rule="secret.early_knowledge",
                    severity=WARNING,
                    chapter_id=str(snap.chapter_id),
                    character_id=str(snap.character_id),
                    item_id=str(item.id),
                    message=(
                        f"人物「{cname(snap.character_id)}」知晓秘密「{name}」，"
                        f"但事实库无知情记录"
                    ),
                    suggestion="在 secret_knowers 登记知情章节",
                    related_ids=[str(snap.id), str(item.id)],
                ))
            else:
                learned_idx = order_by_id.get(learned, 0)
                if learned_idx > snap_idx:
                    issues.append(FactIssue(
                        dimension="secret",
                        rule="secret.early_knowledge",
                        severity=BLOCKING,
                        chapter_id=str(snap.chapter_id),
                        character_id=str(snap.character_id),
                        item_id=str(item.id),
                        message=(
                            f"人物「{cname(snap.character_id)}」在第 "
                            f"{snap_idx + 1} 章已知晓秘密「{name}」，"
                            f"但事实库记录其第 {learned_idx + 1} 章才知晓"
                        ),
                        suggestion="提前知情则调整 learned_chapter_id，否则修正快照",
                        related_ids=[str(snap.id), str(item.id), learned],
                    ))

    # ---- chapter_meta.continuity：前后章节衔接（warning/info）----
    for idx in range(1, len(chapters)):
        prev_ch, cur_ch = chapters[idx - 1], chapters[idx]
        prev_outcome = (prev_ch.chapter_outcome or "").strip()
        cur_goal = (cur_ch.chapter_goal or "").strip()
        if prev_outcome and cur_goal:
            sim = _text_similarity(prev_outcome, cur_goal)
            if sim < CONTINUITY_SIM_THRESHOLD:
                issues.append(FactIssue(
                    dimension="chapter_meta",
                    rule="chapter_meta.continuity",
                    severity=WARNING,
                    chapter_id=str(cur_ch.id),
                    message=(
                        f"上一章「{prev_ch.title}」落点与本章「{cur_ch.title}」目标"
                        f"文本几乎无关联，衔接可能断裂"
                    ),
                    suggestion="检查是否缺少过渡章节或章节目标描述",
                    related_ids=[str(prev_ch.id), str(cur_ch.id)],
                ))
        elif not prev_outcome and cur_goal:
            issues.append(FactIssue(
                dimension="chapter_meta",
                rule="chapter_meta.continuity",
                severity=INFO,
                chapter_id=str(cur_ch.id),
                message=(
                    f"上一章「{prev_ch.title}」未记录 chapter_outcome，无法校验衔接"
                ),
                suggestion="为上一章补充章节结局（chapter_outcome）",
                related_ids=[str(prev_ch.id), str(cur_ch.id)],
            ))

    # ---- 阶段B「人工处置与误报闭环」：豁免与处置回填 ----
    # 对已产生的全部问题先计算指纹并加载人工处置，再应用豁免状态
    if issues:
        await _load_dispositions(db, project_id, issues)
        _apply_disposition_state(issues, exemptions_enabled)

    # 规则内部异常不应阻断整体：此处不捕获，由调用方统一 try/except 兜底
    if limit_issues and len(issues) > limit_issues:
        issues = issues[:limit_issues]
    return issues


# 阶段G「时间与认知系统」：非客观事实的认知类型集合。
# belief（角色相信）/ rumor（传闻）/ lie（谎言）/ uncertain（叙事不确定）——
# 这类快照记录的是「角色认知」而非客观事实，secret.early_knowledge 不按客观
# 知情判定（角色听说≠客观知晓）；默认 fact_kind=fact 保持旧行为。
_NON_FACT_KINDS = {"belief", "rumor", "lie", "uncertain"}


def _parse_time_value(text: str | None) -> tuple[int, ...] | None:
    """将结构化时间文本解析为可比较刻度元组；无法解析返回 None。

    支持的写法（保守解析，解析失败不参与判定）：
      - 纯数字：当作年份，如 "1024" → (1024,)
      - ISO 日期：如 "1024-05-06" → (1024, 5, 6)
      - 中文日期：如 "1024年" / "1024年5月" / "1024年5月6日"
      - 相对章序：如 "第3章" → (2, 0)；"第3章后5天" → (2, 5)
      同制式内可比较；不同制式混比时按元组字典序（年份制式通常大于章序
      制式，若数据混用会被 date_order 提示，属于数据问题而非误报存量）。
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    # 纯数字：年份
    if re.fullmatch(r"\d{1,4}", s):
        return (int(s),)
    # ISO 日期 YYYY-MM-DD / YYYY-M-D
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return tuple(int(x) for x in m.groups())
    # 中文日期：YYYY年 / YYYY年M月 / YYYY年M月D日
    m = re.fullmatch(r"(\d{4})年(?:(\d{1,2})月(?:(\d{1,2})日?)?)?", s)
    if m:
        parts = [int(m.group(1))]
        if m.group(2):
            parts.append(int(m.group(2)))
        if m.group(3):
            parts.append(int(m.group(3)))
        return tuple(parts)
    # 相对章序：第N章 / 第N章后M天
    m = re.fullmatch(r"第(\d+)章(?:(?:后|之后)(\d+)(?:天|日))?", s)
    if m:
        idx = int(m.group(1)) - 1
        tail = int(m.group(2)) if m.group(2) else 0
        return (idx, tail)
    return None


def _event_time_bounds(ev: TimelineEvent) -> tuple[
    tuple[int, ...] | None, tuple[int, ...] | None, str
] | None:
    """解析事件结构化时间为可比较区间；无法解析返回 None。

    按 time_type 分支：
      - unknown：不参与（存量兜底，跳过）；
      - range：time_start / time_end 分别解析（缺省回退 absolute_time），
        返回 (start, end, "range")；
      - exact / approximate：解析 time_start → absolute_time → event_time
        得到单点，返回 (p, p, 类型)；
      - relative：解析 relative_time（如「第3章后5天」），返回 (p, p, "relative")。
    任一解析失败返回 None（保守跳过，由 timeline.event_order 章节序兜底）。
    """
    ttype = getattr(ev, "time_type", "unknown") or "unknown"
    if ttype == "unknown":
        return None
    if ttype == "range":
        start = _parse_time_value(ev.time_start) or _parse_time_value(ev.absolute_time)
        end = _parse_time_value(ev.time_end) or _parse_time_value(ev.absolute_time)
        if start is None or end is None:
            return None
        return (start, end, "range")
    if ttype == "relative":
        p = _parse_time_value(ev.relative_time)
        if p is None:
            return None
        return (p, p, "relative")
    if ttype in ("exact", "approximate"):
        p = (
            _parse_time_value(ev.time_start)
            or _parse_time_value(ev.absolute_time)
            or _parse_time_value(ev.event_time)
        )
        if p is None:
            return None
        return (p, p, ttype)
    return None


async def _load_dispositions(
    db: AsyncSession,
    project_id: Any,
    issues: list[FactIssue],
) -> None:
    """加载本项目全部人工处置，按 issue_key 回填 disposition。

    一次查询全部处置，供多问题批量命中；查询失败时降级为空映射
    （豁免不生效但校验不中断），保证旧库/旧测试兼容。
    """
    try:
        rows = (await db.execute(
            select(FactIssueDisposition).where(
                FactIssueDisposition.project_id == str(project_id)
            )
        )).scalars().all()
        by_key = {
            d.issue_key: d.disposition for d in rows
            if d.issue_key
        }
        for issue in issues:
            issue.issue_key = _issue_key(issue)
            issue.disposition = by_key.get(issue.issue_key)
    except Exception:  # noqa: BLE001
        logger.warning("加载人工处置失败，本次校验不应用豁免", exc_info=True)


def _apply_disposition_state(
    issues: list[FactIssue],
    exemptions_enabled: bool,
) -> None:
    """根据人工处置状态标记问题：accepted_exception 且开关开启 → exempted。

    exempted 问题不计入 blocking（不报错），仍保留在报告中作为「已豁免」记录；
    开关关闭时豁免白名单不生效（仅标注处置状态，不豁免）。
    """
    for issue in issues:
        if (
            exemptions_enabled
            and issue.disposition == "accepted_exception"
        ):
            issue.exempted = True
