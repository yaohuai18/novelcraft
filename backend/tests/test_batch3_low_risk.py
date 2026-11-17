"""第三批低风险改动回归：安全响应头 / request_id / PRAGMA 诊断 / 模块开关 /
分享链接加固 / 晚到结果人工处置 / 迁移脚本可用性。

全部离线：不联网、不读正式库、不需要 API Key。每个用例独立临时 SQLite。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.requests import Request

import app.models  # noqa: F401  注册全部 ORM
from app.core import db_diagnostics, module_flags, observability
from app.core.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.enterprise_file import FileAccessLog, FileShareLink
from app.models.novel_plan_job import NovelPlanJob
from app.models.stored_file import StoredFile
from app.services import enterprise_admin_service
from app.services import novel_plan_job_service as jobs
from app.services import novel_plan_job_state as st
from app.utils.security import hash_password


# --------------------------------------------------------------------- helpers
class Env:
    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="b3_", ignore_cleanup_errors=True)
        self.path = Path(self.dir.name) / "lab.db"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        self.maker = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self._orig = jobs.AsyncSessionLocal

    async def setup(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
        self.dir.cleanup()

    def install(self) -> None:
        jobs.AsyncSessionLocal = self.maker  # type: ignore[assignment]

    def restore(self) -> None:
        jobs.AsyncSessionLocal = self._orig  # type: ignore[assignment]


@pytest.fixture()
def env(monkeypatch):
    e = Env()
    asyncio.run(e.setup())
    e.install()
    jobs._RUNNING.clear()
    try:
        yield e
    finally:
        e.restore()
        asyncio.run(e.dispose())


def _app_with(router, env: Env) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _db():
        async with env.maker() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


# --------------------------------------------------------------------- a 安全头
def test_security_headers_applied():
    app = FastAPI()

    @app.get("/ping")
    async def ping():  # noqa: D401 - test route
        return {"ok": True}

    app.middleware("http")(observability.security_headers_middleware)
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_security_headers_can_be_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "security_headers", False)
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    app.middleware("http")(observability.security_headers_middleware)
    resp = TestClient(app).get("/ping")
    assert "X-Frame-Options" not in resp.headers


# --------------------------------------------------------------- b request_id
def test_request_id_is_echoed_and_logged(caplog):
    app = FastAPI()

    @app.get("/rid")
    async def rid():
        return {"request_id": observability.current_request_id()}

    app.middleware("http")(observability.request_context_middleware)
    client = TestClient(app)

    resp = client.get("/rid", headers={"X-Request-Id": "gateway-abc-123456"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "gateway-abc-123456"
    assert resp.json()["request_id"] == "gateway-abc-123456"


def test_request_id_rejects_unsafe_value():
    app = FastAPI()

    @app.get("/rid")
    async def rid():
        return {"request_id": observability.current_request_id()}

    app.middleware("http")(observability.request_context_middleware)
    resp = TestClient(app).get("/rid", headers={"X-Request-Id": "bad id with spaces<script>"})
    returned = resp.headers["X-Request-Id"]
    assert returned.startswith("req-")
    assert "<" not in returned


def test_request_id_filter_injects_field():
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1, msg="hello", args=(), exc_info=None
    )
    token = observability.set_request_id("req-abcdef0123456789")
    try:
        observability.RequestIdFilter().filter(record)
        assert record.request_id == "req-abcdef0123456789"
    finally:
        observability.reset_request_id(token)


# ------------------------------------------------------------- c PRAGMA 诊断
def test_sqlite_diagnostics_reports_real_pragmas(env):
    info = asyncio.run(db_diagnostics.collect_sqlite_diagnostics(env.engine))
    assert info["driver"] == "sqlite"
    assert info["pragmas"]["journal_mode"] in {"delete", "wal", "memory"}
    assert info["pragmas"]["busy_timeout"] is not None
    assert info["errors"] == {}


# --------------------------------------------------------------- e 模块开关
def test_module_flags_parse_and_unknown_names():
    assert module_flags.parse_disabled_modules("") == set()
    assert module_flags.parse_disabled_modules(" enterprise , billing ") == {"enterprise", "billing"}
    # 未登记的名字被丢弃（会被 warning 记录），不会让所有模块莫名关闭
    assert module_flags.parse_disabled_modules("enterpirse") == set()
    assert module_flags.module_enabled("billing", disabled={"billing"}) is False
    assert module_flags.module_enabled("billing", disabled=set()) is True


# ------------------------------------------------------- f 分享链接加固（服务层）
async def _seed_share(env: Env, *, password: str | None) -> tuple[uuid.UUID, uuid.UUID]:
    async with env.maker() as db:
        user = app.models.User(
            id=uuid.uuid4(),
            username=f"u{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@lab.local",
            password_hash=hash_password("pw123456"),
            role="user",
            status="active",
        )
        db.add(user)
        await db.flush()
        org_id = uuid.uuid4()
        stored = StoredFile(
            id=uuid.uuid4(),
            original_name="a.txt",
            stored_name=f"{uuid.uuid4().hex}.txt",
            file_size=3,
            mime_type="text/plain",
            is_private=False,
            created_by=user.id,
            organization_id=org_id,
        )
        db.add(stored)
        await db.flush()
        link = FileShareLink(
            id=uuid.uuid4(),
            organization_id=org_id,
            file_id=stored.id,
            creator_id=user.id,
            token=uuid.uuid4().hex,
            password_hash=(hash_password(password) if password else None),
            status="active",
            allow_download=True,
            visit_count=0,
        )
        db.add(link)
        await db.commit()
        return link.id, stored.id


def test_share_password_hash_and_verify_roundtrip():
    hashed = hash_password("p@ssw0rd")
    assert hashed.startswith("$2")
    assert enterprise_admin_service._verify_share_password(hashed, "p@ssw0rd") is True
    assert enterprise_admin_service._verify_share_password(hashed, "wrong") is False
    # 历史明文行仍可访问（并在成功后升级为哈希）
    assert enterprise_admin_service._verify_share_password("明文口令", "明文口令") is True
    assert enterprise_admin_service._verify_share_password("明文口令", "别的") is False


def test_share_access_denies_and_throttles(env, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "share_access_max_failures", 2)
    link_id, file_id = asyncio.run(_seed_share(env, password="hash:" + hash_password("right")))

    async def attempt(pw: str):
        async with env.maker() as db:
            return await enterprise_admin_service.access_share_link(
                db, link_id, password=pw, ip_hash="iphash", want_download=False
            )

    # 第一次错：403 + 记 access_denied
    with pytest.raises(Exception) as first:
        asyncio.run(attempt("wrong"))
    assert getattr(first.value, "status_code", None) == 403

    async def count_denied():
        async with env.maker() as db:
            rows = (await db.execute(select(FileAccessLog).where(FileAccessLog.action == "access_denied"))).scalars().all()
            return len(rows)

    assert asyncio.run(count_denied()) == 1

    # 第二次错：达到阈值后下一次直接 429
    with pytest.raises(Exception):
        asyncio.run(attempt("wrong2"))
    with pytest.raises(Exception) as throttled:
        asyncio.run(attempt("wrong3"))
    assert getattr(throttled.value, "status_code", None) == 429

    async def audit_actions():
        async with env.maker() as db:
            rows = (await db.execute(select(AuditLog.action))).scalars().all()
            return list(rows)

    actions = asyncio.run(audit_actions())
    assert "share_access_denied" in actions
    assert "share_access_throttled" in actions


# --------------------------------------------------- g 晚到结果人工处置
def _seed_pending_job(env: Env, user_id: uuid.UUID) -> uuid.UUID:
    async def run():
        async with env.maker() as db:
            job = NovelPlanJob(
                id=uuid.uuid4(),
                user_id=user_id,
                status=st.STATUS_INTERRUPTED,
                mode="mock",
                request_json=json.dumps({"idea": "x"}),
                result_review_status=st.RESULT_REVIEW_PENDING,
                late_result_json=json.dumps({"title_candidates": ["晚到的标题"]}),
                late_result_reason="lease_expired",
                late_result_at=datetime.utcnow(),
                error_code="lease_expired",
                created_at=datetime.utcnow(),
                attempt_count=1,
                max_attempts=1,
            )
            db.add(job)
            await db.commit()
            return job.id

    return asyncio.run(run())


def test_result_review_api_accepts_once(env):
    from app.routers import ai_plan_jobs as router_module

    owner = uuid.uuid4()
    job_id = _seed_pending_job(env, owner)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_db] = lambda: None

    async def _db():
        async with env.maker() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[router_module.get_current_user] = lambda: type("U", (), {"id": owner})()
    client = TestClient(app)

    first = client.post(f"/api/ai/plan-novel/jobs/{job_id}/result-review", json={"decision": "accepted", "note": "看过"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == st.STATUS_INTERRUPTED       # 终态未被改写
    assert body["error_code"] == "lease_expired"          # 原因未被改写
    assert body["result_review_status"] == "accepted"
    assert body["result_review_note"] == "看过"

    second = client.post(f"/api/ai/plan-novel/jobs/{job_id}/result-review", json={"decision": "discarded"})
    assert second.status_code == 409

    async def audit_meta():
        async with env.maker() as db:
            row = (
                await db.execute(select(AuditLog).where(AuditLog.action == "plan_result_reviewed"))
            ).scalars().first()
            return json.loads(row.metadata_json or "{}")

    meta = asyncio.run(audit_meta())
    assert meta["decision"] == "accepted"
    assert "request_id" in meta


def test_result_review_rejects_other_user(env):
    from app.routers import ai_plan_jobs as router_module

    owner = uuid.uuid4()
    job_id = _seed_pending_job(env, owner)
    app = FastAPI()
    app.include_router(router_module.router)

    async def _db():
        async with env.maker() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[router_module.get_current_user] = lambda: type("U", (), {"id": uuid.uuid4()})()
    resp = TestClient(app).post(f"/api/ai/plan-novel/jobs/{job_id}/result-review", json={"decision": "accepted"})
    assert resp.status_code == 404


# ------------------------------------------------------------ d 脚本可用性
def test_consistent_backup_roundtrip(env):
    from scripts import consistent_backup

    src = env.path
    with tempfile.TemporaryDirectory(prefix="bk_") as tmp:
        target = Path(tmp) / "copy.db"
        result = consistent_backup.backup_database(src, target)
        assert result["source_unchanged"] is True
        assert result["backup_integrity"] == "ok"
        assert result["counts_match"] is True
        drill = consistent_backup.restore_drill(target)
        assert drill["ok"] is True


def test_repo_scripts_present():
    root = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("consistent_backup.py", "verify_migration.py", "deploy_drill.py"):
        assert (root / name).exists(), name


# --------------------------------- d 迁移历史兼容路径（缺表补建）覆盖
def test_ensure_table_before_migrations_creates_missing_table(tmp_path):
    """旧备份可能早于 novel_plan_jobs 建表：补建后 v7 的 ALTER 才有对象可加列。"""
    from sqlalchemy.ext.asyncio import create_async_engine as _engine

    from app.core.novel_plan_job_schema import (
        NOVEL_PLAN_JOB_COLUMNS,
        ensure_table_before_migrations,
    )
    from app.core.migrate import PHASE24_MIGRATION_STATEMENTS

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    con.execute("CREATE TABLE generation_records (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()

    async def run():
        eng = _engine(f"sqlite+aiosqlite:///{db}")
        try:
            async with eng.connect() as conn:
                created = await ensure_table_before_migrations(conn)
                await conn.commit()
                cols = [
                    row[1]
                    for row in (
                        await conn.exec_driver_sql("PRAGMA table_info(novel_plan_jobs)")
                    ).fetchall()
                ]
                # 建出的表必须能承受 v7 的 ADD COLUMN（重复列被迁移容忍，但不该再来一次）
                names = {c[0] for c in NOVEL_PLAN_JOB_COLUMNS}
                return created, cols, names
        finally:
            await eng.dispose()

    created, cols, expected = asyncio.run(run())
    assert created is True
    assert set(cols) == expected
    assert "lease_expires_at" in cols and "result_review_status" in cols
    # v7 的列都在补建结果里，ALTER 阶段只会命中 “duplicate column” 容忍分支
    for stmt in PHASE24_MIGRATION_STATEMENTS:
        if "ADD COLUMN" in stmt.upper():
            col = stmt.split("ADD COLUMN", 1)[1].split()[0]
            assert col in cols, col


def test_ensure_table_before_migrations_is_noop_when_present(env):
    from app.core.novel_plan_job_schema import ensure_table_before_migrations

    async def run():
        async with env.engine.connect() as conn:
            return await ensure_table_before_migrations(conn)

    assert asyncio.run(run()) is False
