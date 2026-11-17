# -*- coding: utf-8 -*-
"""Phase E regression tests: entity reference validation (阶段E，补充7).

Coverage (Phase E acceptance):
  1. test_entity_reference_reject:
     validator 拒绝不存在的实体 id 引用（character_id 等），且写路径
     （trigger_state_facts / extract_state_changes）拒绝时不落库。
  2. test_null_reference_allowed:
     可空引用（None / 空字符串）跳过，不误杀。
  3. test_valid_reference_accepted:
     库内合法 id（本项目）通过校验。
  4. test_cross_project_rejected:
     其它项目的 id 引用被拒绝（不属于本项目）。
  5. test_invalid_uuid_rejected:
     非合法 UUID 格式的引用值被拒绝。
  6. test_strict_off_passthrough:
     strict_entity_check 关闭时直接放行（开关可关）。
  7. test_trigger_state_facts_reject_no_write:
     集成：state_facts 写路径校验失败 → 输出被丢弃、事实表无新行、
     facts_version 不递增。
  8. test_extract_state_changes_reject_no_suggestion:
     集成：状态变更写路径校验失败 → 不生成建议、不自动应用、记录 rejected。

（阶段 1 冻结期：本文件自 backend/temp/ 迁入 backend/tests/，纳入正式可追踪回归；
   用例内容与源脚本等价，仅调整文件位置与运行路径，测试逻辑未改动。）

Run: cd backend && python -m pytest tests/test_phase1_entity_reference.py -q
"""
import asyncio
import json
import os
import sys
import uuid

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import Base  # noqa: E402
# 注册全部相关模型（含事实表），避免 create_all 外键断链
from app.models.character import Character  # noqa: E402,F401
from app.models.character_relationship import CharacterRelationship  # noqa: E402,F401
from app.models.character_state_snapshot import CharacterStateSnapshot  # noqa: E402,F401
from app.models.item import Item  # noqa: E402,F401
from app.models.item_possession import ItemPossession  # noqa: E402,F401
from app.models.location import Location  # noqa: E402,F401
from app.models.outline import Outline  # noqa: E402,F401
from app.models.project import Project  # noqa: E402,F401
from app.models.user import User  # noqa: E402,F401
from app.services import state_extraction_service  # noqa: E402
from app.services import entity_reference_validator as ev  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _fresh_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    return engine, session


async def _dispose(engine):
    await engine.dispose()


async def _seed_user(db):
    u = User(
        id=uuid.uuid4(),
        username="phase_e_user",
        email="phase_e_user@example.com",
        password_hash="not-used-in-test",
    )
    db.add(u)
    await db.flush()
    return u


async def _seed_project(db, user_id):
    p = Project(id=uuid.uuid4(), user_id=user_id, title="Phase E Project")
    db.add(p)
    await db.flush()
    return p


async def _seed_character(db, project_id, name="陈默"):
    c = Character(id=uuid.uuid4(), project_id=project_id, name=name)
    db.add(c)
    await db.flush()
    return c


async def _seed_outline(db, project_id, chapter_id):
    o = Outline(id=chapter_id, project_id=project_id, title="第一章", outline_type="chapter")
    db.add(o)
    await db.flush()
    return o


# ---------------------------------------------------------------- validator 单测

def test_null_reference_allowed():
    """可空引用（None / 空字符串）跳过，不误杀。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        await db.commit()
        payload = {
            "snapshots": [
                {"character_id": None, "location_id": "", "item_id": "  "},
            ],
            "nested": {"target_character_id": None},
        }
        res = await ev.validate_entity_references(db, p.id, payload)
        assert res.ok is True, res.violations
        assert res.checked == 0
        await _dispose(engine)

    run(scenario())


def test_valid_reference_accepted():
    """库内合法 id（本项目）通过校验。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        c = await _seed_character(db, p.id)
        loc = Location(id=uuid.uuid4(), project_id=p.id, name="长安")
        db.add(loc)
        await db.commit()
        payload = {
            "snapshots": [
                {"character_id": str(c.id), "location_id": str(loc.id)},
            ],
            "items": [{"owner_character_id": c.id}],
        }
        res = await ev.validate_entity_references(db, p.id, payload)
        assert res.ok is True, res.violations
        assert res.checked >= 3
        await _dispose(engine)

    run(scenario())


def test_entity_reference_reject():
    """不存在的实体 id 引用被拒绝。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        await db.commit()
        ghost = uuid.uuid4()
        payload = {"snapshots": [{"character_id": str(ghost)}]}
        res = await ev.validate_entity_references(db, p.id, payload)
        assert res.ok is False
        assert any(v.entity_type == "character" for v in res.violations)
        await _dispose(engine)

    run(scenario())


def test_cross_project_rejected():
    """其它项目的 id 引用被拒绝（不属于本项目）。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p1 = await _seed_project(db, u.id)
        p2 = Project(id=uuid.uuid4(), user_id=u.id, title="Another Project")
        db.add(p2)
        c_other = Character(id=uuid.uuid4(), project_id=p2.id, name="他人")
        db.add(c_other)
        await db.commit()
        payload = {"snapshots": [{"character_id": str(c_other.id)}]}
        res = await ev.validate_entity_references(db, p1.id, payload)
        assert res.ok is False
        await _dispose(engine)

    run(scenario())


