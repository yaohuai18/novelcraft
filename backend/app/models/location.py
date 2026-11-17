# 地点模型：locations 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 完整项目 JSON 结构中的 locations 数组来源；支持 CSV 表格导入。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    category: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False
    )

    # ---------- 第一批「事实数据库与规则校验」新增结构化字段 ----------
    # 上级地点（如「边军大营」→「北境」）
    parent_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True
    )
    # 从父地点到此地点的行程耗时（小时；NULL 表示未知）
    travel_hours_from: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
