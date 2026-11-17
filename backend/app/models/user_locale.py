# 用户语言/时区/地区设置模型（第二十一批「国际化、多语言和多区域部署」规格二/八）
# 语言来源优先级：用户手动选择 → 账户设置 → 浏览器 → 设备 → 默认。
# 四类语言独立设置：界面语言 / 内容语言 / 通知语言 / AI 输出语言。
# 时间统一 UTC 存储，展示时按用户时区转换；支持 date_format/time_format 地区化格式。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 语言种类键（manual_languages JSON 键）
LANGUAGE_KINDS = ["ui", "content", "notification", "ai_output"]


class UserLocale(Base):
    """用户语言与时区设置。

    - ui_language / content_language / notification_language / ai_output_language
      四类语言独立设置（如界面英语、通知中文、AI 润色日语）。
    - manual_languages: JSON 文本 {kind: language}，优先级高于账户设置。
    - timezone / locale / date_format / time_format / region 用于展示与通知渲染。
    - browser_language / device_language 为自动探测值，账户未设置时兜底。
    - marketing_enabled: 营销通知开关（交易/安全通知不可关闭）。
    """

    __tablename__ = "user_locales"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True
    )

    ui_language: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )

    content_language: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )

    notification_language: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )

    ai_output_language: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )

    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Asia/Shanghai",
        nullable=False
    )

    locale: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )

    date_format: Mapped[str] = mapped_column(
        String(20),
        default="YYYY-MM-DD",
        nullable=False
    )

    time_format: Mapped[str] = mapped_column(
        String(10),
        default="HH:mm",
        nullable=False
    )

    region: Mapped[str] = mapped_column(
        String(16),
        default="CN",
        nullable=False
    )

    # 用户手动选择的语言（JSON 文本 {kind: language}），优先级最高
    manual_languages: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    browser_language: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True
    )

    device_language: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True
    )

    marketing_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
