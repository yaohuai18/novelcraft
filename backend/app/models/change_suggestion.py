# 变更建议模型：change_suggestions 表（第三期「变更确认流程」核心）
# 接受正文并生成 chapter_memory 后，AI 状态变更提取产出的「待确认变更建议」。
# category 取值：character（人物状态）| relationship（角色关系）| timeline（时间线事件）| foreshadow（伏笔）
# status 取值：pending（待确认）| confirmed（已确认写入）| modified（用户修改后确认）| ignored（已忽略）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChangeSuggestion(Base):
    __tablename__ = "change_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    category: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True
    )

    # 相关人物（关系建议时为主方人物，可空表示未匹配到人物库）
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True
    )

    # 关系建议时的另一方人物
    target_character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)

    # 完整提取数据 JSON（确认/修改确认时写入正式库）
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
