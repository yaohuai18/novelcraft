"""U2a：章节发布统一编排器回归（D-U2A-1 ~ D-U2A-4）。

锁定目标：
    1. 三入口（E1 作者端 / E2 开放 API / E3 生命周期入口）成功路径各恰好一条
       ``lifecycle_publish`` 审计，``target_type="chapter_lifecycle"``；
    2. E3 经编排器创建 Publication，已存在时 upsert 不重复创建；
    3. 发布状态与 Publication 同事务落库：业务提交失败不留下半发布状态；
    4. 状态门禁（archived / taken_down / 非 approved）先于 Publication 与发布列写入，
       拒绝统一 409 且各一条拒绝审计；
    5. E2 blocking 拒绝的 ``validation_failed`` 与拒绝审计同事务落库；
    6. 索引 / 统计旁路失败不影响业务成功（E2/E3 走同一同步入口）；
    7. E2 幂等重放不重复写 Publication；
    8. E1 内容错误（无正文）保持 400，不被状态门禁之外的错误覆盖；
    9. E3 响应字段集不变；
   10. AST 守卫：``Publication(...)`` 唯一实例化点、路由不直调旧 publish_chapter、
       路由与 publication_service 不写发布状态列。

硬约束：
    - 进程内 ASGI（httpx.ASGITransport），不启服务、不监听端口；
    - 仓库外 pytest 临时目录 SQLite 文件库 + ORM create_all，不触碰 backend/novelcraft.db；
    - 不调用真实 AI、不发起网络请求；只做离线验证，不涉及 git add / commit。

Run: cd backend && python -m pytest tests/test_phase1_publish_orchestrator.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.api_errors import ApiError, api_error_handler
from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.publication import Publication
from app.models.user import User
from app.routers.chapter_contents import router as chapter_contents_router
from app.routers.openapi_chapters import guard_write
from app.routers.openapi_chapters import router as openapi_chapters_router
from app.routers.publications import router as publications_router
from app.services import audit_service, discover_service, index_service

E1_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/publish"
E2_PATH = "/api/v1/chapters/{chapter_id}/publish"
E3_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/lifecycle/publish"

BACKEND_ROOT = Path(__file__).resolve().parents[1]

VALID_REPORT = json.dumps(
    {"validated": True, "validation_anchor": {"content_version": 1}},
    ensure_ascii=False,
)
BLOCKING_REPORT = json.dumps(
    {
        "validated": True,
        "validation_anchor": {"content_version": 1},
        "issues": [{"severity": "blocking", "code": "contradiction", "message": "事实冲突"}],
    },
    ensure_ascii=False,
)


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    db_dir = tmp_path_factory.mktemp("phase1_publish_orchestrator")
    db_path = db_dir / "phase1_publish_orchestrator.db"

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


def _post(
    env: dict[str, Any],
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                url, json=body if body is not None else {}, headers=headers or {}
            )

    return asyncio.run(_run())


@pytest.fixture(scope="module")
def data(env) -> dict[str, Any]:
    """每个用例独占章节，避免发布状态互相污染。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="u2a_orch_owner",
                email="u2a_orch_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()

            project = Project(user_id=owner.id, title="U2a 编排器项目")
            db.add(project)
            await db.flush()

            # D-U2A-4：已归档作品（publication_status=archived）用于 409 语义验证
            archived_project = Project(
                user_id=owner.id,
                title="U2a 已归档作品",
                publication_status="archived",
            )
            db.add(archived_project)
            await db.flush()
            archived_chapter = Outline(
                project_id=archived_project.id,
                title="U2a-archived-project-chapter",
                outline_type="chapter",
                status="draft",
                lifecycle_status="approved",
                validation_report=VALID_REPORT,
            )
            db.add(archived_chapter)
            await db.flush()
            db.add(
                ChapterContent(
                    outline_id=archived_chapter.id,
                    content="归档作品下的章节正文",
                    word_count=10,
                    version=1,
                    lifecycle_status="approved",
                )
            )

            # (key, 资源状态, 生命周期状态, 校验报告, 是否写正文)
            specs: list[tuple[str, str, str, str, bool]] = [
                ("e1_ok", "draft", "approved", VALID_REPORT, True),
                ("e1_empty", "draft", "approved", VALID_REPORT, False),
                ("e1_atomic", "draft", "approved", VALID_REPORT, True),
                ("e2_ok", "draft", "approved", VALID_REPORT, True),
                ("e2_idem", "draft", "approved", VALID_REPORT, True),
                ("e2_index_fail", "draft", "approved", VALID_REPORT, True),
                ("e2_stats_fail", "draft", "approved", VALID_REPORT, True),
                ("e2_archived", "archived", "approved", VALID_REPORT, True),
                ("e2_taken_down", "taken_down", "approved", VALID_REPORT, True),
                ("e2_not_approved", "draft", "pending_review", VALID_REPORT, True),
                ("e2_blocking", "draft", "approved", BLOCKING_REPORT, True),
                ("e2_atomic", "draft", "approved", VALID_REPORT, True),
                ("e2_audit_fail", "draft", "approved", VALID_REPORT, True),
                ("e3_ok", "draft", "approved", VALID_REPORT, True),
                ("e3_readable", "draft", "approved", VALID_REPORT, True),
                ("e3_upsert", "draft", "approved", VALID_REPORT, True),
                ("e3_atomic", "draft", "approved", VALID_REPORT, True),
                ("e3_index_fail", "draft", "approved", VALID_REPORT, True),
                ("e3_blocking", "draft", "approved", BLOCKING_REPORT, True),
                # 门禁顺序探针：已归档且正文为空 → 必须先 409，而不是正文 400/422
                ("order_archived_empty", "archived", "approved", VALID_REPORT, False),
            ]

            chapters: dict[str, Outline] = {}
            for key, status, lifecycle, report, _has_body in specs:
                chapters[key] = Outline(
                    project_id=project.id,
                    title=f"U2a-{key}",
                    outline_type="chapter",
                    status=status,
                    lifecycle_status=lifecycle,
                    validation_report=report,
                )
            db.add_all(list(chapters.values()))
            await db.flush()

            for key, _status, _lifecycle, _report, has_body in specs:
                if not has_body:
                    continue
                db.add(
                    ChapterContent(
                        outline_id=chapters[key].id,
                        content=f"U2a {key} 正文内容",
                        word_count=10,
                        version=1,
                        lifecycle_status="approved",
                    )
                )

            # upsert 探针：预置一条同章节 Publication（草稿态），验证不重复创建
            db.add(
                Publication(
                    project_id=project.id,
                    chapter_id=chapters["e3_upsert"].id,
                    title="预置发布快照",
                    content="",
                    content_text="",
                    word_count=0,
                    status="draft",
                    visibility="private",
                )
            )
            await db.commit()

            ids.update(
                owner_id=owner.id,
                project_id=project.id,
                archived_project_id=archived_project.id,
                archived_project_chapter_id=archived_chapter.id,
            )
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


