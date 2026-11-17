# -*- coding: utf-8 -*-
"""阶段 1 A2 回归：生命周期流转审计（chapter_lifecycle）。

覆盖：
    A2-1  生命周期 action 与 CONTENT_AUDIT_ACTIONS 分域，内容审计写入器仍拒绝 lifecycle_*；
    A2-2  拒绝事件 reason_code 覆盖：state_not_allowed / validation_report_missing /
          validation_anchor_missing / validation_anchor_stale / content_version_unknown；
    A2-3  content_version_unknown：锚点存在但当前正文 version 不可解析 → 校验门禁拒绝，
          绝不按「无版本信息可放行」处理；
    A2-4  纯门禁拒绝：service 不提交，路由在 409 前提交，事件恰好落库一条；
    A2-5  blocking 拒绝：与 validation_failed 同一事务提交，路由不重复提交；
    A2-6  成功事件在业务 commit 之前写入，随业务事务一次提交；
    A2-7  actor 固定 None / "system"，target_type 固定 chapter_lifecycle；
    A2-8  审计开关关闭（content_audit_enabled=False）不写库且不改变业务结果；
    A2-9  审计写入失败（DB 触发器注入）不改变成功/拒绝门禁结果，失败记录 target_type 正确；
    A2-10 set_lifecycle_status：未知状态值抛 ValueError 且不审计；未提交不可见、调用方提交后可见；
    A2-11 生命周期事件不污染正文 diff 基准（_last_known_state 仍只认 chapter_content）；
    A2-12 未知异常不被路由转成 409（继续向上抛出，测试内表现为 500）；
    A2-13 action / target_type 落库字面量锁定 A2 契约（lifecycle_status 而非 lifecycle_status_change）；
    A2-14 record_lifecycle_audit_result 默认不提交（commit 默认 False，调用方 rollback 后不落库）。

硬约束：
    - 不启动服务、不监听端口、不发起网络请求（tests/conftest.py 离线护栏）；
    - 仅使用仓库外临时 SQLite 文件库 + ORM create_all，不连接 backend/novelcraft.db；
    - 不改模型 / schema / 迁移 / 配置，不改 CONTENT_AUDIT_ACTIONS。

Run: cd backend && python -m pytest tests/test_phase1_lifecycle_audit.py -q
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.routers.chapter_contents import router as chapter_contents_router
from app.services import audit_service
from app.services.chapter_lifecycle import LifecycleRejected, set_lifecycle_status
from app.services.fact_validator import BLOCKING

APPROVE_URL = "/api/projects/{project_id}/chapters/{outline_id}/lifecycle/approve"
PUBLISH_URL = "/api/projects/{project_id}/chapters/{outline_id}/lifecycle/publish"

_TRIGGER_NAME = "a2_block_audit_write"


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库 + 只挂 chapter_contents 路由的进程内 ASGI 应用。"""
    db_dir = tmp_path_factory.mktemp("phase1_lifecycle_audit")
    db_path = db_dir / "phase1_lifecycle_audit.db"

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

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(chapter_contents_router)

    try:
        yield {"app": app, "engine": engine, "SessionLocal": session_factory,
               "db_path": str(db_path)}
    finally:
        asyncio.run(engine.dispose())


def _client(env: dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"], raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _report(version: int | None = 1, *, validated: bool = True,
            issues: list[dict] | None = None, anchor: bool | str = True) -> str:
    """构造校验报告（可精确控制锚点与阻断项）。"""
    data: dict[str, Any] = {"validated": validated}
    if anchor is True:
        data["validation_anchor"] = {"content_version": version}
    elif isinstance(anchor, dict):
        data["validation_anchor"] = anchor
    if issues:
        data["issues"] = issues
    return json.dumps(data, ensure_ascii=False)


