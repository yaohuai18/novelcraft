# 内容统一版本模型：content_versions 表（第十二批规格实现）
# 记录大纲 / 章节正文 / 知识库资料的修改历史，支持查看差异、恢复旧版本、比较 AI 生成结果。
#
# target_type：outline / chapter / knowledge_item
# change_type：create / update / restore / ai_generate / import
# source：manual / ai / import / restore
# status：draft（自动保存草稿）/ stable（正式版本）/ published（已发布）/ archived（已归档）
# locked：目标内容锁定标志（由最新版本承载；锁定后禁止普通 AI 覆盖，修改必须创建新版本）
# AI 候选字段：generation_id（关联 AI 任务）/ parent_version_id / candidate_content /
#              accepted_at / rejected_at
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 目标类型（初期优先实现范围）
TARGET_TYPES = [
    "outline",
    "chapter",
    "knowledge_item",
]

# 变更类型
CHANGE_TYPES = [
    "create",
    "update",
    "restore",
    "ai_generate",
    "import",
]

# 来源
SOURCES = [
    "manual",
    "ai",
    "import",
    "restore",
]

# 版本状态
VERSION_STATUSES = [
    "draft",
    "stable",
    "published",
    "archived",
]


class ContentVersion(Base):
    __tablename__ = "content_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True
    )

    version_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True
    )

    title_snapshot: Mapped[str | None] = mapped_column(
        String(255)
    )

    # 快照统一存 JSON 字符串（content_snapshot 为当时的完整内容）
    content_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    metadata_snapshot: Mapped[str | None] = mapped_column(
        Text
    )

    change_type: Mapped[str] = mapped_column(
        String(30),
        default="update",
        nullable=False
    )

    change_summary: Mapped[str | None] = mapped_column(
        String(500)
    )

    source: Mapped[str] = mapped_column(
        String(30),
        default="manual",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="stable",
        nullable=False,
        index=True
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # ===== AI 候选版本字段（第十批规格十） =====
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    candidate_content: Mapped[str | None] = mapped_column(
        Text
    )

    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime
    )

    # ===== 锁定标志（最新版本承载目标内容锁定状态） =====
    locked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    # ===== 第二十四批「版本管理」：标记重要版本 =====
    is_important: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
