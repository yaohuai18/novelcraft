# -*- coding: utf-8 -*-
"""阶段 1 D3 回归：校验报告的正文版本锚点（硬校验）。

覆盖批次任务书要求的语义：
    D3  after_content_saved 写报告时必须记录 ``validation_anchor``，
        其中 ``content_version`` 为本次校验所依据的正文版本（硬锚点）；
    D3  approve / publish 拒绝「缺锚点」的历史报告；
    D3  approve / publish 拒绝「锚定版本 != 当前正文版本」的过期报告；
    D3  ``facts_version`` 变化只产生非阻断提示（message / warnings /
        facts_version_hint），不拦截 approve / publish；
    D3  拒绝分支不写库：状态与报告保持原样。

硬约束：
    - 不启动服务、不监听端口、不发起网络请求（tests/conftest.py 离线护栏生效）；
    - 仅使用仓库外 pytest 临时目录中的 SQLite 文件库 + ORM create_all，
      不触碰 backend/novelcraft.db、不执行迁移、不连正式库；
    - 不改动 publication_service / content_ops_service / openapi_chapters。

Run: cd backend && python -m pytest tests/test_phase1_lifecycle_anchor.py -q
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.database import Base
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services import chapter_lifecycle


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库（每个 asyncio.run 独立事件循环，用 NullPool）。"""
    db_dir = tmp_path_factory.mktemp("phase1_lifecycle_anchor")
    db_path = db_dir / "phase1_lifecycle_anchor.db"

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
        yield {"engine": engine, "SessionLocal": session_factory}
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture(scope="module")
def ids(env) -> dict[str, Any]:
    """种子 owner + project（章节在各自用例内创建，避免状态互相污染）。"""

    async def _seed() -> dict[str, Any]:
        async with env["SessionLocal"]() as db:
            owner = User(
                username="d3_anchor_owner",
                email="d3_anchor_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()
            project = Project(user_id=owner.id, title="D3 锚点项目")
            db.add(project)
            await db.commit()
            return {"project_id": project.id}

    return asyncio.run(_seed())


@pytest.fixture()
def clean_validator(monkeypatch):
    """把确定性校验替换为“零问题通过”，只在锚点语义上做断言。"""

    async def _no_issues(*args, **kwargs):
        return []

    monkeypatch.setattr(chapter_lifecycle, "run_fact_validation", _no_issues)
    return None


async def _new_chapter(
    env: dict[str, Any],
    project_id: Any,
    *,
    status: str = "generated",
    content: str = "正文内容",
    report: dict | None = None,
    content_version: int = 1,
    facts_version: int = 0,
) -> tuple[Any, Any]:
    async with env["SessionLocal"]() as db:
        outline = Outline(
            project_id=project_id,
            title=f"D3-{status}-{uuid.uuid4().hex[:8]}",
            outline_type="chapter",
            lifecycle_status=status,
            validation_report=json.dumps(report) if report is not None else None,
            facts_version=facts_version,
        )
        db.add(outline)
        await db.flush()
        chapter_content = ChapterContent(
            outline_id=outline.id,
            content=content,
            lifecycle_status=status,
            version=content_version,
            facts_version=facts_version,
        )
        db.add(chapter_content)
        await db.commit()
        return outline.id, chapter_content.id


def _mk(env: dict[str, Any], project_id: Any, **kwargs) -> tuple[Any, Any]:
    return asyncio.run(_new_chapter(env, project_id, **kwargs))


async def _load(env: dict[str, Any], outline_id: Any) -> Outline:
    async with env["SessionLocal"]() as db:
        return (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()


async def _load_content(env: dict[str, Any], content_id: Any) -> ChapterContent:
    async with env["SessionLocal"]() as db:
        return (
            await db.execute(
                select(ChapterContent).where(ChapterContent.id == content_id)
            )
        ).scalar_one()


async def _save(env: dict[str, Any], outline_id: Any, content_id: Any) -> dict:
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        chapter_content = (
            await db.execute(
                select(ChapterContent).where(ChapterContent.id == content_id)
            )
        ).scalar_one()
        return await chapter_lifecycle.after_content_saved(
            db, outline, chapter_content, None, mark_following=False
        )


async def _bump_content_version(env: dict[str, Any], content_id: Any) -> int:
    """模拟“绕过 after_content_saved 的正文改动”（恢复/同步/采纳等路径）。"""
    async with env["SessionLocal"]() as db:
        chapter_content = (
            await db.execute(
                select(ChapterContent).where(ChapterContent.id == content_id)
            )
        ).scalar_one()
        chapter_content.version = chapter_content.version + 1
        chapter_content.content = chapter_content.content + "（改动）"
        await db.commit()
        return chapter_content.version


async def _bump_facts_version(env: dict[str, Any], outline_id: Any, delta: int = 1) -> int:
    """模拟后台事实抽取异步 bump facts_version（正文未改）。"""
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        outline.facts_version = (outline.facts_version or 0) + delta
        await db.commit()
        return outline.facts_version


async def _approve(env: dict[str, Any], outline_id: Any):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.approve_chapter(db, outline)


async def _publish(env: dict[str, Any], outline_id: Any):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.publish_chapter(db, outline)


def _report_with_anchor(content_version: int | None, facts_version: int = 0) -> dict:
    report: dict[str, Any] = {
        "issues": [],
        "validated": True,
        "validation_error": None,
        "needs_recheck": False,
    }
    if content_version is not None:
        report["validation_anchor"] = {
            "content_version": content_version,
            "facts_version": facts_version,
            "outline_version_id": None,
            "anchored_at": "2026-01-01T00:00:00",
        }
    return report


# --------------------------------------------------------------------- D3 写锚点


def test_after_save_records_content_version_anchor(env, ids, clean_validator) -> None:
    """保存后报告必须记录 content_version / facts_version 锚点。"""
    outline_id, content_id = _mk(env, ids["project_id"], content_version=3, facts_version=2)

    asyncio.run(_save(env, outline_id, content_id))

    outline = asyncio.run(_load(env, outline_id))
    report = json.loads(outline.validation_report)
    anchor = report["validation_anchor"]
    content = asyncio.run(_load_content(env, content_id))
    assert anchor["content_version"] == content.version == 3
    assert anchor["facts_version"] == 2
    assert anchor["anchored_at"]


def test_after_save_reanchors_to_latest_content_version(env, ids, clean_validator) -> None:
    """正文版本变化后重新保存 → 锚点刷新为新版本，approve 可通过。"""
    outline_id, content_id = _mk(
        env, ids["project_id"], report=_report_with_anchor(1)
    )
    new_version = asyncio.run(_bump_content_version(env, content_id))

    asyncio.run(_save(env, outline_id, content_id))

    outline = asyncio.run(_load(env, outline_id))
    report = json.loads(outline.validation_report)
    assert report["validation_anchor"]["content_version"] == new_version

    assert asyncio.run(_approve(env, outline_id))["lifecycle_status"] == "approved"


def test_anchor_recorded_even_when_validation_raised(env, ids, monkeypatch) -> None:
    """校验异常时仍记录锚点（但 validated=False，门禁照样拒绝）。"""
    outline_id, content_id = _mk(env, ids["project_id"], content_version=4)

    async def _boom(*args, **kwargs):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(chapter_lifecycle, "run_fact_validation", _boom)

    asyncio.run(_save(env, outline_id, content_id))

    outline = asyncio.run(_load(env, outline_id))
    report = json.loads(outline.validation_report)
    assert report["validated"] is False
    assert report["validation_anchor"]["content_version"] == 4

    with pytest.raises(ValueError):
        asyncio.run(_approve(env, outline_id))


# --------------------------------------------------------------------- D3 缺锚点


def test_approve_rejects_report_without_anchor(env, ids) -> None:
    """缺锚点的历史报告（validated 为 True）不得批准，且拒绝分支不写库。"""
    report = _report_with_anchor(None)
    outline_id, _ = _mk(env, ids["project_id"], report=report)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_approve(env, outline_id))
    assert "锚点" in str(excinfo.value)

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "generated"
    assert json.loads(outline.validation_report) == report


def test_publish_rejects_report_without_anchor(env, ids) -> None:
    """缺锚点报告同样不能发布（approved 状态也不行）。"""
    outline_id, _ = _mk(
        env,
        ids["project_id"],
        status="approved",
        report=_report_with_anchor(None),
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "锚点" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "approved"


def test_approve_rejects_incomplete_anchor(env, ids) -> None:
    """锚点结构残缺（缺 content_version）按缺锚点拒绝。"""
    report = _report_with_anchor(None)
    report["validation_anchor"] = {"facts_version": 0, "anchored_at": "2026-01-01T00:00:00"}
    outline_id, _ = _mk(env, ids["project_id"], report=report)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_approve(env, outline_id))
    assert "锚点" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "generated"


# --------------------------------------------------------------------- D3 版本漂移


def test_approve_rejects_stale_anchor(env, ids) -> None:
    """正文版本已变化 → 报告过期，approve 拒绝且状态/报告原样保留。"""
    report = _report_with_anchor(1)
    outline_id, content_id = _mk(
        env, ids["project_id"], report=report, content_version=1
    )
    asyncio.run(_bump_content_version(env, content_id))  # → v2

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_approve(env, outline_id))
    message = str(excinfo.value)
    assert "v1" in message and "v2" in message

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "generated"
    assert json.loads(outline.validation_report) == report


