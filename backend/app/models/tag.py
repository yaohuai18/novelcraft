# 标签模型：tags / outline_tags / tag_relations 表（第九批 + 第十四批「搜索、标签与发现系统」规格实现）
# 第十四批增强：统一标签模型 Tag（user_id/slug/color/description/usage_count/category/updated_at）、
# 通用关系表 TagRelation（target_type 支持 作品/分卷/章节/人物/地点/势力/时间线事件/世界观规则）。
# OutlineTag 为第九批大纲关联表，保留兼容（关联计数计入 usage_count）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 建议支持的标签类型（第九批规格，兼容保留）
TAG_TYPES = [
    "人物",
    "地点",
    "势力",
    "道具",
    "世界观",
    "伏笔",
    "情节线",
    "重要章节",
]

# 标签分类（第十四批规格第九节）
TAG_CATEGORIES = [
    "题材",
    "风格",
    "角色",
    "地点",
    "剧情",
    "状态",
    "时间",
    "自定义",
]

# 通用关系表允许的目标类型（第十四批规格第八节）
TAG_TARGET_TYPES = [
    "project",        # 作品
    "volume",         # 分卷
    "chapter",        # 章节
    "character",      # 人物
    "location",       # 地点（当前无独立模型，允许登记）
    "faction",        # 势力（当前无独立模型，允许登记）
    "timeline_event", # 时间线事件（当前无独立模型，允许登记）
    "worldview_rule", # 世界观规则
]


class Tag(Base):
    __tablename__ = "tags"

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

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    slug: Mapped[str | None] = mapped_column(
        String(120),
        index=True
    )

    color: Mapped[str] = mapped_column(
        String(30),
        default="#4F6EF7",
        nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text)

    # 标签分类：题材/风格/角色/地点/剧情/状态/时间/自定义
    category: Mapped[str] = mapped_column(
        String(30),
        default="自定义",
        nullable=False
    )

    # 标签类型（第九批字段，兼容保留）
    tag_type: Mapped[str | None] = mapped_column(String(30))

    # 使用次数：TagRelation + OutlineTag 数量之和，服务端维护
    usage_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
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


class OutlineTag(Base):
    """大纲条目与标签的关联表（第九批，保留兼容）。"""
    __tablename__ = "outline_tags"

    outline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        primary_key=True
    )

    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
        index=True
    )


class TagRelation(Base):
    """通用标签关系表（第十四批规格实现）。

    target_type 支持：project/volume/chapter/character/location/faction/timeline_event/worldview_rule。
    target_id 为业务目标主键（UUID）；location/faction/timeline_event 暂无独立模型，允许登记。
    """
    __tablename__ = "tag_relations"
    __table_args__ = (
        UniqueConstraint(
            "tag_id",
            "target_type",
            "target_id",
            name="uq_tag_relation_tag_type_target"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
