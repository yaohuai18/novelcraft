# 导出任务模型：export_tasks 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 状态流转：pending -> running -> completed / failed / cancelled
# 新增：result_token（结果一次性取用令牌）、expires_at（结果有效期）、
#       scope（导出范围 whole_project/volume/chapter/characters/locations/world/timeline/tags/comments/version_history）、
#       格式新增 csv。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 导出范围（规格九：整个项目/分卷/章节/人物/地点/世界观/时间线/标签/评论/版本历史）
EXPORT_SCOPES = [
    "whole_project",
    "volume",
    "chapter",
    "characters",
    "locations",
    "world",
    "timeline",
    "tags",
    "comments",
    "version_history",
]

# 导出格式
EXPORT_FORMATS = [
    "text",
    "markdown",
    "html",
    "json",
    "csv",
]

# 导出任务状态
EXPORT_TASK_STATUSES = [
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
]


class ExportTask(Base):
    __tablename__ = "export_tasks"

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

    # 兼容第十批：导出类型（旧值 whole_project/outline/content/knowledge/timeline/relationship）
    export_type: Mapped[str | None] = mapped_column(String(30))

    # 导出范围（规格九）
    scope: Mapped[str] = mapped_column(
        String(30),
        default="whole_project",
        nullable=False
    )

    export_format: Mapped[str] = mapped_column(
        String(30),
        default="markdown",
        nullable=False
    )

    options: Mapped[str | None] = mapped_column(Text)

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

    content: Mapped[str | None] = mapped_column(Text)

    # 结果取用令牌（短期有效、一次性、绑定当前用户）
    result_token: Mapped[str | None] = mapped_column(String(64), index=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
