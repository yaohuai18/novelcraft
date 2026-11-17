# -*- coding: utf-8 -*-
"""B3 接口接入回归：facts 路由（4 端点）的项目隔离、权限与写路径。

覆盖目标（B3 接口接入批次）：
    1. 鉴权契约：未认证 401 + 中文 detail + ``WWW-Authenticate: Bearer``；
       无效 token / token 指向不存在用户 → 401 且 detail 为中文（无效 token 须带同一认证头）；
    2. 项目存在性 / 成员关系：不存在项目 404；非成员读 403；viewer 可读，
       非成员与 viewer 写 403；
    3. 参数校验：非法 fact_type / authority_level / disposition → 400；
       accepted_exception 缺触发条件 → 400；confidence 越界、非法 UUID → 422；
    4. 项目隔离：两个项目的事实互不泄漏；项目无章节时 ``GET /facts``
       快照必须为空（不得因空 IN 条件扩大查询范围）；
       ``TimelineEvent`` 必须按 project_id 过滤；
    5. 写路径：``/facts/lock`` 与 ``/issues/disposition`` 成功响应，
       并且改动确实落库（新会话回读，而非仅返回内存对象）；
    6. 既有筛选 / 排序语义保持（authority / locked / confidence_min）。

硬约束（与批次任务书一致）：
    - 不启动服务、不监听端口：仅进程内 ASGI（httpx.ASGITransport）；
    - 只使用 pytest 临时目录（仓库外）中的 SQLite 文件库 + ORM create_all，
      不触碰正式库、不执行迁移；
    - 不调用真实 AI、不发起网络请求（tests/conftest.py 的离线护栏仍生效）。

Run: cd backend && python -m pytest tests/test_b3_facts_router.py -q
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.auth import UNAUTHENTICATED_DETAIL
from app.core.database import Base, get_db
from app.models.character import Character
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.collaboration import ProjectMember
from app.models.fact_issue_disposition import FactIssueDisposition
from app.models.item import Item
from app.models.item_possession import ItemPossession
from app.models.outline import Outline
from app.models.project import Project
from app.models.secret_knower import SecretKnower
from app.models.timeline_event import TimelineEvent
from app.models.user import User
from app.routers.facts import router as facts_router
from app.utils.security import create_access_token

UNAUTHENTICATED_HEADER = {"WWW-Authenticate": "Bearer"}


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库 + 只挂 facts 路由的进程内 ASGI 应用。"""
    db_dir = tmp_path_factory.mktemp("b3_facts_router")
    db_path = db_dir / "b3_facts_router.db"

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
    app.include_router(facts_router)

    env_data = {
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
    """进程内 ASGI 客户端（不建立任何 socket）。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]),
        base_url="http://testserver",
    )


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _bearer(user_id: uuid.UUID) -> str:
    return create_access_token(str(user_id))


@pytest.fixture(scope="module")
def data(env) -> dict[str, Any]:
    """模块级种子数据：2 个有数据的项目 + 1 个无章节项目 + 4 类成员身份。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="b3_owner",
                email="b3_owner@example.com",
                password_hash="x",
            )
            editor = User(
                username="b3_editor",
                email="b3_editor@example.com",
                password_hash="x",
            )
            viewer = User(
                username="b3_viewer",
                email="b3_viewer@example.com",
                password_hash="x",
            )
            stranger = User(
                username="b3_stranger",
                email="b3_stranger@example.com",
                password_hash="x",
            )
            db.add_all([owner, editor, viewer, stranger])
            await db.flush()

            project_a = Project(user_id=owner.id, title="项目A")
            project_b = Project(user_id=owner.id, title="项目B")
            project_empty = Project(user_id=owner.id, title="空章节项目")
            db.add_all([project_a, project_b, project_empty])
            await db.flush()

            # 成员：A 项目包含 editor（可写）与 viewer（只读）
            db.add_all(
                [
                    ProjectMember(
                        project_id=project_a.id, user_id=editor.id, role="editor"
                    ),
                    ProjectMember(
                        project_id=project_a.id, user_id=viewer.id, role="viewer"
                    ),
                ]
            )

            ch_a1 = Outline(
                project_id=project_a.id, title="A-第一章", outline_type="chapter"
            )
            ch_b1 = Outline(
                project_id=project_b.id, title="B-第一章", outline_type="chapter"
            )
            db.add_all([ch_a1, ch_b1])
            await db.flush()

            char_a1 = Character(project_id=project_a.id, name="A-角色")
            char_b1 = Character(project_id=project_b.id, name="B-角色")
            db.add_all([char_a1, char_b1])
            await db.flush()

            item_a1 = Item(
                project_id=project_a.id,
                name="A-物品",
                category="item",
                confidence=0.9,
            )
            item_a2 = Item(
                project_id=project_a.id,
                name="A-高权威锁定物品",
                category="item",
                authority_level="high",
                locked=True,
                confidence=0.4,
            )
            secret_a1 = Item(
                project_id=project_a.id,
                name="A-秘密",
                category="secret",
            )
            item_b1 = Item(
                project_id=project_b.id,
                name="B-物品",
                category="item",
            )
            db.add_all([item_a1, item_a2, secret_a1, item_b1])
            await db.flush()

            poss_a1 = ItemPossession(item_id=item_a1.id, owner_character_id=char_a1.id)
            knower_a1 = SecretKnower(
                secret_id=secret_a1.id, character_id=char_a1.id
            )
            db.add_all([poss_a1, knower_a1])

            snap_a1 = CharacterStateSnapshot(
                character_id=char_a1.id,
                chapter_id=ch_a1.id,
                state_json='{"age": 20}',
                possessions='["A-物品"]',
                known_secrets="[]",
                injuries="[]",
            )
            snap_b1 = CharacterStateSnapshot(
                character_id=char_b1.id,
                chapter_id=ch_b1.id,
                state_json='{"age": 40}',
            )
            db.add_all([snap_a1, snap_b1])

            event_a0 = TimelineEvent(
                project_id=project_a.id, title="A-事件0", sequence_order=0
            )
            event_a1 = TimelineEvent(
                project_id=project_a.id, title="A-事件2", sequence_order=2
            )
            event_a_null = TimelineEvent(
                project_id=project_a.id, title="A-事件无序号", sequence_order=None
            )
            event_b1 = TimelineEvent(
                project_id=project_b.id, title="B-事件", sequence_order=1
            )
            db.add_all([event_a0, event_a1, event_a_null, event_b1])

            # 写路径专用行：避免与其他用例的只读断言互相干扰
            item_lock_a = Item(
                project_id=project_a.id, name="A-待锁定物品", category="item"
            )
            snap_lock_a = CharacterStateSnapshot(
                character_id=char_a1.id,
                chapter_id=ch_a1.id,
                state_json='{"age": 21}',
            )
            item_other_b = Item(
                project_id=project_b.id, name="B-跨项目锁定目标", category="item"
            )
            db.add_all([item_lock_a, snap_lock_a, item_other_b])
            await db.commit()

            ids.update(
                owner=owner.id,
                editor=editor.id,
                viewer=viewer.id,
                stranger=stranger.id,
                project_a=project_a.id,
                project_b=project_b.id,
                project_empty=project_empty.id,
                ch_a1=ch_a1.id,
                ch_b1=ch_b1.id,
                char_a1=char_a1.id,
                item_a1=item_a1.id,
                item_a2=item_a2.id,
                secret_a1=secret_a1.id,
                item_b1=item_b1.id,
                poss_a1=poss_a1.id,
                knower_a1=knower_a1.id,
                snap_a1=snap_a1.id,
                snap_b1=snap_b1.id,
                event_a0=event_a0.id,
                event_a1=event_a1.id,
                event_a_null=event_a_null.id,
                event_b1=event_b1.id,
                item_lock_a=item_lock_a.id,
                snap_lock_a=snap_lock_a.id,
                item_other_b=item_other_b.id,
            )
        return ids

    return asyncio.run(_seed())


