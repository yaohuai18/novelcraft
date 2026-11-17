"""阶段E 写路径修复回归测试（E-01 / E-02 / E-03 + 版本同步 fail-closed）。

覆盖（对应本轮裁定 Q1–Q5）：
  1. test_normalize_names_resolves_ids          名字 → id 归一化，且不原地改原始 payload
  2. test_normalize_no_substring_match          归一化禁止包含匹配（只认精确 name / 别名）
  3. test_normalize_list_refs_reported          participants / known_by 未解析计 blocking unresolved
  4. test_trigger_strict_rejects_unresolved_list 严格模式：列表引用未解析 → ok=False / rejected_entity_reference
  5. test_trigger_non_strict_writes_resolved_only 非严格：只写已解析项，完整上报 unresolved
  6. test_trigger_empty_payload_keeps_data      空 payload：ok=False / 保留既有元数据 / 版本不递增
  7. test_trigger_missing_outline_anchor        缺 outline → ok=False / missing_outline / 零写入
  8. test_trigger_missing_chapter_content_anchor 缺 chapter_content → ok=False / 且不清旧事实
  8b. test_trigger_reference_ok_missing_anchor_fail_closed
                                                引用已通过、缺锚点 → fail-closed 零写入
  9. test_trigger_ai_not_configured_no_mock     未配置 AI → 不写 mock 虚构事实
 10. test_trigger_ai_invalid_payload            AI 输出不可解析 → ai_error、零写入
 11. test_trigger_version_synced_both_rows      成功路径：outline 与 chapter_content 版本同步
 12. test_trigger_unresolved_only_no_write      整批未解析且无可写内容 → 不写、版本不变
 13. test_extract_state_changes_no_mock         未配置 AI 时状态变更不产生 mock 建议

全部使用内存 SQLite（StaticPool），不连正式库、不调真实 AI、不启服务。

Run: cd backend && python -m pytest tests/test_phase1_state_extraction.py -q
"""
import asyncio
import copy
import json
import os
import sys
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402,F401
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import Base  # noqa: E402
# 注册全部相关模型，避免 create_all 外键断链
from app.models.character import Character  # noqa: E402,F401
from app.models.character_relationship import CharacterRelationship  # noqa: E402,F401
from app.models.character_state_snapshot import CharacterStateSnapshot  # noqa: E402,F401
from app.models.chapter_content import ChapterContent  # noqa: E402,F401
from app.models.item import Item  # noqa: E402,F401
from app.models.item_possession import ItemPossession  # noqa: E402,F401
from app.models.location import Location  # noqa: E402,F401
from app.models.outline import Outline  # noqa: E402,F401
from app.models.project import Project  # noqa: E402,F401
from app.models.secret_knower import SecretKnower  # noqa: E402,F401
from app.models.timeline_event import TimelineEvent  # noqa: E402,F401
from app.models.user import User  # noqa: E402,F401
from app.services import state_extraction_service as svc  # noqa: E402


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


async def _seed_scope(db):
    u = User(
        id=uuid.uuid4(),
        username="phase_e_state_user",
        email="phase_e_state_user@example.com",
        password_hash="not-used-in-test",
    )
    db.add(u)
    await db.flush()
    p = Project(id=uuid.uuid4(), user_id=u.id, title="Phase E State Project")
    db.add(p)
    await db.flush()
    return u, p


async def _seed_chapter(db, project_id, with_content=True, facts_version=0):
    chapter_id = uuid.uuid4()
    outline = Outline(
        id=chapter_id,
        project_id=project_id,
        title="第一章",
        outline_type="chapter",
        facts_version=facts_version,
    )
    db.add(outline)
    await db.flush()
    cc = None
    if with_content:
        cc = ChapterContent(
            id=uuid.uuid4(),
            outline_id=chapter_id,
            content="正文……",
            word_count=4,
            version=1,
            facts_version=facts_version,
        )
        db.add(cc)
        await db.flush()
    return chapter_id, outline, cc


async def _seed_character(db, project_id, name="陈默"):
    c = Character(id=uuid.uuid4(), project_id=project_id, name=name)
    db.add(c)
    await db.flush()
    return c


async def _seed_location(db, project_id, name="长安"):
    loc = Location(id=uuid.uuid4(), project_id=project_id, name=name)
    db.add(loc)
    await db.flush()
    return loc


