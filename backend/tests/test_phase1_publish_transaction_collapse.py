# -*- coding: utf-8 -*-
"""U1b（方案 B）：章节发布成功路径事务收敛回归。

锁定目标：
    1. E1（作者端发布）成功路径业务 commit 恰好 1 次，且该次提交发生在发布通知之前；
    2. E1 通知失败（含 flush 失败）不改变 2xx，业务数据保持已提交；
    3. 索引 / 统计旁路失败不影响业务提交（savepoint 隔离保持有效）；
    4. E2（开放 API）业务 commit 恰好 1 次，``status`` 与 ``lifecycle_status`` 同事务落库；
    5. E2 门禁拒绝只提交拒绝审计，不产生 Publication 行、不写业务列；
    6. 内容体检拒绝路径不产生任何业务提交；
    7. ``commit=False`` 时 ``flush`` 后 Publication 字段（id / published_at）可直接序列化，
       且未提交时对其它会话不可见（证明真的只是 flush）；
    8. E3（``chapter_lifecycle.publish_chapter``）D4 提交语义保持现状，未新增 commit 参数；
    9. 结构保证：门禁先于 publication_service 调用与任何业务写入。

硬约束：
    - 进程内 ASGI（httpx.ASGITransport），不启服务、不监听端口；
    - 仓库外 pytest 临时目录 SQLite 文件库 + ORM create_all，不触碰 backend/novelcraft.db；
    - 不调用真实 AI、不发起网络请求；
    - 只做离线验证，不涉及 git add / commit。

Run: cd backend && python -m pytest tests/test_phase1_publish_transaction_collapse.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.api_errors import ApiError, api_error_handler
from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.publication import Publication
from app.models.user import User
from app.schemas.publication import PublicationCreate
from app.routers.chapter_contents import router as chapter_contents_router
from app.routers.openapi_chapters import guard_write
from app.routers.openapi_chapters import router as openapi_chapters_router
from app.routers.publications import router as publications_router
from app.services import (
    chapter_lifecycle,
    discover_service,
    event_service,
    index_service,
    publication_service,
)

E1_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/publish"
E2_PATH = "/api/v1/chapters/{chapter_id}/publish"
E3_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/lifecycle/publish"

VALID_REPORT = json.dumps(
    {"validated": True, "validation_anchor": {"content_version": 1}},
    ensure_ascii=False,
)

# 方案 B 目标：业务提交 1 次（快照 + 发布列 + 索引 + 统计），通知单独 1 次旁路提交。
BUSINESS_COMMITS = 1
E1_SUCCESS_COMMITS = BUSINESS_COMMITS + 1  # + 发布通知


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库 + 只挂三个发布入口路由的进程内 ASGI 应用。"""
    db_dir = tmp_path_factory.mktemp("phase1_publish_transaction_collapse")
    db_path = db_dir / "phase1_publish_transaction_collapse.db"

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=NullPool,  # 每个 asyncio.run 独立事件循环，连接不复用
    )
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _prepare() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(publications_router)
    app.include_router(openapi_chapters_router)
    app.include_router(chapter_contents_router)

    env_data: dict[str, Any] = {
        "app": app,
        "engine": engine,
        "SessionLocal": session_factory,
        "db_path": str(db_path),
    }
    try:
        yield env_data
    finally:
        asyncio.run(engine.dispose())