def _facts_url(project_id: uuid.UUID) -> str:
    return f"/api/projects/{project_id}/facts"


# --------------------------------------------------------------- 鉴权契约（401）


def test_missing_authorization_returns_401_contract(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(_facts_url(data["project_a"]))

    resp = asyncio.run(scenario())
    assert resp.status_code == 401
    assert resp.json() == {"detail": UNAUTHENTICATED_DETAIL}
    assert resp.headers.get("WWW-Authenticate") == UNAUTHENTICATED_HEADER[
        "WWW-Authenticate"
    ]


def test_invalid_token_returns_401_with_chinese_detail(env, data):
    """不可解析凭证由 get_current_user 抛出 401，且必须携带认证头（B1 契约）。"""

    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(
                _facts_url(data["project_a"]), headers=_auth("not-a-jwt")
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail == "无效或已过期的登录凭证"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail)
    assert resp.headers.get("WWW-Authenticate") == UNAUTHENTICATED_HEADER[
        "WWW-Authenticate"
    ]


def test_token_for_unknown_user_returns_401_chinese_detail(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(
                _facts_url(data["project_a"]), headers=_auth(_bearer(uuid.uuid4()))
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "用户不存在"}


# ------------------------------------------------------------ 项目存在性与成员


def test_unknown_project_returns_404(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(
                _facts_url(uuid.uuid4()), headers=_auth(_bearer(data["owner"]))
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "项目不存在"}


def test_non_member_read_is_forbidden(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(
                _facts_url(data["project_a"]),
                headers=_auth(_bearer(data["stranger"])),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "无权访问该项目"}


def test_viewer_can_read_project_facts(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            resp = await client.get(
                _facts_url(data["project_a"]),
                headers=_auth(_bearer(data["viewer"])),
            )
        return {"status": resp.status_code, "body": resp.json()}

    out = asyncio.run(scenario())
    assert out["status"] == 200
    assert out["body"]["project_id"] == str(data["project_a"])


def test_editor_can_read_project_facts(env, data):
    async def scenario() -> int:
        async with _client(env) as client:
            resp = await client.get(
                _facts_url(data["project_a"]),
                headers=_auth(_bearer(data["editor"])),
            )
        return resp.status_code

    assert asyncio.run(scenario()) == 200


# --------------------------------------------------------------- 写接口 403


def _lock_body(fact_type: str, fact_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    body = {"fact_type": fact_type, "fact_id": str(fact_id), "locked": True}
    body.update(extra)
    return body


def _disposition_body(**extra: Any) -> dict[str, Any]:
    body = {"issue_key": "rule:test:fp", "disposition": "confirmed"}
    body.update(extra)
    return body


@pytest.mark.parametrize(
    "user_key, expected_detail",
    [
        ("viewer", "无权修改该项目事实"),
        ("stranger", "无权修改该项目事实"),
    ],
)
def test_write_lock_forbidden_for_viewer_and_non_member(env, data, user_key, expected_detail):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data[user_key])),
                json=_lock_body("item", data["item_lock_a"]),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 403
    assert resp.json() == {"detail": expected_detail}


@pytest.mark.parametrize(
    "user_key", ["viewer", "stranger"]
)
def test_write_disposition_forbidden_for_viewer_and_non_member(env, data, user_key):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data[user_key])),
                json=_disposition_body(),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "无权处置该项目问题"}


# ----------------------------------------------------------------- 参数校验 400


def test_invalid_fact_type_returns_400(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body("bogus_type", data["item_lock_a"]),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 400
    assert "不支持的 fact_type" in resp.json()["detail"]


def test_invalid_authority_level_returns_400(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body(
                    "item", data["item_lock_a"], authority_level="supreme"
                ),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 400
    assert resp.json() == {"detail": "authority_level 仅支持 low|normal|high"}


def test_invalid_disposition_returns_400(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data["owner"])),
                json=_disposition_body(disposition="nope"),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 400
    assert "disposition 仅支持" in resp.json()["detail"]


def test_accepted_exception_without_conditions_returns_400(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data["owner"])),
                json=_disposition_body(disposition="accepted_exception"),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 400
    assert "accepted_exception 必须携带触发条件" in resp.json()["detail"]


# ----------------------------------------------------------------- 参数校验 422


def test_lock_confidence_out_of_range_returns_422(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body("item", data["item_lock_a"], confidence=1.5),
            )

    assert asyncio.run(scenario()).status_code == 422


def test_facts_confidence_min_out_of_range_returns_422(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.get(
                _facts_url(data["project_a"]),
                headers=_auth(_bearer(data["owner"])),
                params={"confidence_min": 1.2},
            )

    assert asyncio.run(scenario()).status_code == 422


@pytest.mark.parametrize(
    "method, path_builder",
    [
        ("GET", lambda data: "/api/projects/not-a-uuid/facts"),
        (
            "POST",
            lambda data: f"/api/projects/{data['project_a']}/chapters/not-a-uuid/validate",
        ),
        (
            "POST",
            lambda data: "/api/projects/not-a-uuid/facts/lock",
        ),
    ],
)
def test_invalid_uuid_in_path_returns_422(env, data, method, path_builder):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.request(
                method,
                path_builder(data),
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body("item", data["item_lock_a"])
                if method == "POST"
                else None,
            )

    assert asyncio.run(scenario()).status_code == 422


def test_invalid_fact_id_uuid_returns_422(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json={"fact_type": "item", "fact_id": "not-a-uuid", "locked": True},
            )

    assert asyncio.run(scenario()).status_code == 422


# ------------------------------------------------------------ 项目隔离与排序


def test_snapshots_are_isolated_by_project_chapters(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            return (
                await client.get(
                    _facts_url(data["project_a"]),
                    headers=_auth(_bearer(data["owner"])),
                )
            ).json()

    body = asyncio.run(scenario())
    snapshot_ids = {row["id"] for row in body["snapshots"]}
    assert str(data["snap_a1"]) in snapshot_ids
    assert str(data["snap_b1"]) not in snapshot_ids
    # 只允许出现本项目章节下的快照
    allowed = {str(data["ch_a1"])}
    assert {row["chapter_id"] for row in body["snapshots"]} <= allowed


def test_timeline_events_filtered_by_project_id(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            a = await client.get(
                _facts_url(data["project_a"]), headers=_auth(_bearer(data["owner"]))
            )
            b = await client.get(
                _facts_url(data["project_b"]), headers=_auth(_bearer(data["owner"]))
            )
        return {"a": a.json(), "b": b.json()}

    out = asyncio.run(scenario())
    a_ids = {row["id"] for row in out["a"]["timeline_events"]}
    b_ids = {row["id"] for row in out["b"]["timeline_events"]}

    assert str(data["event_a0"]) in a_ids
    assert str(data["event_a1"]) in a_ids
    assert str(data["event_b1"]) not in a_ids
    assert b_ids == {str(data["event_b1"])}


def test_items_and_relations_are_isolated(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            return (
                await client.get(
                    _facts_url(data["project_a"]),
                    headers=_auth(_bearer(data["owner"])),
                )
            ).json()

    body = asyncio.run(scenario())
    item_ids = {row["id"] for row in body["items"]}
    assert str(data["item_b1"]) not in item_ids
    assert {str(data["item_a1"]), str(data["secret_a1"])} <= item_ids

    assert {row["id"] for row in body["possessions"]} == {str(data["poss_a1"])}
    assert {row["id"] for row in body["secret_knowers"]} == {str(data["knower_a1"])}


def test_project_without_chapters_returns_empty_snapshots(env, data):
    """项目无章节时必须返回空快照，而不是把全库快照都吐出来。"""

    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            body = (
                await client.get(
                    _facts_url(data["project_empty"]),
                    headers=_auth(_bearer(data["owner"])),
                )
            ).json()
            async with env["SessionLocal"]() as db:
                total = (
                    await db.execute(select(func.count()).select_from(CharacterStateSnapshot))
                ).scalar_one()
                total_events = (
                    await db.execute(select(func.count()).select_from(TimelineEvent))
                ).scalar_one()
        return {"body": body, "total_snapshots": total, "total_events": total_events}

    out = asyncio.run(scenario())
    assert out["total_snapshots"] > 0  # 库中确实存在其他项目的快照
    assert out["total_events"] > 0
    assert out["body"]["snapshots"] == []
    assert out["body"]["timeline_events"] == []
    assert out["body"]["items"] == []


def test_items_ordered_by_created_at_and_events_by_sequence(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            body = (
                await client.get(
                    _facts_url(data["project_a"]),
                    headers=_auth(_bearer(data["owner"])),
                )
            ).json()
            async with env["SessionLocal"]() as db:
                rows = (
                    await db.execute(
                        select(Item.id, Item.created_at).where(
                            Item.project_id == data["project_a"]
                        )
                    )
                ).all()
        created = {str(r[0]): r[1] for r in rows}
        return {"body": body, "created": created}

    out = asyncio.run(scenario())
    item_ids = [row["id"] for row in out["body"]["items"]]
    item_times = [out["created"][i] for i in item_ids]
    assert item_times == sorted(item_times)

    event_ids = [row["id"] for row in out["body"]["timeline_events"]]
    assert event_ids == [
        str(data["event_a0"]),
        str(data["event_a1"]),
        str(data["event_a_null"]),  # sequence_order=NULL 排最后
    ]


def test_filters_preserved(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            headers = _auth(_bearer(data["owner"]))
            high = await client.get(
                _facts_url(data["project_a"]),
                headers=headers,
                params={"authority": "high"},
            )
            locked = await client.get(
                _facts_url(data["project_a"]),
                headers=headers,
                params={"locked": "true"},
            )
            conf = await client.get(
                _facts_url(data["project_a"]),
                headers=headers,
                params={"confidence_min": 0.5},
            )
        return {
            "high": high.json(),
            "locked": locked.json(),
            "conf": conf.json(),
        }

    out = asyncio.run(scenario())
    assert {row["id"] for row in out["high"]["items"]} == {str(data["item_a2"])}
    assert {row["id"] for row in out["locked"]["items"]} == {str(data["item_a2"])}
    # confidence_min=0.5：item_a1(0.9) 入选，item_a2(0.4) 被排除
    assert {row["id"] for row in out["conf"]["items"]} == {str(data["item_a1"])}


def test_validate_rejects_chapter_of_other_project(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/chapters/{data['ch_b1']}/validate",
                headers=_auth(_bearer(data["owner"])),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "章节不存在或不属于当前作品"}


# ------------------------------------------------------------- 写路径 + 落库


def test_lock_fact_persists_to_database(env, data):
    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            resp = await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body(
                    "item",
                    data["item_lock_a"],
                    authority_level="high",
                    confidence=0.25,
                    source_record_id="manual-b3",
                ),
            )
        # 新会话回读，确认不是只改了内存对象
        async with env["SessionLocal"]() as db:
            row = await db.get(Item, data["item_lock_a"])
            persisted = {
                "locked": row.locked,
                "authority_level": row.authority_level,
                "confidence": row.confidence,
                "source_record_id": row.source_record_id,
            }
        return {"status": resp.status_code, "body": resp.json(), "persisted": persisted}

    out = asyncio.run(scenario())
    assert out["status"] == 200
    body = out["body"]
    assert body["fact_id"] == str(data["item_lock_a"])
    assert body["fact_type"] == "item"
    assert body["locked"] is True
    assert body["authority_level"] == "high"
    assert body["confidence"] == 0.25
    assert body["source_record_id"] == "manual-b3"
    assert out["persisted"] == {
        "locked": True,
        "authority_level": "high",
        "confidence": 0.25,
        "source_record_id": "manual-b3",
    }


def test_lock_snapshot_projects_ownership_checked(env, data):
    """快照本身无 project_id，锁定必须经章节归属校验；成功路径同样落库。"""

    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            ok = await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["editor"])),
                json=_lock_body("snapshot", data["snap_lock_a"]),
            )
            cross = await client.post(
                f"/api/projects/{data['project_b']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body("snapshot", data["snap_lock_a"]),
            )
        async with env["SessionLocal"]() as db:
            row = await db.get(CharacterStateSnapshot, data["snap_lock_a"])
            locked = row.locked
        return {
            "ok_status": ok.status_code,
            "ok_body": ok.json(),
            "cross_status": cross.status_code,
            "cross_detail": cross.json()["detail"],
            "locked": locked,
        }

    out = asyncio.run(scenario())
    assert out["ok_status"] == 200
    assert out["ok_body"]["locked"] is True
    assert out["locked"] is True
    assert out["cross_status"] == 404
    assert out["cross_detail"] == "事实不存在或不属于当前项目"


def test_lock_fact_from_other_project_returns_404(env, data):
    async def scenario() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(
                f"/api/projects/{data['project_a']}/facts/lock",
                headers=_auth(_bearer(data["owner"])),
                json=_lock_body("item", data["item_other_b"]),
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "事实不存在或不属于当前项目"}


def test_disposition_success_persists_and_is_idempotent(env, data):
    issue_key = "rule:b3-test:entity-1"

    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            first = await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data["owner"])),
                json={
                    "issue_key": issue_key,
                    "disposition": "accepted_exception",
                    "note": "已知例外",
                    "rule": "R-B3-001",
                },
            )
            second = await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data["editor"])),
                json={
                    "issue_key": issue_key,
                    "disposition": "false_positive",
                    "note": "误报确认",
                },
            )
        async with env["SessionLocal"]() as db:
            rows = (
                await db.execute(
                    select(FactIssueDisposition).where(
                        FactIssueDisposition.project_id == str(data["project_a"]),
                        FactIssueDisposition.issue_key == issue_key,
                    )
                )
            ).scalars().all()
            persisted = [
                {
                    "disposition": r.disposition,
                    "note": r.note,
                    "rule": r.rule,
                    "set_by": r.set_by,
                }
                for r in rows
            ]
        return {
            "first_status": first.status_code,
            "first": first.json(),
            "second_status": second.status_code,
            "second": second.json(),
            "persisted": persisted,
        }

    out = asyncio.run(scenario())
    assert out["first_status"] == 200
    assert out["second_status"] == 200

    first = out["first"]
    assert first["issue_key"] == issue_key
    assert first["disposition"] == "accepted_exception"
    assert first["note"] == "已知例外"
    assert first["rule"] == "R-B3-001"
    assert first["set_by"] == str(data["owner"])
    assert first["exempted"] is True

    second = out["second"]
    assert second["disposition"] == "false_positive"
    assert second["note"] == "误报确认"
    assert second["rule"] == "R-B3-001"  # 未传入的触发条件保留原值
    assert second["set_by"] == str(data["editor"])
    assert second["exempted"] is False

    # 幂等：同项目同 issue_key 只有一行，且落库值为最后一次写入
    assert len(out["persisted"]) == 1
    assert out["persisted"][0]["disposition"] == "false_positive"
    assert out["persisted"][0]["note"] == "误报确认"
    assert out["persisted"][0]["set_by"] == str(data["editor"])


