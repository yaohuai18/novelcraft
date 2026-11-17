# 可观测性路由（第十八批「性能优化、可靠性与安全加固」规格二十五）
# GET /health/live、/health/ready、/health/detail（应用/DB/缓存/队列/对象存储/搜索/AI 检查）
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.cache_entry import CacheEntry
from app.models.job import Job
from app.models.outbox_event import OutboxEvent
from app.services import perf_service

router = APIRouter(prefix="/health", tags=["系统"])


@router.get("/live")
async def health_live():
    """存活探针：应用进程存活即 ok。"""
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@router.get("/ready")
async def health_ready(db: AsyncSession = Depends(get_db)):
    """就绪探针：依赖项（DB 等）可用才返回 ok。"""
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "checks": ["database"]}
    except Exception as exc:
        return {"status": "unavailable", "checks": ["database"], "error": str(exc)}


@router.get("/detail")
async def health_detail(db: AsyncSession = Depends(get_db)):
    """详细健康检查：应用 / DB / 缓存 / 队列 / 对象存储 / 搜索 / AI。"""
    from sqlalchemy import func, select

    components = []
    status = "ok"

    # 应用
    components.append({"name": "app", "status": "ok", "detail": settings.app_name})

    # 数据库
    try:
        await db.execute(text("SELECT 1"))
        components.append({"name": "database", "status": "ok", "detail": "SELECT 1 ok"})
    except Exception as exc:
        status = "degraded"
        components.append({"name": "database", "status": "error", "detail": str(exc)})

    # 缓存
    cache_count = (
        await db.execute(select(func.count(CacheEntry.id)))
    ).scalar_one()
    components.append(
        {"name": "cache", "status": "ok", "detail": f"{cache_count} entries, TTL {settings.cache_default_ttl_seconds}s"}
    )

    # 队列：pending/dead 数量仅作展示；死信队列属于正常运维产物（管理端可查看/重跑），不作为故障降级依据
    job_count = (
        await db.execute(select(func.count(Job.id)).where(Job.status == "pending"))
    ).scalar_one()
    dead_count = (
        await db.execute(select(func.count(Job.id)).where(Job.status == "dead"))
    ).scalar_one()
    components.append(
        {
            "name": "queue",
            "status": "ok",
            "detail": f"pending={job_count}, dead={dead_count}",
        }
    )

    # 对象存储
    storage_ok = settings.file_max_bytes > 0
    components.append(
        {"name": "object_storage", "status": "ok" if storage_ok else "degraded",
         "detail": f"max_upload={settings.file_max_bytes} bytes"}
    )

    # 搜索
    search_ok = bool(settings.search_index_enabled) if hasattr(settings, "search_index_enabled") else True
    components.append(
        {"name": "search", "status": "ok" if search_ok else "degraded", "detail": "index service configured"}
    )

    # AI
    ai_ok = bool(settings.ai_model)
    components.append(
        {"name": "ai", "status": "ok" if ai_ok else "degraded", "detail": f"model={settings.ai_model}"}
    )

    return {
        "status": status,
        "components": components,
        "uptime_seconds": perf_service.live.snapshot().get("uptime_seconds", 0),
        "metrics": perf_service.live.snapshot(),
    }


# 兼容历史路由：/api/health
api_router = APIRouter(prefix="/api", tags=["系统"])


@api_router.get("/health")
async def health_check():
    return {"status": "ok"}


# 版本识别接口：GET /health
# 返回应用名称/后端版本/启动时间/数据库路径/构建标识/源码哈希/schema 版本，
# 用于判断浏览器访问的到底是哪一份代码。
START_TIME = datetime.now()


@router.get("")
async def health_identity(db: AsyncSession = Depends(get_db)):
    """版本识别接口（裸 /health）。"""
    try:
        schema_version = (
            await db.execute(
                text(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                )
            )
        ).scalar_one()
    except Exception:
        try:
            schema_version = (
                await db.execute(text("PRAGMA user_version"))
            ).scalar_one()
        except Exception:
            schema_version = None

    # 读取 git 标签与 HEAD，作为构建标识与源码哈希
    import os

    build_id = "unknown"
    source_hash = "unknown"
    repo_dir = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ),
        ".git",
    )
    try:
        head_file = os.path.join(repo_dir, "HEAD")
        if os.path.exists(head_file):
            with open(head_file, "r", encoding="utf-8") as f:
                ref = f.read().strip()
            if ref.startswith("ref: "):
                ref_path = os.path.join(repo_dir, ref[5:].replace("/", os.sep))
                if os.path.exists(ref_path):
                    with open(ref_path, "r", encoding="utf-8") as f:
                        source_hash = f.read().strip()
            else:
                source_hash = ref
    except Exception:
        pass
    try:
        tag_file = os.path.join(repo_dir, "refs", "tags", "marvis-baseline")
        if os.path.exists(tag_file):
            with open(tag_file, "r", encoding="utf-8") as f:
                build_id = "marvis-baseline@" + f.read().strip()[:12]
    except Exception:
        pass

    # 延迟导入，避免与 main 循环依赖
    from app.main import app as fastapi_app

    return {
        "app_name": settings.app_name,
        "version": fastapi_app.version,
        "started_at": START_TIME.isoformat(),
        "database_driver": (
            settings.database_url.split(":")[0] if "://" in settings.database_url else "sqlite"
        ),
        "build_id": build_id,
        "source_hash": source_hash,
        "schema_version": schema_version,
    }