def _count_audits(env: dict[str, Any], chapter_id: Any, action: str) -> int:
    async def _run() -> int:
        async with env["SessionLocal"]() as db:
            return int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.target_id == str(chapter_id),
                            AuditLog.action == action,
                        )
                    )
                ).scalar_one()
            )

    return asyncio.run(_run())


def _audit_target_types(env: dict[str, Any], chapter_id: Any, action: str) -> set[str | None]:
    async def _run() -> set[str | None]:
        async with env["SessionLocal"]() as db:
            rows = await db.execute(
                select(AuditLog.target_type).where(
                    AuditLog.target_id == str(chapter_id),
                    AuditLog.action == action,
                )
            )
            return {r for r in rows.scalars().all()}

    return asyncio.run(_run())


def _patch_fail_first_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次业务 commit 失败（模拟事务提交错误），后续提交恢复真实行为。"""
    real_commit = AsyncSession.commit
    state = {"first": True}

    async def _fail_first(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if state["first"]:
            state["first"] = False
            await self.rollback()
            raise RuntimeError("simulated business commit failure")
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _fail_first)


# --------------------------------------------------------------------- 1. 成功审计


def test_three_entries_emit_exactly_one_publish_audit(env, data) -> None:
    """三入口成功路径各恰好一条 lifecycle_publish，target_type=chapter_lifecycle。"""
    for template, key, body in (
        (E1_PATH, "e1_ok", {}),
        (E2_PATH, "e2_ok", {}),
        (E3_PATH, "e3_ok", {}),
    ):
        response = _post(env, _url(template, data, key), body)
        assert response.status_code in (200, 201), f"{key}: {response.text}"
        chapter_id = data[key + "_id"]
        assert _count_audits(
            env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH
        ) == 1, f"{key} 必须恰好一条 lifecycle_publish"
        assert _audit_target_types(
            env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH
        ) == {audit_service.LIFECYCLE_AUDIT_TARGET_TYPE}


def test_order_guard_state_rejects_before_content_check(env, data) -> None:
    """门禁顺序：已归档 + 空正文 → 409（状态冲突优先于内容 400/422）。"""
    response = _post(env, _url(E1_PATH, data, "order_archived_empty"), {})
    assert response.status_code == 409, response.text
    chapter_id = data["order_archived_empty_id"]
    assert _load_publications(env, chapter_id) == [], "状态拒绝不得留下 Publication"
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("archived", "approved")


def test_e1_empty_body_keeps_400_semantics(env, data) -> None:
    """D-U2A-4：E1 正文为空仍为 400，不被状态门禁升格为 409/5xx。"""
    response = _post(env, _url(E1_PATH, data, "e1_empty"), {})
    assert response.status_code == 400, response.text
    chapter_id = data["e1_empty_id"]
    assert _load_publications(env, chapter_id) == []
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("draft", "approved")


# --------------------------------------------------------------------- 2. E3 快照


def test_e3_creates_publication_readable_after_publish(env, data) -> None:
    """D-U2A-1：E3 成功必须落下可读 Publication，且发布列一致。"""
    response = _post(env, _url(E3_PATH, data, "e3_readable"), {})
    assert response.status_code in (200, 201), response.text

    body = response.json()
    assert set(body) == {
        "project_id",
        "chapter_id",
        "lifecycle_status",
        "approved",
        "blocking",
        "message",
    }, f"E3 响应字段集不得变化：{body}"
    assert body["lifecycle_status"] == "published"

    publications = _load_publications(env, data["e3_readable_id"])
    assert len(publications) == 1, "E3 成功后必须存在唯一 Publication"
    assert publications[0].status == "published"
    outline = _load_outline(env, data["e3_readable_id"])
    assert (outline.status, outline.lifecycle_status) == ("published", "published")


def test_e3_upserts_existing_publication(env, data) -> None:
    """已存在 Publication 时 E3 走 upsert：行数仍为 1，状态转 published。"""
    chapter_id = data["e3_upsert_id"]
    before = _load_publications(env, chapter_id)
    assert len(before) == 1 and before[0].status == "draft"
    original_id = before[0].id

    response = _post(env, _url(E3_PATH, data, "e3_upsert"), {})
    assert response.status_code in (200, 201), response.text

    after = _load_publications(env, chapter_id)
    assert len(after) == 1, "upsert 不得重复创建 Publication"
    assert after[0].id == original_id, "upsert 必须复用既有快照行"
    assert after[0].status == "published"


# --------------------------------------------------------------------- 3. 同事务 / 原子性


@pytest.mark.parametrize(
    ("template", "key"),
    [(E1_PATH, "e1_atomic"), (E2_PATH, "e2_atomic"), (E3_PATH, "e3_atomic")],
)
def test_state_and_publication_rollback_together(env, data, monkeypatch, template, key) -> None:
    """业务提交失败 → 发布状态与 Publication 一起回滚，不留半发布状态。"""
    _patch_fail_first_commit(monkeypatch)
    response = _post(env, _url(template, data, key), {})
    monkeypatch.undo()

    assert response.status_code >= 500, (
        f"业务提交失败必须显式失败，不得吞掉：{response.status_code} {response.text}"
    )
    chapter_id = data[key + "_id"]
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("draft", "approved"), (
        "发布列不得单独落库"
    )
    assert _load_publications(env, chapter_id) == [], "Publication 不得单独落库"
    assert _count_audits(env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH) == 0, (
        "成功审计不得单独落库"
    )


# --------------------------------------------------------------------- 4. 状态门禁


@pytest.mark.parametrize("key", ["e2_archived", "e2_taken_down", "e2_not_approved"])
def test_state_gate_rejections_are_409_with_single_audit(env, data, key) -> None:
    """archived / taken_down / 非 approved → 409，各一条拒绝审计、无 Publication。"""
    response = _post(env, _url(E2_PATH, data, key), {})
    assert response.status_code == 409, response.text

    chapter_id = data[key + "_id"]
    assert _load_publications(env, chapter_id) == [], "门禁拒绝不得创建 Publication"
    assert _count_audits(
        env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED
    ) == 1
    assert _count_audits(env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH) == 0
    outline = _load_outline(env, chapter_id)
    assert outline.status != "published", "拒绝不得写发布列"


def test_archived_project_publish_is_409(env, data) -> None:
    """D-U2A-4：作品归档 → 409，且不落 Publication、不写发布列。"""
    url = E1_PATH.format(
        project_id=data["archived_project_id"],
        chapter_id=data["archived_project_chapter_id"],
    )
    response = _post(env, url, {})
    assert response.status_code == 409, response.text

    chapter_id = data["archived_project_chapter_id"]
    assert _load_publications(env, chapter_id) == []
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("draft", "approved")


def test_blocking_rejection_persists_validation_failed_atomically(env, data, monkeypatch) -> None:
    """blocking 拒绝：validation_failed 与拒绝审计同事务落库，且无 Publication。"""
    from app.services import chapter_lifecycle

    calls = {"commit": 0}
    real_commit = AsyncSession.commit

    async def _count(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["commit"] += 1
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _count)
    response = _post(env, _url(E3_PATH, data, "e3_blocking"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    assert snapshot["commit"] == 1, f"拒绝路径只允许一次审计提交，实际 {snapshot['commit']}"

    chapter_id = data["e3_blocking_id"]
    outline = _load_outline(env, chapter_id)
    assert outline.lifecycle_status == chapter_lifecycle.STATUS_VALIDATION_FAILED
    assert outline.status == "draft", "blocking 拒绝不得写资源发布列"
    assert _load_publications(env, chapter_id) == []
    assert _count_audits(
        env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED
    ) == 1


# --------------------------------------------------------------------- 5. 旁路隔离


@pytest.mark.parametrize(
    ("template", "key", "target", "attr"),
    [
        (E2_PATH, "e2_index_fail", index_service, "sync_project_index"),
        (E2_PATH, "e2_stats_fail", discover_service, "refresh_project_stats"),
        (E3_PATH, "e3_index_fail", index_service, "sync_project_index"),
    ],
)
def test_side_effect_failure_does_not_break_publish(
    env, data, monkeypatch, template, key, target, attr
) -> None:
    """索引 / 统计失败 savepoint 隔离：发布仍成功且数据完整。"""

    async def _broken(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated side effect failure")

    monkeypatch.setattr(target, attr, _broken)
    response = _post(env, _url(template, data, key), {})
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    chapter_id = data[key + "_id"]
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("published", "published")
    publications = _load_publications(env, chapter_id)
    assert len(publications) == 1 and publications[0].status == "published"
    assert _count_audits(env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH) == 1


# --------------------------------------------------------------------- 6. 幂等


def test_publish_audit_failure_keeps_publish_success(env, data, monkeypatch) -> None:
    """D-U2A-2 + A1：成功审计写入失败只降级为「未持久化」，不改变发布结果。"""

    async def _failed(*args, **kwargs):  # noqa: ANN002, ANN003
        return SimpleNamespace(
            written=False,
            status=audit_service.AUDIT_STATUS_FAILED,
            audit_id=None,
        )

    monkeypatch.setattr(audit_service, "record_lifecycle_audit_result", _failed)
    response = _post(env, _url(E2_PATH, data, "e2_audit_fail"), {})
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    chapter_id = data["e2_audit_fail_id"]
    outline = _load_outline(env, chapter_id)
    assert (outline.status, outline.lifecycle_status) == ("published", "published")
    assert len(_load_publications(env, chapter_id)) == 1
    assert _count_audits(env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH) == 0


def test_e2_idempotent_replay_does_not_duplicate_publication(env, data) -> None:
    """E2 幂等重放：第二次直接回放响应，不重复写 Publication / 审计。"""
    url = _url(E2_PATH, data, "e2_idem")
    headers = {"Idempotency-Key": "u2a-idem-key-1"}

    first = _post(env, url, {}, headers=headers)
    second = _post(env, url, {}, headers=headers)

    assert first.status_code in (200, 201), first.text
    assert second.status_code == first.status_code, second.text
    assert second.json() == first.json(), "重放必须返回同一响应体"

    chapter_id = data["e2_idem_id"]
    assert len(_load_publications(env, chapter_id)) == 1, "重放不得重复写 Publication"
    assert _count_audits(env, chapter_id, audit_service.ACTION_LIFECYCLE_PUBLISH) == 1


# --------------------------------------------------------------------- 7. AST 守卫

PUBLISH_COLUMNS = {"lifecycle_status", "status", "published_at"}
ROUTER_FILES = (
    "app/routers/publications.py",
    "app/routers/openapi_chapters.py",
    "app/routers/chapter_contents.py",
)


def _parse(relative: str) -> ast.Module:
    return ast.parse((BACKEND_ROOT / relative).read_text(encoding="utf-8"))


def test_publication_has_single_instantiation_point() -> None:
    """``Publication(...)`` 只允许在 publication_service 内出现一次。"""
    services_dir = BACKEND_ROOT / "app" / "services"
    hits: list[tuple[str, int]] = []
    targets = list(services_dir.glob("*.py")) + [BACKEND_ROOT / f for f in ROUTER_FILES]

    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "Publication":
                    hits.append((str(path.relative_to(BACKEND_ROOT)), node.lineno))

    assert len(hits) == 1, f"Publication 实例化点必须唯一，实际：{hits}"
    assert hits[0][0].replace("\\", "/") == "app/services/publication_service.py", (
        f"唯一实例化点必须位于 publication_service：{hits}"
    )


def test_routers_do_not_call_legacy_lifecycle_publish() -> None:
    """三个发布路由不得直接调用旧 ``chapter_lifecycle.publish_chapter``。"""
    for relative in ROUTER_FILES:
        tree = _parse(relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "publish_chapter":
                    owner = func.value
                    name = owner.id if isinstance(owner, ast.Name) else None
                    line = getattr(node, "lineno", "?")
                    assert name == "publish_orchestrator", (
                        f"{relative}:{line} 发布只能经 publish_orchestrator，"
                        f"实际调用方 {name}"
                    )


def test_routers_and_snapshot_service_do_not_write_publish_columns() -> None:
    """发布函数体内禁写发布状态三列（唯一写点：chapter_lifecycle.mark_published）。"""
    violations: list[str] = []

    def _scan(path: Path, func_name: str, forbid_owners: set[str]) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name != func_name:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, (ast.Assign, ast.AugAssign)):
                    continue
                targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
                for tgt in targets:
                    if not isinstance(tgt, ast.Attribute) or tgt.attr not in PUBLISH_COLUMNS:
                        continue
                    owner = getattr(tgt.value, "id", None)
                    if owner in forbid_owners:
                        violations.append(
                            f"{path.name}:{inner.lineno} -> {owner}.{tgt.attr}"
                        )

    for relative in ROUTER_FILES:
        _scan(BACKEND_ROOT / relative, "publish_chapter", {"outline", "chapter", "outlines"})
    # 快照服务只允许写 Publication 自身状态，不得写 outline 发布三列
    _scan(
        BACKEND_ROOT / "app" / "services" / "publication_service.py",
        "publish_chapter",
        {"outline", "chapter"},
    )

    assert not violations, f"发布状态列只能由 mark_published 写：{violations}"


def test_orchestrator_is_only_publish_entry_with_single_commit() -> None:
    """编排器是唯一发布编排入口，且内部只有一次业务提交。"""
    tree = _parse("app/services/publish_orchestrator.py")
    func = next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "publish_chapter"
    )
    commits = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Attribute) and n.attr == "commit"
    ]
    assert len(commits) == 1, f"编排器成功路径只允许一次 commit，实际 {len(commits)}"

    ordered = sorted(
        (n.lineno, n.func.attr)
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    )
    calls = [attr for _lineno, attr in ordered]
    assert calls.index("validate_publish") < calls.index("publish_chapter"), calls
    assert calls.index("publish_chapter") < calls.index("mark_published"), calls
    assert calls.index("mark_published") < calls.index("sync_publish_side_effects"), calls
