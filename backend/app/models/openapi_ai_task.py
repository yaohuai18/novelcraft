# 开放 API 异步 AI 任务模型（第十九批「移动端适配与开放 API」规格十七）
# POST /api/v1/ai/tasks 异步提交，GET 查询，POST cancel 取消。
# 限制：输入长度 / 输出长度 / 并发数量 / 每日额度 / 项目权限。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

OPENAPI_AI_TASK_STATUSES = [
    "pending",     # 排队中
    "running",     # 执行中
    "completed",   # 已完成
    "failed",      # 失败
    "cancelled",   # 已取消
]


class OpenAPIAITask(Base):
    __tablename__ = "openapi_ai_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        index=True,
        nullable=False
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )

    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        index=True,
        nullable=True
    )

    task_type: Mapped[str] = mapped_column(String(50), nullable=False)

    model: Mapped[str] = mapped_column(String(100), default="default", nullable=False)

    input_text: Mapped[str] = mapped_column(Text, nullable=False)

    # JSON 字符串：{"temperature": 0.7, "output_length": 1000, ...}
    options: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False
    )

    result: Mapped[str | None] = mapped_column(Text)

    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
