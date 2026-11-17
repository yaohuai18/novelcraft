# -*- coding: utf-8 -*-
"""B3-A「事实校验领域能力」正式回归测试。

边界约定（与 B3-A 提交范围一致）：
  - 只覆盖服务层/契约层：fact_validator + schemas/facts + 豁免/认知两开关语义；
  - 不导入 app.main、app.routers.facts、chapter_lifecycle、state_extraction_service、
    chapter_plan_service，因此不依赖 HTTP 入口、路由装载、保存链路、生命周期代码
    或章节计划字段扩展；
  - 阶段F「场景合同」两规则（contract.state_change / contract.forbidden_reveal）
    及其用例不在本文件内，随阶段F一并提交；
  - 全部使用内存 SQLite（sqlite+aiosqlite:///:memory:），不读写正式库 novelcraft.db；
  - 不启动服务、不调用真实 AI、不依赖 backend/temp 下的任何文件。

覆盖规则：
  timeline.age           年龄倒退 → blocking
  timeline.event_order   因果先后倒挂 → blocking
  timeline.travel_time   已知行程耗时超阈值 → warning
  state.teleport         相邻章节地点跳转且行程未知 → warning
  state.death_reappear   死亡后重现 → blocking
  possession.conflict    同一时段双人持有 → blocking
  secret.early_knowledge 提前知情 → blocking；无知情记录 → warning
  chapter_meta.continuity 缺上章落点 → info；与上章完全无关 → warning
  build_fact_summary     valid 仅由 blocking 决定；exempted 单列计数
  fact_exemption_enabled accepted_exception 命中 → exempted（开关关则不豁免）
  fact_cognition_enabled belief 类快照不按客观知情判定（开关关则按旧行为）
  只读幂等               连续两次校验不写入任何事实，问题指纹稳定
"""
import asyncio
import os
import sys
import uuid

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models.character import Character  # noqa: E402
from app.models.character_state_snapshot import CharacterStateSnapshot  # noqa: E402
from app.models.fact_issue_disposition import FactIssueDisposition  # noqa: E402
from app.models.item import Item  # noqa: E402
from app.models.item_possession import ItemPossession  # noqa: E402
from app.models.location import Location  # noqa: E402
from app.models.outline import Outline  # noqa: E402
from app.models.secret_knower import SecretKnower  # noqa: E402
from app.models.timeline_event import TimelineEvent  # noqa: E402
from app.services.fact_validator import (  # noqa: E402
    BLOCKING,
    INFO,
    WARNING,
    FactIssue,
    build_fact_summary,
    run_fact_validation,
)


def run(coro):
    return asyncio.run(coro)


async def _fresh_db() -> tuple:
    """内存库 + 全量表结构（create_all），返回 (engine, session)。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    return engine, session


async def _seed_project_char(db, project_id, name="陈九") -> Character:
    char = Character(id=uuid.uuid4(), project_id=project_id, name=name)
    db.add(char)
    await db.flush()
    return char


async def _seed_chapter(db, project_id, sort_order, title, **kw) -> Outline:
    ch = Outline(
        id=uuid.uuid4(),
        project_id=project_id,
        outline_type="chapter",
        title=title,
        sort_order=sort_order,
        **kw,
    )
    db.add(ch)
    await db.flush()
    return ch


async def _seed_location(db, project_id, name, parent_location_id=None,
                         travel_hours_from=None) -> Location:
    loc = Location(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        parent_location_id=parent_location_id,
        travel_hours_from=travel_hours_from,
    )
    db.add(loc)
    await db.flush()
    return loc


async def _snap(db, char, chapter, **kw) -> CharacterStateSnapshot:
    s = CharacterStateSnapshot(
        id=uuid.uuid4(),
        character_id=char.id,
        chapter_id=chapter.id,
        state_json="{}",
        source="ai_extracted",
        **kw,
    )
    db.add(s)
    await db.flush()
    return s


# ---------- 1. timeline.age：年龄倒退 → blocking ----------
def test_timeline_age_backward_is_blocking():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            await _snap(db, char, ch1, age=30, is_alive=True)
            await _snap(db, char, ch2, age=28, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            ages = [i for i in issues if i.rule == "timeline.age"]
            assert len(ages) == 1
            assert ages[0].severity == BLOCKING
            assert str(ages[0].character_id) == str(char.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 2. timeline.event_order：因果先后倒挂 → blocking ----------
def test_timeline_event_order_backward_is_blocking():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            cause = TimelineEvent(
                id=uuid.uuid4(), project_id=project_id, title="因",
                chapter_id=ch2.id, sequence_order=1, source="ai_extracted",
                confirmed=True,
            )
            ev = TimelineEvent(
                id=uuid.uuid4(), project_id=project_id, title="果",
                chapter_id=ch1.id, sequence_order=1,
                cause_event_id=cause.id, source="ai_extracted", confirmed=True,
            )
            db.add_all([cause, ev])
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            orders = [i for i in issues if i.rule == "timeline.event_order"]
            assert len(orders) == 1
            assert orders[0].severity == BLOCKING
            assert str(orders[0].chapter_id) == str(ch1.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 3. state.teleport：相邻章节瞬移（距离未知）→ warning ----------
def test_state_teleport_warns_when_adjacent_location_unknown():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            loc_a = await _seed_location(db, project_id, "北关城")
            loc_b = await _seed_location(db, project_id, "边军大营")
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            await _snap(db, char, ch1, location_id=loc_a.id, is_alive=True)
            await _snap(db, char, ch2, location_id=loc_b.id, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            teleports = [i for i in issues if i.rule == "state.teleport"]
            assert len(teleports) == 1
            assert teleports[0].severity == WARNING
            assert str(teleports[0].character_id) == str(char.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 4. timeline.travel_time：已知行程耗时超阈值 → warning ----------
def test_timeline_travel_time_too_long_warns():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            parent = await _seed_location(db, project_id, "北境")
            child = await _seed_location(
                db, project_id, "边军大营",
                parent_location_id=parent.id, travel_hours_from=200,
            )
            # 跨章节（不判定为瞬移），命中已知耗时 > 24*7 小时的提示分支
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch3 = await _seed_chapter(db, project_id, 3, "第三章")
            await _snap(db, char, ch1, location_id=parent.id, is_alive=True)
            await _snap(db, char, ch3, location_id=child.id, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            travels = [i for i in issues if i.rule == "timeline.travel_time"]
            assert len(travels) == 1
            assert travels[0].severity == WARNING
            assert "200" in travels[0].message
            assert str(travels[0].location_id) == str(child.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 5. state.death_reappear：死亡重现 → blocking ----------
def test_death_reappear_is_blocking():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            await _snap(db, char, ch1, is_alive=False)
            await _snap(db, char, ch2, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            deaths = [i for i in issues if i.rule == "state.death_reappear"]
            assert len(deaths) == 1
            assert deaths[0].severity == BLOCKING
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 6. possession.conflict：同物品同时两人持有 → blocking ----------
def test_possession_conflict_is_blocking():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char_a = await _seed_project_char(db, project_id, "陈九")
            char_b = await _seed_project_char(db, project_id, "都尉")
            item = Item(
                id=uuid.uuid4(), project_id=project_id, name="玄铁剑",
                category="item", status="active",
            )
            db.add(item)
            await db.flush()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            db.add_all([
                ItemPossession(
                    id=uuid.uuid4(), item_id=item.id,
                    owner_character_id=char_a.id,
                    since_chapter_id=ch1.id, status="active",
                ),
                ItemPossession(
                    id=uuid.uuid4(), item_id=item.id,
                    owner_character_id=char_b.id,
                    since_chapter_id=ch1.id, status="active",
                ),
            ])
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            conflicts = [i for i in issues if i.rule == "possession.conflict"]
            assert len(conflicts) == 1
            assert conflicts[0].severity == BLOCKING
            assert str(conflicts[0].item_id) == str(item.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 7. secret.early_knowledge：秘密提前知情 → blocking ----------
def test_secret_early_knowledge_is_blocking():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            secret = Item(
                id=uuid.uuid4(), project_id=project_id, name="陈九身世之谜",
                category="secret", status="active",
            )
            db.add(secret)
            await db.flush()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            # 快照第一章已知晓；事实库记录第二章才知晓 → 提前知情
            await _snap(db, char, ch1, known_secrets='["陈九身世之谜"]', is_alive=True)
            db.add(SecretKnower(
                id=uuid.uuid4(), secret_id=secret.id, character_id=char.id,
                learned_chapter_id=ch2.id, source="ai_extracted",
            ))
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            early = [i for i in issues if i.rule == "secret.early_knowledge"
                     and i.severity == BLOCKING]
            assert len(early) == 1
            assert str(early[0].character_id) == str(char.id)
            assert str(early[0].item_id) == str(secret.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 8. secret.early_knowledge：无知情记录 → warning ----------
def test_secret_no_knower_record_warns():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            secret = Item(
                id=uuid.uuid4(), project_id=project_id, name="都尉旧怨",
                category="secret", status="active",
            )
            db.add(secret)
            await db.flush()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            await _snap(db, char, ch1, known_secrets='["都尉旧怨"]', is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            warns = [i for i in issues if i.rule == "secret.early_knowledge"
                     and i.severity == WARNING]
            assert len(warns) == 1
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 9. chapter_meta.continuity：衔接缺失 → info / 完全无关 → warning ----------
def test_chapter_meta_continuity_info_and_warning():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            ch1.chapter_outcome = None  # 上章无落点
            ch2.chapter_goal = "本章推进陈九与都尉结盟后的军务整备"
            # ch2 自身有落点：保证 ch2→ch3 走衔接判定（warning/info 二选一），
            # 而不是「缺上章落点」的 info，避免 info 计数变 2
            ch2.chapter_outcome = "陈九加入边军大营"
            ch3 = await _seed_chapter(db, project_id, 3, "第三章")
            ch3.chapter_goal = "本章描写北境风雪与马队行进"
            await db.commit()

            issues = await run_fact_validation(db, project_id)
            infos = [i for i in issues if i.rule == "chapter_meta.continuity"
                     and i.severity == INFO]
            assert len(infos) == 1  # 仅 ch1→ch2 缺上章落点

            ch1.chapter_outcome = "陈九与都尉在边军大营达成同盟，共同整备军务。"
            ch2.chapter_goal = "本章推进陈九与都尉结盟后的军务整备。"
            await db.commit()
            issues2 = await run_fact_validation(db, project_id)
            warns = [i for i in issues2 if i.rule == "chapter_meta.continuity"
                     and i.severity == WARNING]
            # ch1→ch2 相关；ch2→ch3 目标与上章落点完全无关 → warning
            assert len(warns) == 1
            assert str(warns[0].chapter_id) == str(ch3.id)
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 10. build_fact_summary：blocking 决定 valid ----------
def test_build_fact_summary_valid_flag():
    clean = [
        FactIssue(dimension="timeline", rule="timeline.travel_time",
                  severity=WARNING, message="行程未知"),
        FactIssue(dimension="chapter_meta", rule="chapter_meta.continuity",
                  severity=INFO, message="缺落点"),
    ]
    summary = build_fact_summary(clean)
    assert summary["valid"] is True
    assert summary["counts"]["blocking"] == 0
    assert summary["counts"]["warning"] == 1
    assert summary["counts"]["info"] == 1
    assert summary["counts"]["exempted"] == 0

    dirty = clean + [
        FactIssue(dimension="timeline", rule="timeline.age",
                  severity=BLOCKING, message="年龄倒退"),
    ]
    summary2 = build_fact_summary(dirty)
    assert summary2["valid"] is False
    assert summary2["counts"]["blocking"] == 1
    assert summary2["dimensions"]["timeline"]["blocking"] == 1
    assert summary2["dimensions"]["timeline"]["warning"] == 1


# ---------- 11. 多规则同现 + chapter_id 限定范围 ----------
def test_validator_multi_rule_integration():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            # 年龄倒退 + 死亡重现同时出现
            await _snap(db, char, ch1, age=30, is_alive=False)
            await _snap(db, char, ch2, age=29, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id, chapter_id=ch2.id)
            rules = {i.rule for i in issues}
            assert "timeline.age" in rules
            assert "state.death_reappear" in rules
            summary = build_fact_summary(issues)
            assert summary["valid"] is False
            assert summary["counts"]["blocking"] >= 2
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 12. 只读幂等：连续两次校验不写事实，指纹稳定 ----------
def test_validation_is_readonly_and_idempotent():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            await _snap(db, char, ch1, age=30, is_alive=False)
            await _snap(db, char, ch2, age=29, is_alive=True)
            await db.commit()

            async def _counts() -> dict:
                out = {}
                for model in (
                    CharacterStateSnapshot, TimelineEvent, Item,
                    ItemPossession, SecretKnower, Outline,
                ):
                    out[model.__name__] = (await db.execute(
                        select(func.count()).select_from(model)
                    )).scalar_one()
                return out

            before = await _counts()
            first = await run_fact_validation(db, project_id)
            second = await run_fact_validation(db, project_id)
            after = await _counts()

            assert before == after  # 校验不写任何事实表
            assert [i.issue_key for i in first] == [i.issue_key for i in second]
            assert [i.rule for i in first] == [i.rule for i in second]
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 13. fact_exemption_enabled：accepted_exception 豁免 ----------
def test_accepted_exception_exemption_respects_switch():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            await _snap(db, char, ch1, is_alive=False)
            await _snap(db, char, ch2, is_alive=True)
            await db.commit()

            issues = await run_fact_validation(db, project_id, exemptions_enabled=False)
            blocking = [i for i in issues if i.severity == BLOCKING]
            assert blocking, "前置条件：应存在 blocking 问题"
            key = blocking[0].issue_key
            assert key

            db.add(FactIssueDisposition(
                id=uuid.uuid4(), project_id=str(project_id),
                issue_key=key, disposition="accepted_exception",
                entity_type="character", entity_id=str(char.id),
                rule="state.death_reappear",
            ))
            await db.commit()

            on = await run_fact_validation(db, project_id, exemptions_enabled=True)
            exempted = [i for i in on if i.exempted]
            assert len(exempted) == 1
            assert exempted[0].disposition == "accepted_exception"
            summary_on = build_fact_summary(on)
            assert summary_on["counts"]["exempted"] == 1
            assert summary_on["valid"] is True

            off = await run_fact_validation(db, project_id, exemptions_enabled=False)
            assert all(not i.exempted for i in off)
            assert build_fact_summary(off)["valid"] is False
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


# ---------- 14. fact_cognition_enabled：belief 类快照不按客观知情判定 ----------
def test_cognition_switch_controls_belief_snapshots():
    async def _scenario():
        engine, db = await _fresh_db()
        try:
            project_id = uuid.uuid4()
            char = await _seed_project_char(db, project_id)
            secret = Item(
                id=uuid.uuid4(), project_id=project_id, name="陈九身世之谜",
                category="secret", status="active",
            )
            db.add(secret)
            await db.flush()
            ch1 = await _seed_chapter(db, project_id, 1, "第一章")
            ch2 = await _seed_chapter(db, project_id, 2, "第二章")
            # belief：角色听说/相信，不是客观知情
            await _snap(
                db, char, ch1, known_secrets='["陈九身世之谜"]',
                is_alive=True, fact_kind="belief",
            )
            db.add(SecretKnower(
                id=uuid.uuid4(), secret_id=secret.id, character_id=char.id,
                learned_chapter_id=ch2.id, source="ai_extracted",
            ))
            await db.commit()

            on = await run_fact_validation(db, project_id, cognition_enabled=True)
            assert [i for i in on if i.rule == "secret.early_knowledge"] == []

            off = await run_fact_validation(db, project_id, cognition_enabled=False)
            early = [i for i in off if i.rule == "secret.early_knowledge"
                     and i.severity == BLOCKING]
            assert len(early) == 1
        finally:
            await db.close()
            await engine.dispose()

    run(_scenario())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
