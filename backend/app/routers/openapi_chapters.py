# 开放 API v1 章节资源路由（第十九批「移动端适配与开放 API」规格十五/十六）
# 列表/创建/详情/更新/删除 + 发布/取消发布（幂等键，规格十六）。
# 发布前检查：权限 / 章节存在 / 归属 / 有正文 / 项目允许发布 / 审核状态。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.api_response import build_rate_headers, ok
from app.core.api_errors import ApiError
from app.core.database import get_db
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.routers.openapi_utils import bearer_scheme, openapi_guard, log_api_call
from app.schemas.openapi_schemas import (
    ChapterCreateRequest,
    ChapterUpdateRequest,
    PublishRequest,
)
from app.schemas.publication import PublicationCreate
from app.services import (
    audit_service,
    idempotency_service,
    publication_service,
    publish_orchestrator,
)
from app.services.chapter_lifecycle import (
    STATUS_VALIDATION_FAILED,
    LifecycleRejected,
    after_content_saved,
    assert_publish_state,
    mark_later_chapters_needs_recheck,
    _load_json_text,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1", tags=["开放API-章节", "实验"])

async def guard_read(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:read", kind="general", credentials=credentials, db=db)


async def guard_write(request: Request, credentials=Depends(bearer_scheme), db=Depends(get_db)):
    return await openapi_guard(request, required_scope="chapters:write", kind="general", credentials=credentials, db=db)


async def _get_project_for_user(db, project_id: UUID, user) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", "项目不存在", status_code=404)
    if project.user_id != user.id and user.role not in ("operator", "super_admin"):
        raise ApiError("not_found", "项目不存在", status_code=404)
    return project


async def _get_chapter_owned(db, chapter_id: UUID, user) -> tuple[Outline, Project]:
    outline = await db.get(Outline, chapter_id)
    if outline is None or outline.outline_type != "chapter" or outline.status == "deleted":
        raise ApiError("not_found", "章节不存在", status_code=404)
    project = await db.get(Project, outline.project_id)
    if project is None:
        raise ApiError("not_found", "章节不存在", status_code=404)
    if project.user_id != user.id and user.role not in ("operator", "super_admin"):
        raise ApiError("not_found", "章节不存在", status_code=404)
    return outline, project


async def _get_content(db, chapter_id: UUID) -> ChapterContent | None:
    result = await db.execute(
        select(ChapterContent).where(ChapterContent.outline_id == chapter_id)
    )
    return result.scalar_one_or_none()


def _chapter_dict(c: Outline, content: ChapterContent | None) -> dict:
    return {
        "id": str(c.id),
        "project_id": str(c.project_id),
        "parent_id": str(c.parent_id) if c.parent_id else None,
        "title": c.title or "",
        "content": (content.content if content else c.content or ""),
        "word_count": (content.word_count if content else 0),
        "version": (str(content.version) if content else "0"),
        "status": c.status,
        "visibility": c.visibility,
        "sort_order": c.sort_order,
        "published_at": c.published_at.isoformat() if c.published_at else None,
    }


@router.get("/projects/{project_id}/chapters")
async def list_chapters(
    project_id: UUID,
    request: Request,
    page: int = 1,
    page_size: int = 50,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    await _get_project_for_user(db, project_id, guard["principal"].user)
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    result = await db.execute(
        select(Outline)
        .where(
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
            Outline.status != "deleted",
        )
        .order_by(Outline.sort_order.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    chapters = list(result.scalars().all())
    items = []
    for c in chapters:
        content = await _get_content(db, c.id)
        items.append(_chapter_dict(c, content))
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"chapters": items, "count": len(items), "page": page, "page_size": page_size},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.post("/projects/{project_id}/chapters", status_code=201)
async def create_chapter(
    project_id: UUID,
    payload: ChapterCreateRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    await _get_project_for_user(db, project_id, guard["principal"].user)
    if payload.parent_id is not None:
        parent = await db.get(Outline, payload.parent_id)
        if parent is None or parent.project_id != project_id or parent.outline_type != "volume":
            raise ApiError("validation_failed", "分卷不存在或不属于该项目", status_code=422)
    max_sort = (
        await db.execute(
            select(Outline.sort_order)
            .where(Outline.project_id == project_id)
            .order_by(Outline.sort_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    outline = Outline(
        project_id=project_id,
        parent_id=payload.parent_id,
        title=payload.title.strip(),
        outline_type="chapter",
        content="",
        sort_order=max_sort + 1,
        status="draft",
        visibility=payload.visibility,
    )
    db.add(outline)
    await db.flush()
    db.add(ChapterContent(
        outline_id=outline.id,
        content=payload.content or "",
        word_count=len(payload.content.replace(" ", "").replace("\n", "")),
        version=1,
    ))
    await db.commit()
    content = await _get_content(db, outline.id)
    await log_api_call(db, request, guard["principal"], 201)
    return ok(
        request,
        {"chapter": _chapter_dict(outline, content)},
        status_code=201,
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.get("/chapters/{chapter_id}")
async def get_chapter(
    chapter_id: UUID,
    request: Request,
    guard: dict = Depends(guard_read),
    db: AsyncSession = Depends(get_db),
):
    outline, _ = await _get_chapter_owned(db, chapter_id, guard["principal"].user)
    content = await _get_content(db, outline.id)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"chapter": _chapter_dict(outline, content)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.patch("/chapters/{chapter_id}")
async def update_chapter(
    chapter_id: UUID,
    payload: ChapterUpdateRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    outline, _ = await _get_chapter_owned(db, chapter_id, guard["principal"].user)
    if outline.status == "deleted":
        raise ApiError("not_found", "章节不存在", status_code=404)
    if payload.title is not None:
        outline.title = payload.title.strip()
    if payload.visibility is not None:
        outline.visibility = payload.visibility

    content_updated = False
    content_state_before = None
    if payload.content is not None:
        content = await _get_content(db, outline.id)
        now_version = (content.version if content else 0) + 1
        if content is None:
            db.add(ChapterContent(
                outline_id=outline.id,
                content=payload.content,
                word_count=len(payload.content.replace(" ", "").replace("\n", "")),
                version=1,
            ))
        else:
            # 阶段J「内容级审计」：改动正文前抓取 before 快照（只读，不改正文）
            content_state_before = audit_service.snapshot_content_state(content, outline)
            content.content = payload.content
            content.word_count = len(payload.content.replace(" ", "").replace("\n", ""))
            content.version = now_version
        content_updated = True

    await db.commit()
    outline = await db.get(Outline, chapter_id)
    content = await _get_content(db, outline.id)
    if content_updated and content is not None and content.version > 1:
        # 第一阶段：开放 API 直接编辑正文同样补生命周期收尾
        # （校验落库 + 后续章节 needs_recheck，E1/E3 治理）
        try:
            await after_content_saved(
                db,
                outline,
                content,
                record=None,
                audit_action=audit_service.ACTION_CONTENT_SAVE,
                audit_source="api",
                audit_before=content_state_before,
                audit_actor_id=getattr(
                    getattr(guard["principal"], "user", None), "id", None
                ),
                audit_actor_type="api",
                audit_operation="openapi_chapter_update",
                audit_reason="开放 API 更新章节正文",
                audit_metadata={"via": "openapi", "path": "/api/v1/chapters/update"},
            )
        except Exception:  # noqa: BLE001
            pass
        content = await _get_content(db, outline.id)
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"chapter": _chapter_dict(outline, content)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


@router.delete("/chapters/{chapter_id}")
async def delete_chapter(
    chapter_id: UUID,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    outline, _ = await _get_chapter_owned(db, chapter_id, guard["principal"].user)
    # 软删除：避免级联破坏历史发布/版本数据
    outline.status = "deleted"
    await db.commit()

    # 第一阶段（E3 治理）：删除前置章节后，把后续章节标记 needs_recheck
    # （被删章节承载的设定/事实失效，不静默假设后续内容仍有效）
    try:
        await mark_later_chapters_needs_recheck(db, outline)
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"deleted": True, "chapter_id": str(chapter_id)},
        rate_headers=build_rate_headers(guard["rate"]),
    )


async def _check_publish_preconditions(
    db, outline: Outline, project: Project, user
) -> None:
    """规格十六发布前检查：权限/存在/归属/正文/项目发布允许/审核状态。

    第一阶段扩展：生命周期校验 —— 存在阻断性问题（validation_failed /
    validation_report.blocking）时拒绝发布，防止未完成/冲突内容上线。
    """
    if outline.status == "deleted":
        raise ApiError("not_found", "章节不存在", status_code=404)
    if outline.project_id != project.id:
        raise ApiError("conflict", "章节不属于当前项目", status_code=409)

    # B1/U1a：状态门禁优先于内容体检（R-2）——已归档 / 已下架 / 非 approved 先 409，
    # 不因正文为空（422）或校验报告缺失（424）给出与真实原因无关的拒绝。
    try:
        await assert_publish_state(db, outline)
    except LifecycleRejected as exc:
        # 审计行只在 savepoint 内 flush，拒绝响应前单独提交这一条。
        # 提交失败只降级为「审计未持久化」，不得把 409 变成 500。
        if exc.audit_pending:
            await audit_service.commit_pending_rejection_audit(db)
        raise ApiError(
            "conflict",
            str(exc),
            status_code=409,
            details={"reason": exc.reason_code, "gate": exc.gate},
        ) from exc

    content = await _get_content(db, outline.id)
    body = (content.content if content else outline.content or "") or ""
    if not body.strip():
        raise ApiError(
            "validation_failed", "章节正文为空，无法发布", status_code=422,
            details={"reason": "empty_body"},
        )

    # 第一阶段：确定性校验阻断（不批准发布，只做状态阻断与可审计记录）
    if outline.lifecycle_status == STATUS_VALIDATION_FAILED:
        raise ApiError(
            "validation_failed",
            "章节存在阻断性问题（validation_failed），请先修复/重写后再发布",
            status_code=424,
            details={"reason": "blocking_validation"},
        )
    report = _load_json_text(outline.validation_report) or {}
    blocking = [
        i for i in (report.get("issues") or [])
        if isinstance(i, dict) and i.get("severity") == "blocking"
    ]
    if blocking:
        raise ApiError(
            "validation_failed",
            f"章节存在 {len(blocking)} 个阻断性问题，不能发布",
            status_code=424,
            details={"reason": "blocking_validation", "count": len(blocking)},
        )

    if project.publication_status == "archived":
        raise ApiError("conflict", "已归档的作品不能发布章节", status_code=409)
    # 审核状态：作品处于 review 且未审核通过时不允许发布
    if project.publication_status == "review":
        raise ApiError(
            "conflict", "作品处于审核中，暂不能发布章节", status_code=409,
            details={"reason": "project_under_review"},
        )
    # 发布时间设置：若存在未来发布时间（scheduled），当前不可提前发布
    if getattr(outline, "scheduled_publish_at", None) is not None:
        from datetime import datetime
        if outline.scheduled_publish_at > datetime.utcnow():
            raise ApiError(
                "conflict", "章节已设置定时发布，未到发布时间", status_code=409,
                details={"reason": "scheduled"},
            )


async def _apply_idempotency(db, key: str, endpoint: str, request: Request):
    """幂等键重放。返回 (response, handled)。"""
    if not key:
        return None, False
    record = await idempotency_service.find_replay(
        db, idempotency_key=key, endpoint=endpoint
    )
    if record is not None:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=record.response_status,
            content=json.loads(record.response_body or "{}"),
        ), True
    return None, False


def _save_idempotent(db, key: str, endpoint: str, user_id, status_code: int, body: dict):
    if not key:
        return
    import uuid as _uuid
    from datetime import datetime, timedelta
    from app.models.idempotency_record import IdempotencyRecord
    row = IdempotencyRecord(
        idempotency_key=key,
        endpoint=endpoint,
        user_id=user_id,
        response_status=status_code,
        response_body=json.dumps(body, ensure_ascii=False),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(row)


@router.post("/chapters/{chapter_id}/publish")
async def publish_chapter(
    chapter_id: UUID,
    payload: PublishRequest,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    idem_key = request.headers.get("Idempotency-Key", "").strip()
    endpoint = f"publish:{chapter_id}"
    replay, handled = await _apply_idempotency(db, idem_key, endpoint, request)
    if handled:
        return replay

    outline, project = await _get_chapter_owned(
        db, chapter_id, guard["principal"].user
    )
    await _check_publish_preconditions(db, outline, project, guard["principal"].user)

    from fastapi import HTTPException
    try:
        # U2a：门禁 / 快照 / 发布状态写透 / 索引+统计同步 / 唯一一次业务提交
        # 全部收敛到 publish_orchestrator。路由不再写发布状态列，
        # 也不在编排器之外重复提交业务事务。
        outcome = await publish_orchestrator.publish_chapter(
            db,
            publish_orchestrator.PublishCommand(
                project=project,
                outline=outline,
                payload=PublicationCreate(
                    title=payload.title,
                    description=payload.description,
                    visibility=payload.visibility,
                ),
                write_snapshot=True,
                gate_profile=publish_orchestrator.GATE_PROFILE_STATE_ONLY,
            ),
        )
    except LifecycleRejected as exc:
        # D-U2A-4：状态冲突统一 409；拒绝审计提交失败只降级为「未持久化」
        if exc.audit_pending:
            await audit_service.commit_pending_rejection_audit(db)
        raise ApiError(
            "conflict",
            str(exc),
            status_code=409,
            details={"reason": exc.gate or "state_gate"},
        )
    except HTTPException as exc:
        raise ApiError(
            "validation_failed",
            "章节发布检查未通过",
            status_code=exc.status_code,
            details={"reason": "publish_check_failed"},
        )

    result = outcome.publication
    body = {
        "chapter_id": str(result.chapter_id),
        "title": result.title,
        "status": result.status,
        "visibility": result.visibility,
        "word_count": result.word_count,
        "published_at": result.published_at.isoformat() if result.published_at else None,
    }
    resp_body = ok(
        request,
        {"publication": body},
        rate_headers=build_rate_headers(guard["rate"]),
    )
    if idem_key:
        _save_idempotent(
            db, idem_key, endpoint, guard["principal"].user.id, 200,
            json.loads(resp_body.body),
        )
        await db.commit()
    await log_api_call(db, request, guard["principal"], 200)
    return resp_body


@router.post("/chapters/{chapter_id}/unpublish")
async def unpublish_chapter(
    chapter_id: UUID,
    request: Request,
    guard: dict = Depends(guard_write),
    db: AsyncSession = Depends(get_db),
):
    idem_key = request.headers.get("Idempotency-Key", "").strip()
    endpoint = f"unpublish:{chapter_id}"
    replay, handled = await _apply_idempotency(db, idem_key, endpoint, request)
    if handled:
        return replay

    outline, project = await _get_chapter_owned(db, chapter_id, guard["principal"].user)
    if outline.status not in ("published", "unlisted"):
        raise ApiError("conflict", "章节当前未发布，无法撤回", status_code=409)

    from fastapi import HTTPException
    try:
        result = await publication_service.unpublish_chapter(db, project, outline)
    except HTTPException as exc:
        raise ApiError(
            "conflict", "章节撤回失败", status_code=exc.status_code
        )

    body = {
        "chapter_id": str(result.chapter_id),
        "status": result.status,
    }
    if idem_key:
        _save_idempotent(
            db, idem_key, endpoint, guard["principal"].user.id, 200, body
        )
        await db.commit()
    await log_api_call(db, request, guard["principal"], 200)
    return ok(
        request,
        {"publication": body},
        rate_headers=build_rate_headers(guard["rate"]),
    )
