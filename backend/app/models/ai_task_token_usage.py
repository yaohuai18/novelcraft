# B-2「逐次 token 账目表」ORM 模型（阶段 B-2 cost_budget）
#
# 语义：每一次对 LLM 的调用一行，按 (task_id, attempt_no, call_no) 唯一。
#   - attempt_no：执行轮次（1-based）。确认口径 = task.retry_count + 1；
#     resume 沿用账簿中已存在的最大 attempt_no（同一 attempt 内继续追加 call）。
#   - call_no：同一 attempt 内单调递增的调用序号（每次实际下发 +1）。
#
# 结构单一来源：主 DDL 为 app/core/db_migrations.py 的 _PHASE22_USAGE_TABLE_DDL
# （迁移通道 v3 / ensure_phase22_schema）；本 ORM 模型是 create_all 通道（新库）。
# 两条通道的列名/类型/非空/默认/唯一约束必须逐项一致，由
# tests/test_phase1_token_usage_ledger.py 做往返一致性校验（禁止漂移）。
#
# ai_tasks 表零变更：本表不通过外键挂接 ai_tasks（B-2 红线：不改 ai_tasks 结构），
# 归属校验由服务层按 task_id 显式比对完成。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

USAGE_LEDGER_TABLE_NAME = "ai_task_token_usage"
USAGE_LEDGER_UNIQUE_CONSTRAINT = "uq_usage_task_attempt_call"


class AITaskTokenUsage(Base):
    """单次 LLM 调用的 token 账目（B-2 逐次账目表的 ORM 视图）。"""

    __tablename__ = USAGE_LEDGER_TABLE_NAME
    __table_args__ = (
        UniqueConstraint(
            "task_id", "attempt_no", "call_no",
            name=USAGE_LEDGER_UNIQUE_CONSTRAINT,
        ),
        # 两个业务索引与迁移通道同名同列（B-5 裁定）：
        #   idx_usage_status_settled (usage_status, settled_at)
        #   idx_usage_task_dispatched (task_id, dispatched_at)
        Index("idx_usage_status_settled", "usage_status", "settled_at"),
        Index("idx_usage_task_dispatched", "task_id", "dispatched_at"),
        # 正式库建表语句带 AUTOINCREMENT（sqlite_sequence 留痕，id 不复用），
        # create_all 通道靠此表级选项产出等价 DDL；缺省则为普通 rowid 表，与正式库不一致。
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # task_id 存储形态与 ai_tasks.id 一致：SQLAlchemy Uuid 在 SQLite 上落 CHAR(32)。
    task_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    attempt_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    call_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # usage_status / usage_source 取值收敛为 db_migrations 中的允许枚举（各 5 个值）。
    usage_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending_dispatch",
        server_default=text("'pending_dispatch'"),
    )

    usage_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="not_dispatched",
        server_default=text("'not_dispatched'"),
    )

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 未知一律留 NULL：禁止把「供应商未返回」写成 0（0 等价于「零消耗」，会静默放宽预算）。
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 派发时间：**先落库、后发请求**（崩溃可见性）：只要本列非 NULL，
    # 即代表「已向供应商发出过请求」，其费用口径必须可结清或显式标为不确定。
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