def _facts(**kwargs):
    base = {
        "chapter_meta": {},
        "snapshots": [],
        "timeline_events": [],
        "items": [],
        "secrets": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- 归一化单测

def test_normalize_names_resolves_ids():
    """名字 → id 归一化生效，且绝不原地修改原始 payload。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        await _seed_character(db, p.id, "陈默")
        await _seed_location(db, p.id, "长安")
        await db.commit()

        facts = _facts(
            chapter_meta={"location": "长安"},
            snapshots=[{"character": "陈默", "location": "长安"}],
            items=[{"name": "旧剑", "owner": "陈默"}],
            secrets=[{"name": "身世之谜", "known_by": ["陈默"]}],
        )
        snapshot_before = copy.deepcopy(facts)

        normalized, resolutions = await svc._normalize_state_facts(db, p.id, facts)

        assert facts == snapshot_before, "原始 payload 被原地修改"
        assert normalized["snapshots"][0]["character_id"]
        assert normalized["snapshots"][0]["location_id"]
        assert normalized["items"][0]["owner_character_id"]
        assert normalized["secrets"][0]["knower_character_ids"]
        assert [r for r in resolutions if not r.resolved] == []
        await engine.dispose()

    run(scenario())


def test_normalize_no_substring_match():
    """包含匹配被禁止："陈" 不解析为 "陈默"。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        await _seed_character(db, p.id, "陈默")
        await db.commit()

        facts = _facts(snapshots=[{"character": "陈"}])
        normalized, resolutions = await svc._normalize_state_facts(db, p.id, facts)

        assert "character_id" not in normalized["snapshots"][0]
        assert len(resolutions) == 1
        assert resolutions[0].resolved is False
        assert resolutions[0].status == "unknown"
        await engine.dispose()

    run(scenario())


def test_normalize_list_refs_reported():
    """participants / known_by 由归一化器自行校验并计入 blocking unresolved。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        await _seed_character(db, p.id, "陈默")
        await db.commit()

        facts = _facts(
            timeline_events=[{"title": "夜行", "participants": ["陈默", "无名氏"]}],
            secrets=[{"name": "身世之谜", "known_by": ["查无此人"]}],
        )
        normalized, resolutions = await svc._normalize_state_facts(db, p.id, facts)

        unresolved = [r for r in resolutions if not r.resolved]
        paths = {r.path for r in unresolved}
        assert paths == {
            "timeline_events[0].participants[1]",
            "secrets[0].known_by[0]",
        }
        assert all(r.blocking for r in unresolved)
        # 已解析项仍然落 participant_ids，未解析项不混入
        assert len(normalized["timeline_events"][0]["participant_ids"]) == 1
        assert "knower_character_ids" not in normalized["secrets"][0]
        await engine.dispose()

    run(scenario())


# ---------------------------------------------------------------- 写路径集成

def test_trigger_strict_rejects_unresolved_list(monkeypatch):
    """严格模式：列表引用未解析 → 整批拒绝，不写库、版本不变。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=0)
        await _seed_character(db, p.id, "陈默")
        await db.commit()

        facts = _facts(
            timeline_events=[{"title": "夜行", "participants": ["无名氏"]}]
        )
        monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "1")

        async def fake_extract(*args, **kwargs):
            return copy.deepcopy(facts)

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "rejected_entity_reference"
        assert result["rejected"]["reason"] == "unresolved_entity_references"
        assert result["blocking_unresolved"] == 1
        assert result["written"] == {}
        assert result["written_total"] == 0
        assert result["facts_version_status"] == "not_incremented"
        assert (await db.execute(select(TimelineEvent))).scalars().all() == []
        assert (await db.get(Outline, chapter_id)).facts_version == 0
        assert cc.facts_version == 0
        await engine.dispose()

    run(scenario())


def test_trigger_non_strict_writes_resolved_only(monkeypatch):
    """非严格：只写已解析事实，未解析项完整出现在 unresolved 中。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=0)
        await _seed_character(db, p.id, "陈默")
        await db.commit()

        facts = _facts(
            snapshots=[
                {"character": "陈默", "timeline_status": "在世"},
                {"character": "无名氏", "timeline_status": "在世"},
            ]
        )
        monkeypatch.delenv("NOVELCRAFT_STRICT_ENTITY_CHECK", raising=False)
        monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "0")

        async def fake_extract(*args, **kwargs):
            return copy.deepcopy(facts)

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is True, result
        assert result["written"]["snapshots"] == 1
        assert result["written_total"] > 0
        assert result["facts_version_status"] == "synced"
        assert result["unresolved"]
        assert result["blocking_unresolved"] == 1
        assert result["unresolved"][0]["path"] == "snapshots[1].character"
        assert result["rejected"]["reason"] == "partial_unresolved_entity_references"
        rows = (await db.execute(select(CharacterStateSnapshot))).scalars().all()
        assert len(rows) == 1
        assert (await db.get(Outline, chapter_id)).facts_version == 1
        assert cc.facts_version == 1
        await engine.dispose()

    run(scenario())


def test_trigger_empty_payload_keeps_data(monkeypatch):
    """空 payload：不阻塞，不写库、不递增版本，且保留既有元数据。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=3)
        outline.chapter_time = "景历元年"
        outline.chapter_goal = "旧目标"
        outline.chapter_outcome = "旧结局"
        await db.commit()

        async def fake_extract(*args, **kwargs):
            return _facts()

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "empty_payload"
        assert result["facts_version_status"] == "not_incremented"
        assert result["unresolved"][0]["reason"] == "empty"
        assert result["written"] == {}
        fresh = await db.get(Outline, chapter_id)
        await db.refresh(fresh)
        assert fresh.chapter_time == "景历元年"
        assert fresh.chapter_goal == "旧目标"
        assert fresh.chapter_outcome == "旧结局"
        assert fresh.facts_version == 3
        assert cc.facts_version == 3
        await engine.dispose()

    run(scenario())


