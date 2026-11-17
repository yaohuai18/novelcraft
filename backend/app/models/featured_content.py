# 推荐位模型：featured_contents 表（第十七批「统计分析与运营后台」规格十五）
# 首页推荐 / 分类推荐 / 专题活动 / 编辑精选 / 新作推荐。
# 推荐内容必须检查：是否公开、是否被下架、是否仍然存在、作者是否允许展示、活动时间是否有效。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

FEATURED_POSITIONS = [
    "homepage",       # 首页推荐
    "category",       # 分类推荐
    "topic",          # 专题活动
    "editor_pick",    # 编辑精选
    "new_release",    # 新作推荐
]

FEATURED_STATUSES = ["active", "inactive", "expired"]


class FeaturedContent(Base):
    __tablename__ = "featured_contents"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    target_type: Mapped[str] = mapped_column(
        String(30),
        default="project",
        nullable=False,
        index=True
    )

    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True
    )

    position: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True
    )

    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    start_at: Mapped[datetime | None] = mapped_column(DateTime)

    end_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
        index=True
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
