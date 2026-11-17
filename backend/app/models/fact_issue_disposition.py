# 人工审稿问题处置模型（阶段B「人工处置与误报闭环」补充5）
# fact_issue_dispositions：人工对校验问题（FactIssue）的处置状态机。
# disposition 枚举：
#   confirmed          —— 确认为真实问题
#   false_positive     —— 误报（规则/数据判定错误），计入误报反馈闭环
#   accepted_exception —— 接受例外（豁免：后续回归校验中该问题不报错，仍记录为已豁免）
#   fixed              —— 已修复（人工/补丁修复后标记）
#   deferred           —— 暂缓处理
#   wont_fix           —— 决定不修复
# accepted_exception 需携带触发条件（entity+rule+范围，至少一项），写入白名单后
# run_fact_validation 在豁免开关开启时对命中 issue 标记 exempted（不计入 blocking）。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

DISPOSITION_TYPES = (
    "confirmed",
    "false_positive",
    "accepted_exception",
    "fixed",
    "deferred",
    "wont_fix",
)


class FactIssueDisposition(Base):
    """人工审稿问题处置记录（每项目每问题指纹一条，写接口幂等更新）。"""

    __tablename__ = "fact_issue_dispositions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # 索引统一在 __table_args__ 声明，与正式库既有索引名保持逐字一致。
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 问题指纹：同项目下同一规则对同一实体同一条消息的确定性问题共用
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # accepted_exception 触发条件（entity+rule+范围）；其他处置可为空
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    rule: Mapped[str | None] = mapped_column(String(50))
    set_by: Mapped[str | None] = mapped_column(String(36))
    set_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "issue_key",
            name="uq_fact_issue_disposition_project_issue",
        ),
        # 正式库仅这两个单列索引（无 project_id / set_by 索引），不得新增多余索引。
        Index("ix_fact_issue_dispositions_disposition", "disposition"),
        Index("ix_fact_issue_dispositions_issue_key", "issue_key"),
    )
