# -*- coding: utf-8 -*-
"""独立 LLM 连续性审稿报告模型：consistency_review 表（阶段I「独立 LLM 审稿 + 补丁闭环」）。

本表仅存放**只读审稿报告**：LLM 独立连续性审稿（叙事逻辑 / 人物动机 / 人物一致性 /
时间线 / 伏笔呼应 / 设定一致性）的问题清单与统计。

硬约束：
  - 审稿报告不包含正文副本，也绝不改写任何正文（ChapterContent）与事实库；
  - 审稿发现的问题只入人工确认队列（change_suggestions，category=review_issue，
    status=pending），人工确认后才可能被应用；
  - 报告可随时重新生成（覆盖式更新同一行），任务取消/失败时保留状态供重跑。

字段：
  review_id（id）/ project_id / chapter_id / chapter_title / scope / status /
  trigger / model_name / prompt_version / score / issue_count / severity_counts /
  issues（JSON 问题清单）/ suggestion_count（进入建议队列的条数）/ task_id（关联
  ai_tasks）/ raw_response / error_message / note / created_at / completed_at。

回滚方式：新表可删（DROP TABLE IF EXISTS consistency_review 或停用功能开关
llm_consistency_review_enabled），删除后审稿接口不可用，正文与事实库不受影响。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 审稿报告状态（pending/running 为进行中，succeeded 为出报告，cancelled/failed 为中止）
REVIEW_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "cancelled",
    "failed",
)

# 审稿问题类型（LLM 输出归一到该枚举，未知类型归入 narrative_logic）
REVIEW_ISSUE_TYPES = (
    "narrative_logic",
    "motivation",
    "character_consistency",
    "timeline",
    "foreshadow",
    "setting",
    "style",
)

# 审稿问题严重度
REVIEW_SEVERITIES = ("critical", "warning", "suggestion")

# 审稿触发方式
REVIEW_TRIGGERS = ("manual", "auto")


class ConsistencyReview(Base):
    """独立 LLM 连续性审稿报告（只读产物，不含正文，不改正文）。"""

    __tablename__ = "consistency_review"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )

    # project_id / chapter_id 在正式库无库级外键（PRAGMA foreign_key_list 为空），
    # 引用合法性由服务层校验；ORM 不再声明 ForeignKey。
    project_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
    )

    chapter_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 审稿范围：chapter（单章）/ range（章节区间，本阶段仅 chapter 落地）
    scope: Mapped[str] = mapped_column(
        String(20), default="chapter", server_default=text("'chapter'"), nullable=False
    )

    # 正式库 status 无单列索引（仅 project_id / chapter_id / task_id），故此处不建索引。
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default=text("'pending'"), nullable=False
    )

    trigger: Mapped[str] = mapped_column(
        String(20), default="manual", server_default=text("'manual'"), nullable=False
    )

    # 追溯：模型与提示词版本（与 ai_tasks 口径一致）
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 审稿评分（0-100，模型未给出为 NULL）
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    issue_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # 严重度分布 JSON：{"critical": 0, "warning": 1, "suggestion": 3}
    severity_counts: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 问题清单 JSON（severity/type/location/message/suggestion）
    issues: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 进入建议队列（change_suggestions）的条数：报告只入队，不落正文
    suggestion_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # 关联 ai_tasks（异步任务可取消/可恢复的锚点）
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 索引与正式库逐项对齐：ix_consistency_review_project_id / _chapter_id / _task_id。
    __table_args__ = (
        Index("ix_consistency_review_project_id", "project_id"),
        Index("ix_consistency_review_chapter_id", "chapter_id"),
        Index("ix_consistency_review_task_id", "task_id"),
    )
