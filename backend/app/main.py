import asyncio
import json
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import Base, engine
from app.core.experimental import require_experimental_access
from app.core.db_migrations import (
    ensure_phase1_schema,
    ensure_phase3_schema,
    ensure_phase4_schema,
    ensure_phase7_schema,
    ensure_phase8_schema,
    ensure_phase9_schema,
    ensure_phase10_schema,
    ensure_phase11_schema,
    ensure_phase17_schema,
    ensure_phase19_schema,
    ensure_phase22_schema,
)
from app.services import monitor_service, perf_service
from app.models import (
    AITask,
    AIUsageLog,
    ActivityLog,
    ChapterContent,
    ChapterMemory,
    ChapterVersion,
    Character,
    Comment,
    CommentRateLimit,
    ExportTask,
    Favorite,
    GenerationRecord,
    ImportTask,
    Mention,
    Notification,
    NotificationPreference,
    Outline,
    OutlineTag,
    Project,
    ProjectBackup,
    ProjectSetting,
    ProjectInvitation,
    ProjectMember,
    Publication,
    ReaderPreference,
    ReadingProgress,
    RecentItem,
    Report,
    SearchDocument,
    Tag,
    User,
    WorldSetting,
    PerformanceMetric,
    CacheEntry,
    Job,
    RateLimitRecord,
    AIQuota,
    IdempotencyRecord,
    OutboxEvent,
    RefreshToken,
    ConfirmationToken,
    SecurityEvent,
    StoredFile,
    DrillRecord,
)  # noqa: F401  确保模型注册，供 create_all 建表
from app.routers.auth import router as auth_router
from app.routers.analytics import router as analytics_router
from app.routers.recommend_ops import router as recommend_ops_router
from app.routers.ai_generation import router as ai_router
from app.routers.ai_novel_generation import router as ai_novel_router
from app.routers.failure_samples import router as failure_samples_router
from app.routers.ai_tasks import router as ai_task_router
from app.routers.ai_plan_jobs import router as ai_plan_jobs_router
from app.routers.backups import router as backup_router
from app.routers.chapter_contents import router as chapter_content_router
from app.routers.chapter_plans import router as chapter_plans_router
from app.routers.chapter_memories import router as chapter_memories_router
from app.routers.project_settings import router as project_settings_router
from app.routers.characters import router as character_router
from app.routers.character_relationships import router as character_relationships_router
from app.routers.timeline_events import router as timeline_events_router
from app.routers.change_suggestions import router as change_suggestions_router
from app.routers.foreshadows import router as foreshadows_router
from app.routers.facts import router as facts_router
from app.routers.consistency import router as consistency_router
from app.routers.collaboration import router as collaboration_router
from app.routers.comments import comments_router, project_comments_router
from app.routers.discover import router as discover_router
from app.routers.exports import router as export_router
from app.routers.favorites import router as favorite_router
from app.routers.generations import router as generations_router
from app.routers.health import router as health_router
from app.routers.health import api_router as health_api_router
from app.routers.imports import router as import_router
from app.routers.notifications import router as notification_router
from app.routers.outlines import router as outline_router
from app.routers.outline_plan_versions import router as outline_plan_versions_router
from app.routers.projects import router as projects_router
from app.routers.publications import router as publications_router
from app.routers.reader import public_router, reader_router
from app.routers.recent_items import router as recent_item_router
from app.routers.reports import router as report_router
from app.routers.search import router as search_router
from app.routers.stats import router as stats_router
from app.routers.admin import router as admin_router
from app.routers.tags import router as tag_router
from app.routers.versions import router as versions_router
from app.routers.world_settings import router as world_settings_router
from app.routers.ops import router as ops_router
from app.routers.security import router as security_router
from app.routers.openapi_projects import router as openapi_projects_router
from app.routers.openapi_chapters import router as openapi_chapters_router
from app.routers.openapi_tokens import router as openapi_tokens_router
from app.routers.openapi_ai import router as openapi_ai_router
from app.routers.openapi_webhooks import router as openapi_webhooks_router
from app.routers.openapi_apps import router as openapi_apps_router
from app.routers.openapi_mobile_drafts import router as openapi_mobile_drafts_router
from app.routers.billing import router as billing_router
from app.routers.admin_billing import router as admin_billing_router
from app.routers.i18n import router as i18n_router
from app.routers.admin_i18n import router as admin_i18n_router
from app.routers.enterprise import router as enterprise_router
from app.routers.enterprise_collab import router as enterprise_collab_router
from app.routers.enterprise_review import router as enterprise_review_router
from app.routers.enterprise_admin import router as enterprise_admin_router
from app.core.api_errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
)
from starlette.exceptions import HTTPException as StarletteHTTPException


