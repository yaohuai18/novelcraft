"""U2a：章节发布统一编排器（三入口唯一业务事务收口点）。

职责与边界：
    1. 门禁（状态 + 内容证据）→ 发布快照（Publication）→ 发布状态写透
       （mark_published）→ 索引/统计旁路同步 → **唯一一次业务 commit()**；
    2. 拒绝路径：门禁拒绝自行抛 LifecycleRejected（审计只 flush），
       本模块**不提交**，由路由依据 ``audit_pending`` 决定是否提交一次；
    3. 通知 / 幂等记录 / API 调用日志属于路由旁路，不得进入本模块业务事务；
    4. 本模块不写任何发布状态列，也不直接实例化 ``Publication``。

入口能力差异（不扩大既有拒绝面）：
    - ``GATE_PROFILE_STATE_ONLY``：E1（作者端）/ E2（开放 API）沿用既有门禁，
      校验报告/锚点的拒绝仍由各入口既有内容体检负责；
    - ``GATE_PROFILE_FULL``：E3（生命周期入口）保留正文/报告/锚点/blocking 全量门禁。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outline import Outline
from app.models.project import Project
from app.schemas.publication import PublicationCreate, PublicationResponse
from app.services import chapter_lifecycle, publication_service

logger = logging.getLogger(__name__)

GATE_PROFILE_STATE_ONLY = "state_only"
GATE_PROFILE_FULL = "full"

GateProfile = Literal["state_only", "full"]


@dataclass(slots=True)
class PublishCommand:
    """单次发布的输入事实（由路由组装，编排器不回溯请求上下文）。"""

    project: Project
    outline: Outline
    payload: PublicationCreate | None = None
    write_snapshot: bool = True
    gate_profile: GateProfile = GATE_PROFILE_STATE_ONLY
    actor_id: str | None = None


@dataclass(slots=True)
class PublishOutcome:
    """发布结果：发布状态 + 快照响应 + 门禁事实（供路由构造各自响应体）。"""

    lifecycle_status: str
    publication: PublicationResponse | None
    facts: chapter_lifecycle.PublishFacts
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


async def publish_chapter(
    db: AsyncSession,
    cmd: PublishCommand,
) -> PublishOutcome:
    """执行一次完整发布：门禁 → 快照 → 写透 → 旁路同步 → 唯一业务提交。

    成功：返回 PublishOutcome，业务状态已提交（索引/统计失败不回滚业务）。
    拒绝：抛 ``LifecycleRejected``（审计仅 flush，未提交）或既有 ``HTTPException``
    （内容体检 / 可见性 / 作品归档 409），本函数不提交任何东西。
    """
    facts = await chapter_lifecycle.validate_publish(
        db,
        cmd.outline,
        require_validation_evidence=cmd.gate_profile == GATE_PROFILE_FULL,
    )

    publication: PublicationResponse | None = None
    if cmd.write_snapshot:
        publication = await publication_service.publish_chapter(
            db,
            cmd.project,
            cmd.outline,
            cmd.payload or PublicationCreate(),
            commit=False,
        )

    await chapter_lifecycle.mark_published(db, cmd.outline, facts, actor_id=cmd.actor_id)

    # 索引/统计：savepoint 隔离，失败只降级为旁路异常，不回滚业务写入
    await publication_service.sync_publish_side_effects(
        db, cmd.project, cmd.outline, commit=False,
    )

    # 唯一一次业务提交（发布状态 + Publication + outline 列 + 成功审计同一事务）
    await db.commit()

    return PublishOutcome(
        lifecycle_status=chapter_lifecycle.STATUS_PUBLISHED,
        publication=publication,
        facts=facts,
        message=facts.facts_version_hint.get("message") if facts.facts_version_hint else None,
        warnings=list(facts.warnings),
    )