def _seed(
    env: dict[str, Any],
    suffix: str,
    *,
    lifecycle: str = "generated",
    report: str | None = None,
    version: Any = 1,
) -> dict[str, Any]:
    """每个用例独立种子：user + project + outline + chapter_content。"""

    async def _run() -> dict[str, Any]:
        async with env["SessionLocal"]() as db:
            owner = User(
                username=f"a2_owner_{suffix}",
                email=f"a2_owner_{suffix}@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()
            project = Project(user_id=owner.id, title=f"A2 项目 {suffix}")
            db.add(project)
            await db.flush()
            outline = Outline(
                project_id=project.id,
                title=f"A2 章节 {suffix}",
                outline_type="chapter",
                lifecycle_status=lifecycle,
                validation_report=report,
            )
            db.add(outline)
            await db.flush()
            chapter = ChapterContent(
                outline_id=outline.id,
                content="A2 章节正文",
                word_count=6,
                version=version,
                lifecycle_status=lifecycle,
            )
            db.add(chapter)
            await db.commit()
            return {
                "owner_id": owner.id,
                "project_id": project.id,
                "outline_id": outline.id,
                "chapter_id": chapter.id,
            }

    return asyncio.run(_run())


def _auth(env: dict[str, Any], ids: dict[str, Any]) -> None:
    env["app"].dependency_overrides[get_current_user] = (
        lambda: SimpleNamespace(id=ids["owner_id"])
    )


def _url(template: str, ids: dict[str, Any]) -> str:
    return template.format(project_id=ids["project_id"], outline_id=ids["outline_id"])


def _post(env: dict[str, Any], url: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(url)

    return asyncio.run(_run())


def _spy_commits(monkeypatch) -> list[str]:
    """统计 Commit 次数（service 与路由都在 AsyncSession.commit 上留下痕迹）。"""
    calls: list[str] = []
    original = AsyncSession.commit

    async def _commit(self):  # noqa: ANN001
        calls.append("commit")
        return await original(self)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    return calls


def _logs_sync(env: dict[str, Any], action: str | None = None,
               target_id: Any = None) -> list[AuditLog]:
    async def _run() -> list[AuditLog]:
        async with env["SessionLocal"]() as db:
            return await _logs_async(db, action, target_id)

    return asyncio.run(_run())


async def _logs_async(db: AsyncSession, action: str | None = None,
                      target_id: Any = None) -> list[AuditLog]:
    """在给定的 db 会话上查询（供事件循环内部复核可见性）。"""
    stmt = select(AuditLog)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == str(target_id))
    return list((await db.execute(stmt)).scalars().all())


def _peek(env: dict[str, Any], action: str, target_id: Any) -> int:
    """用独立连接数一遍（验证「未提交不可见」）。"""

    async def _run() -> int:
        async with env["SessionLocal"]() as db:
            return len(await _logs_async(db, action, target_id))

    return asyncio.run(_run())


def _only_log(logs: list[AuditLog]) -> AuditLog:
    assert len(logs) == 1, f"期望恰好 1 条事件，实际 {len(logs)} 条"
    return logs[0]


def _meta(log: AuditLog) -> dict:
    return json.loads(log.metadata_json or "{}")


def _diff(log: AuditLog) -> dict:
    return json.loads(log.diff_json or "{}")


def _outline_sync(env: dict[str, Any], outline_id: Any) -> Outline:
    async def _run() -> Outline:
        async with env["SessionLocal"]() as db:
            return (
                await db.execute(select(Outline).where(Outline.id == outline_id))
            ).scalar_one()

    return asyncio.run(_run())


def _set_chapter_version_raw(env: dict[str, Any], chapter_id: Any, raw: str) -> None:
    """把 chapter_contents.version 写成不可解析为 int 的值（模拟版本损坏）。

    SQLite 的 INTEGER 亲和性对非数值文本不做转换，因此落库后可读到非数值，
    正好覆盖 `_as_int(version) is None` 这条门禁分支。
    """

    async def _run() -> None:
        async with env["engine"].begin() as conn:
            await conn.execute(
                update(ChapterContent)
                .where(ChapterContent.id == chapter_id)
                .values(version=raw)
            )

    asyncio.run(_run())


def _install_failing_trigger(env: dict[str, Any], action: str) -> None:
    """DB 级故障注入：仅针对指定 action 的审计插入（不涉及生产 schema）。"""

    async def _run() -> None:
        async with env["engine"].begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_NAME} "
                "BEFORE INSERT ON audit_logs "
                f"WHEN NEW.action = '{action}' "
                "BEGIN SELECT RAISE(ABORT, 'a2 injected audit write failure'); END"
            )

    asyncio.run(_run())