def _validation_error_handler(request: Request, exc: RequestValidationError):
    """开放 API 统一错误结构（规格二十一）：参数校验失败 → validation_failed。"""
    rid = f"req-{uuid.uuid4().hex[:12]}"
    try:
        details = exc.errors()
    except Exception:  # noqa: BLE001
        details = []
    return JSONResponse(
        status_code=422,
        content={
            "api_version": "v1",
            "request_id": rid,
            "error": {
                "code": "validation_failed",
                "message": "请求参数校验失败",
                "request_id": rid,
                "details": details[:20],
            },
        },
        headers={"X-Request-Id": rid},
    )


app = FastAPI(
    title=settings.app_name,
    version="0.2.0"
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(
    RequestValidationError,
    _validation_error_handler,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 第三批 a/b：安全响应头与请求上下文中间件。
# 注意注册顺序：后注册者更外层。monitor_requests（下方装饰器）若注册在最后会变成最外层，
# 其异常分支直接 return JSONResponse 不经内层中间件，错误响应将缺失安全头/request_id。
# 因此这里只导入，实际注册放在 monitor_requests 定义之后（见下方 re-register 注释块）。
from app.core.observability import (
    install_request_id_filter,
    request_context_middleware,
    security_headers_middleware,
)

install_request_id_filter()


@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    """系统监控采样 + 请求体大小限制 + 错误脱敏 + 请求日志（第十八批规格二/二十/二十五）。"""
    start = time.perf_counter()

    # 请求体大小限制（规格二十：请求体大小限制）
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.request_body_max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "请求体过大", "error": "payload_too_large"},
                )
        except ValueError:
            pass

    status_code = 500
    is_error = True
    try:
        response = await call_next(request)
        status_code = response.status_code
        is_error = response.status_code >= 400
        return response
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        # 错误信息脱敏（规格二十：禁止返回 DB 堆栈 / 服务器路径 / 内部地址）
        from app.core.security_utils import sanitize_error_message

        return JSONResponse(
            status_code=500,
            content={
                "detail": "服务器内部错误",
                "error": "internal_error",
                "debug_msg": sanitize_error_message(str(exc)),
            },
        )
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        monitor_service.record_request(
            is_error=is_error,
            duration_ms=duration_ms,
        )
        perf_service.live.record(
            duration_ms=duration_ms,
            endpoint=request.url.path,
            method=request.method,
            is_error=is_error,
            is_timeout=duration_ms > 30000,
        )
        if settings.request_log_enabled:
            print(
                f"[req] {request.method} {request.url.path} -> {status_code} "
                f"{duration_ms:.1f}ms"
            )


# monitor_requests 已注册（装饰器），在此之后注册安全头与请求上下文 →
# 两者位于 monitor 外层，monitor 直接返回的错误响应（500/413）也能带上安全头与 request_id。
app.middleware("http")(security_headers_middleware)
app.middleware("http")(request_context_middleware)


app.include_router(health_router)
app.include_router(health_api_router)
app.include_router(auth_router)
app.include_router(ai_router)
app.include_router(ai_novel_router)
app.include_router(failure_samples_router)
app.include_router(ai_task_router)
app.include_router(ai_plan_jobs_router)
app.include_router(import_router)
app.include_router(export_router)
app.include_router(backup_router)
app.include_router(projects_router)
app.include_router(project_settings_router)
app.include_router(chapter_memories_router)
app.include_router(character_router)
app.include_router(character_relationships_router)
app.include_router(timeline_events_router)
app.include_router(change_suggestions_router)
app.include_router(foreshadows_router)
app.include_router(consistency_router)
app.include_router(facts_router)
app.include_router(world_settings_router)
app.include_router(outline_router)
app.include_router(chapter_content_router)
app.include_router(chapter_plans_router)
app.include_router(outline_plan_versions_router)
app.include_router(generations_router)
app.include_router(tag_router)
app.include_router(search_router)
app.include_router(discover_router)
app.include_router(recent_item_router)
app.include_router(favorite_router)
app.include_router(versions_router)
app.include_router(publications_router)
app.include_router(public_router)
app.include_router(reader_router)
app.include_router(notification_router)
app.include_router(project_comments_router)
app.include_router(comments_router)
app.include_router(collaboration_router)
app.include_router(report_router, dependencies=[Depends(require_experimental_access)])
app.include_router(stats_router)
app.include_router(admin_router)
# 第三批 e：未验证/未上线模块统一开关（DISABLED_MODULES 为空时行为与改造前完全一致）。
# 关闭即整块不挂载：路径 404，不存在"半开半闭"的中间态。
from app.core.module_flags import module_enabled as _module_enabled
from app.core.module_flags import parse_disabled_modules as _parse_disabled_modules

