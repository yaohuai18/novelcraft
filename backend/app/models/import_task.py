# 导入任务模型：import_tasks 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 状态流转：pending -> parsing -> preview -> importing -> completed / failed / cancelled
# 新增：source_type（markdown/text/json/csv）、progress（0-100）、options（导入选项 JSON）、
#       started_at；project_id 允许为空（导入为新项目时可先无归属项目）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 支持导入的文件类型
IMPORT_FILE_TYPES = [
    "markdown",
    "text",
    "html",
    "json",
    "csv",
]

# 导入任务状态（与规格一致）
IMPORT_TASK_STATUSES = [
    "pending",
    "parsing",
    "preview",
    "importing",
    "completed",
    "failed",
    "cancelled",
]


class ImportTask(Base):
    __tablename__ = "import_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 导入目标项目：为空表示导入将创建新项目
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True
    )

    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    file_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    # 内容来源类型（与 file_type 对齐，额外支持 csv）
    source_type: Mapped[str | None] = mapped_column(String(30))

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        nullable=False
    )

    progress: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    total_items: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    processed_items: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 导入选项（JSON：作品名/拆分方式/覆盖/重复策略/是否导入标题/标签/保留格式等）
    options: Mapped[str | None] = mapped_column(Text)

    error_message: Mapped[str | None] = mapped_column(Text)

    preview_data: Mapped[str | None] = mapped_column(Text)

    report_data: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
