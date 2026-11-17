# 发布基线服务（阶段C「发布基线绑定」，补充 6 实现）
# 职责：发布/导出时收集当前不可变版本基线（baseline_meta）与
# 事实库 + 设定引用式快照（baseline_snapshot，仅实体 id 引用，不复制正文）。
# 设计约束（方案文档）：
#   - 字段可空：旧发布记录 baseline_meta=NULL 不追溯；
#   - 生成失败降级 NULL 不阻断发布（调用方 try/except 包裹）；
#   - 不重写旧正文、不触碰已批准章节与人工设定。
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_task import AITask
from app.models.character import Character
from app.models.chapter_content import ChapterContent
from app.models.item import Item
from app.models.item_possession import ItemPossession
from app.models.location import Location
from app.models.outline import Outline
from app.models.secret_knower import SecretKnower
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.world_setting import WorldSetting

# 校验规则版本（阶段C 基线元数据 rules_version 来源）：
# 引用式版本标识，rules_version = 该项目 world_rules 行数；
# 后续如需语义化规则版本号，可在此定义常量并同步递增。
RULES_VERSION = "1.0"

# 基线快照引用的实体类型（事实库 + 设定，不复制正文）
# 事实实体（entities 列表）
_FACT_ENTITY_TABLES = (
    ("character", Character),
    ("timeline_event", TimelineEvent),
    ("location", Location),
    ("item", Item),
)
# 设定表（settings 字典）
_SETTING_TABLES = (
    ("world_settings", WorldSetting),
    ("world_rules", WorldRule),
)
# 关联表（relations 字典；无 project_id 列，按 join Item.project_id 过滤）
_RELATION_TABLES = (
    ("secret_knowers", SecretKnower, SecretKnower.secret_id),
    ("item_possessions", ItemPossession, ItemPossession.item_id),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


async def collect_facts_version(
    db: AsyncSession, project_id, outline_id=None
) -> int | None:
    """收集当前事实库版本号（facts_version）。
    优先取该章 outline.facts_version，其次 chapter_content.facts_version。
    """
    if outline_id is not None:
        outline = await db.get(Outline, outline_id)
        if outline is not None and outline.facts_version is not None:
            return outline.facts_version
        cc_result = await db.execute(
            select(ChapterContent).where(
                ChapterContent.outline_id == outline_id
            )
        )
        cc = cc_result.scalar_one_or_none()
        if cc is not None and cc.facts_version is not None:
            return cc.facts_version
    result = await db.execute(
        select(func.max(Outline.facts_version)).where(
            Outline.project_id == project_id
        )
    )
    return result.scalar()


async def collect_setting_version(db: AsyncSession, project_id) -> int | None:
    """收集设定版本（引用式标识）：world_settings 行数（该项目设定条目数）。"""
    ws_count = (
        await db.execute(
            select(func.count(WorldSetting.id)).where(
                WorldSetting.project_id == project_id
            )
        )
    ).scalar()
    return ws_count


async def collect_rules_version(db: AsyncSession, project_id) -> int | None:
    """收集规则版本（引用式标识）：world_rules 行数（该项目设定规则条目数）。"""
    wr_count = (
        await db.execute(
            select(func.count(WorldRule.id)).where(
                WorldRule.project_id == project_id
            )
        )
    ).scalar()
    return wr_count


async def collect_generation_baseline(db: AsyncSession, project_id) -> tuple:
    """收集最近一次该作品生成任务的 model_name / prompt_version。
    返回 (model_name, prompt_version)；无任务记录时返回 (None, None)。
    """
    result = await db.execute(
        select(AITask.model_name, AITask.prompt_version)
        .where(AITask.project_id == project_id)
        .where(
            (AITask.model_name.isnot(None))
            | (AITask.prompt_version.isnot(None))
        )
        .order_by(AITask.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None, None
    return row[0], row[1]


async def collect_validation_report_id(
    db: AsyncSession, outline_id=None
) -> str | None:
    """收集该校验报告标识（引用式）：基于 outline.validation_report 中的
    validated_at 派生，无校验记录时返回 None（不阻断发布）。
    """
    if outline_id is None:
        return None
    outline = await db.get(Outline, outline_id)
    if outline is None:
        return None
    report = _load_json(outline.validation_report)
    validated_at = report.get("validated_at")
    if not validated_at:
        return None
    return f"report:{outline_id}:{validated_at}"


async def collect_baseline_meta(
    db: AsyncSession, project_id, outline_id=None
) -> dict:
    """收集完整基线元数据（6 个版本字段）。
    任一项收集失败时该项为 None；整体失败由调用方降级为 NULL。
    """
    meta: dict = {}
    meta["facts_version"] = await collect_facts_version(
        db, project_id, outline_id
    )
    try:
        meta["setting_version"] = await collect_setting_version(db, project_id)
    except Exception:  # noqa: BLE001
        meta["setting_version"] = None
    try:
        meta["rules_version"] = await collect_rules_version(db, project_id)
    except Exception:  # noqa: BLE001
        meta["rules_version"] = None
    try:
        model_name, prompt_version = await collect_generation_baseline(
            db, project_id
        )
        meta["prompt_version"] = prompt_version
        meta["model_name"] = model_name
    except Exception:  # noqa: BLE001
        meta["prompt_version"] = None
        meta["model_name"] = None
    try:
        meta["validation_report_id"] = await collect_validation_report_id(
            db, outline_id
        )
    except Exception:  # noqa: BLE001
        meta["validation_report_id"] = None
    return meta


async def collect_baseline_snapshot(
    db: AsyncSession, project_id, outline_id=None
) -> dict:
    """收集事实库 + 设定不可变快照（引用式：仅记录实体 id 列表与捕获时间，
    不复制正文/事实正文）。返回结构：
    {
      "type": "reference",
      "captured_at": "...",
      "facts_version": <int|None>,
      "entities": [{"entity_type": ..., "ids": [...]}, ...],
      "settings": {"world_settings": [...], "world_rules": [...]},
      "relations": {"secret_knowers": [...], "item_possessions": [...]},
    }
    """
    entities: list = []
    for key, model in _FACT_ENTITY_TABLES:
        result = await db.execute(
            select(model.id).where(model.project_id == project_id)
        )
        entities.append(
            {"entity_type": key, "ids": [str(row) for row in result.scalars().all()]}
        )
    settings: dict = {}
    for key, model in _SETTING_TABLES:
        result = await db.execute(
            select(model.id).where(model.project_id == project_id)
        )
        settings[key] = [str(row) for row in result.scalars().all()]
    relations: dict = {}
    for key, model, fk_column in _RELATION_TABLES:
        query = select(model.id).join(Item, Item.id == fk_column)
        result = await db.execute(query)
        relations[key] = [str(row) for row in result.scalars().all()]
    return {
        "type": "reference",
        "captured_at": _now_iso(),
        "facts_version": await collect_facts_version(
            db, project_id, outline_id
        ),
        "entities": entities,
        "settings": settings,
        "relations": relations,
    }


async def collect_baseline(
    db: AsyncSession, project_id, outline_id=None
) -> dict:
    """组合收集基线（meta + snapshot）。
    调用方应 try/except 包裹：任何失败整体降级为 NULL，不阻断发布。
    """
    return {
        "meta": await collect_baseline_meta(db, project_id, outline_id),
        "snapshot": await collect_baseline_snapshot(db, project_id, outline_id),
    }
