# 阅读事件模型：reading_events 表（第十七批「统计分析与运营后台」规格八）
# 短期原始事件（按业务需要保留）；长期数据由 ContentDailyStat 聚合。
# event_type: page_view / chapter_start / chapter_complete / favorite /
#             unfavorite / comment / share
# 防虚增：同一 session 短时间重复事件在写入层去重；
# 防污染：机器人事件（metadata.bot=true）单独标记不入公开统计；
# 未公开章节事件通过 project 公开状态与章节可见性校验过滤。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

READING_EVENT_TYPES = [
    "page_view",
    "chapter_start",
    "chapter_complete",
    "favorite",
    "unfavorite",
    "comment",
    "share",
]


class ReadingEvent(Base):
    __tablename__ = "reading_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    anonymous_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True
    )

    event_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    session_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    # 元信息（JSON 字符串）：阅读时长秒数、是否机器人、渠道来源、设备标识哈希等
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)

    # 防重复刷新的去重指纹（session:type:target），唯一约束兜底
    dedup_key: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True
    )
