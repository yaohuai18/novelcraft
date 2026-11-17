# -*- coding: utf-8 -*-
"""阶段 1 D1+D2 回归：生命周期语义收敛（校验异常显式化 / 统一阻断 / 状态机）。

覆盖批次任务书要求的语义：
    D1  校验器抛异常 → 报告 ``validated=False`` + ``validation_error``，
        生命周期落 ``validation_failed``，且无法 approve / publish；
    D2  approve 与 lifecycle publish 统一走 ``_collect_blocking_issues``，
        统一排除 ``exempted=True`` 的 blocking issue；
    D2  ``validated is not True`` 时 approve / publish 一律拒绝；
    D2  approve 来源仅 ``{draft, generated, pending_review}``；
    D2  publish 来源仅 ``{approved}``；
    D2  ``set_lifecycle_status`` 不得绕过审批/发布门禁（不能直写 approved /
        published，已发布章节不得直接改写）。
    D3（锚点门禁的 D1/D2 兼容）本文件种子报告统一带 ``validation_anchor``，
        锚点专项用例见 tests/test_phase1_lifecycle_anchor.py。

硬约束：
    - 不启动服务、不监听端口、不发起网络请求（tests/conftest.py 离线护栏生效）；
    - 仅使用仓库外 pytest 临时目录中的 SQLite 文件库 + ORM create_all，
      不触碰 backend/novelcraft.db、不执行迁移、不连正式库；
    - 不导入或修改 publication_service / content_ops_service /
      openapi_chapters / audit_service 的既有实现；
    - 拒绝分支不写库：断言拒绝后状态与报告保持原样。

Run: cd backend && python -m pytest tests/test_phase1_lifecycle_semantics.py -q
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

BLOCKING_ISSUE = {
    "rule": "fact_conflict",
    "severity": "blocking",
    "message": "与设定冲突",
}


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库（每个 asyncio.run 独立事件循环，用 NullPool）。"""
    db_dir = tmp_path_factory.mktemp("phase1_lifecycle_semantics")
    db_path = db_dir / "phase1_lifecycle_semantics.db"

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
                username="d1d2_lifecycle_owner",
                email="d1d2_lifecycle_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()
            project = Project(user_id=owner.id, title="D1+D2 语义收敛项目")
            db.add(project)
            await db.commit()
            return {"project_id": project.id}

    return asyncio.run(_seed())


async def _new_chapter(
    env: dict[str, Any],
    project_id: Any,
    *,
    status: str,
    content: str = "正文内容",
    report: dict | None = None,
    content_version: int = 1,
    facts_version: int = 0,
) -> tuple[Any, Any]:
    """建一个章节 + 正文，返回 (outline_id, content_id)。"""
    async with env["SessionLocal"]() as db:
        outline = Outline(
            project_id=project_id,
            title=f"D1D2-{status}-{uuid.uuid4().hex[:8]}",
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
    """同步封装：在独立事件循环中建章节（NullPool，连接不复用）。"""
    return asyncio.run(_new_chapter(env, project_id, **kwargs))


async def _load(env: dict[str, Any], outline_id: Any) -> Outline:
    async with env["SessionLocal"]() as db:
        return (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()


async def _approve(env: dict[str, Any], outline_id: Any, actor_id: str | None = None):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.approve_chapter(db, outline, actor_id)


async def _publish(env: dict[str, Any], outline_id: Any, actor_id: str | None = None):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.publish_chapter(db, outline, actor_id)


async def _set_status(env: dict[str, Any], outline_id: Any, target: str):
    async with env["SessionLocal"]() as db:
        outline = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return await chapter_lifecycle.set_lifecycle_status(db, outline, target)


def _valid_report(
    issues: list[dict] | None = None,
    *,
    content_version: int | None = 1,
    facts_version: int | None = 0,
) -> dict[str, Any]:
    """构造“已通过校验”的报告；D3 起必须带正文版本锚点。

    content_version=None 表示构造缺锚点的历史报告。
    """
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


# --------------------------------------------------------------------- D1


def test_validation_exception_marks_validation_failed(env, ids, monkeypatch) -> None:
    """校验器抛异常 → validated=False + validation_error + validation_failed。"""
    outline_id, content_id = _mk(env, ids["project_id"], status="draft")

    async def _boom(*args, **kwargs):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(chapter_lifecycle, "run_fact_validation", _boom)

    async def _save() -> dict:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == outline_id))
            ).scalar_one()
            chapter_content = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == content_id)
                )
            ).scalar_one()
            # mark_following=False：本用例不断言后续章节标记
            return await chapter_lifecycle.after_content_saved(
                db, outline, chapter_content, None, mark_following=False
            )

    summary = asyncio.run(_save())

    # 保存链路不抛出，但必须显式暴露“校验没成功”
    assert summary["validated"] is False
    assert summary["validation_error"]["type"] == "RuntimeError"
    assert summary["lifecycle_status"] == "validation_failed"

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "validation_failed"
    report = json.loads(outline.validation_report)
    assert report["validated"] is False
    assert report["validation_error"]["type"] == "RuntimeError"
    assert report["issues"] == []


