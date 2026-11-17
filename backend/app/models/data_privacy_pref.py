# 数据隐私偏好模型（第二十二批「数据分析、推荐系统和智能运营」规格二十一）
# 用户可独立关闭：个性化推荐 / 行为分析 / 营销通知 / 数据共享 / AI 数据使用。
# 关闭行为分析后仍保留必要的安全、计费和审计记录（由 privacy_control_service 保证）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DataPrivacyPref(Base):
    __tablename__ = "data_privacy_prefs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # 个性化推荐
    personal_recommendation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 行为分析
    behavior_analytics: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 营销通知
    marketing_notification: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 数据共享（第三方/跨服务）
    data_sharing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # AI 数据使用（用行为数据训练/优化模型）
    ai_data_usage: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
