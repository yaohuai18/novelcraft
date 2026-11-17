# -*- coding: utf-8 -*-
"""阶段 1 A1 回归：内容审计写入的事务隔离与失败可观察性。

覆盖：
    A1-1 审计 flush 失败不向调用方抛出，返回 failed 结果并登记失败记录；
    A1-2 savepoint 回滚后，同一 Session 中已挂起的业务改动仍能成功 commit；
    A1-3 失败记录可查询，且包含 action / 项目 / 章节 / 错误类型 / 错误摘要 / 时间；
    A1-3b 失败缓冲有界（FIFO 覆盖最旧），累计计数不受上限截断；
    A1-4 开关关闭（content_audit_enabled=False）与「无字段变更」可区分且不写库；
    A1-5 record_content_audit / record_backfill_audit 默认 commit=False（不提交调用方事务）；
    A1-6 正常审计随调用方事务一并提交；
    A1-7 本文件只使用仓库外临时 SQLite，不触碰 backend/novelcraft.db。

硬约束：
    - 不启动服务、不监听端口、不发起网络请求（tests/conftest.py 离线护栏生效）；
    - 仅使用临时 SQLite 文件库 + ORM create_all，不执行迁移、不连接正式库；
    - 不修改 chapter_lifecycle / version_service / publication_service 等批次外文件。

Run: cd backend && python -m pytest tests/test_phase1_audit_reliability.py -q
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.config import settings
from app.core.database import Base
from app.models.audit_log import AuditLog
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services import audit_service

BACKEND_DIR = Path(__file__).resolve().parents[1]
FORMAL_DB = BACKEND_DIR / "novelcraft.db"

_TRIGGER_NAME = "a1_block_audit_write"


# ---------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库（NullPool：每个 Session 独立连接，可观测提交可见性）。"""
    db_dir = tmp_path_factory.mktemp("phase1_audit_reliability")
    db_path = db_dir / "phase1_audit_reliability.db"

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _prepare() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())

    try:
        yield {"engine": engine, "SessionLocal": session_factory, "db_path": db_path}
    finally:
        asyncio.run(engine.dispose())


async def _seed(env: dict[str, Any], suffix: str) -> dict[str, Any]:
    """每个用例独立种子：user + project + outline + chapter_content。"""
    async with env["SessionLocal"]() as db:
        owner = User(
            username=f"a1_owner_{suffix}",
            email=f"a1_owner_{suffix}@example.com",
            password_hash="x",
        )
        db.add(owner)
        await db.flush()
        project = Project(user_id=owner.id, title=f"A1 项目 {suffix}")
        db.add(project)
        await db.flush()
        outline = Outline(
            project_id=project.id,
            title=f"A1 章节 {suffix}",
            outline_type="chapter",
            lifecycle_status="generated",
        )
        db.add(outline)
        await db.flush()
        chapter = ChapterContent(
            outline_id=outline.id,
            content="原始正文",
            lifecycle_status="generated",
            version=1,
        )
        db.add(chapter)
        await db.commit()
        return {
            "project_id": project.id,
            "outline_id": outline.id,
            "chapter_id": chapter.id,
        }


def _seed_sync(env: dict[str, Any], suffix: str) -> dict[str, Any]:
    return asyncio.run(_seed(env, suffix))


async def _install_failing_trigger(env: dict[str, Any], action: str) -> None:
    """在测试库上注入 DB 级故障：匹配 action 的审计插入一律失败（不涉及生产 schema）。"""
    async with env["engine"].begin() as conn:
        await conn.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_NAME} "
            "BEFORE INSERT ON audit_logs "
            f"WHEN NEW.action = '{action}' "
            "BEGIN SELECT RAISE(ABORT, 'a1 injected audit write failure'); END"
        )


async def _drop_failing_trigger(env: dict[str, Any]) -> None:
    async with env["engine"].begin() as conn:
        await conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")


def _install_trigger_sync(env: dict[str, Any], action: str) -> None:
    asyncio.run(_install_failing_trigger(env, action))


def _drop_trigger_sync(env: dict[str, Any]) -> None:
    asyncio.run(_drop_failing_trigger(env))


async def _count_logs(
    env: dict[str, Any], action: str | None = None, target_id: Any = None
) -> int:
    async with env["SessionLocal"]() as db:
        stmt = select(func.count(AuditLog.id))
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if target_id is not None:
            stmt = stmt.where(AuditLog.target_id == str(target_id))
        return (await db.execute(stmt)).scalar_one()


