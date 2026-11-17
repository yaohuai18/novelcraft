# -*- coding: utf-8 -*-
"""B1/U1a：外部发布通道前置状态门禁与拒绝审计回归。

覆盖范围（仅 U1a，不触碰 commit 边界 / Publication 快照 / visibility）：
    1. E1（作者端 POST /api/projects/{pid}/chapters/{cid}/publish）：
       archived / taken_down / 非 approved 生命周期 → 409，且拒绝写入
       ``lifecycle_publish_rejected`` 审计（reason_code + gate 可区分）；
    2. E2（开放 API POST /api/v1/chapters/{cid}/publish）：
       同样状态下 409，且**优先于**正文为空的 422（R-2 裁定）；
    3. E3（POST /api/projects/{pid}/chapters/{cid}/lifecycle/publish）：
       拒绝仍为 409 + 既有 HTTPException detail 契约不变；
    4. ``assert_publish_state`` 通过时为纯只读：不写审计、不改 outline 任何列，
       可重复调用而不重复记账；taken_down 被识别为资源状态而非 lifecycle 枚举。

硬约束：
    - 进程内 ASGI（httpx.ASGITransport），不启服务、不监听端口；
    - 仓库外 pytest 临时目录 SQLite 文件库 + ORM create_all，不触碰
      backend/novelcraft.db、不执行迁移、不连正式库；
    - 不调用真实 AI、不发起网络请求（tests/conftest.py 离线护栏生效）；
    - 不改动 publication_service / 生命周期状态机 / 提交边界。

Run: cd backend && python -m pytest tests/test_phase1_external_publish_gates.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import json
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
from app.models.audit_log import AuditLog
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.routers.chapter_contents import router as chapter_contents_router
from app.routers.openapi_chapters import guard_write
from app.routers.openapi_chapters import router as openapi_chapters_router
from app.routers.publications import router as publications_router
from app.services import audit_service, chapter_lifecycle

E1_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/publish"
E2_PATH = "/api/v1/chapters/{chapter_id}/publish"
E3_PATH = "/api/projects/{project_id}/chapters/{chapter_id}/lifecycle/publish"

VALID_REPORT = json.dumps(
    {"validated": True, "validation_anchor": {"content_version": 1}},
    ensure_ascii=False,
)

# 正常（门禁放行）发布路径的事务提交次数基线。
# U1a：补正只影响拒绝路径，成功路径未改动，实测 3。
# U1b（方案 B）：E1 成功路径收敛为「业务提交 1 次 + 发布通知提交 1 次」= 2。
BUSINESS_COMMIT_PER_PUBLISH = 1
NOTIFY_COMMIT_PER_PUBLISH = 1
SUCCESSFUL_PUBLISH_COMMITS = BUSINESS_COMMIT_PER_PUBLISH + NOTIFY_COMMIT_PER_PUBLISH


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库 + 只挂三个发布入口路由的进程内 ASGI 应用。"""
    db_dir = tmp_path_factory.mktemp("phase1_external_publish_gates")
    db_path = db_dir / "phase1_external_publish_gates.db"

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
    """种子数据：1 owner + 1 项目 + 5 个章节（归档 / 下架 / 已发布 / 已审批 / 下架无正文）。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="u1a_gate_owner",
                email="u1a_gate_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()

            project = Project(user_id=owner.id, title="U1a 外部发布门禁项目")
            db.add(project)
            await db.flush()

            chapters = {
                # 已归档：lifecycle 仍为 approved，资源状态已不可发布
                "ch_archived": Outline(
                    project_id=project.id,
                    title="U1a-已归档章节",
                    outline_type="chapter",
                    status="archived",
                    lifecycle_status="approved",
                    validation_report=VALID_REPORT,
                ),
                # 已下架（资源状态），正文完整
                "ch_taken_down": Outline(
                    project_id=project.id,
                    title="U1a-已下架章节",
                    outline_type="chapter",
                    status="taken_down",
                    lifecycle_status="approved",
                    validation_report=VALID_REPORT,
                ),
                # 已下架且正文为空：用于证明 409 优先于 422
                "ch_taken_down_empty": Outline(
                    project_id=project.id,
                    title="U1a-已下架空正文章节",
                    outline_type="chapter",
                    status="taken_down",
                    lifecycle_status="approved",
                    validation_report=VALID_REPORT,
                ),
                # 已发布：第二次发布属于状态不允许
                "ch_published": Outline(
                    project_id=project.id,
                    title="U1a-已发布章节",
                    outline_type="chapter",
                    status="published",
                    lifecycle_status="published",
                    validation_report=VALID_REPORT,
                ),
                # 正常已审批：门禁应放行（只读、不写审计）
                "ch_approved": Outline(
                    project_id=project.id,
                    title="U1a-已审批章节",
                    outline_type="chapter",
                    status="draft",
                    lifecycle_status="approved",
                    validation_report=VALID_REPORT,
                ),
            }
            db.add_all(list(chapters.values()))
            await db.flush()

            db.add_all(
                [
                    ChapterContent(
                        outline_id=chapters["ch_archived"].id,
                        content="U1a 已归档章节正文",
                        word_count=10,
                        version=1,
                        lifecycle_status="approved",
                    ),
                    ChapterContent(
                        outline_id=chapters["ch_taken_down"].id,
                        content="U1a 已下架章节正文",
                        word_count=10,
                        version=1,
                        lifecycle_status="approved",
                    ),
                    ChapterContent(
                        outline_id=chapters["ch_published"].id,
                        content="U1a 已发布章节正文",
                        word_count=10,
                        version=1,
                        lifecycle_status="published",
                    ),
                    ChapterContent(
                        outline_id=chapters["ch_approved"].id,
                        content="U1a 已审批章节正文",
                        word_count=10,
                        version=1,
                        lifecycle_status="approved",
                    ),
                ]
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
    # 开放 API 鉴权/限流依赖替换：只暴露 chapters:write 主体，聚焦门禁本身
    env["app"].dependency_overrides[guard_write] = lambda: {
        "principal": SimpleNamespace(user=SimpleNamespace(id=seeded["owner_id"])),
        "rate": {"allowed": True, "limit": 100, "count": 1, "retry_after": 0},
        "client_class": "general",
    }
    return seeded


# --------------------------------------------------------------------- 辅助断言


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


def _reject_audits(env: dict[str, Any], outline_id: Any) -> list[dict[str, Any]]:
    """取该章节的发布拒绝审计（按 metadata 解析 reason_code / gate）。"""

    async def _run() -> list[dict[str, Any]]:
        async with env["SessionLocal"]() as db:
            rows = (
                await db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action
                        == audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED,
                        AuditLog.target_id == str(outline_id),
                    )
                    .order_by(AuditLog.id.asc())
                )
            ).scalars().all()
            return [json.loads(row.metadata_json or "{}") for row in rows]

    return asyncio.run(_run())


def _outline_state(env: dict[str, Any], outline_id: Any) -> tuple[str, str, Any]:
    """返回 (status, lifecycle_status, published_at)，用于证明门禁未写业务列。"""
    row = _load_outline(env, outline_id)
    return row.status, row.lifecycle_status, row.published_at


# --------------------------------------------------------------------- E1


def test_e1_archived_rejected_409_and_audited(env, data) -> None:
    """E1：已归档章节发布 → 409，拒绝审计 reason_code=archived，业务列不变。"""
    before = _outline_state(env, data["ch_archived_id"])
    audits_before = len(_reject_audits(env, data["ch_archived_id"]))

    response = _post(env, _url(E1_PATH, data, "ch_archived"))

    assert response.status_code == 409, response.text
    assert "归档" in response.json()["detail"]
    assert _outline_state(env, data["ch_archived_id"]) == before

    audits = _reject_audits(env, data["ch_archived_id"])
    assert len(audits) == audits_before + 1, "拒绝必须写且只写一条审计"
    assert audits[-1]["reason_code"] == audit_service.LIFECYCLE_REJECT_ARCHIVED
    assert audits[-1]["gate"] == "resource_archived"


def test_e1_taken_down_rejected_409_and_audited(env, data) -> None:
    """E1：已下架章节发布 → 409（taken_down 属 outline.status，非 lifecycle 枚举）。"""
    before = _outline_state(env, data["ch_taken_down_id"])

    response = _post(env, _url(E1_PATH, data, "ch_taken_down"))

    assert response.status_code == 409, response.text
    assert "下架" in response.json()["detail"]
    assert _outline_state(env, data["ch_taken_down_id"]) == before

    audits = _reject_audits(env, data["ch_taken_down_id"])
    assert audits, "已下架发布拒绝必须留审计"
    assert audits[-1]["reason_code"] == audit_service.LIFECYCLE_REJECT_TAKEN_DOWN
    assert audits[-1]["gate"] == "resource_taken_down"
    assert audits[-1]["from_status"] == "approved"
    assert audits[-1]["to_status"] == "approved", "拒绝不得伪造状态推进"


def test_e1_published_rejected_409_state_not_allowed(env, data) -> None:
    """E1：已发布章节再次发布 → 409，拒绝码归入 state_not_allowed。"""
    response = _post(env, _url(E1_PATH, data, "ch_published"))

    assert response.status_code == 409, response.text

    audits = _reject_audits(env, data["ch_published_id"])
    assert audits
    assert (
        audits[-1]["reason_code"] == audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED
    )
    assert audits[-1]["gate"] == "state_machine"


# --------------------------------------------------------------------- E2


def test_e2_taken_down_beats_empty_body_409_not_422(env, data) -> None:
    """E2：已下架 + 正文为空 → 409（状态门禁优先），不得返回 422。"""
    response = _post(env, _url(E2_PATH, data, "ch_taken_down_empty"), {})

    assert response.status_code == 409, (
        "状态门禁必须优先于正文体检（R-2），实际："
        f"{response.status_code} {response.text}"
    )
    body = response.json()["error"]
    assert body["code"] == "conflict"
    assert body["details"]["reason"] == audit_service.LIFECYCLE_REJECT_TAKEN_DOWN
    assert body["details"]["gate"] == "resource_taken_down"

    audits = _reject_audits(env, data["ch_taken_down_empty_id"])
    assert audits and audits[-1]["reason_code"] == (
        audit_service.LIFECYCLE_REJECT_TAKEN_DOWN
    )


def test_e2_archived_rejected_409(env, data) -> None:
    """E2：已归档章节发布 → 409，reason=archived，且不产生 424/422。"""
    response = _post(env, _url(E2_PATH, data, "ch_archived"), {})

    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["details"]["reason"] == audit_service.LIFECYCLE_REJECT_ARCHIVED
    assert body["details"]["gate"] == "resource_archived"


# --------------------------------------------------------------------- E3


def test_e3_taken_down_rejected_409_contract_unchanged(env, data) -> None:
    """E3：已下架发布仍返回 409 + 既有 detail 文本契约（不改响应结构）。"""
    response = _post(env, _url(E3_PATH, data, "ch_taken_down"))

    assert response.status_code == 409, response.text
    payload = response.json()
    assert set(payload.keys()) == {"detail"}, "拒绝响应契约不得变动"
    assert "下架" in payload["detail"]


# --------------------------------------------------------------------- 服务级


def test_assert_publish_state_is_read_only_and_idempotent(env, data) -> None:
    """门禁通过时纯只读：不写审计、不改列，重复调用不重复记账。"""

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            outline = await db.get(Outline, data["ch_approved_id"])
            before = (outline.status, outline.lifecycle_status, outline.published_at)
            await chapter_lifecycle.assert_publish_state(db, outline)
            await chapter_lifecycle.assert_publish_state(db, outline)
            await db.commit()
            refreshed = await db.get(Outline, data["ch_approved_id"])
            assert (
                refreshed.status,
                refreshed.lifecycle_status,
                refreshed.published_at,
            ) == before

    asyncio.run(_run())
    assert _reject_audits(env, data["ch_approved_id"]) == []


def test_assert_publish_state_rejects_and_marks_audit_pending(env, data) -> None:
    """门禁拒绝：抛 LifecycleRejected，审计标记为「已 flush 未提交」。"""

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            outline = await db.get(Outline, data["ch_archived_id"])
            with pytest.raises(chapter_lifecycle.LifecycleRejected) as excinfo:
                await chapter_lifecycle.assert_publish_state(db, outline)
            exc = excinfo.value
            assert exc.reason_code == audit_service.LIFECYCLE_REJECT_ARCHIVED
            assert exc.gate == "resource_archived"
            assert exc.audit_pending is True
            assert exc.from_status == "approved"
            assert exc.to_status == "approved"
            await db.rollback()

    asyncio.run(_run())


# ------------------------------------------------- 拒绝审计提交失败（U1a 补正）


def _patch_commit_failure(
    monkeypatch: pytest.MonkeyPatch, *, fail_rollback: bool = False
) -> dict[str, int]:
    """把 AsyncSession.commit 打成必抛异常，并统计 commit / rollback 调用次数。

    用于证明「拒绝审计提交失败」只降级为审计未持久化，既不吞掉 409，
    也不忘记尝试 rollback 清理失败事务。
    """
    calls = {"commit": 0, "rollback": 0}
    real_rollback = AsyncSession.rollback

    async def _commit(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["commit"] += 1
        raise RuntimeError("simulated rejection audit commit failure")

    async def _rollback(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["rollback"] += 1
        if fail_rollback:
            raise RuntimeError("simulated rollback failure")
        return await real_rollback(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(AsyncSession, "rollback", _rollback)
    return calls


def test_e1_rejection_audit_commit_failure_keeps_409(env, data, monkeypatch) -> None:
    """E1：拒绝审计 commit 失败 → 仍 409（不得变 500），业务列不变，审计不可见。"""
    outline_id = data["ch_taken_down_id"]
    before = _outline_state(env, outline_id)
    audits_before = len(_reject_audits(env, outline_id))

    calls = _patch_commit_failure(monkeypatch)
    response = _post(env, _url(E1_PATH, data, "ch_taken_down"))
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    assert set(response.json().keys()) == {"detail"}, "拒绝响应契约不得变动"
    assert "下架" in response.json()["detail"]
    assert snapshot["commit"] == 1, "拒绝路径只允许一次审计提交尝试"
    assert snapshot["rollback"] >= 1, "提交失败必须尝试 rollback"
    assert _outline_state(env, outline_id) == before, "不得写业务状态列"
    assert (
        len(_reject_audits(env, outline_id)) == audits_before
    ), "提交失败后审计行不应可见"


def test_e2_rejection_audit_commit_failure_keeps_409(env, data, monkeypatch) -> None:
    """E2：拒绝审计 commit 失败 → 仍 409 + 既有 error 契约（reason/gate 不变）。"""
    outline_id = data["ch_taken_down_empty_id"]
    before = _outline_state(env, outline_id)
    audits_before = len(_reject_audits(env, outline_id))

    calls = _patch_commit_failure(monkeypatch)
    response = _post(env, _url(E2_PATH, data, "ch_taken_down_empty"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "conflict"
    assert body["details"] == {
        "reason": audit_service.LIFECYCLE_REJECT_TAKEN_DOWN,
        "gate": "resource_taken_down",
    }, "不得新增 audited 等响应字段"
    assert snapshot["commit"] == 1
    assert snapshot["rollback"] >= 1
    assert _outline_state(env, outline_id) == before
    assert len(_reject_audits(env, outline_id)) == audits_before


def test_e3_rejection_audit_commit_failure_keeps_409(env, data, monkeypatch) -> None:
    """E3：拒绝审计 commit 失败 → 仍 409（HTTPException detail 契约不变）。"""
    outline_id = data["ch_taken_down_id"]
    before = _outline_state(env, outline_id)
    audits_before = len(_reject_audits(env, outline_id))

    calls = _patch_commit_failure(monkeypatch)
    response = _post(env, _url(E3_PATH, data, "ch_taken_down"))
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    payload = response.json()
    assert set(payload.keys()) == {"detail"}
    assert "下架" in payload["detail"]
    assert snapshot["commit"] == 1
    assert snapshot["rollback"] >= 1
    assert _outline_state(env, outline_id) == before
    assert len(_reject_audits(env, outline_id)) == audits_before


def test_rejection_audit_rollback_failure_still_409(env, data, monkeypatch) -> None:
    """rollback 自身失败也不得覆盖生命周期拒绝：E2 仍 409，业务列不变。"""
    outline_id = data["ch_archived_id"]
    before = _outline_state(env, outline_id)

    calls = _patch_commit_failure(monkeypatch, fail_rollback=True)
    response = _post(env, _url(E2_PATH, data, "ch_archived"), {})
    snapshot = dict(calls)
    monkeypatch.undo()

    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"]["reason"] == (
        audit_service.LIFECYCLE_REJECT_ARCHIVED
    )
    assert snapshot["commit"] == 1
    assert snapshot["rollback"] >= 1
    assert _outline_state(env, outline_id) == before


def test_e2_three_reject_reasons_each_exactly_one_audit(env, data) -> None:
    """E2 三类拒绝（archived / taken_down / 非 approved）各自恰好一条拒绝审计。"""
    cases = [
        ("ch_archived", audit_service.LIFECYCLE_REJECT_ARCHIVED, "resource_archived"),
        (
            "ch_taken_down",
            audit_service.LIFECYCLE_REJECT_TAKEN_DOWN,
            "resource_taken_down",
        ),
        (
            "ch_published",
            audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED,
            "state_machine",
        ),
    ]
    for chapter_key, reason_code, gate in cases:
        outline_id = data[chapter_key + "_id"]
        before = _outline_state(env, outline_id)
        audits_before = len(_reject_audits(env, outline_id))

        response = _post(env, _url(E2_PATH, data, chapter_key), {})

        assert response.status_code == 409, response.text
        assert response.json()["error"]["details"]["reason"] == reason_code
        assert response.json()["error"]["details"]["gate"] == gate
        assert _outline_state(env, outline_id) == before

        audits = _reject_audits(env, outline_id)
        assert len(audits) == audits_before + 1, (
            f"{chapter_key} 拒绝必须写且只写一条审计，"
            f"实际增量 {len(audits) - audits_before}"
        )
        assert audits[-1]["reason_code"] == reason_code
        assert audits[-1]["gate"] == gate


def test_e3_rejection_writes_exactly_one_audit(env, data) -> None:
    """E3：单次拒绝只产生一条审计（单次提交语义）。"""
    outline_id = data["ch_taken_down_id"]
    audits_before = len(_reject_audits(env, outline_id))

    response = _post(env, _url(E3_PATH, data, "ch_taken_down"))

    assert response.status_code == 409, response.text
    audits = _reject_audits(env, outline_id)
    assert len(audits) == audits_before + 1
    assert audits[-1]["reason_code"] == audit_service.LIFECYCLE_REJECT_TAKEN_DOWN


def test_successful_publish_commit_count_unchanged(env, data, monkeypatch) -> None:
    """正常发布路径的 commit 次数不受补正影响（补正只在拒绝路径生效）。"""
    calls = {"commit": 0}
    real_commit = AsyncSession.commit

    async def _commit(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["commit"] += 1
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    response = _post(env, _url(E1_PATH, data, "ch_approved"), {"force": True})
    count = calls["commit"]
    monkeypatch.undo()

    assert response.status_code in (200, 201), response.text
    assert count == SUCCESSFUL_PUBLISH_COMMITS, (
        f"正常发布事务次数变化：{count} != {SUCCESSFUL_PUBLISH_COMMITS}"
    )


def test_rejection_handlers_only_commit_within_audit_pending_branch() -> None:
    """结构保证：四处拒绝路径的提交/回滚只能发生在 audit_pending 分支内。

    这样 ``audit_pending=False``（审计未写入或已随业务事务提交）时，
    既不会产生额外提交，也不会产生多余 rollback。
    """
    from app.routers import chapter_contents, openapi_chapters, publications

    sources = {
        "E1": inspect.getsource(publications.publish_chapter),
        "E2": inspect.getsource(openapi_chapters._check_publish_preconditions),
        "E3-approve": inspect.getsource(chapter_contents.approve_chapter),
        "E3-publish": inspect.getsource(chapter_contents.publish_chapter),
    }
    for label, src in sources.items():
        assert "except LifecycleRejected as exc:" in src, label
        block = src[src.index("except LifecycleRejected as exc:") :].split("raise", 1)[0]
        assert "if exc.audit_pending:" in block, f"{label} 缺少 audit_pending 判断"
        assert "commit_pending_rejection_audit(db)" in block, f"{label} 未走统一 helper"
        assert "await db.commit()" not in block, f"{label} 仍有裸提交（可能把 409 变 500）"
        assert "await db.rollback()" not in block, f"{label} 仍有裸 rollback"