def test_trigger_missing_outline_anchor(monkeypatch):
    """缺 outline：ok=False / missing_outline，且零写入。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        await _seed_character(db, p.id, "陈默")
        await db.commit()
        chapter_id = uuid.uuid4()

        async def fake_extract(*args, **kwargs):
            return _facts(snapshots=[{"character": "陈默"}])

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "missing_version_anchor"
        assert result["facts_version_status"] == "missing_outline"
        assert (await db.execute(select(CharacterStateSnapshot))).scalars().all() == []
        await engine.dispose()

    run(scenario())


def test_trigger_missing_chapter_content_anchor(monkeypatch):
    """缺 chapter_content：ok=False / missing_chapter_content，且不得清理旧事实。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, _ = await _seed_chapter(
            db, p.id, with_content=False, facts_version=2
        )
        c = await _seed_character(db, p.id, "陈默")
        db.add(
            CharacterStateSnapshot(
                character_id=c.id,
                chapter_id=chapter_id,
                state_json=json.dumps({"timeline_status": "在世"}),
                source="ai_extracted",
            )
        )
        await db.commit()

        async def fake_extract(*args, **kwargs):
            return _facts(
                chapter_meta={"chapter_time": "景历二十三年秋"},
                items=[{"name": "旧剑"}],
            )

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "missing_version_anchor"
        assert result["facts_version_status"] == "missing_chapter_content"
        assert result["written"] == {}
        # 旧事实未被清理、新事实未写入、元数据与版本未被改写
        snaps = (await db.execute(select(CharacterStateSnapshot))).scalars().all()
        assert len(snaps) == 1
        assert (await db.execute(select(Item))).scalars().all() == []
        fresh = await db.get(Outline, chapter_id)
        await db.refresh(fresh)
        assert fresh.facts_version == 2
        assert fresh.chapter_time is None
        await engine.dispose()

    run(scenario())


def test_trigger_reference_ok_missing_anchor_fail_closed(monkeypatch):
    """契约：引用已通过、即将写入时才检查锚点；缺 chapter_content → fail-closed 零写入。

    与「引用拒绝且缺锚点」用例的区别：本用例的实体引用全部解析成功、validator 也通过，
    缺锚点是唯一阻断原因，因此能真正证明缺失版本锚点被正确上报。
    """

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, _ = await _seed_chapter(
            db, p.id, with_content=False, facts_version=5
        )
        c = await _seed_character(db, p.id, "陈默")
        db.add(
            CharacterStateSnapshot(
                character_id=c.id,
                chapter_id=chapter_id,
                state_json=json.dumps({"timeline_status": "在世"}),
                source="ai_extracted",
            )
        )
        await db.commit()

        async def fake_extract(*args, **kwargs):
            return _facts(
                chapter_meta={"chapter_time": "景历二十三年秋"},
                snapshots=[{"character": "陈默"}],
            )

        monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "1")
        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        # 引用全部通过，阻断原因只能是缺锚点
        assert result["blocking_unresolved"] == 0, result
        assert result["rejected"] is None, result
        assert result["ok"] is False, result
        assert result["skipped_reason"] == "missing_version_anchor"
        assert result["facts_version_status"] == "missing_chapter_content"
        assert result["written"] == {}
        assert result["written_total"] == 0
        # 零业务写入：旧事实仍在、无新事实、元数据与版本不变
        snaps = (await db.execute(select(CharacterStateSnapshot))).scalars().all()
        assert len(snaps) == 1
        assert (await db.execute(select(TimelineEvent))).scalars().all() == []
        assert (await db.execute(select(Item))).scalars().all() == []
        fresh = await db.get(Outline, chapter_id)
        await db.refresh(fresh)
        assert fresh.facts_version == 5
        assert fresh.chapter_time is None
        await engine.dispose()

    run(scenario())


