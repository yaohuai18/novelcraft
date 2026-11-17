# 项目级设置模型：project_settings 表（第二期「章节能被记住」规格实现）
# 本期生效：auto_summarize（接受正文后自动生成章节摘要的开关）。
# character_confirm / foreshadow_confirm / timeline_confirm 为确认策略
# （require=需确认 / auto=自动确认），随第三期「人物库/时间线」功能生效。
# auto_check：生成正文后自动一致性检查开关（第四期「写错能发现」，默认开）。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONFIRM_POLICIES = ["require", "auto"]


class ProjectSetting(Base):
    __tablename__ = "project_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    auto_summarize: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # 第四期「写错能发现」：生成正文后自动一致性检查开关，默认开
    auto_check: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    character_confirm: Mapped[str] = mapped_column(
        String(10), default="require", nullable=False
    )
    foreshadow_confirm: Mapped[str] = mapped_column(
        String(10), default="require", nullable=False
    )
    timeline_confirm: Mapped[str] = mapped_column(
        String(10), default="require", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