def _count_logs_sync(
    env: dict[str, Any], action: str | None = None, target_id: Any = None
) -> int:
    return asyncio.run(_count_logs(env, action, target_id))


async def _load_project_title(env: dict[str, Any], project_id: Any) -> str:
    async with env["SessionLocal"]() as db:
        return (
            await db.execute(select(Project.title).where(Project.id == project_id))
        ).scalar_one()


def _project_title_sync(env: dict[str, Any], project_id: Any) -> str:
    return asyncio.run(_load_project_title(env, project_id))


# ---------------------------------------------------------------- 用例


def test_audit_flush_failure_is_isolated_from_business_transaction(env):
    """A1-1 / A1-2：审计 flush 失败不抛出；savepoint 回滚后业务改动仍可提交。"""
    ids = _seed_sync(env, "flush_failure")
    audit_service.reset_audit_failures()
    _install_trigger_sync(env, audit_service.ACTION_CONTENT_SAVE)
    try:

        async def _run() -> dict[str, Any]:
            async with env["SessionLocal"]() as db:
                outline = (
                    await db.execute(
                        select(Outline).where(Outline.id == ids["outline_id"])
                    )
                ).scalar_one()
                chapter = (
                    await db.execute(
                        select(ChapterContent).where(
                            ChapterContent.id == ids["chapter_id"]
                        )
                    )
                ).scalar_one()
                before = audit_service.snapshot_content_state(chapter, outline)

                # 先挂起业务改动（正文 + 标题），再让审计写入失败
                chapter.content = "已保存的新正文"
                chapter.version = 2
                outline.title = "A1-业务改动已落库"
                project = (
                    await db.execute(
                        select(Project).where(Project.id == ids["project_id"])
                    )
                ).scalar_one()
                project.title = "A1-业务改动已落库"

                after = audit_service.snapshot_content_state(chapter, outline)
                result = await audit_service.record_content_audit_result(
                    db,
                    action=audit_service.ACTION_CONTENT_SAVE,
                    project_id=ids["project_id"],
                    target_id=ids["outline_id"],
                    before=before,
                    after=after,
                )

                # 关键：异常被隔离在 savepoint 内，这里必须还能提交
                await db.commit()
                return {"result": result}

        payload = asyncio.run(_run())
        result = payload["result"]

        assert result.status == audit_service.AUDIT_STATUS_FAILED
        assert result.log is None
        assert result.failure is not None
        assert result.failure["error_type"] == "IntegrityError"

        # 业务行真的落库了（另一条连接可见）
        assert _project_title_sync(env, ids["project_id"]) == "A1-业务改动已落库"
        # 审计行确实没写进去
        assert (
            _count_logs_sync(env, audit_service.ACTION_CONTENT_SAVE, ids["outline_id"]) == 0
        )
        assert audit_service.audit_failure_count() == 1
    finally:
        _drop_trigger_sync(env)


def test_audit_failure_record_queryable_with_context(env):
    """A1-3：失败记录可查询，包含 action / 项目 / 章节 / 错误类型 / 摘要 / 时间。"""
    ids = _seed_sync(env, "failure_record")
    audit_service.reset_audit_failures()
    _install_trigger_sync(env, audit_service.ACTION_CONTENT_RESTORE)
    try:

        async def _run() -> Any:
            async with env["SessionLocal"]() as db:
                outline = (
                    await db.execute(
                        select(Outline).where(Outline.id == ids["outline_id"])
                    )
                ).scalar_one()
                chapter = (
                    await db.execute(
                        select(ChapterContent).where(
                            ChapterContent.id == ids["chapter_id"]
                        )
                    )
                ).scalar_one()
                before = audit_service.snapshot_content_state(chapter, outline)
                chapter.content = "恢复后的正文"
                chapter.version = 2
                after = audit_service.snapshot_content_state(chapter, outline)
                return await audit_service.record_content_audit_result(
                    db,
                    action=audit_service.ACTION_CONTENT_RESTORE,
                    project_id=ids["project_id"],
                    target_id=ids["outline_id"],
                    before=before,
                    after=after,
                )

        result = asyncio.run(_run())

        assert result.status == audit_service.AUDIT_STATUS_FAILED
        entries = audit_service.list_audit_failures()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == audit_service.ACTION_CONTENT_RESTORE
        assert entry["project_id"] == str(ids["project_id"])
        assert entry["target_id"] == str(ids["outline_id"])
        assert entry["target_type"] == audit_service.CONTENT_AUDIT_TARGET_TYPE
        assert entry["phase"] == "record_content_audit"
        assert entry["error_type"] == "IntegrityError"
        assert "a1 injected audit write failure" in entry["error_summary"]
        assert entry["at"].endswith("Z")
        assert entry["extra"]["content_version"] == 2
        assert result.failure == entry
    finally:
        _drop_trigger_sync(env)


