# -*- coding: utf-8 -*-
"""第一批数据完整性与并发安全：绑定校验 / 消费标记 / 原子 CAS / 归属语义。

全部离线：仓库外临时 SQLite + 进程内 ASGI（httpx.ASGITransport），不启服务、
不连真实库、不调真实模型。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core import migrate
from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.models.chapter_content import ChapterContent
from app.models.chapter_version import ChapterVersion
from app.models.generation import GenerationRecord
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.routers import chapter_contents as chapter_contents_router_module
from app.routers.chapter_contents import router as chapter_contents_router
from app.services import generation_service
from app.services.generation_attribution import backfill_generation_attribution

CONTENT_PATH = "/api/projects/{project_id}/chapters/{outline_id}/content"


# --------------------------------------------------------------------- 基础设施
@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    db_dir = tmp_path_factory.mktemp("batch1_data_integrity")
    db_path = db_dir / "batch1.db"

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=NullPool,
        connect_args={"timeout": 10},
    )
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _prepare() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())

    app = FastAPI()

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(chapter_contents_router)

    yield {
        "app": app,
        "engine": engine,
        "SessionLocal": session_factory,
        "db_path": str(db_path),
    }
    asyncio.run(engine.dispose())


@pytest.fixture(scope="module")
def data(env) -> dict[str, Any]:
    """种子：owner + 两个作品，每个作品两个章节（正文 version=1）。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="batch1_owner",
                email="batch1_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()

            p1 = Project(user_id=owner.id, title="批1-项目A")
            p2 = Project(user_id=owner.id, title="批1-项目B")
            db.add_all([p1, p2])
            await db.flush()

            chapters = {}
            for key, project, title in (
                ("a1", p1, "A-第一章"),
                ("a2", p1, "A-第二章"),
                ("b1", p2, "B-第一章"),
            ):
                ch = Outline(
                    project_id=project.id,
                    title=title,
                    outline_type="chapter",
                    lifecycle_status="draft",
                )
                db.add(ch)
                await db.flush()
                db.add(
                    ChapterContent(
                        outline_id=ch.id,
                        content="初始正文",
                        word_count=4,
                        version=1,
                    )
                )
                chapters[key] = ch.id

            await db.commit()
            ids.update(
                owner_id=owner.id,
                p1=p1.id,
                p2=p2.id,
                a1=chapters["a1"],
                a2=chapters["a2"],
                b1=chapters["b1"],
            )
        return ids

    seeded = asyncio.run(_seed())
    env["app"].dependency_overrides[get_current_user] = (
        lambda: SimpleNamespace(id=seeded["owner_id"])
    )
    return seeded


def _client(env) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"], raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _put(env, project_id, outline_id, payload) -> httpx.Response:
    url = CONTENT_PATH.format(project_id=project_id, outline_id=outline_id)

    async def _run():
        async with _client(env) as client:
            return await client.put(url, json=payload)

    return asyncio.run(_run())


async def _put_async(env, project_id, outline_id, payload) -> httpx.Response:
    url = CONTENT_PATH.format(project_id=project_id, outline_id=outline_id)
    async with _client(env) as client:
        return await client.put(url, json=payload)


def _make_record(
    env,
    *,
    project_id,
    chapter_id=None,
    action_type="rewrite",
    status="completed",
    attribution_status="project_bound",
    response_content="改后的正文",
) -> GenerationRecord:
    async def _run():
        async with env["SessionLocal"]() as db:
            record = await generation_service.record_generation(
                db,
                project_id=project_id,
                chapter_id=chapter_id,
                action_type=action_type,
                attribution_status=attribution_status,
                response_content=response_content,
                status=status,
            )
            assert record is not None
            return record

    return asyncio.run(_run())


def _load(env, model, pk):
    async def _run():
        async with env["SessionLocal"]() as db:
            return await db.get(model, pk)

    return asyncio.run(_run())


