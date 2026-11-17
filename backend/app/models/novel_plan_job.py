"""创作规划后台任务表（novel_plan_jobs）。

背景：`POST /api/ai/plan-novel` 是同步接口，整本大纲单次输出 2 万~2.8 万 token，
实测耗时 240~322 秒。前端 240 秒硬超时先断开，响应被丢弃，结果只留在内存里，
等于白跑并白烧 token。本表把「整本规划」改成「提交任务 + 轮询取结果」，
关掉页面也不丢结果。

与其他任务系统的边界（重要）：
- **不复用 `ai_tasks`**。`ai_tasks` 属任务中心（状态机、租约、reaper 那套），
  当前零真实流量，且其可靠性改造补丁处于归档状态（未应用、未迁移）。
  本表是独立的、极小的作业表，只服务"整本规划"这一个长请求。
- **不改动任何既有表**：本表纯新增，无外键（避免与 ensure DDL 形成双份结构来源），
  删用户时由调用方保证不残留（实际场景不会删用户）。

状态机（真源：app/services/novel_plan_job_state.py）：
    queued                 已入库，尚未被领取
    running                已由持有有效租约的 worker 领取
    succeeded              结果已持久化并通过最终 CAS 写回
    failed                 确定失败，保留错误信息
    cancel_requested       用户请求取消，worker 在安全点停止
    cancelled              已停止，不会产生结果
    interrupted            执行者消失 / 服务终止 / 租约过期
    （晚到结果不是状态）  任务被中断/取消后执行者才返回的成功结果，用
                           result_review_status=pending_review 表达，
                           原始终态与原因必须保留，系统不得自动应用

注意：`progress` 只是阶段提示（0 排队 / 10 执行中 / 100 结束），**不是**完成比例，
不代表已生成多少内容——规划是单次模型调用，中途没有可度量的产出。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NovelPlanJob(Base):
    __tablename__ = "novel_plan_jobs"
    __table_args__ = (
        Index(
            "ix_novel_plan_jobs_lease", "status", "lease_expires_at"
        ),
        Index("ix_novel_plan_jobs_scope", "user_id", "idempotency_scope"),
        # 幂等：同一用户 + 同一作用域 + 同一 key 只能有一个任务。
        # 部分唯一索引（WHERE idempotency_key IS NOT NULL）保证未传 key 的任务不受限。
        Index("ix_novel_plan_jobs_result_review", "result_review_status"),
        Index(
            "uq_novel_plan_jobs_idem",
            "user_id",
            "idempotency_scope",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)

    # queued / running / succeeded / failed / interrupted
    status: Mapped[str] = mapped_column(
        String(16), default="queued", nullable=False, index=True
    )

    # real / mock（执行后写入，供前端展示"真实模型"还是"演示模式"）
    mode: Mapped[str | None] = mapped_column(String(8))

    # 提交时的原始请求（PlanNovelRequest 的 JSON），重启后可原样重放/审计
    request_json: Mapped[str] = mapped_column(Text, nullable=False)

    # 成功时的结果：{"mode": ..., "plan": {...}, "message": ...}
    result_json: Mapped[str | None] = mapped_column(Text)

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    # 0 排队 / 10 执行中 / 100 结束（阶段提示，非完成比例）
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)

    # 执行者标识与心跳：重启后据此判定遗留任务
    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ---- 第二批：租约 / 领取计数 / 取消 / 晚到结果 ----
    # 租约到期时间。恢复逻辑只处理「租约已过期」的 running，不再无差别中断。
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 被领取的次数（每次 CAS 领取 +1）；max_attempts 默认 1 = 不自动重试。
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 终态时间；与 completed_at 同步写（后者保留给已上线的前端）。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 幂等：同一用户 + 同一作用域 + 同一 key 只产生一个任务。
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    idempotency_scope: Mapped[str | None] = mapped_column(String(64))
    # 晚到结果被保存时的冲突原因（兼容字段）：interrupted / cancelled / cancel_requested
    result_conflict_reason: Mapped[str | None] = mapped_column(String(64))

    # ---- 晚到结果：独立状态轴，绝不改写 status ----
    # none / pending_review / accepted / discarded
    result_review_status: Mapped[str | None] = mapped_column(String(16), index=True)
    # 晚到的成功结果（不写入 result_json，避免被当作正常成功结果消费）
    late_result_json: Mapped[str | None] = mapped_column(Text)
    # 晚到原因：lease_lost / cancelled / service_shutdown / interrupted
    late_result_reason: Mapped[str | None] = mapped_column(String(64))
    late_result_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 人工处置留痕：result_review_status 由 pending_review → accepted/discarded 时写。
    result_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    result_reviewed_by: Mapped[uuid.UUID | None] = mapped_column()
    result_review_note: Mapped[str | None] = mapped_column(String(500))