def test_audit_failure_buffer_is_bounded(env):
    """A1-3b：失败缓冲有界（FIFO），累计计数不被上限截断。"""
    audit_service.reset_audit_failures()
    overflow = audit_service.AUDIT_FAILURE_LIMIT + 50
    for index in range(overflow):
        audit_service.record_audit_failure(
            action=audit_service.ACTION_CONTENT_SAVE,
            project_id="project-x",
            target_id=f"outline-{index}",
            phase="unit_probe",
            error=RuntimeError("probe failure"),
            extra={"seq": index},
        )

    assert len(audit_service.audit_failures) == audit_service.AUDIT_FAILURE_LIMIT
    assert audit_service.audit_failure_count() == overflow

    latest = audit_service.list_audit_failures(limit=3)
    assert len(latest) == 3
    # 新的在前：最后写入的 seq 是 overflow-1
    assert [item["extra"]["seq"] for item in latest] == [overflow - 1, overflow - 2, overflow - 3]

    audit_service.reset_audit_failures()
    assert audit_service.list_audit_failures() == []
    assert audit_service.audit_failure_count() == 0


def test_audit_disabled_switch_is_distinguishable(env, monkeypatch):
    """A1-4a：开关关闭 → skipped_disabled，不写库、不登记失败。"""
    ids = _seed_sync(env, "disabled")
    audit_service.reset_audit_failures()
    monkeypatch.setattr(settings, "content_audit_enabled", False)

    async def _run() -> Any:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == ids["outline_id"]))
            ).scalar_one()
            chapter = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == ids["chapter_id"])
                )
            ).scalar_one()
            before = audit_service.snapshot_content_state(chapter, outline)
            chapter.content = "开关关闭时的正文"
            after = audit_service.snapshot_content_state(chapter, outline)
            result = await audit_service.record_content_audit_result(
                db,
                action=audit_service.ACTION_CONTENT_SAVE,
                project_id=ids["project_id"],
                target_id=ids["outline_id"],
                before=before,
                after=after,
            )
            await db.commit()
            return result

    result = asyncio.run(_run())

    assert result.status == audit_service.AUDIT_STATUS_SKIPPED_DISABLED
    assert result.log is None
    assert result.failure is None
    assert _count_logs_sync(env, audit_service.ACTION_CONTENT_SAVE, ids["outline_id"]) == 0
    assert audit_service.audit_failure_count() == 0


def test_audit_no_field_change_is_distinguishable(env):
    """A1-4b：无字段变更 → skipped_unchanged，不写库、不登记失败。"""
    ids = _seed_sync(env, "no_change")
    audit_service.reset_audit_failures()

    async def _run() -> Any:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == ids["outline_id"]))
            ).scalar_one()
            chapter = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == ids["chapter_id"])
                )
            ).scalar_one()
            snapshot = audit_service.snapshot_content_state(chapter, outline)
            result = await audit_service.record_content_audit_result(
                db,
                action=audit_service.ACTION_CONTENT_SAVE,
                project_id=ids["project_id"],
                target_id=ids["outline_id"],
                before=snapshot,
                after=dict(snapshot),
            )
            await db.commit()
            return result

    result = asyncio.run(_run())

    assert result.status == audit_service.AUDIT_STATUS_SKIPPED_UNCHANGED
    assert result.log is None
    assert result.failure is None
    assert _count_logs_sync(env, audit_service.ACTION_CONTENT_SAVE, ids["outline_id"]) == 0
    assert audit_service.audit_failure_count() == 0


