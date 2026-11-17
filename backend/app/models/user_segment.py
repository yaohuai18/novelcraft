# 用户分群模型（第二十二批「数据分析、推荐系统和智能运营」规格十八）
# 分群规则：rule_type 表示条件语义，rule_params 存 JSON 参数。
# 示例：reg_no_project_7d / active_7d_consecutive / inactive_30d /
#       ai_quota_low / trial_ending_soon / has_unpublished_work / team_admin
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SEGMENT_RULE_TYPES = [
    "reg_no_project_7d",       # 注册七天内未创建项目
    "active_7d_consecutive",   # 连续七天创作
    "inactive_30d",            # 最近三十天未登录
    "ai_quota_low",            # AI 额度即将用完
    "trial_ending_soon",       # 试用期即将结束
    "has_unpublished_work",    # 有作品未发布
    "team_admin",              # 团队管理员
]


class UserSegment(Base):
    __tablename__ = "user_segments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    rule_params: Mapped[str | None] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
