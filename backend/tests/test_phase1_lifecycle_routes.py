# -*- coding: utf-8 -*-
"""阶段 1 P0 回归：approve / publish 成功响应的 LifecycleResponse 序列化契约。

背景（只读审查结论）：
    ``LifecycleResponse``（app/schemas/chapter_content.py）声明字段为
    ``project_id / chapter_id / lifecycle_status``（snake_case，无 alias），
    而路由此前以 ``projectId / chapterId / lifecycleStatus`` 构造响应，
    Pydantic 校验必然抛 ValidationError；且 ``db.commit()`` 已在响应构造前执行，
    造成「状态已落库、客户端却收到 500（字段缺失）」的契约错误。

本测试覆盖：
    1. approve 成功 → 2xx，响应体含正确的 snake_case 字段与值；
    2. publish 成功 → 2xx，响应体含正确的 snake_case 字段与值；
    3. 覆盖真实 ``LifecycleResponse`` 序列化路径（走路由 response_model，
       而非直接实例化 schema）：响应体字段集合与 schema 字段集合一致，
       且可被 ``LifecycleResponse.model_validate`` 回读；
    4. 成功路径不得出现「字段缺失」的 500（ASGI 客户端关闭
       raise_app_exceptions，未捕获异常会以 500 暴露而不是抛到测试栈）。

硬约束（与批次任务书一致）：
    - 不启动服务、不监听端口：仅进程内 ASGI（httpx.ASGITransport）；
    - 只使用仓库外 pytest 临时目录中的 SQLite 文件库 + ORM create_all，
      不触碰 backend/novelcraft.db、不执行迁移、不连正式库；
    - 不调用真实 AI、不发起任何网络请求（tests/conftest.py 离线护栏生效）；
    - 不改动 approve/publish 的业务判定、状态机、审计、提交边界与错误映射。

Run: cd backend && python -m pytest tests/test_phase1_lifecycle_routes.py -q
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  注册全部模型，保证 create_all 依赖可解析
from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.routers.chapter_contents import router as chapter_contents_router
from app.schemas.chapter_content import LifecycleResponse

ROUTER_PREFIX = "/api/projects/{project_id}/chapters"
APPROVE_PATH = "/api/projects/{project_id}/chapters/{outline_id}/lifecycle/approve"
PUBLISH_PATH = "/api/projects/{project_id}/chapters/{outline_id}/lifecycle/publish"

EXPECTED_KEYS = {"project_id", "chapter_id", "lifecycle_status", "approved", "blocking", "message"}
LEGACY_KEYS = {"projectId", "chapterId", "lifecycleStatus"}


# --------------------------------------------------------------------- 基础设施


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict[str, Any]:
    """仓库外临时 SQLite 文件库 + 只挂 chapter_contents 路由的进程内 ASGI 应用。"""
    db_dir = tmp_path_factory.mktemp("phase1_lifecycle_routes")
    db_path = db_dir / "phase1_lifecycle_routes.db"

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

    # 鉴权在路由层之外：直接注入当前用户，聚焦响应契约本身
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(chapter_contents_router)

    env_data: dict[str, Any] = {
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
    """进程内 ASGI 客户端；关闭 raise_app_exceptions，使未捕获异常以 500 暴露。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=env["app"], raise_app_exceptions=False
        ),
        base_url="http://testserver",
    )


@pytest.fixture(scope="module")
def data(env) -> dict[str, Any]:
    """种子数据：owner + 1 项目 + 2 章节（待审批 / 已审批）。"""

    async def _seed() -> dict[str, Any]:
        ids: dict[str, Any] = {}
        async with env["SessionLocal"]() as db:
            owner = User(
                username="p0_lifecycle_owner",
                email="p0_lifecycle_owner@example.com",
                password_hash="x",
            )
            db.add(owner)
            await db.flush()

            project = Project(user_id=owner.id, title="P0 生命周期契约项目")
            db.add(project)
            await db.flush()

            # 待审批章节：generated + 已通过校验（D1 门禁要求 validated is True）
            ch_approve = Outline(
                project_id=project.id,
                title="P0-待审批章节",
                outline_type="chapter",
                lifecycle_status="generated",
                validation_report='{"validated": true, "validation_anchor": {"content_version": 1}}',
            )
            # 已审批章节：approved + 已通过校验，可直接发布
            ch_publish = Outline(
                project_id=project.id,
                title="P0-已审批章节",
                outline_type="chapter",
                lifecycle_status="approved",
                validation_report='{"validated": true, "validation_anchor": {"content_version": 1}}',
            )
            db.add_all([ch_approve, ch_publish])
            await db.flush()

            db.add_all(
                [
                    ChapterContent(
                        outline_id=ch_approve.id,
                        content="P0 待审批章节正文",
                        word_count=9,
                        version=1,
                        lifecycle_status="generated",
                    ),
                    ChapterContent(
                        outline_id=ch_publish.id,
                        content="P0 已审批章节正文",
                        word_count=9,
                        version=1,
                        lifecycle_status="approved",
                    ),
                ]
            )
            await db.commit()

            ids.update(
                owner_id=owner.id,
                project_id=project.id,
                ch_approve_id=ch_approve.id,
                ch_publish_id=ch_publish.id,
            )
        return ids

    seeded = asyncio.run(_seed())

    # 路由层鉴权替换：返回带 id 的当前用户（owner 本人 → 跳过成员角色查询）
    env["app"].dependency_overrides[get_current_user] = (
        lambda: SimpleNamespace(id=seeded["owner_id"])
    )
    return seeded