def test_record_content_audit_default_does_not_commit(env):
    """A1-5a：record_content_audit 默认 commit=False，不提交调用方事务。"""
    ids = _seed_sync(env, "no_commit")
    audit_service.reset_audit_failures()

    async def _run() -> dict[str, Any]:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == ids["outline_id"]))
            ).scalar_one()
            chapter = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == ids["chapter_id"])
                )
            ).scalar_one()
            before = audit_service.snapshot_content_state(chapter, outline)
            chapter.content = "未提交的正文"
            after = audit_service.snapshot_content_state(chapter, outline)
            log = await audit_service.record_content_audit(
                db,
                action=audit_service.ACTION_CONTENT_SAVE,
                project_id=ids["project_id"],
                target_id=ids["outline_id"],
                before=before,
                after=after,
            )
            visible_before_commit = await _count_logs(
                env, audit_service.ACTION_CONTENT_SAVE, ids["outline_id"]
            )
            await db.commit()
            return {"log": log, "visible_before_commit": visible_before_commit}

    payload = asyncio.run(_run())

    assert payload["log"] is not None
    # 调用方未提交时，另一条连接看不到该审计行
    assert payload["visible_before_commit"] == 0
    # 调用方提交后可见
    assert (
        _count_logs_sync(env, audit_service.ACTION_CONTENT_SAVE, ids["outline_id"]) == 1
    )


def test_record_backfill_audit_default_does_not_commit(env):
    """A1-5b：record_backfill_audit 默认 commit=False，不提交调用方事务。"""
    ids = _seed_sync(env, "backfill_no_commit")
    audit_service.reset_audit_failures()

    async def _run() -> dict[str, Any]:
        async with env["SessionLocal"]() as db:
            log = await audit_service.record_backfill_audit(
                db,
                project_id=ids["project_id"],
                outline_id=ids["outline_id"],
                backfill_id="a1-backfill-probe",
                summary={"inserted": 1},
            )
            visible_before_commit = await _count_logs(
                env, audit_service.ACTION_BACKFILL, ids["outline_id"]
            )
            await db.commit()
            return {"log": log, "visible_before_commit": visible_before_commit}

    payload = asyncio.run(_run())

    assert payload["log"] is not None
    assert payload["visible_before_commit"] == 0
    assert _count_logs_sync(env, audit_service.ACTION_BACKFILL, ids["outline_id"]) == 1


def test_normal_audit_commits_with_caller_transaction(env):
    """A1-6：正常审计写入随调用方一次 commit 一起落库，且归属信息完整。"""
    ids = _seed_sync(env, "normal_commit")
    audit_service.reset_audit_failures()

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == ids["outline_id"]))
            ).scalar_one()
            chapter = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == ids["chapter_id"])
                )
            ).scalar_one()
            before = audit_service.snapshot_content_state(chapter, outline)
            chapter.content = "正常保存的正文"
            chapter.version = 2
            after = audit_service.snapshot_content_state(chapter, outline)
            result = await audit_service.record_content_audit_result(
                db,
                action=audit_service.ACTION_CONTENT_SAVE,
                project_id=ids["project_id"],
                target_id=ids["outline_id"],
                before=before,
                after=after,
                source="manual",
            )
            assert result.status == audit_service.AUDIT_STATUS_WRITTEN
            assert result.written is True
            await db.commit()

    asyncio.run(_run())

    async def _load() -> list[AuditLog]:
        async with env["SessionLocal"]() as db:
            return list(
                (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.action == audit_service.ACTION_CONTENT_SAVE,
                            AuditLog.target_id == str(ids["outline_id"]),
                        )
                    )
                )
                .scalars()
                .all()
            )

    logs = asyncio.run(_load())
    assert len(logs) == 1
    log = logs[0]
    assert log.project_id == str(ids["project_id"])
    assert log.target_id == str(ids["outline_id"])
    assert log.content_version == 2
    assert json.loads(log.diff_json)["changed_fields"]
    assert audit_service.audit_failure_count() == 0


def test_audit_tests_do_not_touch_formal_database(env):
    """A1-7：测试库在仓库外临时目录，正式库文件大小与 mtime 不变。"""
    url = str(env["engine"].url)
    assert "novelcraft.db" not in url
    assert str(env["db_path"].parent) in url

    before = None
    if FORMAL_DB.exists():
        stat = FORMAL_DB.stat()
        before = (stat.st_size, stat.st_mtime_ns)

    ids = _seed_sync(env, "formal_db_guard")

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            await audit_service.record_backfill_audit(
                db,
                project_id=ids["project_id"],
                outline_id=ids["outline_id"],
                backfill_id="a1-formal-guard",
            )
            await db.commit()

    asyncio.run(_run())
    assert _count_logs_sync(env, audit_service.ACTION_BACKFILL) >= 1

    if before is not None:
        after_stat = FORMAL_DB.stat()
        assert (after_stat.st_size, after_stat.st_mtime_ns) == before
