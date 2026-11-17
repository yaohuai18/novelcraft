# -*- coding: utf-8 -*-
"""B3-prep：运营章节下架同步 Publication 状态/可见性 + 公共读拒绝锁定。

覆盖范围（仅 backend/app/services/content_ops_service.py 的
``unpublish_chapter``；publication_service 不改逻辑，仅作为读取方被验证）：

    T1  ``Publication.status`` 同步为 taken_down（复用 RESOURCE_STATUS_TAKEN_DOWN）
    T2  outline 与 Publication 的 visibility 均为 private
    T3  ``published_at`` 保留（outline 与 Publication 两处均不被清空）
    T4  ``lifecycle_status`` 不变（下架是资源状态，不是生命周期迁移）
    T5  公共单章 / 公开目录 / 章节导航均不返回下架章节
    T6  作者侧可识别 taken_down（preview_chapter 返回 status=taken_down）
    T7  下架失败（审计写入异常）时三轴无部分写入（事务由调用方收口）
    T8  helper 内部不提交事务（commit 次数为 0）
    T9  无 Publication 的章节下架不抛异常，仅同步 outline 两轴
    T10 既有作品隐藏 / 恢复行为不受影响（只动作品两轴，不动章节与发布记录）

硬约束：
    - 仓库外 pytest 临时目录 SQLite 文件库 + ORM create_all，不触碰
      backend/novelcraft.db、不执行迁移、不连正式库；
    - 只调用 service 层函数，不启服务、不监听端口、不发起网络请求
      （tests/conftest.py 离线护栏生效）；
    - 不改动 schema / 模型 / 迁移 / 审计白名单 / 公共读取逻辑。

Run: cd backend && python -m pytest tests/test_b3_prep_taken_down_sync.py -q
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.database import Base
from app.models.outline import Outline
from app.models.project import Project
from app.models.publication import Publication
from app.models.user import User
from app.services import content_ops_service, publication_service
from app.services.chapter_lifecycle import RESOURCE_STATUS_TAKEN_DOWN

PUBLISHED_AT = datetime(2026, 9, 1, 8, 30, 0)
CH1_CONTENT = "<p>B3-prep 下架同步正文</p>"


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库（NullPool：每个 asyncio.run 独立事件循环）。"""
    db_dir = tmp_path_factory.mktemp("b3_prep_taken_down_sync")
    db_path = db_dir / "b3_prep_taken_down_sync.db"

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

    env_data: dict[str, Any] = {
        "engine": engine,
        "SessionLocal": session_factory,
        "db_path": str(db_path),
    }
    try:
        yield env_data
    finally:
        asyncio.run(engine.dispose())


def _run(coro_factory):
    return asyncio.run(coro_factory())


async def _seed(env: dict[str, Any], *, with_publication: bool = True) -> dict[str, Any]:
    """种子：1 owner + 1 已公开作品 + 1 已发布章节（含发布记录）+ 1 草稿章节。"""
    ids: dict[str, Any] = {}
    async with env["SessionLocal"]() as db:
        owner = User(
            username=f"b3prep_{uuid.uuid4().hex[:8]}",
            email=f"b3prep_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
        )
        db.add(owner)
        await db.flush()

        project = Project(
            user_id=owner.id,
            title="B3-prep 下架同步项目",
            status="active",
            publication_status="published",
            project_slug=f"b3prep-{uuid.uuid4().hex[:8]}",
        )
        db.add(project)
        await db.flush()

        volume = Outline(
            project_id=project.id,
            title="第一卷",
            outline_type="volume",
            status="published",
            sort_order=1,
        )
        chapter = Outline(
            project_id=project.id,
            title="B3-prep 已发布章节",
            outline_type="chapter",
            status="published",
            visibility="public",
            lifecycle_status="approved",
            published_at=PUBLISHED_AT,
            sort_order=1,
        )
        draft_chapter = Outline(
            project_id=project.id,
            title="B3-prep 草稿章节",
            outline_type="chapter",
            status="draft",
            visibility="private",
            lifecycle_status="draft",
            sort_order=2,
        )
        db.add_all([volume, chapter, draft_chapter])
        await db.flush()
        chapter.parent_id = volume.id
        draft_chapter.parent_id = volume.id

        if with_publication:
            db.add(
                Publication(
                    project_id=project.id,
                    chapter_id=chapter.id,
                    title=chapter.title,
                    content=CH1_CONTENT,
                    content_text="B3-prep 下架同步正文",
                    word_count=11,
                    status="published",
                    visibility="public",
                    published_at=PUBLISHED_AT,
                )
            )
        await db.commit()

        ids.update(
            {
                "owner_id": owner.id,
                "project_id": project.id,
                "chapter_id": chapter.id,
                "draft_chapter_id": draft_chapter.id,
            }
        )
    return ids