def _url(template: str, project_id: Any, outline_id: Any) -> str:
    return template.format(project_id=project_id, outline_id=outline_id)


def _post(env: dict[str, Any], url: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with _client(env) as client:
            return await client.post(url)

    return asyncio.run(_run())


async def _load_outline(env: dict[str, Any], outline_id: Any) -> Outline:
    async with env["SessionLocal"]() as db:
        row = (
            await db.execute(select(Outline).where(Outline.id == outline_id))
        ).scalar_one()
        return row


# --------------------------------------------------------------------- 契约断言


def _assert_schema_response(
    body: dict[str, Any],
    *,
    project_id: Any,
    outline_id: Any,
    lifecycle_status: str,
) -> None:
    """响应体必须来自 LifecycleResponse 的真实序列化：字段集合与 schema 一致。"""
    expected = set(LifecycleResponse.model_fields.keys())
    assert expected == EXPECTED_KEYS, "LifecycleResponse 字段集合发生变化，请同步本测试"
    assert set(body.keys()) == expected, (
        f"响应体字段与 LifecycleResponse 不一致：多={set(body) - expected}，"
        f"少={expected - set(body)}"
    )
    assert not (LEGACY_KEYS & set(body)), "响应体不得出现 camelCase 遗留字段"

    # 真实序列化产物可被 schema 回读（字段缺失会在此再次暴露）
    parsed = LifecycleResponse.model_validate(body)
    assert str(parsed.project_id) == str(project_id)
    assert str(parsed.chapter_id) == str(outline_id)
    assert parsed.lifecycle_status == lifecycle_status
    assert parsed.approved is True
    assert parsed.blocking == 0


def _assert_not_field_missing_500(response: httpx.Response) -> None:
    """成功路径不得出现字段缺失导致的 500（旧缺陷形态）。"""
    assert response.status_code != 500, (
        "生命周期成功响应不应 500（疑似 LifecycleResponse 构造字段名与 schema 不一致）："
        f"{response.text}"
    )
    assert response.status_code == 200, f"期望 200，实际 {response.status_code}: {response.text}"


# --------------------------------------------------------------------- 用例


def test_approve_success_response_contract(env, data) -> None:
    """approve 成功：2xx + snake_case 字段 + 状态真实落库。"""
    url = _url(APPROVE_PATH, data["project_id"], data["ch_approve_id"])
    response = _post(env, url)

    _assert_not_field_missing_500(response)
    body = response.json()
    _assert_schema_response(
        body,
        project_id=data["project_id"],
        outline_id=data["ch_approve_id"],
        lifecycle_status="approved",
    )

    outline = asyncio.run(_load_outline(env, data["ch_approve_id"]))
    assert outline.lifecycle_status == "approved"


def test_publish_success_response_contract(env, data) -> None:
    """publish 成功：2xx + snake_case 字段 + 状态真实落库。"""
    url = _url(PUBLISH_PATH, data["project_id"], data["ch_publish_id"])
    response = _post(env, url)

    _assert_not_field_missing_500(response)
    body = response.json()
    _assert_schema_response(
        body,
        project_id=data["project_id"],
        outline_id=data["ch_publish_id"],
        lifecycle_status="published",
    )

    outline = asyncio.run(_load_outline(env, data["ch_publish_id"]))
    assert outline.lifecycle_status == "published"


def test_lifecycle_routes_declare_lifecycle_response_model(env) -> None:
    """两个端点必须以 LifecycleResponse 作为响应契约（保证走真实序列化路径）。

    说明：当前 FastAPI 版本延迟展开 include_router，故不检查 app.routes，
    改为读取进程内 OpenAPI 文档中的响应 schema 引用。
    """

    async def _openapi() -> dict[str, Any]:
        async with _client(env) as client:
            response = await client.get("/openapi.json")
            assert response.status_code == 200, response.text
            return response.json()

    spec = asyncio.run(_openapi())
    for path in (APPROVE_PATH, PUBLISH_PATH):
        assert path in spec["paths"], f"OpenAPI 缺少端点 {path}"
        post = spec["paths"][path]["post"]
        ref = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/LifecycleResponse", f"{path} 响应契约被改动：{ref}"

    props = spec["components"]["schemas"]["LifecycleResponse"]["properties"]
    assert set(props.keys()) == EXPECTED_KEYS
    assert not (LEGACY_KEYS & set(props.keys()))
