# 大纲计划版本（OutlinePlanVersion）服务
# 「已确认大纲版本化」核心逻辑：
#   - 快照：把项目完整目录（summary/volume/chapter/scene 树）+ 每章结构化章节计划
#     归一化为 JSON 快照（章节计划解析复用 chapter_plan_service 的 manual/legacy 规则）；
#   - 草稿：AI 生成或人工编辑目录大纲后调用 sync_draft_version 产生 draft 版本；
#   - 确认：把当时快照持久化为 status=confirmed（记录 confirmedAt/confirmedBy），
#     旧 confirmed 置 superseded（不删除），保证同一项目唯一 active confirmed；
#   - 生成唯一事实来源：get_confirmed_chapter_plan 按 chapterId 在 active confirmed
#     快照中稳定查找绑定 ChapterPlan，禁止按下标/可变序号/标题模糊匹配。
# 兼容策略：旧 outlines.chapter_plan 数据在首次快照时被归一化吸收；
# 历史项目无 confirmed 时，生成接口按需求返回拒绝，由前端引导用户确认大纲。
import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.outline_plan_version import OutlinePlanVersion
from app.models.project import Project
from app.services.chapter_plan_service import (
    build_legacy_plan,
    chapter_number_of,
    parse_chapter_plan,
)

STATUS_DRAFT = "draft"
STATUS_CONFIRMED = "confirmed"
STATUS_SUPERSEDED = "superseded"


class OutlinePlanError(Exception):
    """版本化业务错误（路由层转 HTTPException）。"""


class NoConfirmedVersionError(OutlinePlanError):
    """项目尚无 active confirmed 大纲版本。"""


