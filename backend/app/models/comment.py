# 评论模型（第十五批「通知、评论与协作反馈」规格实现）
# Comment：作品/章节/段落/大纲/知识库资料/版本差异的评论与批注；
#          类型区分公开评论/私人批注/审核意见/协作讨论；状态 open/resolved/reopened/hidden/deleted。
# Mention：评论中的 @用户名 提及，被提及者收到通知。
# CommentRateLimit：评论防滥用（user_id/ip_hash/window_start/count，IP 脱敏哈希）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 评论类型（规格八：可见范围不同）
COMMENT_TYPES = [
    "public_comment",   # 公开评论：作者和读者可见
    "private_comment",  # 私人批注：仅作者本人可见
    "review_comment",   # 审核意见：作者和审核人员可见
    "collaboration",    # 协作讨论：项目成员可见
]

# 评论状态（规格十 / 第二十四批规格七：待处理/处理中/已解决/已忽略/已重新打开）
COMMENT_STATUSES = [
    "open", "resolved", "reopened", "hidden", "deleted",
    "pending", "processing", "ignored",
]

# 段落评论 position_data 键（规格九）
POSITION_KEYS = ["block_id", "start_offset", "end_offset", "selected_text"]


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    target_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    # 关联对象 ID（作品/章节/段落/大纲条目/知识库资料/版本差异）
    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    # ===== 第二十四批「企业级协作」：组织隔离 + 规格七命名（resource_type/resource_id） =====
    # 组织范围：组织间评论数据严格隔离
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    # 规格七字段名（与 target_type/target_id 同步写入，兼容既有查询）
    resource_type: Mapped[str | None] = mapped_column(String(40), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # 父评论 ID（支持回复）
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"),
        index=True
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    comment_type: Mapped[str] = mapped_column(
        String(30),
        default="public_comment",
        nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        nullable=False,
        index=True
    )

    # 段落评论定位信息：{"block_id","start_offset","end_offset","selected_text"} JSON 字符串
    position_data: Mapped[str | None] = mapped_column(Text)

    # ===== 第二十四批「评论和批注」：选中文字批注锚点（不覆盖正文） =====
    range_start: Mapped[int | None] = mapped_column(Integer)
    range_end: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 正文修改后无法自动匹配定位时标记为 orphaned（提示用户重新定位）
    orphaned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    # 软删除（规格七：deleted_at；删除内容不应直接消失于审计）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    comment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    mentioned_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class CommentRateLimit(Base):
    __tablename__ = "comment_rate_limits"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True
    )

    # IP 脱敏哈希（sha256），不保存原始 IP
    ip_hash: Mapped[str | None] = mapped_column(
        String(64),
        index=True
    )

    window_start: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True
    )

    count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