def test_validation_exception_blocks_approve_and_publish(env, ids, monkeypatch) -> None:
    """校验异常留下的报告无法通过 approve / publish 门禁。"""
    outline_id, content_id = _mk(env, ids["project_id"], status="draft")

    async def _boom(*args, **kwargs):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(chapter_lifecycle, "run_fact_validation", _boom)

    async def _save() -> None:
        async with env["SessionLocal"]() as db:
            outline = (
                await db.execute(select(Outline).where(Outline.id == outline_id))
            ).scalar_one()
            chapter_content = (
                await db.execute(
                    select(ChapterContent).where(ChapterContent.id == content_id)
                )
            ).scalar_one()
            await chapter_lifecycle.after_content_saved(
                db, outline, chapter_content, None, mark_following=False
            )

    asyncio.run(_save())

    with pytest.raises(ValueError):
        asyncio.run(_approve(env, outline_id))
    with pytest.raises(ValueError):
        asyncio.run(_publish(env, outline_id))

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "validation_failed"


def test_legacy_report_without_validated_flag_is_rejected(env, ids) -> None:
    """历史报告（无 validated 键）不得被当作“已校验通过”（D1 门禁）。"""
    legacy = {"issues": [], "summary": {}, "validated_at": "2026-01-01T00:00:00"}
    outline_id, _ = _mk(
        env, ids["project_id"], status="generated", report=legacy
    )

    with pytest.raises(ValueError):
        asyncio.run(_approve(env, outline_id))

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "generated"


# --------------------------------------------------------------------- D2 阻断判定


def test_exempted_blocking_issue_does_not_block(env, ids) -> None:
    """exempted=True 的 blocking issue 在 approve 与 publish 两侧都不阻断。"""
    exempted = dict(BLOCKING_ISSUE, exempted=True)
    outline_id, _ = _mk(
        env, ids["project_id"], status="generated", report=_valid_report([exempted])
    )

    approve_result = asyncio.run(_approve(env, outline_id))
    assert approve_result["lifecycle_status"] == "approved"
    assert approve_result["blocking"] == 0

    publish_result = asyncio.run(_publish(env, outline_id))
    assert publish_result["lifecycle_status"] == "published"

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "published"


def test_non_exempted_blocking_issue_blocks_approve(env, ids) -> None:
    """未豁免 blocking issue → approve 拒绝，且不写入 approved。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status="generated", report=_valid_report([BLOCKING_ISSUE])
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_approve(env, outline_id))
    assert "阻断性" in str(excinfo.value)

    outline = asyncio.run(_load(env, outline_id))
    assert outline.lifecycle_status == "validation_failed"


def test_non_exempted_blocking_issue_blocks_publish(env, ids) -> None:
    """未豁免 blocking issue → publish 拒绝（approved 也不行）。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status="approved", report=_valid_report([BLOCKING_ISSUE])
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "阻断性" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "validation_failed"


# --------------------------------------------------------------------- D2 状态机


def test_published_chapter_cannot_be_approved(env, ids) -> None:
    """published → approve 拒绝（状态机不允许回退到 approved 通道）。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status="published", report=_valid_report()
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_approve(env, outline_id))
    assert "审批通过" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "published"


@pytest.mark.parametrize("status", ["draft", "generated", "pending_review", "validation_failed"])
def test_publish_requires_approved(env, ids, status) -> None:
    """非 approved → publish 拒绝，状态保持原样。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status=status, report=_valid_report()
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_publish(env, outline_id))
    assert "发布" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == status


def test_set_lifecycle_status_allows_non_gate_targets(env, ids) -> None:
    """set_lifecycle_status 仍可写非门禁状态（draft/generated/pending_review/validation_failed）。"""
    outline_id, _ = _mk(env, ids["project_id"], status="draft")

    result = asyncio.run(_set_status(env, outline_id, "pending_review"))
    assert result["lifecycle_status"] == "pending_review"
    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "pending_review"

    result = asyncio.run(_set_status(env, outline_id, "validation_failed"))
    assert result["lifecycle_status"] == "validation_failed"


@pytest.mark.parametrize("target", ["approved", "published"])
def test_set_lifecycle_status_rejects_gate_targets(env, ids, target) -> None:
    """approved / published 不能经通用写入器直写（禁止绕过门禁）。"""
    outline_id, _ = _mk(env, ids["project_id"], status="generated")

    with pytest.raises(ValueError):
        asyncio.run(_set_status(env, outline_id, target))

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "generated"


def test_set_lifecycle_status_rejects_published_rewrite(env, ids) -> None:
    """已发布章节不得直接改写生命周期状态。"""
    outline_id, _ = _mk(
        env, ids["project_id"], status="published", report=_valid_report()
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_set_status(env, outline_id, "draft"))
    assert "已发布" in str(excinfo.value)

    assert asyncio.run(_load(env, outline_id)).lifecycle_status == "published"


def test_set_lifecycle_status_rejects_unknown_target(env, ids) -> None:
    """未知状态仍按原逻辑拒绝。"""
    outline_id, _ = _mk(env, ids["project_id"], status="draft")

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_set_status(env, outline_id, "archived"))
    assert "未知生命周期状态" in str(excinfo.value)