def test_trigger_ai_not_configured_no_mock(monkeypatch):
    """未配置 AI：不得写 mock 固定虚构事实。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=0)
        await db.commit()

        monkeypatch.setattr(svc, "is_ai_configured", lambda: False)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "ai_not_configured"
        assert result["written"] == {}
        assert result["unresolved"][0]["reason"] == "ai_not_configured"
        assert (await db.execute(select(CharacterStateSnapshot))).scalars().all() == []
        assert (await db.execute(select(TimelineEvent))).scalars().all() == []
        assert (await db.execute(select(Item))).scalars().all() == []
        assert (await db.get(Outline, chapter_id)).facts_version == 0
        assert cc.facts_version == 0
        await engine.dispose()

    run(scenario())


def test_trigger_ai_invalid_payload(monkeypatch):
    """AI 输出不可解析：ai_error、零写入。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, _ = await _seed_chapter(db, p.id, facts_version=0)
        await db.commit()

        monkeypatch.setattr(svc, "is_ai_configured", lambda: True)
        monkeypatch.setattr(svc, "resolve_ai_provider", lambda: ("mock", "mock-model"))

        async def fake_chat(*args, **kwargs):
            return "这不是 JSON"

        # 2026-11-15：extract_state_facts 已改走截断重试包装，patch 实际调用名
        monkeypatch.setattr(svc, "_chat_with_ai_retry_on_truncation", fake_chat)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "ai_error"
        assert result["written"] == {}
        assert (await db.get(Outline, chapter_id)).facts_version == 0
        await engine.dispose()

    run(scenario())


def test_trigger_version_synced_both_rows(monkeypatch):
    """成功路径：outline 与 chapter_content 的 facts_version 同步 +1。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=7)
        await _seed_character(db, p.id, "陈默")
        await db.commit()

        async def fake_extract(*args, **kwargs):
            return _facts(
                chapter_meta={"chapter_time": "景历二十三年秋"},
                snapshots=[{"character": "陈默"}],
            )

        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is True, result
        assert result["facts_version_status"] == "synced"
        assert result["written"]["snapshots"] == 1
        fresh = await db.get(Outline, chapter_id)
        await db.refresh(fresh)
        await db.refresh(cc)
        assert fresh.facts_version == 8
        assert cc.facts_version == 8
        assert fresh.chapter_time == "景历二十三年秋"
        await engine.dispose()

    run(scenario())


def test_trigger_unresolved_only_no_write(monkeypatch):
    """整批引用都解析不到且无可写内容：只上报，不写库、版本不变。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, cc = await _seed_chapter(db, p.id, facts_version=4)
        await db.commit()

        async def fake_extract(*args, **kwargs):
            return _facts(snapshots=[{"character": "无名氏"}])

        monkeypatch.setenv("NOVELCRAFT_STRICT_ENTITY_CHECK", "0")
        monkeypatch.setattr(svc, "extract_state_facts", fake_extract)
        result = await svc.trigger_state_facts(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["ok"] is False, result
        assert result["skipped_reason"] == "rejected_entity_reference"
        assert result["rejected"]["reason"] == "unresolved_entity_references"
        assert result["written"] == {}
        assert result["facts_version_status"] == "not_incremented"
        assert result["blocking_unresolved"] == 1
        assert (await db.execute(select(CharacterStateSnapshot))).scalars().all() == []
        assert (await db.get(Outline, chapter_id)).facts_version == 4
        assert cc.facts_version == 4
        await engine.dispose()

    run(scenario())


def test_extract_state_changes_no_mock(monkeypatch):
    """未配置 AI 时状态变更提取不产生 mock 建议、不落库。"""

    async def scenario():
        engine, db = await _fresh_db()
        _, p = await _seed_scope(db)
        chapter_id, outline, _ = await _seed_chapter(db, p.id, facts_version=0)
        await db.commit()

        monkeypatch.setattr(svc, "is_ai_configured", lambda: False)
        result = await svc.extract_state_changes(
            db, p.id, chapter_id, "第一章", "正文……"
        )

        assert result["suggestions"] == 0, result
        assert result["skipped_reason"] == "ai_not_configured", result
        assert result["auto_applied"] == {
            "character": 0, "relationship": 0, "timeline": 0, "foreshadow": 0
        }
        from app.models.change_suggestion import ChangeSuggestion
        sugg = (await db.execute(select(ChangeSuggestion))).scalars().all()
        assert sugg == []
        await engine.dispose()

    run(scenario())
