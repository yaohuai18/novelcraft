# 事实数据库与规则校验接口（第一批「事实数据库与规则校验」第五节；
# 阶段A「事实权威与锁定」/ 阶段B「人工处置与误报闭环」补充写接口）
#
# 只读端点（2 个）：
#   POST /api/projects/{project_id}/chapters/{chapter_id}/validate
#       对项目/指定章节运行确定性规则校验，返回问题清单与校验摘要；
#       校验只读，不写库、不调 LLM、不阻断生成/保存。
#   GET  /api/projects/{project_id}/facts
#       返回项目事实视图（items / item_possessions / secret_knowers /
#       角色快照 / 时间线事件），供人工核查与前端展示；
#       结果按 project_id 严格隔离（快照表无 project_id 字段，
#       经该项目章节 ID 集合过滤，无章节时返回空集合）。
#
# 写端点（2 个，均要求 owner/editor）：
#   POST /api/projects/{project_id}/facts/lock
#       事实锁定/解锁（可同步更新 authority_level / confidence /
#       source_record_id），锁定后事实重算跳过该行。
#   POST /api/projects/{project_id}/issues/disposition
#       审稿问题人工处置（按 issue_key 幂等），accepted_exception
#       写入豁免白名单。
#
# 4 个端点均需 JWT 鉴权，并校验项目归属：只读端点允许作者或协作者，
# 写端点仅允许 owner/editor。
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.fact_issue_disposition import FactIssueDisposition
from app.models.item import Item
from app.models.item_possession import ItemPossession
from app.models.outline import Outline
from app.models.project import Project
from app.models.secret_knower import SecretKnower
from app.models.timeline_event import TimelineEvent
from app.models.user import User
from app.schemas.facts import (
    FactDispositionRequest,
    FactDispositionResponse,
    FactItemView,
    FactLockRequest,
    FactLockResponse,
    FactPossessionView,
    FactSecretKnowerView,
    FactSnapshotView,
    FactTimelineEventView,
    FactValidationResponse,
    FactViewResponse,
    to_fact_issue_model,
)
from app.services import collaboration_service
from app.services.fact_validator import build_fact_summary, run_fact_validation

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["事实数据库与规则校验"],
)


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


async def _ensure_project_readable(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Project:
    """校验项目存在且当前用户可读（作者或协作者），返回 Project。"""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )
    if project.user_id != current_user.id:
        role = await collaboration_service.get_role(
            db, project.id, current_user.id
        )
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该项目",
            )
    return project


@router.post(
    "/chapters/{chapter_id}/validate",
    response_model=FactValidationResponse,
)
async def validate_chapter_facts(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """对项目（向前包含到指定章节）运行确定性事实校验。

    - 校验为纯只读：不写库、不调 AI、不产生副作用；
    - 返回结构化问题清单（issues）与校验摘要（summary）；
    - summary.valid=False 表示存在 blocking 级确定性错误，仅提示不阻断。
    """
    await _ensure_project_readable(project_id, current_user, db)

    chapter = await db.get(Outline, chapter_id)
    if chapter is None or chapter.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在或不属于当前作品",
        )

    issues = await run_fact_validation(
        db, project_id, chapter_id=chapter_id
    )
    summary = build_fact_summary(issues)

    return FactValidationResponse(
        project_id=project_id,
        chapter_id=chapter_id,
        issues=[to_fact_issue_model(i.to_dict()) for i in issues],
        summary=summary,
    )


def _fact_filters(
    model,
    authority: str | None,
    locked: bool | None,
    confidence_min: float | None,
) -> list:
    """阶段A：按权威等级 / 锁定状态 / 置信度下限构造 SQL 过滤条件（空列表=不过滤）。"""
    conds = []
    if authority:
        conds.append(model.authority_level == authority)
    if locked is not None:
        conds.append(model.locked == locked)
    if confidence_min is not None:
        conds.append(model.confidence >= confidence_min)
    return conds


