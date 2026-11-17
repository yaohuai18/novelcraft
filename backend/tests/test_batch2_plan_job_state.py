"""第二批回归：规划任务状态机 / 租约 / 幂等 / 取消 / 晚到结果。

全部离线：不联网、不读正式库、不需要 API Key。每个用例独立临时 SQLite。
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部 ORM（create_all 需要）
from app.core.database import Base
from app.models.novel_plan_job import NovelPlanJob
from app.services import novel_plan_job_service as svc
from app.services import novel_plan_job_state as st


# --------------------------------------------------------------------- helpers
class _Plan:
    title_candidates = ["测试标题"]
    volumes: list = []

    def model_dump(self, mode: str = "json") -> dict:  # noqa: ARG002
        return {"title_candidates": ["测试标题"], "volumes": []}


class _EmptyPlan:
    title_candidates: list = []

    def model_dump(self, mode: str = "json") -> dict:  # noqa: ARG002
        return {"title_candidates": []}


class Env:
    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="npj_", ignore_cleanup_errors=True)
        self.path = Path(self.dir.name) / "lab.db"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        self.maker = async_sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )
        self._orig = svc.AsyncSessionLocal

    async def setup(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
        self.dir.cleanup()

    def install(self) -> None:
        svc.AsyncSessionLocal = self.maker  # type: ignore[assignment]

    def restore(self) -> None:
        svc.AsyncSessionLocal = self._orig  # type: ignore[assignment]


@pytest.fixture()
def env(monkeypatch):
    e = Env()
    asyncio.run(e.setup())
    e.install()
    svc._RUNNING.clear()
    try:
        yield e
    finally:
        e.restore()
        asyncio.run(e.dispose())


PAYLOAD = {
    "idea": "一个关于时间循环的故事",
    "genre": "悬疑",
    "chapter_count": 12,
    "words_per_chapter": 1500,
}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


async def _seed(e: Env, **fields) -> uuid.UUID:
    job_id = _uuid()
    defaults = dict(
        id=job_id,
        user_id=_uuid(),
        status=st.STATUS_QUEUED,
        request_json="{}",
        progress=0,
        created_at=datetime.utcnow(),
        attempt_count=0,
        max_attempts=1,
        idempotency_scope="draft:test",
    )
    defaults.update(fields)
    async with e.maker() as db:
        db.add(NovelPlanJob(**defaults))
        await db.commit()
    return job_id


async def _load(e: Env, job_id: uuid.UUID) -> NovelPlanJob:
    async with e.maker() as db:
        rows = await db.execute(
            NovelPlanJob.__table__.select().where(NovelPlanJob.__table__.c.id == job_id)
        )
        row = rows.mappings().one()
        job = NovelPlanJob()
        for key, value in row.items():
            setattr(job, key, value)
        return job


# --------------------------------------------------------------------- 1) 领取
def test_two_workers_claim_same_job_only_one_wins(env) -> None:
    job_id = asyncio.run(_seed(env))

    async def scenario() -> tuple[bool, bool]:
        async def one(worker: str) -> bool:
            async with env.maker() as db:
                return await svc.claim_job(db, job_id, worker)

        return await asyncio.gather(one("w-a"), one("w-b"))

    a, b = asyncio.run(scenario())
    assert sorted([a, b]) == [False, True]
    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_RUNNING
    assert job.attempt_count == 1
    assert job.lease_expires_at is not None


# --------------------------------------------------------------------- 2) 幂等
def test_same_idempotency_key_returns_same_job(env) -> None:
    user = _uuid()

    async def scenario():
        async with env.maker() as db:
            first, created1 = await svc.create_job(
                db, user_id=user, request_payload=PAYLOAD, mode="mock", idempotency_key="k-1"
            )
        async with env.maker() as db:
            second, created2 = await svc.create_job(
                db, user_id=user, request_payload=PAYLOAD, mode="mock", idempotency_key="k-1"
            )
        return first, created1, second, created2

    first, created1, second, created2 = asyncio.run(scenario())
    assert created1 is True
    assert created2 is False
    assert first.id == second.id


def test_concurrent_same_idempotency_key_collapses_to_one(env) -> None:
    user = _uuid()

    async def one():
        async with env.maker() as db:
            return await svc.create_job(
                db, user_id=user, request_payload=PAYLOAD, mode="mock", idempotency_key="k-2"
            )

    async def scenario():
        return await asyncio.gather(one(), one(), one())

    results = asyncio.run(scenario())
    ids = {job.id for job, _ in results}
    assert len(ids) == 1
    assert sum(1 for _, created in results if created) == 1


# --------------------------------------------------------- 3) 同作用域活跃任务
def test_active_job_reused_for_same_scope(env) -> None:
    user = _uuid()
    scope = svc.scope_of(PAYLOAD)

    async def scenario():
        async with env.maker() as db:
            first, created1 = await svc.create_job(
                db, user_id=user, request_payload=PAYLOAD, mode="mock"
            )
        async with env.maker() as db:
            second, created2 = await svc.create_job(
                db, user_id=user, request_payload=PAYLOAD, mode="mock"
            )
        return first, created1, second, created2, scope

    first, created1, second, created2, scope = asyncio.run(scenario())
    assert created1 is True and created2 is False
    assert first.id == second.id
    assert first.idempotency_scope == scope


# ------------------------------------------------------- 4-6) 重启与租约恢复
def test_queued_survives_restart_and_is_claimable(env) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_QUEUED))

    async def scenario():
        stats = await svc.recover_stale_jobs()
        job = await _load(env, job_id)
        async with env.maker() as db:
            claimed = await svc.claim_next_job(db, "w-restart")
        return stats, job, claimed

    stats, job, claimed = asyncio.run(scenario())
    assert stats["expired"] == 0
    assert stats["queued"] == 1
    assert job.status == st.STATUS_QUEUED
    assert claimed == job_id


def test_running_with_live_lease_not_interrupted(env) -> None:
    job_id = asyncio.run(
        _seed(
            env,
            status=st.STATUS_RUNNING,
            worker_id="w-alive",
            lease_expires_at=datetime.utcnow() + timedelta(seconds=90),
            heartbeat_at=datetime.utcnow(),
        )
    )

    async def scenario():
        stats = await svc.recover_stale_jobs()
        return stats, await _load(env, job_id)

    stats, job = asyncio.run(scenario())
    assert stats["expired"] == 0
    assert job.status == st.STATUS_RUNNING


def test_running_with_expired_lease_interrupted(env) -> None:
    job_id = asyncio.run(
        _seed(
            env,
            status=st.STATUS_RUNNING,
            worker_id="w-dead",
            lease_expires_at=datetime.utcnow() - timedelta(seconds=30),
        )
    )

    async def scenario():
        stats = await svc.recover_stale_jobs()
        return stats, await _load(env, job_id)

    stats, job = asyncio.run(scenario())
    assert stats["expired"] == 1
    assert job.status == st.STATUS_INTERRUPTED
    assert job.error_code == st.ERR_LEASE_EXPIRED


# ------------------------------------------------------- 7) 失租后不能写成功
def test_after_lease_lost_cannot_finalize_succeeded(env) -> None:
    job_id = asyncio.run(
        _seed(
            env,
            status=st.STATUS_RUNNING,
            worker_id="w-lost",
            lease_expires_at=datetime.utcnow() - timedelta(seconds=5),
        )
    )

    async def scenario():
        return await svc._finalize(  # noqa: SLF001
            job_id, worker="w-lost", status=st.STATUS_SUCCEEDED, result_payload={"x": 1}
        )

    assert asyncio.run(scenario()) is False
    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_RUNNING  # 没被写成 succeeded
    assert job.result_json is None


# ----------------------------------------------------- 8-9) 晚到结果与取消
def test_late_result_preserved_as_pending_review(env) -> None:
    job_id = asyncio.run(
        _seed(env, status=st.STATUS_INTERRUPTED, worker_id="w-old", error_code="lease_expired")
    )

    async def scenario():
        ok = await svc.store_late_result(
            job_id,
            result_payload={"mode": "mock", "plan": {"title_candidates": ["迟到"]}},
            reason="lease_expired",
            prompt_tokens=11,
            completion_tokens=22,
        )
        return ok, await _load(env, job_id)

    ok, job = asyncio.run(scenario())
    assert ok is True
    # 2026-11-12 语义修正：原始终态必须保留，晚到结果走独立字段
    assert job.status == st.STATUS_INTERRUPTED
    assert job.error_code == "lease_expired"  # 原始失败原因未被覆盖
    assert job.result_review_status == st.RESULT_REVIEW_PENDING
    assert job.late_result_json and "迟到" in job.late_result_json
    assert job.late_result_reason == "lease_expired"
    assert job.result_json is None  # 不写结果主字段，避免被当成正常成功
    assert job.worker_id == "w-old"  # 原执行者保留
    payload = svc.serialize_job(job)
    assert payload["status"] == st.STATUS_INTERRUPTED
    assert payload["result_review_status"] == st.RESULT_REVIEW_PENDING
    assert payload["has_late_result"] is True
    assert payload["plan"]["title_candidates"][0] == "迟到"
    assert payload["result_source"] == "late_result"


def test_cancel_paths_and_cancel_requested_late_result(env) -> None:
    user = _uuid()
    queued_id = asyncio.run(_seed(env, user_id=user, status=st.STATUS_QUEUED))
    running_id = asyncio.run(
        _seed(
            env,
            user_id=user,
            status=st.STATUS_RUNNING,
            worker_id="w-run",
            lease_expires_at=datetime.utcnow() + timedelta(seconds=60),
        )
    )

    async def scenario():
        async with env.maker() as db:
            s1, c1 = await svc.request_cancel(db, job_id=queued_id, user_id=user)
        async with env.maker() as db:
            s2, c2 = await svc.request_cancel(db, job_id=running_id, user_id=user)
        async with env.maker() as db:
            s3, c3 = await svc.request_cancel(db, job_id=running_id, user_id=user)
        saved = await svc.store_late_result(
            running_id,
            result_payload={"mode": "mock", "plan": {"title_candidates": ["取消后到达"]}},
            reason=st.STATUS_CANCEL_REQUESTED,
        )
        return (s1, c1, s2, c2, s3, c3, saved, await _load(env, running_id))

    s1, c1, s2, c2, s3, c3, saved, job = asyncio.run(scenario())
    assert (s1, c1) == (st.STATUS_CANCELLED, True)
    assert (s2, c2) == (st.STATUS_CANCEL_REQUESTED, True)
    assert (s3, c3) == (st.STATUS_CANCEL_REQUESTED, False)  # 幂等
    assert saved is True
    # 仍处于 cancel_requested（worker 之后才落 cancelled），晚到结果不改变状态
    assert job.status == st.STATUS_CANCEL_REQUESTED
    assert job.result_review_status == st.RESULT_REVIEW_PENDING
    assert job.late_result_reason == st.STATUS_CANCEL_REQUESTED


# --------------------------------------------------- 10) provider 失败与空结果
def test_empty_result_marks_failed(env, monkeypatch) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_QUEUED, request_json="{}"))

    async def fake_empty(**_kwargs):
        return _EmptyPlan()

    monkeypatch.setattr(svc, "ai_plan_novel", fake_empty)
    asyncio.run(svc.run_job(job_id))

    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_FAILED
    assert job.error_code == st.ERR_EMPTY_RESULT


def test_provider_exception_marks_failed_with_code(env, monkeypatch) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_QUEUED, request_json="{}"))

    async def fake_fail(**_kwargs):
        raise RuntimeError("ai_provider_timeout: 模型响应超时")

    monkeypatch.setattr(svc, "ai_plan_novel", fake_fail)
    asyncio.run(svc.run_job(job_id))

    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_FAILED
    assert job.error_code == "ai_provider_timeout"


def test_success_path_writes_result_and_attempt(env, monkeypatch) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_QUEUED, request_json="{}", mode="mock"))

    async def fake_ok(**_kwargs):
        return _Plan()

    monkeypatch.setattr(svc, "ai_plan_novel", fake_ok)
    asyncio.run(svc.run_job(job_id))

    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_SUCCEEDED
    assert job.attempt_count == 1
    assert job.finished_at is not None
    assert job.result_json and "测试标题" in job.result_json


# ------------------------------------- 12b) 执行中取消：终态必须落到 cancelled
def test_cancel_during_running_reaches_cancelled(env, monkeypatch) -> None:
    """用户在执行中点取消：不能永远停在 cancel_requested。

    演练（deploy-drill A）暴露的真实缺陷：mock 生成结束后没有任何代码
    把 cancel_requested 推进到 cancelled，前端会一直显示“正在取消”。
    """
    owner = _uuid()
    job_id = asyncio.run(_seed(env, user_id=owner, status=st.STATUS_QUEUED, request_json="{}", mode="mock"))

    async def fake_slow(**_kwargs):
        # 模拟“用户在这段时间里点了取消”
        async with env.maker() as other:
            await svc.request_cancel(other, job_id=job_id, user_id=owner)
        return _Plan()

    monkeypatch.setattr(svc, "ai_plan_novel", fake_slow)
    asyncio.run(svc.run_job(job_id))

    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_CANCELLED
    assert job.error_code == st.ERR_CANCELLED_BY_USER
    assert job.finished_at is not None
    # 已经产生的规划不丢：作为晚到结果保留待复核
    assert job.result_review_status == st.RESULT_REVIEW_PENDING
    assert job.late_result_json and "测试标题" in job.late_result_json
    assert job.result_json is None


# ------------------------------------------------ 11) AI 调用期间不持有写事务
def test_no_write_transaction_held_during_ai_call(env, monkeypatch) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_QUEUED, request_json="{}", mode="mock"))

    async def fake_slow(**_kwargs):
        # AI 调用期间，另一个会话应当能立刻完成一次写入（不被长事务锁住）
        async with env.maker() as other:
            await other.execute(
                NovelPlanJob.__table__.update()
                .where(NovelPlanJob.__table__.c.id == job_id)
                .values(progress=42)
            )
            await asyncio.wait_for(other.commit(), timeout=1.0)
        return _Plan()

    monkeypatch.setattr(svc, "ai_plan_novel", fake_slow)
    asyncio.run(svc.run_job(job_id))

    job = asyncio.run(_load(env, job_id))
    assert job.status == st.STATUS_SUCCEEDED


# ---------------------------------------------------------- 12) 越权读/取消
def test_cross_user_read_and_cancel_rejected(env) -> None:
    owner = _uuid()
    intruder = _uuid()
    job_id = asyncio.run(_seed(env, user_id=owner, status=st.STATUS_QUEUED))

    async def scenario():
        async with env.maker() as db:
            read = await svc.get_job(db, job_id=job_id, user_id=intruder)
        async with env.maker() as db:
            status_value, changed = await svc.request_cancel(
                db, job_id=job_id, user_id=intruder
            )
        return read, status_value, changed, await _load(env, job_id)

    read, status_value, changed, job = asyncio.run(scenario())
    assert read is None
    assert status_value is None and changed is False
    assert job.status == st.STATUS_QUEUED  # 未被越权取消


# ------------------------------------------------- 13) 序列化保持向后兼容
def test_serialize_keeps_legacy_fields(env) -> None:
    job_id = asyncio.run(_seed(env, status=st.STATUS_FAILED, error_code="x"))
    job = asyncio.run(_load(env, job_id))
    payload = svc.serialize_job(job)

    for legacy in (
        "job_id",
        "status",
        "mode",
        "progress",
        "elapsed_seconds",
        "error_code",
        "error_message",
        "created_at",
        "started_at",
        "completed_at",
    ):
        assert legacy in payload
    for added in (
        "heartbeat_at",
        "lease_expires_at",
        "finished_at",
        "attempt_count",
        "can_retry",
        "can_cancel",
        "result_review_status",
        "late_result_reason",
        "late_result_at",
        "has_late_result",
        "result_pending_review",  # 老前端兼容镜像
    ):
        assert added in payload
    assert payload["result_review_status"] == st.RESULT_REVIEW_NONE
    assert payload["can_retry"] is True



# --------------------------------- 8b) 晚到结果：cancelled 终态同样保留
def test_cancelled_late_result_keeps_terminal_status(env) -> None:
    job_id = asyncio.run(
        _seed(
            env,
            status=st.STATUS_CANCELLED,
            worker_id="w-gone",
            error_code=st.ERR_CANCELLED_BY_USER,
        )
    )

    async def scenario():
        ok = await svc.store_late_result(
            job_id,
            result_payload={"mode": "mock", "plan": {"title_candidates": ["取消后到达"]}},
            reason=st.ERR_CANCELLED_BY_USER,
        )
        return ok, await _load(env, job_id)

    ok, job = asyncio.run(scenario())
    assert ok is True
    assert job.status == st.STATUS_CANCELLED  # 原始终态未被抹掉
    assert job.error_code == st.ERR_CANCELLED_BY_USER
    assert job.result_review_status == st.RESULT_REVIEW_PENDING
    assert job.late_result_reason == st.ERR_CANCELLED_BY_USER
    payload = svc.serialize_job(job)
    assert payload["status"] == st.STATUS_CANCELLED
    assert payload["result_review_status"] == st.RESULT_REVIEW_PENDING
    assert payload["result_pending_review"] is True
    assert payload["can_retry"] is True


def test_late_result_is_idempotent(env) -> None:
    job_id = asyncio.run(
        _seed(env, status=st.STATUS_INTERRUPTED, worker_id="w-old", error_code="lease_expired")
    )

    async def scenario():
        first = await svc.store_late_result(
            job_id, result_payload={"mode": "mock", "plan": {"title_candidates": ["A"]}},
            reason="lease_expired",
        )
        second = await svc.store_late_result(
            job_id, result_payload={"mode": "mock", "plan": {"title_candidates": ["B"]}},
            reason="lease_expired",
        )
        return first, second, await _load(env, job_id)

    first, second, job = asyncio.run(scenario())
    assert (first, second) == (True, False)  # 第二次不再覆盖
    assert "A" in (job.late_result_json or "")
    assert "B" not in (job.late_result_json or "")


# ------------------------------------------------------------- 14) 迁移幂等
def test_migration_v7_idempotent_on_isolated_sqlite() -> None:
    from app.core.migrate import PHASE24_MIGRATION_STATEMENTS

    # v6 时期的旧表结构（ensure DDL 已是 v7 形态，不能拿来当起点）
    legacy_ddl = """
    CREATE TABLE novel_plan_jobs (
        id CHAR(32) NOT NULL,
        user_id CHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'queued',
        mode VARCHAR(8),
        request_json TEXT NOT NULL,
        result_json TEXT,
        error_code VARCHAR(64),
        error_message TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        worker_id VARCHAR(64),
        heartbeat_at DATETIME,
        created_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        PRIMARY KEY (id)
    )
    """

    with tempfile.TemporaryDirectory(prefix="npj_mig_") as tmp:
        path = Path(tmp) / "m.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(legacy_ddl)
            for statement in PHASE24_MIGRATION_STATEMENTS:
                conn.execute(statement)
            conn.commit()
            # 迁移网关会容忍 duplicate column；这里只验证索引语句本身幂等
            for statement in PHASE24_MIGRATION_STATEMENTS:
                if "CREATE" in statement.upper():
                    conn.execute(statement)
            conn.commit()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(novel_plan_jobs)")}
            assert {
                "lease_expires_at",
                "attempt_count",
                "max_attempts",
                "cancel_requested_at",
                "finished_at",
                "idempotency_key",
                "idempotency_scope",
                "result_conflict_reason",
                "result_review_status",
                "late_result_json",
                "late_result_reason",
                "late_result_at",
            } <= cols
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(novel_plan_jobs)")}
            assert "uq_novel_plan_jobs_idem" in indexes
            assert "ix_novel_plan_jobs_lease" in indexes
            assert "ix_novel_plan_jobs_result_review" in indexes
        finally:
            conn.close()