def _drop_failing_trigger(env: dict[str, Any]) -> None:
    async def _run() -> None:
        async with env["engine"].begin() as conn:
            await conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")

    asyncio.run(_run())


# --------------------------------------------------------------------- 用例


def test_lifecycle_actions_are_disjoint_from_content_audit(env) -> None:
    """A2-1：不扩大 CONTENT_AUDIT_ACTIONS；内容审计写入器仍拒绝 lifecycle_*。"""
    assert audit_service.CONTENT_AUDIT_ACTIONS == (
        audit_service.ACTION_CONTENT_SAVE,
        audit_service.ACTION_CONTENT_RESTORE,
        audit_service.ACTION_AI_APPLY,
        audit_service.ACTION_BACKFILL,
    )
    for action in audit_service.LIFECYCLE_AUDIT_ACTIONS:
        assert action not in audit_service.CONTENT_AUDIT_ACTIONS

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            with pytest.raises(ValueError, match="不支持的内容审计 action"):
                await audit_service.record_content_audit_result(
                    db,
                    action=audit_service.ACTION_LIFECYCLE_APPROVE,
                    project_id=None,
                    target_id=None,
                )

    asyncio.run(_run())

    # record_audit_failure 默认 target_type 不变（A1 调用方行为不破）
    audit_service.reset_audit_failures()
    default_entry = audit_service.record_audit_failure(action="content_save")
    assert default_entry["target_type"] == audit_service.CONTENT_AUDIT_TARGET_TYPE
    custom_entry = audit_service.record_audit_failure(
        action="lifecycle_approve",
        target_type=audit_service.LIFECYCLE_AUDIT_TARGET_TYPE,
    )
    assert custom_entry["target_type"] == audit_service.LIFECYCLE_AUDIT_TARGET_TYPE
    audit_service.reset_audit_failures()


def test_approve_success_writes_single_lifecycle_event(env, monkeypatch) -> None:
    """A2-6 / A2-7：成功事件随业务事务提交，字段契约完整。"""
    ids = _seed(env, "approve_ok", report=_report(1), version=1)
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 200, response.text
    log = _only_log(_logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE, ids["outline_id"]))
    assert log.target_type == audit_service.LIFECYCLE_AUDIT_TARGET_TYPE
    assert log.target_id == str(ids["outline_id"])
    assert log.project_id == str(ids["project_id"])
    assert log.actor_id is None
    assert log.actor_type == "system"
    assert log.change_source == "lifecycle:approve"

    meta = _meta(log)
    assert meta["from_status"] == "generated"
    assert meta["to_status"] == "approved"
    assert meta["gate"] == "approve"
    assert meta["content_version"] == 1
    assert meta["reason_code"] is None
    assert meta["actor_known"] is False

    diff = _diff(log)
    assert diff["changed_fields"] == ["lifecycle_status"]
    assert diff["content_changed"] is False
    assert log.content_hash_before is None and log.content_hash_after is None

    # 既有行为未变：service 业务提交 1 次 + 路由收尾 commit 1 次
    assert len(commits) == 2
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "approved"


def test_approve_reject_state_not_allowed_single_commit(env, monkeypatch) -> None:
    """A2-2 / A2-4：纯门禁拒绝 → service 不提交，路由提交 1 次，事件仅 1 条。"""
    ids = _seed(env, "approve_state", lifecycle="approved", report=_report(1))
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 409, response.text
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, ids["outline_id"])
    )
    meta = _meta(log)
    assert meta["reason_code"] == audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED
    assert meta["gate"] == "state_machine"
    assert meta["from_status"] == "approved"
    assert meta["to_status"] == "approved"
    assert _diff(log)["changed_fields"] == []
    assert log.actor_id is None and log.actor_type == "system"
    assert len(commits) == 1, "纯拒绝必须由路由恰好提交一次"
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "approved"