def test_invalid_uuid_rejected():
    """非合法 UUID 格式的引用值被拒绝（脏 id 不落库）。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        await db.commit()
        payload = {"snapshots": [{"character_id": "not-a-uuid"}]}
        res = await ev.validate_entity_references(db, p.id, payload)
        assert res.ok is False
        assert any("UUID" in v.message for v in res.violations)
        await _dispose(engine)

    run(scenario())


def test_strict_off_passthrough():
    """strict_entity_check 关闭时直接放行（开关可关）。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        await db.commit()
        payload = {"snapshots": [{"character_id": str(uuid.uuid4())}]}
        res = await ev.validate_entity_references(db, p.id, payload, strict=False)
        assert res.ok is True
        assert res.checked == 0
        await _dispose(engine)

    run(scenario())


def test_strict_flag_env(monkeypatch):
    """环境变量可关闭 strict_entity_check；默认 True。"""
    from app.services.ai_service import strict_entity_check_enabled

    monkeypatch.delenv("NOVELCRAFT_STRICT_ENTITY_CHECK", raising=False)
    assert strict_entity_check_enabled() is True
    monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "0")
    assert strict_entity_check_enabled() is False
    monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "false")
    assert strict_entity_check_enabled() is False
    monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "1")
    assert strict_entity_check_enabled() is True


# ---------------------------------------------------------------- 写路径集成

def test_trigger_state_facts_reject_no_write(monkeypatch):
    """state_facts 写路径：校验失败 → 丢弃输出不落库、facts_version 不递增。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        chapter_id = uuid.uuid4()
        await _seed_outline(db, p.id, chapter_id)
        await db.commit()

        ghost = uuid.uuid4()
        bad_facts = {
            "chapter_meta": {},
            "snapshots": [{"character": "陈默", "character_id": str(ghost)}],
            "timeline_events": [],
            "items": [],
            "secrets": [],
        }

        async def fake_extract(*args, **kwargs):
            return bad_facts

        monkeypatch.setattr(state_extraction_service, "extract_state_facts", fake_extract)
        result = await state_extraction_service.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )
        # 契约：引用拒绝 → ok=False / rejected_entity_reference，业务零写入
        assert result["ok"] is False, result
        assert result["skipped_reason"] == "rejected_entity_reference", result
        assert result["facts_version_status"] == "not_incremented", result
        assert result["rejected"] is not None, result
        assert result["written"] == {}, result
        # 事实表无新行
        rows = (await db.execute(select(CharacterStateSnapshot))).scalars().all()
        assert len(rows) == 0
        outline = await db.get(Outline, chapter_id)
        assert (outline.facts_version or 0) == 0
        await _dispose(engine)

    run(scenario())


def test_extract_state_changes_reject_no_suggestion(monkeypatch):
    """状态变更写路径：校验失败 → 不生成建议、不自动应用、记录 rejected。"""

    async def scenario():
        engine, db = await _fresh_db()
        u = await _seed_user(db)
        p = await _seed_project(db, u.id)
        chapter_id = uuid.uuid4()
        await _seed_outline(db, p.id, chapter_id)
        await db.commit()

        ghost = uuid.uuid4()
        bad_json = json.dumps(
            {
                "character_updates": [
                    {"character": "陈默", "character_id": str(ghost),
                     "changes": ["死亡"], "current_location": "长安"},
                ],
                "relationships": [],
                "timeline_events": [],
                "foreshadows": [],
            },
            ensure_ascii=False,
        )

        async def fake_chat(system_prompt, user_prompt, max_tokens=1200, task_type=None):
            return bad_json

        # 2026-11-15：extract_state_changes 已改走截断重试包装（_chat_with_ai_retry_on_truncation），
        # 测试需 patch 实际调用的名字，否则真实 chat_with_ai 会被触发（联网挂起）
        monkeypatch.setattr(
            state_extraction_service, "_chat_with_ai_retry_on_truncation", fake_chat
        )
        monkeypatch.setattr(state_extraction_service, "is_ai_configured", lambda: True)
        from app.services.generation_service import resolve_ai_provider
        monkeypatch.setattr(
            "app.services.state_extraction_service.resolve_ai_provider",
            lambda: ("mock", "mock-model"),
        )
        result = await state_extraction_service.extract_state_changes(
            db, p.id, chapter_id, "第一章", "正文……"
        )
        assert result["rejected"] is not None, result
        assert result["suggestions"] == 0, result
        assert result["auto_applied"] == {
            "character": 0, "relationship": 0, "timeline": 0, "foreshadow": 0,
        }, result
        # 无建议落库、无快照落库
        from app.models.change_suggestion import ChangeSuggestion
        sugg = (await db.execute(select(ChangeSuggestion))).scalars().all()
        assert len(sugg) == 0
        snap = (await db.execute(select(CharacterStateSnapshot))).scalars().all()
        assert len(snap) == 0
        # 拒绝行为被记录到 generation_records
        from app.models.generation import GenerationRecord
        recs = (await db.execute(select(GenerationRecord))).scalars().all()
        assert len(recs) == 1
        assert "实体引用校验失败" in (recs[0].error_message or "")
        await _dispose(engine)

    run(scenario())
