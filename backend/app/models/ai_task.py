# AI 任务模型：ai_tasks 表（第八批规格实现 + P0 任务系统补齐）
# 任务状态：pending / running / succeeded / failed / cancelled / timeout
# 任务类型：generate_summary / generate_volumes / generate_chapters / continue / rewrite / polish
# P0 补齐字段：model_name / prompt_version / idempotency_key / request_snapshot /
# input_tokens / output_tokens / timeout_at（迁移 v2）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AITask(Base):
    __tablename__ = "ai_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    outline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    result: Mapped[str | None] = mapped_column(
        Text
    )

    error_message: Mapped[str | None] = mapped_column(
        Text
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # ---- 阶段D「异步任务增强」：自动重试 / 成本预算 / 取消恢复 ----
    # max_retries：自动重试上限（0 = 不自动重试，默认关闭；仅幂等任务生效）。
    # cost_budget：成本预算上限（累计 input+output token 数，NULL = 不检查，默认关闭）。
    # resume_token：取消后恢复的进度快照（JSON 文本，NULL = 不可恢复，默认关闭）。
    max_retries: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    cost_budget: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True
    )

    resume_token: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    # ---- P0 补齐：模型 / 提示词版本 / 幂等键 / 参数快照 / token 用量 / 超时 ----
    model_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    prompt_version: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True
    )

    request_snapshot: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True
    )

    output_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True
    )

    timeout_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )
