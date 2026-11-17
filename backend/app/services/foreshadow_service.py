# 伏笔同步导入服务（第四期「写错能发现」伏笔追踪）
# import_from_memory：从 chapter_memory.foreshadows_added 同步导入伏笔清单；
# list_unpaid：查询未回收伏笔（pending|planted），供检查/续写上下文使用。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_memory import ChapterMemory
from app.models.foreshadow import Foreshadow


def _now_utc() -> datetime:
    return datetime.utcnow()


async def list_unpaid(
    db: AsyncSession, project_id: uuid.UUID, limit: int = 100
) -> list[Foreshadow]:
    """查询未回收伏笔（planned|planted），按创建时间倒序。"""
    rows = (
        await db.execute(
            select(Foreshadow)
            .where(
                Foreshadow.project_id == project_id,
                Foreshadow.status.in_(["planned", "planted"]),
            )
            .order_by(Foreshadow.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def import_from_memory(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_ids: list[uuid.UUID] | None = None,
    mark_planted: bool = True,
    replace_duplicates: bool = False,
) -> dict:
    """从章节记忆（chapter_memory.foreshadows_added）同步导入伏笔清单。

    返回 {imported, skipped_duplicates, items: [Foreshadow]}。
    内容相同的伏笔默认跳过（replace_duplicates=True 时更新为 planted 并刷新计划回收说明）。
    """
    stmt = select(ChapterMemory).where(ChapterMemory.project_id == project_id)
    if chapter_ids:
        stmt = stmt.where(ChapterMemory.chapter_id.in_(chapter_ids))
    rows = (await db.execute(stmt)).scalars().all()

    # 预取章节标题（ChapterMemory 无 title 字段，联查 Outline）
    titles: dict[str, str] = {}
    if rows:
        from app.models.outline import Outline

        title_rows = (
            await db.execute(
                select(Outline.id, Outline.title).where(
                    Outline.id.in_([m.chapter_id for m in rows])
                )
            )
        ).all()
        titles = {str(r[0]): (r[1] or "") for r in title_rows}

    # 现有伏笔内容索引（按 project 内去重）
    existing_rows = (
        await db.execute(select(Foreshadow).where(Foreshadow.project_id == project_id))
    ).scalars().all()
    existing_by_content: dict[str, Foreshadow] = {}
    for f in existing_rows:
        existing_by_content.setdefault((f.content or "").strip(), f)

    imported = 0
    skipped = 0
    created: list[Foreshadow] = []
    now = _now_utc()
    for m in rows:
        try:
            mem = json.loads(m.memory_json or "{}")
        except Exception:  # noqa: BLE001
            mem = {}
        foreshadows = mem.get("foreshadows_added") or []
        if not isinstance(foreshadows, list):
            continue
        for fh in foreshadows:
            if not isinstance(fh, dict):
                continue
            content = str(fh.get("content") or "").strip()
            if not content:
                continue
            planned_payoff = str(fh.get("planned_payoff") or "").strip()
            existing = existing_by_content.get(content)
            if existing is not None:
                if replace_duplicates:
                    existing.content = content
                    existing.planned_payoff = planned_payoff or existing.planned_payoff
                    if mark_planted and existing.status not in ("resolved", "abandoned"):
                        existing.status = "planted"
                    existing.updated_at = now
                skipped += 1
                continue
            new_f = Foreshadow(
                project_id=project_id,
                content=content,
                planted_chapter_id=m.chapter_id,
                planned_payoff=planned_payoff or None,
                status="planted" if mark_planted else "planned",
                source="imported",
                note=f"从章节记忆导入（章节：{titles.get(str(m.chapter_id), '')}）",
                created_at=now,
                updated_at=now,
            )
            db.add(new_f)
            existing_by_content[content] = new_f
            created.append(new_f)
            imported += 1

    if imported or (replace_duplicates and skipped):
        await db.commit()
        for f in created:
            await db.refresh(f)
    return {"imported": imported, "skipped_duplicates": skipped, "items": created}
