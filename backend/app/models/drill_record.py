# 故障演练记录模型（第十八批「性能优化、可靠性与安全加固」规格十六）
# 备份恢复演练 / 数据库故障演练 / 队列故障演练 / 搜索重建演练 / 权限故障演练
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DrillRecord(Base):
    __tablename__ = "drill_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    drill_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="planned",
        nullable=False
    )

    result: Mapped[str | None] = mapped_column(
        Text
    )

    notes: Mapped[str | None] = mapped_column(
        Text
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