@router.get("/facts", response_model=FactViewResponse)
async def list_project_facts(
    project_id: uuid.UUID,
    authority: str | None = Query(
        default=None, description="low|normal|high，按权威等级过滤"
    ),
    locked: bool | None = Query(
        default=None, description="True/False，按人工锁定状态过滤"
    ),
    confidence_min: float | None = Query(
        default=None, ge=0, le=1, description="置信度下限 0-1"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回项目事实视图（只读）。包含物品/秘密、持有关系、知情关系、
    角色快照与时间线事件；支持按 authority/locked/confidence_min 过滤。

    项目隔离：character_state_snapshots 表没有 project_id 字段，只能通过
    「本项目章节 ID 集合」过滤；项目没有章节时快照必须为空结果，
    不得让空 IN 条件退化为全表查询（否则会跨项目泄漏）。
    """
    await _ensure_project_readable(project_id, current_user, db)

    items_res = await db.execute(
        select(Item)
        .where(
            Item.project_id == project_id,
            *_fact_filters(Item, authority, locked, confidence_min),
        )
        .order_by(Item.created_at)
    )
    items = list(items_res.scalars().all())
    items_view = [
        FactItemView(
            id=i.id,
            name=i.name,
            category=i.category,
            first_chapter_id=i.first_chapter_id,
            status=i.status,
            authority_level=i.authority_level,
            confidence=i.confidence,
            locked=i.locked,
            source_record_id=i.source_record_id,
        )
        for i in items
    ]

    poss_res = await db.execute(
        select(ItemPossession)
        .where(
            ItemPossession.item_id.in_([i.id for i in items]),
            *_fact_filters(ItemPossession, authority, locked, confidence_min),
        )
        .order_by(ItemPossession.created_at)
    )
    possessions_view = [
        FactPossessionView(
            id=p.id,
            item_id=p.item_id,
            owner_character_id=p.owner_character_id,
            since_chapter_id=p.since_chapter_id,
            until_chapter_id=p.until_chapter_id,
            status=p.status,
            authority_level=p.authority_level,
            confidence=p.confidence,
            locked=p.locked,
            source_record_id=p.source_record_id,
        )
        for p in poss_res.scalars().all()
    ]

    secret_ids = [i.id for i in items if i.category == "secret"]
    knowers: list[SecretKnower] = []
    if secret_ids:
        knower_res = await db.execute(
            select(SecretKnower)
            .where(
                SecretKnower.secret_id.in_(secret_ids),
                *_fact_filters(SecretKnower, authority, locked, confidence_min),
            )
            .order_by(SecretKnower.created_at)
        )
        knowers = list(knower_res.scalars().all())
    knowers_view = [
        FactSecretKnowerView(
            id=k.id,
            secret_id=k.secret_id,
            character_id=k.character_id,
            learned_chapter_id=k.learned_chapter_id,
            source=k.source,
            authority_level=k.authority_level,
            confidence=k.confidence,
            locked=k.locked,
            source_record_id=k.source_record_id,
        )
        for k in knowers
    ]

    # 项目隔离：快照表无 project_id，只能经本项目的章节 ID 集合过滤。
    # 先取章节集合；为空时直接返回空快照，绝不下发空 IN（空 IN 若被实现为
    # 无条件下发会退化成全表查询，造成跨项目数据泄漏）。
    chapter_ids_res = await db.execute(
        select(Outline.id).where(Outline.project_id == project_id)
    )
    chapter_ids = list(chapter_ids_res.scalars().all())

    snapshots_view: list[FactSnapshotView] = []
    if chapter_ids:
        snaps_res = await db.execute(
            select(CharacterStateSnapshot)
            .where(
                CharacterStateSnapshot.chapter_id.in_(chapter_ids),
                *_fact_filters(
                    CharacterStateSnapshot, authority, locked, confidence_min
                ),
            )
            .order_by(CharacterStateSnapshot.created_at.desc())
            .limit(500)
        )
        snapshots_view = [
            FactSnapshotView(
                id=s.id,
                character_id=s.character_id,
                chapter_id=s.chapter_id,
                age=s.age,
                birth_year=s.birth_year,
                is_alive=s.is_alive,
                location_id=s.location_id,
                possessions=_parse_json_list(s.possessions),
                known_secrets=_parse_json_list(s.known_secrets),
                injuries=_parse_json_list(s.injuries),
                status=s.status,
                authority_level=s.authority_level,
                confidence=s.confidence,
                locked=s.locked,
                source_record_id=s.source_record_id,
            )
            for s in snaps_res.scalars().all()
        ]

    events_res = await db.execute(
        select(TimelineEvent)
        .where(
            TimelineEvent.project_id == project_id,
            *_fact_filters(TimelineEvent, authority, locked, confidence_min),
        )
        .order_by(TimelineEvent.sequence_order.asc().nulls_last())
        .limit(500)
    )
    events_view = [
        FactTimelineEventView(
            id=e.id,
            chapter_id=e.chapter_id,
            event_date_label=e.event_date_label,
            sequence_order=e.sequence_order,
            absolute_time=e.absolute_time,
            relative_time=e.relative_time,
            location_id=e.location_id,
            participants=_parse_json_list(e.participants)
            if isinstance(e.participants, str)
            else (list(e.participants) if e.participants else []),
            status=e.status,
            authority_level=e.authority_level,
            confidence=e.confidence,
            locked=e.locked,
            source_record_id=e.source_record_id,
        )
        for e in events_res.scalars().all()
    ]

    return FactViewResponse(
        project_id=project_id,
        items=items_view,
        possessions=possessions_view,
        secret_knowers=knowers_view,
        snapshots=snapshots_view,
        timeline_events=events_view,
    )


# ---------- 阶段A「事实权威与锁定」：事实锁定写接口 ----------
_FACT_TYPE_MODELS = {
    "snapshot": CharacterStateSnapshot,
    "timeline_event": TimelineEvent,
    "item": Item,
    "possession": ItemPossession,
    "secret_knower": SecretKnower,
}
_FACT_TYPE_NAMES = tuple(_FACT_TYPE_MODELS.keys())
_AUTHORITY_LEVELS = ("low", "normal", "high")
_WRITE_ROLES = {"owner", "editor"}


async def _get_fact_for_lock(
    db: AsyncSession,
    project_id: uuid.UUID,
    fact_type: str,
    fact_id: uuid.UUID,
):
    """定位事实行并校验归属项目（不存在/不属于项目→HTTPException）。"""
    model = _FACT_TYPE_MODELS.get(fact_type)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 fact_type：{fact_type}",
        )
    obj = await db.get(model, fact_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="事实不存在",
        )
    if fact_type in ("item", "timeline_event"):
        ok = getattr(obj, "project_id", None) == project_id
    elif fact_type == "snapshot":
        ch = await db.get(Outline, obj.chapter_id)
        ok = ch is not None and ch.project_id == project_id
    else:  # possession / secret_knower
        ref_id = obj.item_id if fact_type == "possession" else obj.secret_id
        ref = await db.get(Item, ref_id)
        ok = ref is not None and ref.project_id == project_id
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="事实不存在或不属于当前项目",
        )
    return obj


@router.post(
    "/facts/lock",
    response_model=FactLockResponse,
)
async def lock_fact(
    project_id: uuid.UUID,
    payload: FactLockRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """阶段A：事实锁定/解锁写接口（owner/editor）。

    - locked=True 锁定：后续事实重算（trigger_state_facts）跳过该行，
      自动提取不会覆盖人工设定；
    - 可选同步更新 authority_level / confidence / source_record_id；
    - 对已锁定行重复锁定/解锁属幂等操作。
    """
    if payload.fact_type not in _FACT_TYPE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 fact_type：{payload.fact_type}",
        )
    if (
        payload.authority_level is not None
        and payload.authority_level not in _AUTHORITY_LEVELS
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="authority_level 仅支持 low|normal|high",
        )
    role = await collaboration_service.require_role(
        db, project_id, current_user.id, _WRITE_ROLES
    )
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权修改该项目事实",
        )

    obj = await _get_fact_for_lock(db, project_id, payload.fact_type, payload.fact_id)
    obj.locked = payload.locked
    if payload.authority_level is not None:
        obj.authority_level = payload.authority_level
    if payload.confidence is not None:
        obj.confidence = payload.confidence
    if payload.source_record_id is not None:
        obj.source_record_id = payload.source_record_id
    await db.commit()
    await db.refresh(obj)
    return FactLockResponse(
        fact_type=payload.fact_type,
        fact_id=obj.id,
        locked=bool(obj.locked),
        authority_level=obj.authority_level,
        confidence=obj.confidence,
        source_record_id=obj.source_record_id,
    )


# ---------- 阶段B「人工处置与误报闭环」（补充5）：处置写接口 ----------
_DISPOSITION_TYPES = (
    "confirmed",
    "false_positive",
    "accepted_exception",
    "fixed",
    "deferred",
    "wont_fix",
)


@router.post(
    "/issues/disposition",
    response_model=FactDispositionResponse,
)
async def set_issue_disposition(
    project_id: uuid.UUID,
    payload: FactDispositionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """人工审稿问题处置写接口（owner/editor）。

    - 按 issue_key（同项目下同规则同实体同消息的问题指纹）幂等更新处置状态；
    - accepted_exception 需携带触发条件（entity_type+entity_id / rule / 范围
      至少一项），写入豁免白名单：后续回归校验命中该问题则标记 exempted
      （不计入 blocking、不报错），仍记录为「已豁免」；
    - 豁免开关 settings.fact_exemption_enabled=False 时白名单不生效
      （仅记录处置状态，不豁免）。
    """
    if payload.disposition not in _DISPOSITION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="disposition 仅支持 confirmed|false_positive|accepted_exception|fixed|deferred|wont_fix",
        )
    if (
        payload.disposition == "accepted_exception"
        and not (payload.entity_type or payload.entity_id or payload.rule)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="accepted_exception 必须携带触发条件（entity_type/entity_id/rule 至少一项）",
        )
    role = await collaboration_service.require_role(
        db, project_id, current_user.id, _WRITE_ROLES
    )
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权处置该项目问题",
        )

    # 同项目同 issue_key 幂等更新（存在则覆盖处置与备注，保留触发条件）
    existing = (
        await db.execute(
            select(FactIssueDisposition).where(
                FactIssueDisposition.project_id == str(project_id),
                FactIssueDisposition.issue_key == payload.issue_key,
            )
        )
    ).scalar_one_or_none()

    now = datetime.utcnow()
    if existing is None:
        existing = FactIssueDisposition(
            project_id=str(project_id),
            issue_key=payload.issue_key,
            disposition=payload.disposition,
            note=payload.note,
            entity_type=payload.entity_type,
            entity_id=str(payload.entity_id) if payload.entity_id else None,
            rule=payload.rule,
            set_by=str(current_user.id),
            set_at=now,
        )
        db.add(existing)
    else:
        existing.disposition = payload.disposition
        existing.note = payload.note
        if payload.entity_type is not None:
            existing.entity_type = payload.entity_type
        if payload.entity_id is not None:
            existing.entity_id = str(payload.entity_id)
        if payload.rule is not None:
            existing.rule = payload.rule
        existing.set_by = str(current_user.id)
        existing.set_at = now
    await db.commit()
    await db.refresh(existing)

    exempted = (
        payload.disposition == "accepted_exception"
        and bool(getattr(settings, "fact_exemption_enabled", True))
    )
    return FactDispositionResponse(
        issue_key=existing.issue_key,
        disposition=existing.disposition,
        note=existing.note,
        entity_type=existing.entity_type,
        entity_id=uuid.UUID(existing.entity_id) if existing.entity_id else None,
        rule=existing.rule,
        set_by=existing.set_by,
        set_at=existing.set_at,
        exempted=exempted,
    )
