# 故障演练服务（第十八批「性能优化、可靠性与安全加固」规格十六/二十六）
# 备份恢复演练 / 数据库故障演练 / 队列故障演练 / 搜索重建演练 / 权限系统故障演练。
# 只落演练记录与预案文档建议，不写死生产破坏性流程。
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drill_record import DrillRecord

DRILL_TYPES = {
    "backup_restore",
    "database_restore",
    "database_failure",
    "queue_failure",
    "search_rebuild",
    "permission_failure",
}

# 灾难恢复预案（规格十六：RPO / RTO）
DR_POLICY = {
    "rpo_minutes": 15,
    "rto_minutes": 60,
    "note": "RPO 不超过 15 分钟，RTO 不超过 1 小时；建议每周执行一次备份恢复演练。",
}


async def start_drill(
    db: AsyncSession,
    *,
    drill_type: str,
    created_by: uuid.UUID | None,
    notes: str | None = None,
) -> DrillRecord:
    if drill_type not in DRILL_TYPES:
        raise ValueError(f"未知演练类型: {drill_type}")
    row = DrillRecord(
        drill_type=drill_type,
        status="running",
        started_at=datetime.utcnow(),
        created_by=created_by,
        notes=notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def finish_drill(
    db: AsyncSession,
    *,
    record: DrillRecord,
    passed: bool,
    notes: str | None = None,
) -> DrillRecord:
    """完成演练：记录结果（passed/failed）与复盘说明。"""
    if record.status != "running":
        raise ValueError("演练未处于运行中状态")
    record.status = "completed"
    record.result = "passed" if passed else "failed"
    record.completed_at = datetime.utcnow()
    if notes:
        record.notes = f"{record.notes or ''}\n{notes}".strip()
    await db.commit()
    await db.refresh(record)
    return record


async def list_drills(
    db: AsyncSession,
    *,
    drill_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[DrillRecord], int]:
    filters = []
    if drill_type:
        if drill_type not in DRILL_TYPES:
            raise ValueError(f"未知演练类型: {drill_type}")
        filters.append(DrillRecord.drill_type == drill_type)
    if status:
        filters.append(DrillRecord.status == status)
    total = (
        await db.execute(select(func.count(DrillRecord.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(DrillRecord)
            .where(*filters)
            .order_by(DrillRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total
