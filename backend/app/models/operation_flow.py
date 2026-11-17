# 智能运营消息编排模型（第二十二批「数据分析、推荐系统和智能运营」规格十九）
# OperationFlow：消息流程（触发条件/等待时间/消息渠道/发送次数上限/退出条件/用户同意）。
# OperationMessageLog：发送记录（频率控制、退订、重复触发保护）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

FLOW_TRIGGER_TYPES = [
    "user_registered", "no_project_after_register", "project_created",
    "first_chapter_completed", "inactive_days", "segment_matched",
]
CHANNELS = ["in_app", "email", "push"]


class OperationFlow(Base):
    __tablename__ = "operation_flows"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    trigger_params: Mapped[str | None] = mapped_column(Text)  # JSON
    wait_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="in_app", nullable=False)
    template: Mapped[str | None] = mapped_column(String(200))
    max_sends_per_user: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    exit_condition: Mapped[str | None] = mapped_column(Text)  # JSON
    consent_required: Mapped[bool] = mapped_column(default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class OperationMessageLog(Base):
    __tablename__ = "operation_message_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_flows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="sent", nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