def _client(env: dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"], raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _post(env: dict[str, Any], url: str, body: dict[str, Any] | None = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(url, json=body if body is not None else {})

    return asyncio.run(_run())


@pytest.fixture(scope="module")
def data(env) -> dict[str, Any]:
    """种子数据：每个用例独占一个已审批章节，避免发布状态互相污染。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="u1b_tx_owner",
                email="u1b_tx_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()

            project = Project(user_id=owner.id, title="U1b 事务收敛项目")
            db.add(project)
            await db.flush()

            keys = [
                "e1_ok",
                "e1_rollback_probe",
                "e1_notify_raise",
                "e1_notify_flush",
                "e1_index_fail",
                "e1_stats_fail",
                "e1_flush_only",
                "e2_ok",
                "e2_atomic",
                "e2_content_reject",
                "e3_ok",
            ]
            chapters: dict[str, Outline] = {}
            for key in keys:
                chapters[key] = Outline(
                    project_id=project.id,
                    title=f"U1b-{key}",
                    outline_type="chapter",
                    status="draft",
                    lifecycle_status="approved",
                    validation_report=VALID_REPORT,
                )
            # 门禁拒绝用：已下架（资源状态），且正文完整（证明 409 先于内容体检）
            chapters["e2_taken_down"] = Outline(
                project_id=project.id,
                title="U1b-e2_taken_down",
                outline_type="chapter",
                status="taken_down",
                lifecycle_status="approved",
                validation_report=VALID_REPORT,
            )
            db.add_all(list(chapters.values()))
            await db.flush()

            for key, outline in chapters.items():
                if key == "e2_content_reject":
                    continue  # 故意不写正文：用于验证内容拒绝无业务提交
                db.add(
                    ChapterContent(
                        outline_id=outline.id,
                        content=f"U1b {key} 正文内容",
                        word_count=10,
                        version=1,
                        lifecycle_status="approved",
                    )
                )
            await db.commit()

            ids.update(owner_id=owner.id, project_id=project.id)
            for key, outline in chapters.items():
                ids[key + "_id"] = outline.id
        return ids

    seeded = asyncio.run(_seed())

    env["app"].dependency_overrides[get_current_user] = (
        lambda: SimpleNamespace(id=seeded["owner_id"])
    )
    env["app"].dependency_overrides[guard_write] = lambda: {
        "principal": SimpleNamespace(
            user=SimpleNamespace(id=seeded["owner_id"]),
            token=SimpleNamespace(id=uuid.UUID(int=7), app_id=None),
        ),
        "rate": {"allowed": True, "limit": 100, "count": 1, "retry_after": 0},
        "client_class": "general",
    }
    return seeded


# --------------------------------------------------------------------- 辅助


def _url(template: str, data: dict[str, Any], chapter_key: str) -> str:
    return template.format(
        project_id=data["project_id"], chapter_id=data[chapter_key + "_id"]
    )


def _load_outline(env: dict[str, Any], outline_id: Any) -> Outline:
    async def _run() -> Outline:
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(select(Outline).where(Outline.id == outline_id))
            ).scalar_one()

    return asyncio.run(_run())


def _load_publications(env: dict[str, Any], chapter_id: Any) -> list[Publication]:
    async def _run() -> list[Publication]:
        async with env["SessionLocal"]() as db:
            rows = await db.execute(
                select(Publication).where(Publication.chapter_id == chapter_id)
            )
            return list(rows.scalars().all())

    return asyncio.run(_run())


def _patch_commit_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """统计 commit 次数与每次 flush 的写入集，并记录「通知被调用时已发生的 commit 次数」。

    写入集按「发生时的 commit 序号」归组：序号 0 表示业务提交之前的写入（即业务事务），
    序号 1 表示第一次提交之后（旁路/通知）的写入。用于证明业务提交恰好承载一次全部写入，
    且终止于通知之前。
    """
    calls: dict[str, Any] = {"commit": 0, "at_notify": None, "writes": []}
    real_commit = AsyncSession.commit
    real_flush = AsyncSession.flush

    async def _flush(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["writes"].append(
            {
                "before_commit": calls["commit"],
                "new": [type(o).__name__ for o in self.new],
                "dirty": [type(o).__name__ for o in self.dirty],
                "dirty_lifecycle": [
                    getattr(o, "lifecycle_status", None)
                    for o in self.dirty
                    if isinstance(o, Outline)
                ],
            }
        )
        return await real_flush(self, *args, **kwargs)

    async def _commit(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["commit"] += 1
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(AsyncSession, "flush", _flush)
    return calls


def _business_writes(calls: dict[str, Any]) -> list[dict[str, Any]]:
    """业务事务内的写入集：第一次 commit 之前发生的全部 flush。"""
    return [w for w in calls["writes"] if w["before_commit"] == 0]


def _wrap_notify(monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any], inner) -> None:
    async def _wrapper(*args, **kwargs):
        calls["at_notify"] = calls["commit"]
        return await inner(*args, **kwargs)

    monkeypatch.setattr(event_service, "emit_chapter_published", _wrapper)


# --------------------------------------------------------------------- E1


def test_e1_success_business_commit_exactly_once_before_notify(
    env, data, monkeypatch
) -> None:
    """E1：业务 commit 恰好 1 次，且发生在发布通知之前；总提交 2 次。"""
    calls = _patch_commit_counter(monkeypatch)
    _wrap_notify(monkeypatch, calls, event_service.emit_chapter_published)

    response = _post(env, _url(E1_PATH, data, "e1_ok"), {"force": True})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["at_notify"] == BUSINESS_COMMITS, (
        f"通知发送时业务提交次数应为 {BUSINESS_COMMITS}，实际 {snapshot['at_notify']}"
    )
    assert snapshot["commit"] == E1_SUCCESS_COMMITS, (
        f"E1 总提交次数应为 {E1_SUCCESS_COMMITS}，实际 {snapshot['commit']}"
    )
    # 业务提交之前的全部写入必须同属一个事务：快照 + 章节发布列（status 由 service 写）
    writes = _business_writes(snapshot)
    assert writes, "业务事务内必须发生过 flush（commit=False 只 flush）"
    touched = {name for w in writes for name in (w["new"] + w["dirty"])}
    assert "Publication" in touched, f"业务事务未包含发布快照：{writes}"
    assert "SearchDocument" in touched, f"业务事务未包含索引旁路写入：{writes}"
    outline = _load_outline(env, data["e1_ok_id"])
    assert outline.status == "published"
    assert outline.lifecycle_status == "published", (
        "D-U2A-2：E1 发布成功必须在同一业务事务内写 lifecycle_status"
    )


def test_e1_notify_failure_keeps_success_and_committed_data(env, data, monkeypatch) -> None:
    """E1：通知函数抛异常 → 仍 2xx，业务数据保持已提交，提交次数不增加。"""
    calls = _patch_commit_counter(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated notification failure")

    monkeypatch.setattr(event_service, "emit_chapter_published", _boom)
    response = _post(env, _url(E1_PATH, data, "e1_notify_raise"), {"force": True})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["commit"] == BUSINESS_COMMITS, (
        f"通知失败不得产生额外业务提交，实际 {snapshot['commit']}"
    )
    outline = _load_outline(env, data["e1_notify_raise_id"])
    assert outline.status == "published", "通知失败后章节必须仍是已发布"
    publications = _load_publications(env, data["e1_notify_raise_id"])
    assert len(publications) == 1, "通知失败不得丢失发布快照"
    assert publications[0].status == "published"


def test_e1_notify_flush_failure_does_not_pollute_business(env, data, monkeypatch) -> None:
    """E1：通知 flush 失败（会话进入失败态）→ 仍 2xx，业务已提交，rollback 只清理通知。"""
    calls = _patch_commit_counter(monkeypatch)
    real_rollback = AsyncSession.rollback
    rollbacks = {"n": 0}

    async def _rollback(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        rollbacks["n"] += 1
        return await real_rollback(self, *args, **kwargs)

    async def _poisoned_notify(db, *args, **kwargs):
        # 用违反主键唯一性的写入制造 flush 失败，验证 commit 失败路径不被业务误伤
        async with env["SessionLocal"]() as probe:
            existing = (
                await probe.execute(select(Project).where(Project.id == data["project_id"]))
            ).scalar_one()
            duplicate = Project(
                id=existing.id, user_id=existing.user_id, title="duplicate pk"
            )
        db.add(duplicate)
        await db.flush()  # IntegrityError

    monkeypatch.setattr(AsyncSession, "rollback", _rollback)
    monkeypatch.setattr(event_service, "emit_chapter_published", _poisoned_notify)
    response = _post(env, _url(E1_PATH, data, "e1_notify_flush"), {"force": True})
    snapshot = dict(calls)
    rollback_count = rollbacks["n"]
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["commit"] == BUSINESS_COMMITS, (
        f"通知 flush 失败不得产生额外提交，实际 {snapshot['commit']}"
    )
    assert rollback_count >= 1, "通知失败后必须清理会话状态"
    outline = _load_outline(env, data["e1_notify_flush_id"])
    assert outline.status == "published", "业务发布必须保持已提交"
    assert len(_load_publications(env, data["e1_notify_flush_id"])) == 1


def test_e1_index_sync_failure_still_commits_business_once(env, data, monkeypatch) -> None:
    """E1：作品索引同步失败 → savepoint 隔离，业务提交仍恰好 1 次并返回 2xx。"""
    async def _broken(*args, **kwargs):
        raise RuntimeError("simulated project index failure")

    monkeypatch.setattr(index_service, "sync_project_index", _broken)
    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E1_PATH, data, "e1_index_fail"), {"force": True})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["commit"] == E1_SUCCESS_COMMITS, snapshot["commit"]
    assert snapshot["at_notify"] is None, "本用例未包裹通知，不应有记录"
    assert _load_outline(env, data["e1_index_fail_id"]).status == "published"
    assert len(_load_publications(env, data["e1_index_fail_id"])) == 1


def test_e1_stats_failure_still_commits_business_once(env, data, monkeypatch) -> None:
    """E1：公开统计刷新失败 → 业务提交仍恰好 1 次，发布数据完整。"""
    async def _broken(*args, **kwargs):
        raise RuntimeError("simulated stats refresh failure")

    monkeypatch.setattr(discover_service, "refresh_project_stats", _broken)
    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E1_PATH, data, "e1_stats_fail"), {"force": True})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["commit"] == E1_SUCCESS_COMMITS, snapshot["commit"]
    publications = _load_publications(env, data["e1_stats_fail_id"])
    assert len(publications) == 1 and publications[0].status == "published"


def test_publish_chapter_flush_only_exposes_serializable_fields(env, data) -> None:
    """commit=False：flush 后 Publication.id / published_at 可用于序列化，且未提交不可见。"""

    async def _run() -> dict[str, Any]:
        async with env["SessionLocal"]() as db:
            project = (
                await db.execute(select(Project).where(Project.id == data["project_id"]))
            ).scalar_one()
            outline = (
                await db.execute(
                    select(Outline).where(Outline.id == data["e1_flush_only_id"])
                )
            ).scalar_one()

            response = await publication_service.publish_chapter(
                db, project, outline, PublicationCreate(), commit=False
            )
            payload = response.model_dump()
            assert response.id is not None, "flush 后必须能拿到 Publication.id"
            assert response.published_at is not None, "flush 后必须能拿到 published_at"
            assert response.status == "published"
            # U2a：发布三列已从快照服务剥离，统一由 mark_published 写透
            assert outline.status != "published", "快照服务不得写发布列"
            assert outline.id is not None
            # 关键：不提交，直接回滚，模拟调用方改动前放弃事务
            await db.rollback()
            return {"publication_id": payload["id"], "status": payload["status"]}

    result = asyncio.run(_run())

    async def _check() -> Any:
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(
                    select(Publication).where(Publication.id == result["publication_id"])
                )
            ).scalar_one_or_none()

    assert asyncio.run(_check()) is None, "commit=False 时不得落库（必须真的没提交）"
    assert result["status"] == "published"


# --------------------------------------------------------------------- E2


def test_e2_success_business_commit_exactly_once(env, data, monkeypatch) -> None:
    """E2：业务 commit 恰好 1 次（幂等/日志旁路提交不计入业务事务）。"""
    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E2_PATH, data, "e2_ok"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert response.json()["data"]["publication"]["status"] == "published"
    # 业务提交 1 次；其余为幂等/日志旁路提交
    assert snapshot["at_notify"] is None
    writes = _business_writes(snapshot)
    touched = {name for w in writes for name in (w["new"] + w["dirty"])}
    assert "Publication" in touched, f"业务事务未包含发布快照：{writes}"
    # 无 Idempotency-Key：业务提交 1 次 + API 调用日志旁路提交 1 次；业务事务恒为 1 次
    assert snapshot["commit"] == BUSINESS_COMMITS + 1, (
        f"E2 提交序列应为业务 1 次 + 日志旁路 1 次，实际 {snapshot['commit']}"
    )
    bypass = [w for w in snapshot["writes"] if w["before_commit"] >= BUSINESS_COMMITS]
    bypass_touched = {name for w in bypass for name in (w["new"] + w["dirty"])}
    assert not (bypass_touched & {"Publication", "Outline"}), (
        f"旁路提交不得再触碰业务对象：{bypass}"
    )
    outline = _load_outline(env, data["e2_ok_id"])
    assert (outline.status, outline.lifecycle_status) == ("published", "published")


def test_e2_status_and_lifecycle_are_atomic(env, data, monkeypatch) -> None:
    """E2：业务提交失败时 status 与 lifecycle_status 必须一起回滚，证明二者同一事务。"""
    real_commit = AsyncSession.commit
    state = {"first": True}

    async def _fail_first_commit(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if state["first"]:
            state["first"] = False
            await self.rollback()
            raise RuntimeError("simulated business commit failure")
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _fail_first_commit)
    response = _post(env, _url(E2_PATH, data, "e2_atomic"), {})
    monkeypatch.undo()

    assert response.status_code >= 500, (
        f"业务提交失败必须显式失败，不得吞掉：{response.status_code} {response.text}"
    )
    outline = _load_outline(env, data["e2_atomic_id"])
    assert outline.status == "draft", "status 不得单独落库"
    assert outline.lifecycle_status == "approved", "lifecycle_status 不得单独落库"
    assert _load_publications(env, data["e2_atomic_id"]) == [], "发布快照不得落库"


def test_e2_gate_rejection_before_any_business_write(env, data, monkeypatch) -> None:
    """E2 门禁拒绝：只提交一次拒绝审计，不创建 Publication、不写业务列。"""
    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E2_PATH, data, "e2_taken_down"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    assert snapshot["commit"] == 1, f"拒绝路径只允许审计提交，实际 {snapshot['commit']}"
    writes = _business_writes(snapshot)
    touched = {name for w in writes for name in (w["new"] + w["dirty"])}
    assert "Publication" not in touched, f"门禁拒绝前不得产生发布写入：{writes}"
    assert "Outline" not in touched, f"门禁拒绝前不得写章节发布列：{writes}"
    assert _load_publications(env, data["e2_taken_down_id"]) == []
    outline = _load_outline(env, data["e2_taken_down_id"])
    assert outline.status == "taken_down"
    assert outline.lifecycle_status == "approved"


def test_e2_content_rejection_has_no_business_commit(env, data, monkeypatch) -> None:
    """E2 内容体检拒绝：不产生任何业务提交（连 flush 都不落库）。"""
    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E2_PATH, data, "e2_content_reject"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (400, 422), response.text
    assert snapshot["commit"] == 0, (
        f"内容拒绝路径不得提交事务，实际 {snapshot['commit']}"
    )
    assert _load_publications(env, data["e2_content_reject_id"]) == []
    outline = _load_outline(env, data["e2_content_reject_id"])
    assert (outline.status, outline.lifecycle_status) == ("draft", "approved")


# --------------------------------------------------------------------- E3 / 结构


def test_e3_d4_commit_semantics_unchanged(env, data, monkeypatch) -> None:
    """E3：D4 提交语义保持现状，chapter_lifecycle.publish_chapter 未新增 commit 参数。"""
    sig = inspect.signature(chapter_lifecycle.publish_chapter)
    assert "commit" not in sig.parameters, (
        "本批禁止在 chapter_lifecycle.publish_chapter 上新增未使用的 commit 参数"
    )

    source = inspect.getsource(chapter_lifecycle.publish_chapter)
    assert "await db.commit()" in source, "E3 成功分支必须保留 D4 现有提交语义"

    calls = _patch_commit_counter(monkeypatch)
    response = _post(env, _url(E3_PATH, data, "e3_ok"), {"force": True})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert snapshot["commit"] >= 1, "E3 发布必须仍按 D4 语义提交"


def test_gate_runs_before_service_and_writes() -> None:
    """结构保证：门禁先于快照与发布列写入，编排器内只有一次业务提交。"""
    from app.routers import chapter_contents, openapi_chapters, publications
    from app.services import publish_orchestrator

    orch = inspect.getsource(publish_orchestrator.publish_chapter)
    gate = orch.index("chapter_lifecycle.validate_publish")
    snapshot = orch.index("publication_service.publish_chapter")
    mark = orch.index("chapter_lifecycle.mark_published")
    sync = orch.index("sync_publish_side_effects")
    assert gate < snapshot < mark < sync, (
        "U2a 顺序必须为 门禁 → 快照 → 发布列写透 → 索引/统计同步"
    )
    assert orch.count("await db.commit()") == 1, "编排器成功路径只允许一次业务提交"
    assert "commit=False" in orch, "编排器内快照/同步不得自行提交"

    e1 = inspect.getsource(publications.publish_chapter)
    assert "publish_orchestrator.publish_chapter" in e1, "E1 必须经编排器发布"
    assert "publication_service.publish_chapter(" not in e1, "E1 不得直接调快照服务"
    assert "lifecycle_status = " not in e1, "E1 不得写发布状态列"
    assert e1.count("await db.commit()") == 1, (
        "E1 路由只允许通知旁路提交，业务提交由编排器收口"
    )

    e2_src = inspect.getsource(openapi_chapters.publish_chapter)
    assert "_check_publish_preconditions" in e2_src, "E2 既有前置体检必须保留"
    assert "publish_orchestrator.publish_chapter" in e2_src, "E2 必须经编排器发布"
    assert "publication_service.publish_chapter(" not in e2_src, "E2 不得直接调快照服务"
    assert "lifecycle_status = " not in e2_src, "E2 不得写发布状态列"

    e3 = inspect.getsource(chapter_contents.publish_chapter)
    assert "PublishCommand" in e3, "E3 必须经编排器发布"
    assert "GATE_PROFILE_FULL" in e3, "E3 必须保留全量门禁（报告/锚点/blocking）"
    assert "write_snapshot=True" in e3, "E3 必须写入 Publication 快照（D-U2A-1）"
    assert "await db.commit()" not in e3, "E3 路由不得二次提交"
