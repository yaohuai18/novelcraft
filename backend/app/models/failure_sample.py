# 失败样本（2026-11-16 第三档升级）：把「本项目过去犯过的错」变成生成时的固定输入。
# 来源：一致性检查 issue / 人工改稿差异 / 审计修复，防止同类错误反复犯。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FailureSample(Base):
    __tablename__ = "failure_samples"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        index=True, nullable=True
    )  # NULL = 全局样本（所有项目生效）

    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # character_drift / timeline_conflict / foreshadow_leak / naming / style / other

    bad_example: Mapped[str] = mapped_column(Text, nullable=False)  # 错误写法示例

    correct_way: Mapped[str] = mapped_column(Text, nullable=False)  # 正确写法/规则

    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    # manual / consistency_issue / edit_diff / audit

    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
