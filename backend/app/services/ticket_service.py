# 工单服务（第十七批「统计分析与运营后台」规格十三/十四：待处理工单、support 角色处理）
# SupportTicket 模型字段见 models/support_ticket.py：
#   id / user_id / subject / description / category / priority / status / assignee_id /
#   handled_at / resolved_at / created_at / updated_at / note
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support_ticket import SupportTicket

TICKET_STATUSES = ["open", "in_progress", "resolved", "closed"]


class TicketError(ValueError):
    pass


async def create_ticket(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    subject: str,
    description: str,
    category: str = "general",
    priority: str = "normal",
) -> SupportTicket:
    if not subject or not subject.strip():
        raise TicketError("工单标题不能为空")
    ticket = SupportTicket(
        user_id=user_id,
        subject=subject.strip(),
        description=description or "",
        category=category,
        priority=priority,
        status="open",
    )
    db.add(ticket)
    await db.flush()
    return ticket


async def list_tickets(
    db: AsyncSession,
    *,
    status: str | None = None,
    assignee_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    filters = []
    if status:
        if status not in TICKET_STATUSES:
            raise TicketError(f"非法状态: {status}")
        filters.append(SupportTicket.status == status)
    if assignee_id is not None:
        filters.append(SupportTicket.assignee_id == assignee_id)

    total = (
        await db.execute(
            select(func.count(SupportTicket.id)).where(*filters)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(SupportTicket)
            .where(*filters)
            .order_by(SupportTicket.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(t.id),
                "user_id": str(t.user_id),
                "subject": t.subject,
                "category": t.category,
                "priority": t.priority,
                "status": t.status,
                "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                "created_at": t.created_at.isoformat() + "Z",
                "updated_at": t.updated_at.isoformat() + "Z",
                "handled_at": t.handled_at.isoformat() + "Z" if t.handled_at else None,
            }
            for t in rows
        ],
    }


async def handle_ticket(
    db: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: str,
    assignee_id: uuid.UUID | None = None,
    note: str | None = None,
) -> SupportTicket:
    """处理工单（support / operator / super_admin）。"""
    if status not in TICKET_STATUSES:
        raise TicketError(f"非法状态: {status}")
    ticket = (
        await db.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise TicketError("工单不存在")

    ticket.status = status
    ticket.assignee_id = assignee_id or actor_id
    ticket.handled_at = datetime.utcnow()
    if status in ("resolved", "closed"):
        ticket.resolved_at = datetime.utcnow()
    if note:
        ticket.note = note
    ticket.updated_at = datetime.utcnow()
    await db.flush()
    return ticket


async def get_pending_count(db: AsyncSession) -> int:
    """待处理工单数（open + in_progress）。"""
    return (
        await db.execute(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.status.in_(["open", "in_progress"])
            )
        )
    ).scalar_one()