def test_approve_reject_validation_report_missing(env, monkeypatch) -> None:
    """A2-2：无有效校验报告 → validation_report_missing。"""
    ids = _seed(env, "approve_report", report=None)
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 409, response.text
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, ids["outline_id"])
    )
    meta = _meta(log)
    assert meta["reason_code"] == audit_service.LIFECYCLE_REJECT_VALIDATION_REPORT_MISSING
    assert meta["gate"] == "validation_report"
    assert len(commits) == 1


def test_approve_reject_anchor_missing_and_stale(env, monkeypatch) -> None:
    """A2-2：缺锚点与锚点过期必须可区分。"""
    missing = _seed(env, "anchor_missing", report=_report(anchor=False))
    _auth(env, missing)
    commits = _spy_commits(monkeypatch)
    assert _post(env, _url(APPROVE_URL, missing)).status_code == 409
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, missing["outline_id"])
    )
    assert _meta(log)["reason_code"] == audit_service.LIFECYCLE_REJECT_ANCHOR_MISSING
    assert _meta(log)["gate"] == "validation_anchor"
    assert len(commits) == 1

    stale = _seed(env, "anchor_stale", report=_report(1), version=2)
    _auth(env, stale)
    commits.clear()
    assert _post(env, _url(APPROVE_URL, stale)).status_code == 409
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, stale["outline_id"])
    )
    assert _meta(log)["reason_code"] == audit_service.LIFECYCLE_REJECT_ANCHOR_STALE
    assert _meta(log)["content_version"] == 2
    assert len(commits) == 1


def test_approve_reject_content_version_unknown_blocks(env, monkeypatch) -> None:
    """A2-3：锚点存在但当前正文 version 不可解析 → 拒绝，绝不当作可放行。"""
    ids = _seed(env, "version_unknown", report=_report(1), version=1)
    _set_chapter_version_raw(env, ids["chapter_id"], "not-a-number")
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 409, response.text
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, ids["outline_id"])
    )
    meta = _meta(log)
    assert meta["reason_code"] == audit_service.LIFECYCLE_REJECT_CONTENT_VERSION_UNKNOWN
    assert meta["gate"] == "validation_anchor"
    assert meta["content_version"] is None
    assert len(commits) == 1
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "generated"


def test_approve_reject_blocking_commits_with_validation_failed(env, monkeypatch) -> None:
    """A2-5：blocking 拒绝与 validation_failed 同事务提交，路由不重复提交。"""
    report = _report(
        1,
        issues=[{"severity": BLOCKING, "code": "conflict", "message": "设定冲突"}],
    )
    ids = _seed(env, "approve_blocking", report=report)
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 409, response.text
    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED, ids["outline_id"])
    )
    meta = _meta(log)
    assert meta["reason_code"] == audit_service.LIFECYCLE_REJECT_BLOCKING
    assert meta["from_status"] == "generated"
    assert meta["to_status"] == "validation_failed"
    assert meta["blocking_count"] == 1
    # 状态与事件必须同时可见（同一次 commit）
    outline = _outline_sync(env, ids["outline_id"])
    assert outline.lifecycle_status == "validation_failed"
    assert outline.repair_records is not None
    assert len(commits) == 1, "blocking 拒绝只允许 service 内那一次提交"