class ChapterNotBoundError(OutlinePlanError):
    """当前章节未绑定到 active confirmed 版本。"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(content: dict, plans: dict) -> str:
    raw = (
        json.dumps(content, ensure_ascii=False, sort_keys=True)
        + "||"
        + json.dumps(plans, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


async def _load_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise OutlinePlanError("project_not_found")
    return project


async def _load_tree(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """读取 project 全部大纲树（volume/chapter/scene），按 sort_order 排序。"""
    rows = (
        await db.execute(
            select(Outline)
            .where(Outline.project_id == project_id)
            .order_by(Outline.outline_type.asc(), Outline.sort_order.asc(), Outline.created_at.asc())
            .limit(2000)
        )
    ).scalars().all()
    return list(rows)


def _plan_payload(plan: dict | None, source: str) -> dict:
    plan = plan or {}
    return {
        "title": plan.get("title") or "",
        "summary": plan.get("summary") or "",
        "sceneGoals": plan.get("sceneGoals") or [],
        "requiredCharacters": plan.get("requiredCharacters") or [],
        "requiredLocations": plan.get("requiredLocations") or [],
        "requiredEvents": plan.get("requiredEvents") or [],
        "expectedOutcome": plan.get("expectedOutcome") or "",
        "forbiddenReveals": plan.get("forbiddenReveals") or [],
        "status": plan.get("status") or source,
        "planSource": source,
        # 阶段F「场景合同」：快照载荷纳入新字段，旧数据缺失时保持 None/[]
        "sceneTime": plan.get("sceneTime") or None,
        "allowedReveals": plan.get("allowedReveals") or [],
        "expectedStateChanges": plan.get("expectedStateChanges") or None,
    }


async def snapshot_project(
    db: AsyncSession, project_id: uuid.UUID
) -> dict:
    """构建项目当前目录 + 章节计划快照（确认/草稿的物理内容）。"""
    project = await _load_project(db, project_id)
    rows = await _load_tree(db, project_id)

    summaries = [r for r in rows if r.outline_type == "summary"]
    volumes = [r for r in rows if r.outline_type == "volume"]
    chapters = [r for r in rows if r.outline_type == "chapter"]
    scenes = [r for r in rows if r.outline_type == "scene"]

    content: dict = {
        "projectId": str(project.id),
        "projectTitle": project.title or "",
        "genre": project.genre or "",
        "summary": (
            summaries[0].summary if summaries else ""
        ),
        "summaryContent": (
            summaries[0].content if summaries else ""
        ),
        "volumes": [],
        "rootChapters": [],
    }

    def _node_minimal(r: Outline) -> dict:
        return {
            "id": str(r.id),
            "title": r.title or "",
            "summary": r.summary or "",
            "content": r.content or "",
            "sortOrder": int(r.sort_order or 0),
        }

    chapters_by_parent: dict[str, list[Outline]] = {}
    for c in chapters:
        key = str(c.parent_id) if c.parent_id else "__root__"
        chapters_by_parent.setdefault(key, []).append(c)

    scenes_by_parent: dict[str, list[Outline]] = {}
    for s in scenes:
        scenes_by_parent.setdefault(str(s.parent_id), []).append(s)

    def _chapter_payload(c: Outline) -> dict:
        payload = _node_minimal(c)
        payload["outlineType"] = "chapter"
        payload["parentId"] = str(c.parent_id) if c.parent_id else None
        payload["scenes"] = [
            _node_minimal(s) for s in scenes_by_parent.get(str(c.id), [])
        ]
        return payload

    for vol in volumes:
        vol_payload = _node_minimal(vol)
        vol_payload["outlineType"] = "volume"
        vol_payload["parentId"] = str(vol.parent_id) if vol.parent_id else None
        vol_payload["chapters"] = [
            _chapter_payload(c) for c in chapters_by_parent.get(str(vol.id), [])
        ]
        content["volumes"].append(vol_payload)
    content["rootChapters"] = [
        _chapter_payload(c) for c in chapters_by_parent.get("__root__", [])
    ]

    # 章节计划快照：按 chapterId 为 key 的稳定映射（生成时唯一事实来源）
    plans: dict[str, dict] = {}
    for c in chapters:
        plan, source = await _load_chapter_plan_for_snapshot(db, project_id, c)
        number = await chapter_number_of(db, project_id, c)
        plans[str(c.id)] = {
            "chapterId": str(c.id),
            "chapterNumber": number,
            "outlineTitle": c.title or "",
            **_plan_payload(plan, source),
        }
    return {"content_snapshot": content, "chapter_plans_snapshot": plans}


async def _load_chapter_plan_for_snapshot(
    db: AsyncSession, project_id: uuid.UUID, chapter: Outline
) -> tuple[dict | None, str]:
    """读取单章计划（manual/legacy 归一），供快照使用。"""
    parsed = parse_chapter_plan(chapter.chapter_plan)
    if parsed is not None:
        return parsed, "manual"
    if chapter.title or (chapter.summary and str(chapter.summary).strip()) or (
        chapter.content and str(chapter.content).strip()
    ):
        return build_legacy_plan(chapter), "legacy"
    return None, "none"


def _version_summary(v: OutlinePlanVersion) -> dict:
    try:
        content = json.loads(v.content_snapshot or "{}")
    except Exception:  # noqa: BLE001
        content = {}
    try:
        plans = json.loads(v.chapter_plans_snapshot or "{}")
    except Exception:  # noqa: BLE001
        plans = {}
    return {
        "id": str(v.id),
        "project_id": str(v.project_id),
        "version": v.version,
        "status": v.status,
        "confirmed_at": v.confirmed_at.isoformat() if v.confirmed_at else None,
        "confirmed_by": str(v.confirmed_by) if v.confirmed_by else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
        "content_summary": {
            "projectTitle": content.get("projectTitle") or "",
            "volumeCount": len(content.get("volumes") or []),
            "chapterCount": len(plans or {}),
        },
    }


async def _max_version(db: AsyncSession, project_id: uuid.UUID) -> int:
    rows = (
        await db.execute(
            select(OutlinePlanVersion.version)
            .where(OutlinePlanVersion.project_id == project_id)
            .order_by(OutlinePlanVersion.version.desc())
            .limit(1)
        )
    ).scalars().first()
    return int(rows or 0)


async def get_active_confirmed_version(
    db: AsyncSession, project_id: uuid.UUID
) -> OutlinePlanVersion | None:
    return (
        await db.execute(
            select(OutlinePlanVersion)
            .where(
                OutlinePlanVersion.project_id == project_id,
                OutlinePlanVersion.status == STATUS_CONFIRMED,
            )
            .order_by(OutlinePlanVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_latest_draft_version(
    db: AsyncSession, project_id: uuid.UUID
) -> OutlinePlanVersion | None:
    return (
        await db.execute(
            select(OutlinePlanVersion)
            .where(
                OutlinePlanVersion.project_id == project_id,
                OutlinePlanVersion.status == STATUS_DRAFT,
            )
            .order_by(OutlinePlanVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_versions(
    db: AsyncSession, project_id: uuid.UUID
) -> list[OutlinePlanVersion]:
    rows = (
        await db.execute(
            select(OutlinePlanVersion)
            .where(OutlinePlanVersion.project_id == project_id)
            .order_by(OutlinePlanVersion.version.desc())
            .limit(200)
        )
    ).scalars().all()
    return list(rows)


async def get_version_detail(
    db: AsyncSession, project_id: uuid.UUID, version_id: uuid.UUID
) -> OutlinePlanVersion | None:
    return (
        await db.execute(
            select(OutlinePlanVersion).where(
                OutlinePlanVersion.id == version_id,
                OutlinePlanVersion.project_id == project_id,
            )
        )
    ).scalar_one_or_none()


def _snapshot_identical(
    v: OutlinePlanVersion, snapshot: dict
) -> bool:
    try:
        content = json.loads(v.content_snapshot or "{}")
        plans = json.loads(v.chapter_plans_snapshot or "{}")
    except Exception:  # noqa: BLE001
        return False
    return (
        _fingerprint(content, plans)
        == _fingerprint(snapshot["content_snapshot"], snapshot["chapter_plans_snapshot"])
    )


async def sync_draft_version(
    db: AsyncSession, project_id: uuid.UUID
) -> OutlinePlanVersion | None:
    """AI 生成或人工编辑目录大纲后调用：同步/创建 draft 版本。

    返回值为「当前生效的版本」（可能为 confirmed，内容未变化时无新草稿）。
    规则：
      - 当前目录与 active confirmed 一致 → 无未确认修改，返回 confirmed（不产生草稿）；
      - 当前目录与最新 draft 一致 → 返回该 draft；
      - 否则创建/更新 draft（version = 当前最大版本 + 1，草稿不覆盖 confirmed）。
    """
    snapshot = await snapshot_project(db, project_id)
    active = await get_active_confirmed_version(db, project_id)
    if active is not None and _snapshot_identical(active, snapshot):
        return active

    latest_draft = await get_latest_draft_version(db, project_id)
    if latest_draft is not None and _snapshot_identical(latest_draft, snapshot):
        return latest_draft

    max_version = await _max_version(db, project_id)
    draft = OutlinePlanVersion(
        project_id=project_id,
        version=max_version + 1,
        status=STATUS_DRAFT,
        content_snapshot=json.dumps(snapshot["content_snapshot"], ensure_ascii=False),
        chapter_plans_snapshot=json.dumps(snapshot["chapter_plans_snapshot"], ensure_ascii=False),
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return draft


async def build_confirm_impact(
    db: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict]:
    """确认影响面：当前快照各章与 active confirmed 的差异（不落库）。

    每章条目包含：chapterId / chapterNumber / planTitle / outlineTitle /
    titleConflict（计划标题与大纲标题不一致）/ hasWrittenContent / followingPlan。
    """
    active = await get_active_confirmed_version(db, project_id)
    snapshot = await snapshot_project(db, project_id)
    plans: dict[str, dict] = snapshot["chapter_plans_snapshot"]

    written_ids = set()
    if active is not None:
        try:
            old_plans = json.loads(active.chapter_plans_snapshot or "{}")
        except Exception:  # noqa: BLE001
            old_plans = {}
    else:
        old_plans = {}

    chapter_rows = (
        await db.execute(
            select(Outline).where(
                Outline.project_id == project_id,
                Outline.outline_type == "chapter",
            )
        )
    ).scalars().all()
    has_any_following = len(chapter_rows) > 1
    content_rows = (
        await db.execute(
            select(ChapterContent.outline_id).where(
                ChapterContent.outline_id.in_([c.id for c in chapter_rows])
            )
        )
    ).scalars().all()
    written_ids = {str(x) for x in content_rows}

    impact: list[dict] = []
    ordered = sorted(
        plans.items(), key=lambda kv: (kv[1].get("chapterNumber") or 0)
    )
    for chapter_id, plan in ordered:
        outline_title = plan.get("outlineTitle") or ""
        plan_title = plan.get("title") or ""
        old_plan = old_plans.get(chapter_id) or {}
        old_title = (old_plan.get("title") or "") if isinstance(old_plan, dict) else ""
        title_changed = bool(
            active is not None and plan_title and old_title and plan_title != old_title
        )
        impact.append(
            {
                "chapterId": chapter_id,
                "chapterNumber": plan.get("chapterNumber") or 0,
                "planTitle": plan_title,
                "outlineTitle": outline_title,
                "titleConflict": bool(
                    plan_title and outline_title and plan_title != outline_title
                ),
                "titleChanged": title_changed,
                "hasWrittenContent": chapter_id in written_ids,
                "followingPlan": has_any_following,
            }
        )
    return impact


async def confirm_outline_plan(
    db: AsyncSession,
    project_id: uuid.UUID,
    confirmed_by: uuid.UUID,
    *,
    dry_run: bool = False,
) -> dict:
    """确认大纲：持久化 active confirmed 版本（或 dry_run 返回预览）。

    规则：
      - 同一项目唯一 active confirmed；旧 confirmed 置 superseded（不删除）；
      - 快照取当前目录 + 章节计划（确认当时状态），不依赖前端传入内容；
      - 若最新 draft 与当前树一致则复用它转 confirmed，否则新建 confirmed 版本；
      - 不静默改写任何 outlines 行：标题冲突只提示，由用户决策。
    """
    impact = await build_confirm_impact(db, project_id)
    snapshot = await snapshot_project(db, project_id)

    active = await get_active_confirmed_version(db, project_id)
    if active is not None and _snapshot_identical(active, snapshot):
        # 内容未变化：无新版本，直接返回当前 confirmed（幂等确认）
        if dry_run:
            return {
                "version": active,
                "superseded_version": None,
                "impact": impact,
                "dry_run": True,
                "unchanged": True,
            }
        return {
            "version": active,
            "superseded_version": None,
            "impact": impact,
            "dry_run": False,
            "unchanged": True,
        }

    if dry_run:
        latest_draft = await get_latest_draft_version(db, project_id)
        if latest_draft is not None and _snapshot_identical(latest_draft, snapshot):
            target = latest_draft
        else:
            target = OutlinePlanVersion(
                project_id=project_id,
                version=(await _max_version(db, project_id)) + 1,
                status=STATUS_DRAFT,
                content_snapshot=json.dumps(
                    snapshot["content_snapshot"], ensure_ascii=False
                ),
                chapter_plans_snapshot=json.dumps(
                    snapshot["chapter_plans_snapshot"], ensure_ascii=False
                ),
            )
            # dry_run 预览对象不落库，但必须 flush 一次以生成 id/created_at/updated_at，
            # 否则路由序列化时字段为 None 直接 500（事务随后回滚，无持久化副作用）
            db.add(target)
            await db.flush()
        return {
            "version": target,
            "superseded_version": str(active.id) if active else None,
            "impact": impact,
            "dry_run": True,
            "unchanged": False,
        }

    superseded_id: uuid.UUID | None = None
    if active is not None:
        active.status = STATUS_SUPERSEDED
        superseded_id = active.id

    latest_draft = await get_latest_draft_version(db, project_id)
    now = _now_utc()
    if latest_draft is not None and _snapshot_identical(latest_draft, snapshot):
        latest_draft.status = STATUS_CONFIRMED
        latest_draft.confirmed_at = now
        latest_draft.confirmed_by = confirmed_by
        confirmed = latest_draft
    else:
        confirmed = OutlinePlanVersion(
            project_id=project_id,
            version=(await _max_version(db, project_id)) + 1,
            status=STATUS_CONFIRMED,
            content_snapshot=json.dumps(
                snapshot["content_snapshot"], ensure_ascii=False
            ),
            chapter_plans_snapshot=json.dumps(
                snapshot["chapter_plans_snapshot"], ensure_ascii=False
            ),
            confirmed_at=now,
            confirmed_by=confirmed_by,
        )
        db.add(confirmed)
    await db.commit()
    await db.refresh(confirmed)
    return {
        "version": confirmed,
        "superseded_version": str(superseded_id) if superseded_id else None,
        "impact": impact,
        "dry_run": False,
        "unchanged": False,
    }


async def get_confirmed_chapter_plan(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> tuple[OutlinePlanVersion, dict]:
    """正文生成唯一事实来源：在 active confirmed 版本快照中按 chapterId 稳定查找。

    返回 (version, chapter_plan_dict)。
    失败时抛 NoConfirmedVersionError / ChapterNotBoundError（路由层转 409/404）。
    """
    active = await get_active_confirmed_version(db, project_id)
    if active is None:
        raise NoConfirmedVersionError(
            "该项目尚无已确认大纲版本，请先确认大纲后再生成正文"
        )
    try:
        plans = json.loads(active.chapter_plans_snapshot or "{}")
    except Exception:  # noqa: BLE001
        plans = {}
    plan = plans.get(str(chapter_id))
    if not isinstance(plan, dict):
        raise ChapterNotBoundError(
            "当前章节未绑定到已确认大纲版本，请确认包含该章节的新大纲版本"
        )
    return active, plan


async def plan_status(
    db: AsyncSession, project_id: uuid.UUID
) -> dict:
    """项目大纲版本化状态（预览页 / 章节编辑页）。"""
    active = await get_active_confirmed_version(db, project_id)
    latest_draft = await get_latest_draft_version(db, project_id)
    versions = await list_versions(db, project_id)

    has_unconfirmed = False
    if active is not None:
        snapshot = await snapshot_project(db, project_id)
        has_unconfirmed = not _snapshot_identical(active, snapshot)

    return {
        "projectId": str(project_id),
        "hasConfirmed": active is not None,
        "activeConfirmed": (
            _version_summary(active) if active is not None else None
        ),
        "latestDraft": (
            _version_summary(latest_draft) if latest_draft is not None else None
        ),
        "hasUnconfirmedChanges": has_unconfirmed,
        "versionCount": len(versions),
    }