# ------------------------------------------------------------------ 1. 绑定校验
def test_reject_chapter_mismatch(env, data):
    """同作品内章节 A 的生成结果不能接受到章节 B。"""
    record = _make_record(env, project_id=data["p1"], chapter_id=data["a1"])
    resp = _put(
        env,
        data["p1"],
        data["a2"],
        {
            "content": "接受到错误章节",
            "source": "ai_rewrite",
            "generation_record_id": str(record.id),
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "generation_record_chapter_mismatch"
    # 数据库未被改动
    assert _load(env, ChapterContent, _chapter_content_id(env, data["a2"])).version == 1


def _chapter_content_id(env, outline_id):
    async def _run():
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(
                    select(ChapterContent.id).where(
                        ChapterContent.outline_id == outline_id
                    )
                )
            ).scalar_one()

    return asyncio.run(_run())


def test_reject_cross_project(env, data):
    record = _make_record(env, project_id=data["p2"], chapter_id=data["b1"])
    resp = _put(
        env,
        data["p1"],
        data["a1"],
        {
            "content": "跨作品接受",
            "source": "ai_rewrite",
            "generation_record_id": str(record.id),
        },
    )
    assert resp.status_code == 404, resp.text


def test_reject_action_mismatch(env, data):
    """ai_continue 不能用一条 rewrite 记录。"""
    record = _make_record(
        env, project_id=data["p1"], chapter_id=data["a1"], action_type="rewrite"
    )
    resp = _put(
        env,
        data["p1"],
        data["a1"],
        {
            "content": "错误来源类型",
            "source": "ai_continue",
            "generation_record_id": str(record.id),
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "generation_record_action_mismatch"


def test_reject_unacceptable_status(env, data):
    record = _make_record(
        env,
        project_id=data["p1"],
        chapter_id=data["a1"],
        status="incomplete",
    )
    resp = _put(
        env,
        data["p1"],
        data["a1"],
        {
            "content": "未完成正文",
            "source": "ai_rewrite",
            "generation_record_id": str(record.id),
        },
    )
    assert resp.status_code == 400, resp.text


# ------------------------------------------------------------------ 2. 消费标记
def test_accept_once_then_conflict(env, data):
    record = _make_record(env, project_id=data["p1"], chapter_id=data["a1"])
    payload = {
        "content": "第一次接受",
        "source": "ai_rewrite",
        "generation_record_id": str(record.id),
        "base_version": 1,
    }
    first = _put(env, data["p1"], data["a1"], payload)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    consumed = _load(env, GenerationRecord, record.id)
    assert consumed.consumed_at is not None
    assert str(consumed.consumed_chapter_id) == str(data["a1"])
    assert consumed.final_content == "第一次接受"

    second = _put(
        env,
        data["p1"],
        data["a1"],
        {
            "content": "第二次接受",
            "source": "ai_rewrite",
            "generation_record_id": str(record.id),
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "generation_record_already_consumed"


def test_concurrent_accept_same_record_only_one_wins(env, data):
    """两个请求同时接受同一条记录：只有一条能成功。"""
    record = _make_record(env, project_id=data["p1"], chapter_id=data["a1"])
    payload = {
        "content": "并发接受",
        "source": "ai_rewrite",
        "generation_record_id": str(record.id),
    }

    async def _both():
        return await asyncio.gather(
            _put_async(env, data["p1"], data["a1"], payload),
            _put_async(env, data["p1"], data["a1"], payload),
        )

    results = asyncio.run(_both())
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]

    async def _versions():
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(
                    select(ChapterVersion).where(
                        ChapterVersion.generation_record_id == record.id
                    )
                )
            ).scalars().all()

    assert len(asyncio.run(_versions())) == 1


# ------------------------------------------------------------------ 3. 原子 CAS
def test_optimistic_lock_is_atomic(env, data):
    """同一 base_version 的两次保存：一次成功，一次 409，最终版本只 +1。"""
    payload = {"content": "并发保存", "source": "manual", "base_version": 1}

    async def _both():
        return await asyncio.gather(
            _put_async(env, data["p1"], data["a2"], payload),
            _put_async(env, data["p1"], data["a2"], payload),
        )

    results = asyncio.run(_both())
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]

    row = _load(env, ChapterContent, _chapter_content_id(env, data["a2"]))
    assert row.version == 2

    async def _count():
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(
                    select(ChapterVersion).where(
                        ChapterVersion.outline_id == data["a2"]
                    )
                )
            ).scalars().all()

    assert len(asyncio.run(_count())) == 1


def test_conflict_response_shape_unchanged(env, data):
    """409 冲突体保持 conflict/current_version/submitted_base_version 三个字段。"""
    resp = _put(
        env,
        data["p1"],
        data["a2"],
        {"content": "过期版本", "source": "manual", "base_version": 1},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["conflict"] is True
    assert detail["submitted_base_version"] == 1
    assert detail["current_version"] >= 2


# --------------------------------------------------------------- 4. 归属语义
def test_project_bound_requires_project_id(env):
    async def _run():
        async with env["SessionLocal"]() as db:
            with pytest.raises(ValueError):
                await generation_service.record_generation(
                    db,
                    action_type="outline",
                    attribution_status="project_bound",
                    response_content="x",
                )

    asyncio.run(_run())


def test_attribution_is_mandatory(env):
    async def _run():
        async with env["SessionLocal"]() as db:
            with pytest.raises(ValueError):
                await generation_service.record_generation(
                    db,
                    action_type="outline",
                    response_content="x",
                )

    asyncio.run(_run())


def test_unknown_attribution_rejected(env):
    async def _run():
        async with env["SessionLocal"]() as db:
            with pytest.raises(ValueError):
                await generation_service.record_generation(
                    db,
                    action_type="outline",
                    attribution_status="whatever",
                    response_content="x",
                )

    asyncio.run(_run())


def test_pre_project_allows_null_project_id(env):
    record = _make_record(
        env,
        project_id=None,
        chapter_id=None,
        action_type="outline",
        attribution_status="pre_project",
    )
    row = _load(env, GenerationRecord, record.id)
    assert row.project_id is None
    assert row.attribution_status == "pre_project"


# --------------------------------------------------------------- 5. 历史回填
def test_backfill_dry_run_then_idempotent(env, data):
    """确定性回填：chapter_id 能定位作品的补 project_id，其余标 unattributed。"""

    async def _seed_legacy():
        async with env["SessionLocal"]() as db:
            linked = GenerationRecord(
                project_id=None,
                chapter_id=data["b1"],
                action_type="rewrite",
                system_prompt="",
                user_prompt="",
                response_content="历史-有章节",
            )
            orphan = GenerationRecord(
                project_id=None,
                chapter_id=None,
                action_type="outline",
                system_prompt="",
                user_prompt="",
                response_content="历史-无关联",
            )
            db.add_all([linked, orphan])
            await db.commit()
            return linked.id, orphan.id

    linked_id, orphan_id = asyncio.run(_seed_legacy())

    async def _backfill(dry_run):
        async with env["SessionLocal"]() as db:
            return await backfill_generation_attribution(db, dry_run=dry_run)

    dry = asyncio.run(_backfill(True))
    assert dry["total"] >= 2
    assert dry["dry_run"] is True
    row = _load(env, GenerationRecord, linked_id)
    assert row.attribution_status is None  # dry-run 不写库

    real = asyncio.run(_backfill(False))
    assert real["dry_run"] is False
    assert real["project_bound"] >= 1
    assert real["rules"].get("chapter_outline", 0) >= 1
    assert real["rules"].get("no_deterministic_link", 0) >= 1

    row = _load(env, GenerationRecord, linked_id)
    assert row.attribution_status == "project_bound"
    assert row.project_id == data["p2"]

    orphan = _load(env, GenerationRecord, orphan_id)
    assert orphan.attribution_status == "unattributed"
    assert orphan.project_id is None

    again = asyncio.run(_backfill(False))
    assert again["total"] == 0  # 幂等：没有待处理行


# --------------------------------------------------------- 6. 迁移序列与内容字段
def test_migration_registry_has_v5_and_v6():
    # 第二批把最新版本推进到 v7；本用例只校验第一批的 v5/v6 仍在注册表中且内容正确。
    assert migrate.SCHEMA_VERSION >= 6
    assert 5 in migrate.MIGRATION_STEPS
    assert 6 in migrate.MIGRATION_STEPS
    statements = migrate.MIGRATION_STEPS[6][1]
    joined = " ".join(statements)
    for column in (
        "attribution_status",
        "consumed_at",
        "consumed_chapter_id",
        "consumed_version_id",
    ):
        assert column in joined
    assert len(statements) == 6


def test_rewrite_preview_fills_content_fields(env, data, monkeypatch):
    """rewrite 预览：raw/processed 写模型输出，final 保持空（未被接受）。"""
    monkeypatch.setattr(
        chapter_contents_router_module,
        "chat_with_ai",
        _fake_chat,
    )
    monkeypatch.setattr(
        chapter_contents_router_module,
        "build_continuation_context",
        _fake_context,
    )

    url = "/api/projects/{pid}/chapters/{cid}/ai/rewrite".format(
        pid=data["p1"], cid=data["a1"]
    )

    async def _run():
        async with _client(env) as client:
            return await client.post(
                url, json={"content": "原始正文内容", "instruction": "更紧凑"}
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200, resp.text
    record_id = resp.json().get("generation_record_id")
    assert record_id, resp.json()

    row = _load(env, GenerationRecord, uuid.UUID(str(record_id)))
    assert row.raw_content == "改写后的正文"
    assert row.processed_content == "改写后的正文"
    assert row.response_content == "改写后的正文"
    assert row.final_content == ""
    assert row.attribution_status == "project_bound"
    assert row.chapter_id == data["a1"]


async def _fake_chat(*args, **kwargs):
    return "改写后的正文"


async def _fake_context(*args, **kwargs):
    return {
        "system_prompt": "系统",
        "user_prompt": "用户",
        "snapshot": {},
    }