def test_publish_success_and_reject_paths(env, monkeypatch) -> None:
    """A2-6 / A2-2：发布成功写 lifecycle_publish；非 approved 来源拒绝。"""
    ok = _seed(env, "publish_ok", lifecycle="approved", report=_report(1))
    _auth(env, ok)
    assert _post(env, _url(PUBLISH_URL, ok)).status_code == 200
    log = _only_log(_logs_sync(env, audit_service.ACTION_LIFECYCLE_PUBLISH, ok["outline_id"]))
    meta = _meta(log)
    assert meta["from_status"] == "approved"
    assert meta["to_status"] == "published"
    assert meta["gate"] == "publish"
    assert log.target_type == audit_service.LIFECYCLE_AUDIT_TARGET_TYPE
    assert _outline_sync(env, ok["outline_id"]).lifecycle_status == "published"

    bad = _seed(env, "publish_state", lifecycle="generated", report=_report(1))
    _auth(env, bad)
    commits = _spy_commits(monkeypatch)
    assert _post(env, _url(PUBLISH_URL, bad)).status_code == 409
    reject = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED, bad["outline_id"])
    )
    assert _meta(reject)["reason_code"] == audit_service.LIFECYCLE_REJECT_STATE_NOT_ALLOWED
    assert len(commits) == 1


def test_publish_blocking_commits_with_validation_failed(env, monkeypatch) -> None:
    """A2-5：发布侧 blocking 同样与 validation_failed 同事务。"""
    report = _report(
        1,
        issues=[{"severity": BLOCKING, "code": "conflict", "message": "设定冲突"}],
    )
    ids = _seed(env, "publish_blocking", lifecycle="approved", report=report)
    _auth(env, ids)
    commits = _spy_commits(monkeypatch)

    assert _post(env, _url(PUBLISH_URL, ids)).status_code == 409

    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED, ids["outline_id"])
    )
    assert _meta(log)["reason_code"] == audit_service.LIFECYCLE_REJECT_BLOCKING
    assert _meta(log)["to_status"] == "validation_failed"
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "validation_failed"
    assert len(commits) == 1


def test_audit_disabled_keeps_business_result(env, monkeypatch) -> None:
    """A2-8：开关关闭 → 不写审计，业务结果不变。"""
    monkeypatch.setattr(settings, "content_audit_enabled", False, raising=False)
    ids = _seed(env, "disabled", report=_report(1))
    _auth(env, ids)

    assert _post(env, _url(APPROVE_URL, ids)).status_code == 200
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE, ids["outline_id"]) == []
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "approved"

    rejected = _seed(env, "disabled_reject", lifecycle="approved", report=_report(1))
    _auth(env, rejected)
    commits = _spy_commits(monkeypatch)
    assert _post(env, _url(APPROVE_URL, rejected)).status_code == 409
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
                 rejected["outline_id"]) == []
    # 无审计行可提交 → 路由不得产生空提交
    assert commits == []


def test_audit_write_failure_does_not_change_gate_result(env, monkeypatch) -> None:
    """A2-9：审计写失败被隔离，成功/拒绝门禁结果不变，且失败可观察。"""
    ids = _seed(env, "fail_success", report=_report(1))
    _auth(env, ids)
    audit_service.reset_audit_failures()
    _install_failing_trigger(env, audit_service.ACTION_LIFECYCLE_APPROVE)
    try:
        assert _post(env, _url(APPROVE_URL, ids)).status_code == 200
        assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE, ids["outline_id"]) == []
        assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "approved"
        failures = audit_service.list_audit_failures()
        assert failures and failures[0]["phase"] == "record_lifecycle_audit"
        assert failures[0]["target_type"] == audit_service.LIFECYCLE_AUDIT_TARGET_TYPE
        assert failures[0]["action"] == audit_service.ACTION_LIFECYCLE_APPROVE
    finally:
        _drop_failing_trigger(env)

    rejected = _seed(env, "fail_reject", lifecycle="approved", report=_report(1))
    _auth(env, rejected)
    audit_service.reset_audit_failures()
    _install_failing_trigger(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED)
    try:
        commits = _spy_commits(monkeypatch)
        assert _post(env, _url(APPROVE_URL, rejected)).status_code == 409
        # 审计没写成 → audit_pending=False → 路由不提交
        assert commits == []
        assert audit_service.audit_failure_count() == 1
    finally:
        _drop_failing_trigger(env)
        audit_service.reset_audit_failures()


