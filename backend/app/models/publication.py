# 发布记录模型：publications 表（第十三批「发布与阅读模式」规格实现）
# 发布记录保存面向读者的内容快照（转换后的阅读正文），不直接依赖编辑中的草稿。
# chapter_id 关联 outlines.id（outline_type=chapter 的章节节点，与 ChapterContent 一致）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 发布状态常量（作品/章节/发布记录通用）
PUBLICATION_STATUSES = [
    "draft",       # 草稿
    "review",      # 审核中
    "published",   # 已发布
    "unlisted",    # 仅通过链接访问
    "archived",    # 已归档
]

# 章节可见性常量
CHAPTER_VISIBILITIES = [
    "private",          # 仅作者可见
    "project_member",   # 项目成员可见
    "link_only",        # 拥有链接的用户可见
    "public",           # 所有人可见
]


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)
    cover_text: Mapped[str | None] = mapped_column(String(255))

    # 发布正文快照：content 为转换后的阅读 HTML；content_text 为纯文本（用于字数/目录）
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 发布记录状态与可见性（与章节/作品状态语义一致）
    status: Mapped[str] = mapped_column(
        String(30),
        default="published",
        nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(30),
        default="private",
        nullable=False
    )

    # ---------- 阶段C「发布基线绑定」（补充 6）新增字段 ----------
    # 发布时自动收集的不可变版本基线（JSON）：facts_version / setting_version /
    # rules_version / prompt_version / model_name / validation_report_id。
    # 可空：旧发布记录不追溯；收集失败降级为 NULL 不阻断发布。
    baseline_meta: Mapped[str | None] = mapped_column(Text)

    # 事实库 + 设定不可变快照（JSON，引用式：仅记录实体 id 列表与捕获时间，
    # 不复制正文/事实正文，避免与事实库演进失联时正文被复制放大）。
    baseline_snapshot: Mapped[str | None] = mapped_column(Text)

    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