def test_disposition_isolated_between_projects(env, data):
    issue_key = "rule:b3-test:shared-key"

    async def scenario() -> dict[str, Any]:
        async with _client(env) as client:
            a = await client.post(
                f"/api/projects/{data['project_a']}/issues/disposition",
                headers=_auth(_bearer(data["owner"])),
                json={"issue_key": issue_key, "disposition": "confirmed"},
            )
            b = await client.post(
                f"/api/projects/{data['project_b']}/issues/disposition",
                headers=_auth(_bearer(data["owner"])),
                json={"issue_key": issue_key, "disposition": "deferred"},
            )
        async with env["SessionLocal"]() as db:
            rows = (
                await db.execute(
                    select(FactIssueDisposition).where(
                        FactIssueDisposition.issue_key == issue_key
                    )
                )
            ).scalars().all()
            mapping = {r.project_id: r.disposition for r in rows}
        return {
            "a": a.status_code,
            "b": b.status_code,
            "mapping": mapping,
        }

    out = asyncio.run(scenario())
    assert out["a"] == 200
    assert out["b"] == 200
    assert out["mapping"] == {
        str(data["project_a"]): "confirmed",
        str(data["project_b"]): "deferred",
    }


# ------------------------------------------------------------------- 静态守卫


def test_module_never_references_formal_database_file():
    """守卫：本文件不得出现正式库文件名或只读连接串。"""
    with open(__file__, encoding="utf-8") as fh:
        src = fh.read()
    forbidden_db = "novelcraft" + chr(46) + "db"
    forbidden_uri = "mode" + "=" + "ro"
    assert forbidden_db not in src
    assert forbidden_uri not in src