_DISABLED_MODULES = _parse_disabled_modules(getattr(settings, "disabled_modules", ""))


def _module_on(name: str) -> bool:
    return _module_enabled(name, disabled=_DISABLED_MODULES)


if _module_on("ops"):
    app.include_router(ops_router)
if _module_on("security"):
    app.include_router(security_router)
if _module_on("openapi"):
    app.include_router(openapi_projects_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_chapters_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_tokens_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_ai_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_webhooks_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_apps_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(openapi_mobile_drafts_router, dependencies=[Depends(require_experimental_access)])
if _module_on("billing"):
    app.include_router(billing_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(admin_billing_router)
if _module_on("i18n"):
    app.include_router(i18n_router)
    app.include_router(admin_i18n_router)
if _module_on("analytics"):
    app.include_router(analytics_router, dependencies=[Depends(require_experimental_access)])
    app.include_router(recommend_ops_router, dependencies=[Depends(require_experimental_access)])
if _module_on("enterprise"):
    app.include_router(enterprise_router)
    app.include_router(enterprise_collab_router)
    app.include_router(enterprise_review_router)
    app.include_router(enterprise_admin_router)


@app.get("/", dependencies=[Depends(require_experimental_access)], tags=["实验"])
async def root():
    return {
        "message": "NovelCraft API is running"
    }


# 本地开发便利：启动时自动建表（仅本地开发用，正式环境应改用 Alembic 迁移）
@app.on_event("startup")
async def create_tables():
    """启动建表/迁移编排（正式迁移机制）。

    首次（schema_migrations 为空）：备份旧库 → create_all + ensure 系列 → 记录基线 v1；
    已有记录：跳过自动 create_all（生产环境禁止不可控的自动建表）。
    """
    # 第三批 c：记录数据库实际生效的 PRAGMA（只读，不修改任何设置）。
    from app.core.db_diagnostics import log_database_diagnostics

    await log_database_diagnostics(engine)
    from app.core import migrate

    async def _init_schema(conn):
        # I-3a-fix（D9）：phase19 前移。chapter_fact_dependencies 的建表/索引语句唯一来源是
        # db_migrations.PHASE19_MIGRATION_STATEMENTS（v4 迁移与 ensure_phase19_schema 同源），
        # 该表使用独立的 phase19_metadata，不注册到 Base.metadata。因此必须先由 phase19 的 DDL
        # 建表，再执行 create_all（checkfirst 只建缺失表、不覆盖已存在的表），否则同一张表
        # 会同时存在「create_all 结构」与「phase19 DDL 结构」两个来源，签名不可判定。
        # phase20 / phase22 的同类顺序问题本批次不处理（登记为后续独立事项）。
        await ensure_phase19_schema(conn)
        await conn.run_sync(Base.metadata.create_all)
        await ensure_phase1_schema(conn)
        await ensure_phase3_schema(conn)
        await ensure_phase4_schema(conn)
        await ensure_phase7_schema(conn)
        await ensure_phase8_schema(conn)
        await ensure_phase9_schema(conn)
        await ensure_phase10_schema(conn)
        await ensure_phase11_schema(conn)
        await ensure_phase17_schema(conn)
        # I-3a-fix（D9）：phase19 已在 create_all 之前执行，此处不再重复调用。
        # B-2（A-4）：生产 schema 初始化入口接入逐次 token 账目表（只增补缺失对象）
        await ensure_phase22_schema(conn)

    # I-3a-fix（D1）：外层不得持有 SQLAlchemy 事务。pysqlite legacy 模式下
    # engine.begin() 只置 SQLAlchemy 侧状态、不真正发出 BEGIN（do_begin 为空实现），
    # 若与迁移内部显式 DBAPI BEGIN IMMEDIATE / COMMIT / ROLLBACK 并存，会形成两套
    # 互不知情的事务状态。这里改用 engine.connect()，事务边界完全由 migrate 的显式
    # 事务网关掌握；本块的退出不会再对迁移结果产生 commit/rollback 语义。
    async with engine.connect() as connection:
        # v7 的 ALTER 假定 novel_plan_jobs 已存在。恢复 2026-11-10 之前的旧备份时
        # 该表可能缺失；此时先按最终形态幂等建表，后续 ALTER 会全部命中
        # duplicate column（迁移网关容忍），不会因缺表而失败关闭。
        # 生产库常态（表已存在）不走这条分支，schema 变更仍由 v7 迁移唯一负责。
        from app.core.novel_plan_job_schema import (
            ensure_novel_plan_job_schema,
            ensure_table_before_migrations,
        )

        if await ensure_table_before_migrations(connection):
            await connection.commit()
            print("[plan-jobs] 迁移前补建 novel_plan_jobs（旧备份恢复场景）")

        _result = await migrate.run_migrations(
            connection,
            settings.database_url,
            init_callback=_init_schema,
        )
        # phase22 兼容检查入口（保持与本轮迁移收口同批）：
        # 正式库常态为 noop → repair=False，仅只读校验 token 账目表结构，不执行任何 DDL；
        # 仅首启/升级（非正式库常态）才 repair=True 增补缺失对象。
        await ensure_phase22_schema(connection, repair=_result["status"] != "noop")
        # 整本规划后台作业表：纯新增（CREATE TABLE IF NOT EXISTS + 索引），
        # 不改任何既有表；这张表不在 schema_migrations 版本链里，故每次启动都做幂等补齐。
        _plan_job_schema = await ensure_novel_plan_job_schema(connection, repair=True)
        await connection.commit()
        if _plan_job_schema["created"]:
            print("[plan-jobs] 已创建 novel_plan_jobs 表")
        # 批次 5：FTS 索引回填（迁移 v10 只建表，存量 content_bigrams 与索引行在此补齐）
        from app.services import fts_search as _fts_search

        _fts_result = await _fts_search.ensure_chapters_fts_index(connection)
        await connection.commit()
        if _fts_result.get("backfilled") or _fts_result.get("indexed"):
            print(f"[fts] 回填 bigrams={_fts_result['backfilled']} 行，索引补齐={_fts_result['indexed']} 行")
    if _result["status"] == "initialized":
        print(f"[migrate] 完成基线 v1 初始化，迁移前备份={_result['backup']}")
    else:
        print(f"[migrate] 已是最新版本，跳过自动 create_all（applied={_result['applied']}）")

    # P1：BackgroundTasks 生产限制——启动恢复遗留 AI 任务
    # 记录本次 worker 启动时间：早于该时刻的 running 任务视为进程重启遗留。
    await _recover_stale_ai_tasks()
    await _recover_stale_plan_jobs()

    # 第二批：启动进程内 worker 扫描 loop（领取靠数据库 CAS，多进程也安全）
    import asyncio as _asyncio

    from app.services.novel_plan_job_service import worker_loop as _plan_worker_loop

    _plan_stop = _asyncio.Event()
    app.state.plan_job_stop = _plan_stop
    app.state.plan_job_worker = _asyncio.create_task(_plan_worker_loop(_plan_stop))


async def _recover_stale_plan_jobs():
    """启动恢复：只中断「租约已过期」的 running；queued 留给 worker 继续领取。

    第二批修正：不再无差别中断所有 queued/running —— 那会把还没开始的任务
    和另一个进程正持有有效租约的任务一起杀掉。整本规划仍不自动重试。
    """
    from app.services.novel_plan_job_service import recover_stale_jobs

    try:
        stats = await recover_stale_jobs()
        if stats["expired"] or stats["queued"]:
            print(
                "[plan-jobs] 启动恢复：租约过期 %d 个已置 interrupted，待领取 %d 个"
                % (stats["expired"], stats["queued"])
            )
    except Exception as exc:  # noqa: BLE001 恢复失败不应阻止服务启动
        print(f"[plan-jobs] 启动恢复失败（不影响服务启动）：{exc!r}")


@app.on_event("shutdown")
async def _shutdown_plan_jobs():
    """关闭：停扫描、取消本进程执行中的作业，不伪造 succeeded/failed。"""
    import asyncio as _asyncio

    from app.services.novel_plan_job_service import (
        shutdown_mark_own_jobs as _shutdown_mark_own_jobs,
        worker_id as _plan_worker_id,
    )

    stop = getattr(app.state, "plan_job_stop", None)
    if stop is not None:
        stop.set()
    task = getattr(app.state, "plan_job_worker", None)
    if task is not None:
        task.cancel()
        try:
            await _asyncio.wait_for(task, timeout=3)
        except Exception:  # noqa: BLE001 关闭阶段不因超时卡住
            pass
    try:
        count = await _asyncio.wait_for(_shutdown_mark_own_jobs(_plan_worker_id()), timeout=5)
        if count:
            print(f"[plan-jobs] 关闭：{count} 个本机运行中作业已标 interrupted")
    except Exception as exc:  # noqa: BLE001
        print(f"[plan-jobs] 关闭标记失败（租约会自然过期）：{exc!r}")


async def _recover_stale_ai_tasks():
    """启动时恢复遗留 AI 任务：
    1) 超过 timeout_at 的 pending/running 任务 → timeout；
    2) 进程重启遗留的 running 任务（started_at 早于本次启动）→ failed（可重试）；
    3) 未超时的 pending 任务重新入队执行。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select, text, update

    from app.core.database import AsyncSessionLocal
    from app.models.ai_task import AITask
    from app.services.ai_task_service import run_ai_task

    now = datetime.utcnow()
    worker_started_at = now - timedelta(seconds=5)

    async with AsyncSessionLocal() as db:
        # 1) 超时任务
        await db.execute(
            update(AITask)
            .where(
                AITask.status.in_(["pending", "running"]),
                AITask.timeout_at.is_not(None),
                AITask.timeout_at <= now,
            )
            .values(
                status="timeout",
                completed_at=now,
                error_message="进程重启恢复：任务已超时",
            )
        )
        # 2) 重启遗留的 running 任务（未超时，但本次启动前已在执行）
        await db.execute(
            update(AITask)
            .where(
                AITask.status == "running",
                AITask.started_at.is_not(None),
                AITask.started_at < worker_started_at,
            )
            .values(
                status="failed",
                completed_at=now,
                error_message="进程重启导致任务中断，请重试",
            )
        )
        await db.commit()

        # 3) 未超时的 pending 任务重新入队
        pending_ids = (await db.execute(
            select(AITask.id).where(AITask.status == "pending")
        )).scalars().all()

    for task_id in pending_ids:
        asyncio.ensure_future(run_ai_task(task_id, AsyncSessionLocal))
    return

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # 第一期「生成不丢失」：既有表 chapter_versions 追加列（幂等）
    async with engine.begin() as connection:
        await ensure_phase1_schema(connection)
    # 第三期「人物和时间不漂」：既有表 characters / timeline_events 追加列（幂等）
    async with engine.begin() as connection:
        await ensure_phase3_schema(connection)
    # 第四期「写错能发现」：既有表 project_settings 追加 auto_check 列（幂等）
    async with engine.begin() as connection:
        await ensure_phase4_schema(connection)
    # 文26 P1「章节摘要和续写记忆闭环」：既有表 chapter_memories 追加 generation_record_id 列（幂等）
    async with engine.begin() as connection:
        await ensure_phase7_schema(connection)
    # 文26 P2「人物、关系、时间线和伏笔正式管理」：既有表追加 status/time_label/title/importance/resolved_chapter_id 列（幂等）
    async with engine.begin() as connection:
        await ensure_phase8_schema(connection)
    # 章节计划（ChapterPlan）：outlines 表追加 chapter_plan 列（幂等，旧数据回退兼容）
    async with engine.begin() as connection:
        await ensure_phase9_schema(connection)
    # 「已确认大纲版本化」：新建 outline_plan_versions 表 + generation_records 追加版本追溯列（幂等）
    async with engine.begin() as connection:
        await ensure_phase10_schema(connection)
    # 「生成链路数据一致性/可追溯性」：generation_records 追加原始响应与元数据列（幂等）
    async with engine.begin() as connection:
        await ensure_phase11_schema(connection)
    async with engine.begin() as connection:
        await ensure_phase17_schema(connection)