async def _load(db: AsyncSession, model, pk):
    return (
        await db.execute(select(model).where(model.id == pk))
    ).scalar_one_or_none()


async def _take_down(env: dict[str, Any], ids: dict[str, Any], *, commit: bool = True) -> Outline:
    """模拟路由行为：调用 helper → 由调用方提交。"""
    async with env["SessionLocal"]() as db:
        chapter = await content_ops_service.unpublish_chapter(
            db,
            chapter_id=ids["chapter_id"],
            actor_id=ids["owner_id"],
            reason="B3-prep 运营下架",
        )
        if commit:
            await db.commit()
        return chapter


# --------------------------------------------------------------------- T1-T4


def test_t1_t2_t3_t4_taken_down_syncs_publication_without_touching_lifecycle(env):
    ids = _run(lambda: _seed(env))

    async def _flow():
        async with env["SessionLocal"]() as db:
            await content_ops_service.unpublish_chapter(
                db,
                chapter_id=ids["chapter_id"],
                actor_id=ids["owner_id"],
                reason="B3-prep 运营下架",
            )
            await db.commit()

        async with env["SessionLocal"]() as db:
            outline = await _load(db, Outline, ids["chapter_id"])
            pub = (
                await db.execute(
                    select(Publication).where(Publication.chapter_id == ids["chapter_id"])
                )
            ).scalar_one_or_none()
            return outline, pub

    outline, pub = _run(_flow)

    # T1：Publication 状态同步为 taken_down，且与资源状态常量一致
    assert RESOURCE_STATUS_TAKEN_DOWN == "taken_down"
    assert pub is not None
    assert pub.status == "taken_down"
    assert outline.status == RESOURCE_STATUS_TAKEN_DOWN

    # T2：两处 visibility 均为 private
    assert outline.visibility == "private"
    assert pub.visibility == "private"

    # T3：published_at 保留（不再被下架清空）
    assert outline.published_at == PUBLISHED_AT
    assert pub.published_at == PUBLISHED_AT

    # T4：lifecycle_status 不变
    assert outline.lifecycle_status == "approved"


# --------------------------------------------------------------------- T5


def test_t5_public_reads_reject_taken_down_chapter(env):
    ids = _run(lambda: _seed(env))
    _run(lambda: _take_down(env, ids))

    async def _flow():
        async with env["SessionLocal"]() as db:
            project = await _load(db, Project, ids["project_id"])
            chapter = await _load(db, Outline, ids["chapter_id"])

            with pytest.raises(HTTPException) as single:
                await publication_service.get_public_chapter(db, project, chapter)

            toc = await publication_service._build_toc(db, project, visible_only=True)
            navigable = await publication_service._get_navigable_chapters(db, project)
            public_project = await publication_service.get_public_project(db, project)
            return single.value, toc, navigable, public_project

    exc, toc, navigable, public_project = _run(_flow)

    # 公共单章：404（章节不可访问）
    assert exc.status_code == 404
    # 公开目录：下架章节不出现
    toc_chapter_ids = [c.chapter_id for v in toc for c in v.chapters]
    assert ids["chapter_id"] not in toc_chapter_ids
    assert public_project.chapter_count == 0
    # 章节导航：下架章节不在导航范围内
    assert ids["chapter_id"] not in [c.id for c in navigable]


# --------------------------------------------------------------------- T6


def test_t6_author_preview_identifies_taken_down(env):
    ids = _run(lambda: _seed(env))
    _run(lambda: _take_down(env, ids))

    async def _flow():
        async with env["SessionLocal"]() as db:
            project = await _load(db, Project, ids["project_id"])
            chapter = await _load(db, Outline, ids["chapter_id"])
            preview = await publication_service.preview_chapter(db, project, chapter)
            preview_project = await publication_service.preview_project(db, project)
            return preview, preview_project

    preview, preview_project = _run(_flow)

    assert preview.status == "taken_down"
    assert preview.visibility == "private"
    assert preview.preview is True
    # 作者预览目录仍能看到该章节（可见性只影响公共读）
    preview_ids = [c.chapter_id for v in preview_project.volumes for c in v.chapters]
    assert ids["chapter_id"] in preview_ids


# --------------------------------------------------------------------- T7


