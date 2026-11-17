# -*- coding: utf-8 -*-
"""阶段 1 D4 回归：publish 成功后单向写透真实发布列。

覆盖：
    D4  发布成功时 outline.status → "published" 且 published_at 落值；
    D4  拒绝分支（缺锚点 / 锚点过期 / 仍存在阻断问题 / 非 approved 来源）
        不得写发布列，发布列保持原样；
    D4  历史已有 published_at（首次发布时间）不被覆盖；
    D4  set_lifecycle_status 仍不能作为绕过门禁直写 published 的后门。

硬约束：
    - 不启动服务、不监听端口、不发起网络请求（tests/conftest.py 离线护栏生效）；
    - 仅使用仓库外的临时 SQLite 文件库 + ORM create_all，
      不触碰 backend/novelcraft.db、不执行迁移、不连正式库；
    - 不改动 publication_service / content_ops_service / openapi_chapters。

Run: cd backend && python -m pytest tests/test_phase1_lifecycle_publish_writeback.py -q
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
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


# --------------------------------------------------------------------- 基础设置


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库（每个 asyncio.run 独立事件循环，用 NullPool）。"""
    db_dir = tmp_path_factory.mktemp("phase1_lifecycle_publish_writeback")
    db_path = db_dir / "phase1_lifecycle_publish_writeback.db"

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
                username="d4_writeback_owner",
                email="d4_writeback_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()
            project = Project(user_id=owner.id, title="D4 写透项目")
            db.add(project)
            await db.commit()
            return {"project_id": project.id}

    return asyncio.run(_seed())


async def _new_chapter(
    env: dict[str, Any],
    project_id: Any,
    *,
    status: str = "approved",
    content: str = "正文内容",
    report: dict | None = None,
    content_version: int = 1,
    facts_version: int = 0,
    published_at: datetime | None = None,
    outline_status: str = "draft",
) -> tuple[Any, Any]:
    async with env["SessionLocal"]() as db:
        outline = Outline(
            project_id=project_id,
            title=f"D4-{status}-{uuid.uuid4().hex[:8]}",
            outline_type="chapter",
            lifecycle_status=status,
            status=outline_status,
            validation_report=json.dumps(report) if report is not None else None,
            facts_version=facts_version,
            published_at=published_at,
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


async def _publish(env: dict[str, Any], outline_id: Any):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.publish_chapter(db, outline)


async def _direct_write(env: dict[str, Any], outline_id: Any, target: str) -> dict:
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.set_lifecycle_status(db, outline, target)


def _report_with_anchor(
    content_version: int | None,
    facts_version: int = 0,
    issues: list[dict] | None = None,
) -> dict:
    report: dict[str, Any] = {
        "issues": issues or [],
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


# --------------------------------------------------------------------- D4 成功写透


def test_publish_writes_status_and_published_at(env, ids) -> None:
    """发布成功：lifecycle_status=published，status=published，published_at 落值。"""
    outline_id, _ = _mk(env, ids["project_id"], report=_report_with_anchor(1))

    before = asyncio.run(_load(env, outline_id))
    assert before.status == "draft"
    assert before.published_at is None

    result = asyncio.run(_publish(env, outline_id))
    assert result["lifecycle_status"] == "published"

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "published"
    assert outline.status == "published"
    assert isinstance(outline.published_at, datetime)


def test_publish_keeps_first_published_at(env, ids) -> None:
    """历史已有 published_at 时保留首次发布时间，不被本次发布覆盖。"""
    first = datetime(2026, 1, 2, 3, 4, 5)
    outline_id, _ = _mk(
        env,
        ids["project_id"],
        report=_report_with_anchor(1),
        published_at=first,
    )

    asyncio.run(_publish(env, outline_id))

    outline = asyncio.run(_load(env, outline_id))
    assert outline.status == "published"
    assert outline.published_at == first


# --------------------------------------------------------------------- D4 拒绝分支不写透


def test_publish_reject_missing_anchor_keeps_columns(env, ids) -> None:
    """缺锚点的历史报告 → 拒绝，发布列保持原样。"""
    outline_id, _ = _mk(env, ids["project_id"], report=_report_with_anchor(None))

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "锚点" in str(excinfo.value)

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "approved"
    assert outline.status == "draft"
    assert outline.published_at is None


def test_publish_reject_stale_anchor_keeps_columns(env, ids) -> None:
    """审批后正文被改动 → 报告过期拒绝，发布列保持原样。"""
    outline_id, content_id = _mk(
        env, ids["project_id"], report=_report_with_anchor(1)
    )
    asyncio.run(_bump_content_version(env, content_id))

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "过期" in str(excinfo.value)

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "approved"
    assert outline.status == "draft"
    assert outline.published_at is None


def test_publish_reject_blocking_issues_keeps_publish_columns(env, ids) -> None:
    """仍存在未豁免阻断问题 → 置 validation_failed，但不写发布列。"""
    report = _report_with_anchor(
        1,
        issues=[
            {
                "type": "fact_conflict",
                "severity": "blocking",
                "exempted": False,
                "message": "事实冲突",
            }
        ],
    )
    outline_id, _ = _mk(env, ids["project_id"], report=report)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "阻断" in str(excinfo.value)

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "validation_failed"
    assert outline.status == "draft"
    assert outline.published_at is None


def test_publish_reject_not_approved_keeps_columns(env, ids) -> None:
    """来源状态非 approved（如 draft）→ 状态机拒绝，发布列保持原样。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status="draft", report=_report_with_anchor(1)
    )

    with pytest.raises(ValueError):
        asyncio.run(_publish(env, outline_id))

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "draft"
    assert outline.status == "draft"
    assert outline.published_at is None


def test_republish_rejected_keeps_first_published_at(env, ids) -> None:
    """二次发布被状态机拒绝，首次发布时间与 status 不被改写。"""
    outline_id, _ = _mk(env, ids["project_id"], report=_report_with_anchor(1))
    asyncio.run(_publish(env, outline_id))
    first = asyncio.run(_load(env, outline_id)).published_at

    with pytest.raises(ValueError):
        asyncio.run(_publish(env, outline_id))

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "published"
    assert outline.status == "published"
    assert outline.published_at == first


def test_direct_write_cannot_publish(env, ids) -> None:
    """set_lifecycle_status 不能作为直写 published 的后门，发布列不变。"""
    outline_id, _ = _mk(env, ids["project_id"], report=_report_with_anchor(1))

    with pytest.raises(ValueError):
        asyncio.run(_direct_write(env, outline_id, "published"))

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "approved"
    assert outline.status == "draft"
    assert outline.published_at is None