def test_set_lifecycle_status_pending_not_visible_until_caller_commit(env) -> None:
    """A2-10：service 内不提交；调用方 commit 后事件才可见。"""
    ids = _seed(env, "set_pending", report=_report(1))

    async def _run() -> None:
        db = env["SessionLocal"]()
        try:
            outline = await db.get(Outline, ids["outline_id"])
            with pytest.raises(LifecycleRejected) as excinfo:
                await set_lifecycle_status(db, outline, "approved")
            assert "只能通过审批/发布接口写入" in str(excinfo.value)
            assert excinfo.value.reason_code == audit_service.LIFECYCLE_REJECT_TARGET_NOT_WRITABLE
            assert excinfo.value.audit_pending is True
            # 未提交：另开独立连接不可见
            async with env["SessionLocal"]() as other:
                assert await _logs_async(
                    other,
                    audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
                    ids["outline_id"],
                ) == []
            await db.commit()
            # 调用方提交后：独立连接恰好看到一条
            async with env["SessionLocal"]() as other:
                visible = await _logs_async(
                    other,
                    audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
                    ids["outline_id"],
                )
            assert len(visible) == 1
        finally:
            await db.close()

    asyncio.run(_run())

    log = _only_log(
        _logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS_REJECTED, ids["outline_id"])
    )
    meta = _meta(log)
    assert meta["reason_code"] == audit_service.LIFECYCLE_REJECT_TARGET_NOT_WRITABLE
    assert meta["from_status"] == "generated"
    assert meta["to_status"] == "approved"
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "generated"


def test_set_lifecycle_status_unknown_target_writes_no_audit(env) -> None:
    """A2-10：未知状态值是入参错误（ValueError），不产生拒绝审计。"""
    ids = _seed(env, "set_unknown")

    async def _run() -> None:
        db = env["SessionLocal"]()
        try:
            outline = await db.get(Outline, ids["outline_id"])
            with pytest.raises(ValueError) as excinfo:
                await set_lifecycle_status(db, outline, "not_a_state")
            assert not isinstance(excinfo.value, LifecycleRejected)
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_run())
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
                 ids["outline_id"]) == []


def test_set_lifecycle_status_success_and_published_guard(env) -> None:
    """A2-10：直写成功写 lifecycle_status；已发布章节改写被拒绝且只审计一次。"""
    ids = _seed(env, "set_ok", lifecycle="generated")

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            outline = await db.get(Outline, ids["outline_id"])
            await set_lifecycle_status(db, outline, "draft")

    asyncio.run(_run())
    log = _only_log(_logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS, ids["outline_id"]))
    # A2 契约（字面量断言，防常量漂移）：action=lifecycle_status，target_type=chapter_lifecycle
    assert log.action == "lifecycle_status"
    assert log.target_type == "chapter_lifecycle"
    assert _meta(log)["from_status"] == "generated"
    assert _meta(log)["to_status"] == "draft"
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
                 ids["outline_id"]) == []

    published = _seed(env, "set_published", lifecycle="published")
    for _ in range(2):  # 连续两次拒绝也不允许重复落库
        async def _attempt() -> None:
            db = env["SessionLocal"]()
            try:
                outline = await db.get(Outline, published["outline_id"])
                with pytest.raises(LifecycleRejected) as excinfo:
                    await set_lifecycle_status(db, outline, "draft")
                assert excinfo.value.reason_code == audit_service.LIFECYCLE_REJECT_ALREADY_PUBLISHED
                await db.commit()
            finally:
                await db.close()

        asyncio.run(_attempt())

    rejects = _logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS_REJECTED,
                    published["outline_id"])
    assert len(rejects) == 2, "两次拒绝各一条（每次只落一条，无重复）"
    assert {_meta(r)["reason_code"] for r in rejects} == {
        audit_service.LIFECYCLE_REJECT_ALREADY_PUBLISHED
    }


