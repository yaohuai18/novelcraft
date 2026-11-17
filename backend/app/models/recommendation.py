# 推荐系统记录模型（第二十二批「数据分析、推荐系统和智能运营」规格十三/二十三）
# RecommendLog：曝光日志（用于重复曝光惩罚、多样性、监控）。
# RecommendAction：用户行为（点击/完读/隐藏/屏蔽/投诉），用于推荐效果监控。
# BlockedRecommend：用户主动屏蔽的推荐对象。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 推荐对象类型（规格十）
RECOMMEND_TYPES = [
    "project", "chapter", "tag", "template", "task", "ai_tool", "tutorial", "event", "cooperation",
]


class RecommendLog(Base):
    __tablename__ = "recommend_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scene: Mapped[str] = mapped_column(String(30), index=True)  # homepage / reading_end / writing / ai / notification
    recommend_type: Mapped[str] = mapped_column(String(30), nullable=False)
    recommend_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RecommendAction(Base):
    __tablename__ = "recommend_actions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    log_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recommend_logs.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # click / complete / hide / block / report
    recommend_type: Mapped[str | None] = mapped_column(String(30))
    recommend_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class BlockedRecommend(Base):
    __tablename__ = "blocked_recommends"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
