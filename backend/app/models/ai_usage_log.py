# AI 调用记录模型：ai_usage_logs 表（第八批规格实现）
# 用于统计消耗与排查问题；当前模型无法返回 token 统计时字段全部为 NULL。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIUsageLog(Base):
    __tablename__ = "ai_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    completion_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    total_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    latency_ms: Mapped[int | None] = mapped_column(
        Integer
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
