# 统一行为事件模型（第二十二批「数据分析、推荐系统和智能运营」规格三/四）
# 事件结构：id / event_name / user_id / anonymous_id / session_id / project_id /
#           object_type / object_id / properties / device_type / app_version /
#           locale / region / occurred_at / received_at
# 幂等：event_id 唯一键，重复上报自动去重（规格二十六：重复事件去除）。
# 隐私：禁止采集正文/AI全文/密码/支付凭证/令牌/敏感个人信息（规格四）；
#       文本分析只允许字数/语言/标签/统计特征（properties 白名单清洗见 analytics_service）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 合法事件名（统一事件名称，规格四：事件名称统一）
EVENT_NAMES = [
    "user.login", "user.logout", "user.register",
    "project.created", "project.opened",
    "chapter.created", "chapter.edited", "chapter.saved", "chapter.published",
    "reading.page_view", "reading.chapter_start", "reading.chapter_complete",
    "favorite.added", "favorite.removed",
    "comment.created",
    "share.created",
    "subscription.upgraded", "subscription.cancelled",
    "ai.task_submitted", "ai.task_completed", "ai.task_failed",
    "recommend.displayed", "recommend.clicked",
    "goal.completed", "goal.updated",
    "search.performed",
]

# 禁止采集的敏感字段关键词（properties 键名/值检测，规格四）
SENSITIVE_KEYS = [
    "password", "passwd", "token", "access_token", "refresh_token",
    "secret", "api_key", "apikey", "authorization", "cookie",
    "payment", "card", "cvv", "pan", "bank", "invoice_no", "tax_no",
    "id_card", "idcard", "ssn", "phone", "mobile", "email", "address",
    "ip", "latitude", "longitude", "location", "device_id", "idfa",
    "content", "body", "text", "prompt", "output", "full_text", "chapter_content",
]

# 文本类事件允许的匿名统计特征键
ALLOWED_TEXT_FEATURE_KEYS = [
    "word_count", "language", "tags", "tag_count", "emotion",
    "read_seconds", "progress", "publish_type", "channel", "source",
    "duration_minutes", "step", "goal_type", "percent", "result", "variant",
    "error_code", "latency_ms", "plan_id", "plan_type", "period", "from_page", "to_page",
]


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_analytics_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    object_type: Mapped[str | None] = mapped_column(String(30))
    object_id: Mapped[str | None] = mapped_column(String(64), index=True)

    properties: Mapped[str | None] = mapped_column(Text)  # JSON 文本
    device_type: Mapped[str | None] = mapped_column(String(30))
    app_version: Mapped[str | None] = mapped_column(String(30))
    locale: Mapped[str | None] = mapped_column(String(20))
    region: Mapped[str | None] = mapped_column(String(20))

    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