def test_t7_failure_produces_no_partial_write(env, monkeypatch):
    ids = _run(lambda: _seed(env))

    async def _boom(*args, **kwargs):
        raise RuntimeError("B3-prep 模拟审计写入失败")

    monkeypatch.setattr(content_ops_service, "record_audit", _boom)

    async def _flow():
        async with env["SessionLocal"]() as db:
            with pytest.raises(RuntimeError):
                await content_ops_service.unpublish_chapter(
                    db,
                    chapter_id=ids["chapter_id"],
                    actor_id=ids["owner_id"],
                    reason="B3-prep 运营下架",
                )
            # 调用方未提交：异常冒泡后回滚整个事务
            await db.rollback()

        async with env["SessionLocal"]() as db:
            outline = await _load(db, Outline, ids["chapter_id"])
            pub = (
                await db.execute(
                    select(Publication).where(Publication.chapter_id == ids["chapter_id"])
                )
            ).scalar_one_or_none()
            return outline, pub

    outline, pub = _run(_flow)

    # 三轴均未产生部分写入：outline 两轴 + Publication 两轴全部保持原状
    assert outline.status == "published"
    assert outline.visibility == "public"
    assert pub.status == "published"
    assert pub.visibility == "public"


# --------------------------------------------------------------------- T8


def test_t8_helper_does_not_commit(env, monkeypatch):
    ids = _run(lambda: _seed(env))
    commits = {"count": 0}

    async def _flow():
        async with env["SessionLocal"]() as db:
            real_commit = db.commit

            async def _spy_commit():
                commits["count"] += 1
                return await real_commit()

            monkeypatch.setattr(db, "commit", _spy_commit)
            await content_ops_service.unpublish_chapter(
                db,
                chapter_id=ids["chapter_id"],
                actor_id=ids["owner_id"],
                reason="B3-prep 运营下架",
            )
            await db.rollback()

    _run(_flow)
    # 事务边界仍由调用方（admin 路由）收口
    assert commits["count"] == 0


# --------------------------------------------------------------------- T9


def test_t9_chapter_without_publication_is_tolerated(env):
    ids = _run(lambda: _seed(env, with_publication=False))

    async def _flow():
        async with env["SessionLocal"]() as db:
            await content_ops_service.unpublish_chapter(
                db,
                chapter_id=ids["chapter_id"],
                actor_id=ids["owner_id"],
                reason="B3-prep 运营下架（无发布记录）",
            )
            await db.commit()

        async with env["SessionLocal"]() as db:
            outline = await _load(db, Outline, ids["chapter_id"])
            pub = (
                await db.execute(
                    select(Publication).where(Publication.chapter_id == ids["chapter_id"])
                )
            ).scalar_one_or_none()
            return outline, pub

    outline, pub = _run(_flow)
    assert outline.status == "taken_down"
    assert outline.visibility == "private"
    assert pub is None


# --------------------------------------------------------------------- T10


def test_t10_project_hide_and_restore_do_not_touch_chapters(env):
    ids = _run(lambda: _seed(env))

    async def _flow():
        async with env["SessionLocal"]() as db:
            await content_ops_service.hide_project(
                db,
                project_id=ids["project_id"],
                actor_id=ids["owner_id"],
                reason="B3-prep 作品隐藏",
            )
            await db.commit()

        async with env["SessionLocal"]() as db:
            hidden_project = await _load(db, Project, ids["project_id"])
            hidden_chapter = await _load(db, Outline, ids["chapter_id"])
            hidden_pub = (
                await db.execute(
                    select(Publication).where(Publication.chapter_id == ids["chapter_id"])
                )
            ).scalar_one()

        async with env["SessionLocal"]() as db:
            await content_ops_service.restore_project(
                db,
                project_id=ids["project_id"],
                actor_id=ids["owner_id"],
                reason="B3-prep 作品恢复",
            )
            await db.commit()

        async with env["SessionLocal"]() as db:
            restored_project = await _load(db, Project, ids["project_id"])
            restored_chapter = await _load(db, Outline, ids["chapter_id"])
            restored_pub = (
                await db.execute(
                    select(Publication).where(Publication.chapter_id == ids["chapter_id"])
                )
            ).scalar_one()

        return (
            hidden_project,
            hidden_chapter,
            hidden_pub,
            restored_project,
            restored_chapter,
            restored_pub,
        )

    (
        hidden_project,
        hidden_chapter,
        hidden_pub,
        restored_project,
        restored_chapter,
        restored_pub,
    ) = _run(_flow)

    # 作品两轴按既有行为变化，章节与发布记录四轴不受影响
    assert hidden_project.publication_status == "unlisted"
    assert restored_project.publication_status == "published"

    for chapter, pub in (
        (hidden_chapter, hidden_pub),
        (restored_chapter, restored_pub),
    ):
        assert chapter.status == "published"
        assert chapter.visibility == "public"
        assert chapter.published_at == PUBLISHED_AT
        assert pub.status == "published"
        assert pub.visibility == "public"
        assert pub.published_at == PUBLISHED_AT
