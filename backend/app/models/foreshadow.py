# 伏笔管理模型：foreshadows 表（第四期「写错能发现」伏笔追踪核心）
# 伏笔可从 chapter_memory.foreshadows_added 与规划 NovelPlan.foreshadowing 同步导入。
# status 取值：planned（已登记未埋设）| planted（已埋设）| resolved（已回收）| abandoned（已弃用）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

FORESHADOW_STATUSES = ["planned", "planted", "resolved", "abandoned"]


class Foreshadow(Base):
    __tablename__ = "foreshadows"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # 伏笔标题（文26 P2）
    title: Mapped[str | None] = mapped_column(String(255))

    content: Mapped[str] = mapped_column(Text, nullable=False)  # 伏笔内容

    # 重要度（文26 P2：high|medium|low）
    importance: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", index=True
    )

    # 回收章节（文26 P2，resolved 时填写）
    resolved_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # 埋设章节（可能为空：规划阶段登记的伏笔尚未埋设）
    planted_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # 计划回收章节（可能为空：尚未确定）
    planned_payoff_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # 计划回收方式说明（来自规划 foreshadowing 的 planned_payoff 或用户填写）
    planned_payoff: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="planned", index=True
    )

    # 来源：ai_extracted（AI 从正文提取）| ai_check（AI 检查发现）| manual（手工登记）| imported（从规划导入）
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")

    note: Mapped[str | None] = mapped_column(Text)  # 备注

    paid_off_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"), nullable=True
    )
    paid_off_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