def test_publish_rejects_stale_anchor(env, ids) -> None:
    """审批后正文被改动 → 发布时报告过期，拒绝且保持 approved。"""
    outline_id, content_id = _mk(
        env,
        ids["project_id"],
        status="generated",
        report=_report_with_anchor(1),
    )
    assert asyncio.run(_approve(env, outline_id))["lifecycle_status"] == "approved"

    asyncio.run(_bump_content_version(env, content_id))

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "过期" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "approved"


def test_stale_anchor_after_resave_is_accepted(env, ids, clean_validator) -> None:
    """过期报告经重新保存（重校验 + 重锚）后可正常审批。"""
    outline_id, content_id = _mk(env, ids["project_id"], report=_report_with_anchor(1))
    asyncio.run(_bump_content_version(env, content_id))

    with pytest.raises(ValueError):
        asyncio.run(_approve(env, outline_id))

    asyncio.run(_save(env, outline_id, content_id))

    assert asyncio.run(_approve(env, outline_id))["lifecycle_status"] == "approved"


# --------------------------------------------------------------------- D3 facts_version 仅提示


def test_facts_version_change_is_non_blocking_hint(env, ids) -> None:
    """facts_version 变化（正文未变）不阻断审批，只给出提示。"""
    outline_id, _ = _mk(
        env,
        ids["project_id"],
        report=_report_with_anchor(1, facts_version=0),
        facts_version=0,
    )
    asyncio.run(_bump_facts_version(env, outline_id, 2))  # 后台事实抽取 → v2

    result = asyncio.run(_approve(env, outline_id))
    assert result["lifecycle_status"] == "approved"
    assert result["blocking"] == 0
    hint = result["facts_version_hint"]
    assert hint is not None and hint["blocking"] is False
    assert hint["anchored_facts_version"] == 0
    assert hint["current_facts_version"] == 2
    assert result["warnings"] and result["message"]

    outline = asyncio.run(_load(env, outline_id))
    report = json.loads(outline.validation_report)
    assert report["facts_version_hint"]["blocking"] is False
    assert report["validation_anchor"]["content_version"] == 1


def test_facts_version_change_does_not_block_publish(env, ids) -> None:
    """发布侧同样只提示不阻断。"""
    outline_id, _ = _mk(
        env,
        ids["project_id"],
        status="approved",
        report=_report_with_anchor(1, facts_version=0),
        facts_version=0,
    )
    asyncio.run(_bump_facts_version(env, outline_id, 1))

    result = asyncio.run(_publish(env, outline_id))
    assert result["lifecycle_status"] == "published"
    assert result["facts_version_hint"] is not None
    assert result["facts_version_hint"]["blocking"] is False


def test_no_hint_when_facts_version_unchanged(env, ids) -> None:
    """facts_version 未变化时不出提示（message 保持 None）。"""
    outline_id, _ = _mk(
        env, ids["project_id"], report=_report_with_anchor(1, facts_version=2), facts_version=2
    )

    result = asyncio.run(_approve(env, outline_id))
    assert result["facts_version_hint"] is None
    assert result["warnings"] == []
    assert result["message"] is None
