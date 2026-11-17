# -*- coding: utf-8 -*-
"""第一批数据完整性：generation_records 归属状态回填。

确定性规则（只用外键，不做时间邻近/内容相似等弱证据推断）：
  1. project_id 已存在                     -> project_bound
  2. chapter_id 指向的 outlines 有 project_id -> project_bound（同时回填 project_id）
  3. volume_id  指向的 outlines 有 project_id -> project_bound（同时回填 project_id）
  4. outline_plan_version_id 指向的版本有 project_id -> project_bound（回填 project_id）
  5. 以上都不成立                           -> unattributed（project_id 保持 NULL）

幂等：只处理 attribution_status IS NULL 的行；已归属的行不会被再次改写。
dry_run=True 时统计照做但不提交。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation import GenerationRecord
from app.models.outline import Outline
from app.models.outline_plan_version import OutlinePlanVersion

RULE_PROJECT_PRESENT = "project_id_present"
RULE_CHAPTER = "chapter_outline"
RULE_VOLUME = "volume_outline"
RULE_PLAN_VERSION = "outline_plan_version"
RULE_UNATTRIBUTED = "no_deterministic_link"


@dataclass
class BackfillStats:
    total: int = 0
    project_bound: int = 0
    unattributed: int = 0
    project_id_backfilled: int = 0
    rules: dict[str, int] = field(default_factory=dict)
    dry_run: bool = True

    def hit(self, rule: str) -> None:
        self.rules[rule] = self.rules.get(rule, 0) + 1

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "project_bound": self.project_bound,
            "pre_project": 0,
            "unattributed": self.unattributed,
            "project_id_backfilled": self.project_id_backfilled,
            "rules": dict(sorted(self.rules.items())),
            "dry_run": self.dry_run,
        }


async def _outline_project_id(
    db: AsyncSession, outline_id: uuid.UUID | None
) -> uuid.UUID | None:
    if outline_id is None:
        return None
    return (
        await db.execute(select(Outline.project_id).where(Outline.id == outline_id))
    ).scalar_one_or_none()


async def _plan_version_project_id(
    db: AsyncSession, version_id: uuid.UUID | None
) -> uuid.UUID | None:
    if version_id is None:
        return None
    return (
        await db.execute(
            select(OutlinePlanVersion.project_id).where(
                OutlinePlanVersion.id == version_id
            )
        )
    ).scalar_one_or_none()


async def backfill_generation_attribution(
    db: AsyncSession, *, dry_run: bool = True, limit: int | None = None
) -> dict:
    """回填历史记录的 attribution_status；返回统计（含每条规则命中数）。"""
    stats = BackfillStats(dry_run=dry_run)

    stmt = select(GenerationRecord).where(
        GenerationRecord.attribution_status.is_(None)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    for row in rows:
        stats.total += 1
        project_id = row.project_id

        if project_id is not None:
            row.attribution_status = "project_bound"
            stats.project_bound += 1
            stats.hit(RULE_PROJECT_PRESENT)
            continue

        resolved = await _outline_project_id(db, row.chapter_id)
        rule = RULE_CHAPTER
        if resolved is None:
            resolved = await _outline_project_id(db, row.volume_id)
            rule = RULE_VOLUME
        if resolved is None:
            resolved = await _plan_version_project_id(db, row.outline_plan_version_id)
            rule = RULE_PLAN_VERSION

        if resolved is not None:
            row.project_id = resolved
            row.attribution_status = "project_bound"
            stats.project_bound += 1
            stats.project_id_backfilled += 1
            stats.hit(rule)
        else:
            row.attribution_status = "unattributed"
            stats.unattributed += 1
            stats.hit(RULE_UNATTRIBUTED)

    if dry_run:
        await db.rollback()
    else:
        await db.commit()

    return stats.as_dict()