def test_lifecycle_events_do_not_pollute_content_diff_base(env) -> None:
    """A2-11：生命周期事件不成为正文 diff 基准（_last_known_state 只认 chapter_content）。"""
    ids = _seed(env, "diff_base", report=_report(1))
    _auth(env, ids)
    assert _post(env, _url(APPROVE_URL, ids)).status_code == 200
    assert len(_logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE, ids["outline_id"])) == 1

    async def _run() -> Any:
        async with env["SessionLocal"]() as db:
            return await audit_service._last_known_state(db, ids["outline_id"])

    assert asyncio.run(_run()) is None


def test_unexpected_exception_is_not_converted_to_409(env, monkeypatch) -> None:
    """A2-12：路由只捕获 LifecycleRejected；未知异常继续上抛（此处表现为 500）。"""
    from app.services import chapter_lifecycle

    ids = _seed(env, "boom", report=_report(1))
    _auth(env, ids)

    def _boom(report):  # noqa: ANN001
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(chapter_lifecycle, "_collect_blocking_issues", _boom)

    response = _post(env, _url(APPROVE_URL, ids))

    assert response.status_code == 500, response.text
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED,
                 ids["outline_id"]) == []
    assert _outline_sync(env, ids["outline_id"]).lifecycle_status == "generated"


def test_lifecycle_action_contract_literals() -> None:
    """A2-13：action / target_type 落库字面量与 A2 裁定契约逐项一致。"""
    assert audit_service.LIFECYCLE_AUDIT_TARGET_TYPE == "chapter_lifecycle"
    assert audit_service.ACTION_LIFECYCLE_APPROVE == "lifecycle_approve"
    assert audit_service.ACTION_LIFECYCLE_PUBLISH == "lifecycle_publish"
    assert audit_service.ACTION_LIFECYCLE_STATUS == "lifecycle_status"
    assert audit_service.ACTION_LIFECYCLE_APPROVE_REJECTED == "lifecycle_approve_rejected"
    assert audit_service.ACTION_LIFECYCLE_PUBLISH_REJECTED == "lifecycle_publish_rejected"
    assert audit_service.ACTION_LIFECYCLE_STATUS_REJECTED == "lifecycle_status_rejected"
    assert set(audit_service.LIFECYCLE_AUDIT_ACTIONS) == {
        "lifecycle_approve",
        "lifecycle_publish",
        "lifecycle_status",
        "lifecycle_approve_rejected",
        "lifecycle_publish_rejected",
        "lifecycle_status_rejected",
    }
    # 与内容审计分域：生命周期 action 不得进入内容审计白名单
    assert set(audit_service.LIFECYCLE_AUDIT_ACTIONS).isdisjoint(
        set(audit_service.CONTENT_AUDIT_ACTIONS)
    )


def test_lifecycle_audit_default_does_not_commit(env) -> None:
    """A2-14：record_lifecycle_audit_result 默认不提交（commit 默认 False，rollback 后不落库）。"""
    import inspect

    signature = inspect.signature(audit_service.record_lifecycle_audit_result)
    assert signature.parameters["commit"].default is False

    ids = _seed(env, "no_commit")

    async def _run() -> None:
        async with env["SessionLocal"]() as db:
            result = await audit_service.record_lifecycle_audit_result(
                db,
                action=audit_service.ACTION_LIFECYCLE_STATUS,
                project_id=ids["project_id"],
                target_id=ids["outline_id"],
                from_status="generated",
                to_status="draft",
                reason="默认不提交校验",
            )
            assert result.written is True
            # 同事务内可见
            visible = await _logs_async(db, audit_service.ACTION_LIFECYCLE_STATUS,
                                       ids["outline_id"])
            assert len(visible) == 1
            await db.rollback()

    asyncio.run(_run())
    # 调用方未提交 → 事务回滚后不落库，证明该函数自身没有 commit
    assert _logs_sync(env, audit_service.ACTION_LIFECYCLE_STATUS, ids["outline_id"]) == []
