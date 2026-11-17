# -*- coding: utf-8 -*-
"""B3-A「实体引用校验」正式回归测试（服务层口径）。

边界约定（与 B3-A 提交范围一致）：
  - 只覆盖 entity_reference_validator 服务本体：字段白名单递归、可空引用、
    非法 UUID、跨项目引用、strict 开关；
  - 不导入 state_extraction_service（写路径 / trigger_state_facts）、
    ai_service（strict_entity_check_enabled 环境开关）、app.main、
    app.routers.facts，因此不依赖保存链路、AI 服务、HTTP 入口；
  - 写路径集成用例（校验失败 → 不落库 / 不生成建议）随阶段E提交，
    不在本文件内；
  - 全部使用内存 SQLite（sqlite+aiosqlite:///:memory:），不读写正式库；
  - 不启动服务、不调用真实 AI、不依赖 backend/temp 下任何文件。

覆盖场景：
  null/空串/空白引用 → 跳过不误杀
  本项目合法 id     → 通过（含 character / location / item / secret / event）
  不存在 id         → 拒绝
  跨项目 id         → 拒绝
  非 UUID 字符串    → 拒绝
  strict=False      → 直接放行（不查库）
"""
import asyncio
import os
import sys
import uuid

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models.character import Character  # noqa: E402
from app.models.item import Item  # noqa: E402
from app.models.location import Location  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.timeline_event import TimelineEvent  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import entity_reference_validator as ev  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _fresh_db() -> tuple:
    """内存库 + 全量表结构（create_all），返回 (engine, session)。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    return engine, session


async def _seed_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        username="b3a_user",
        email="b3a_user@example.com",
        password_hash="not-used-in-test",
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_project(db, user_id, title="B3-A 项目") -> Project:
    project = Project(id=uuid.uuid4(), user_id=user_id, title=title)
    db.add(project)
    await db.flush()
    return project


async def _seed_character(db, project_id, name="陈默") -> Character:
    char = Character(id=uuid.uuid4(), project_id=project_id, name=name)
    db.add(char)
    await db.flush()
    return char


def _assert_rejected(res, entity_type: str | None = None) -> None:
    assert res.ok is False, res.as_dict()
    assert res.violations, res.as_dict()
    if entity_type is not None:
        assert any(v.entity_type == entity_type for v in res.violations), res.as_dict()


# ---------- 1. 可空引用：None / 空串 / 空白 → 跳过不误杀 ----------
def test_null_reference_allowed():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            await db.commit()

            payload = {
                "snapshots": [
                    {"character_id": None, "location_id": "", "item_id": "  "},
                ],
                "nested": {"target_character_id": None},
            }
            res = await ev.validate_entity_references(db, project.id, payload)

            assert res.ok is True, res.as_dict()
            assert res.checked == 0
            assert res.violations == []
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 2. 本项目合法 id（含嵌套 list）→ 通过且计数 ----------
def test_valid_reference_accepted():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            char = await _seed_character(db, project.id)
            location = Location(id=uuid.uuid4(), project_id=project.id, name="长安")
            item = Item(
                id=uuid.uuid4(), project_id=project.id, name="玄铁剑",
                category="item", status="active",
            )
            secret = Item(
                id=uuid.uuid4(), project_id=project.id, name="身世之谜",
                category="secret", status="active",
            )
            event = TimelineEvent(
                id=uuid.uuid4(), project_id=project.id, title="入营",
                sequence_order=1, source="ai_extracted", confirmed=True,
            )
            db.add_all([location, item, secret, event])
            await db.commit()

            payload = {
                "snapshots": [
                    {
                        "character_id": str(char.id),
                        "location_id": str(location.id),
                        "event_id": str(event.id),
                    },
                ],
                "items": [{"owner_character_id": char.id, "item_id": item.id}],
                "secrets": [{"secret_id": secret.id, "knower_character_id": char.id}],
            }
            res = await ev.validate_entity_references(db, project.id, payload)

            assert res.ok is True, res.as_dict()
            assert res.violations == []
            assert res.checked >= 6
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 3. 引用不存在实体 → 拒绝 ----------
def test_missing_entity_rejected():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            await db.commit()

            ghost = uuid.uuid4()
            payload = {
                "snapshots": [{"character_id": str(ghost)}],
                "items": [{"item_id": str(uuid.uuid4())}],
            }
            res = await ev.validate_entity_references(db, project.id, payload)

            _assert_rejected(res, "character")
            assert any("不存在或不属于本项目" in v.message for v in res.violations)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 4. 跨项目引用 → 拒绝 ----------
def test_cross_project_rejected():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            own = await _seed_project(db, user.id, "本项目")
            other = await _seed_project(db, user.id, "其它项目")
            foreign_char = await _seed_character(db, other.id, "他人")
            await db.commit()

            payload = {"snapshots": [{"character_id": str(foreign_char.id)}]}
            res = await ev.validate_entity_references(db, own.id, payload)

            _assert_rejected(res, "character")
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 5. 非 UUID 值 → 拒绝（脏 id 不入库） ----------
def test_invalid_uuid_rejected():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            await db.commit()

            payload = {
                "snapshots": [{"character_id": "not-a-uuid"}],
                "items": [{"item_id": 12345}],
            }
            res = await ev.validate_entity_references(db, project.id, payload)

            _assert_rejected(res, "character")
            uuid_msgs = [v for v in res.violations if "UUID" in v.message]
            assert len(uuid_msgs) == 2, res.as_dict()
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 6. strict=False → 直接放行（不查库） ----------
def test_strict_off_passthrough():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            await db.commit()

            payload = {"snapshots": [{"character_id": str(uuid.uuid4())}]}
            res = await ev.validate_entity_references(
                db, project.id, payload, strict=False
            )

            assert res.ok is True
            assert res.checked == 0
            assert res.violations == []
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 7. 白名单外字段不校验（避免 chapter_id/project_id 误杀） ----------
def test_non_whitelisted_id_fields_ignored():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            user = await _seed_user(db)
            project = await _seed_project(db, user.id)
            await db.commit()

            payload = {
                "chapter_id": str(uuid.uuid4()),
                "project_id": str(uuid.uuid4()),
                "review_id": str(uuid.uuid4()),
            }
            res = await ev.validate_entity_references(db, project.id, payload)

            assert res.ok is True, res.as_dict()
            assert res.checked == 0
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
