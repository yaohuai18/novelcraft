# AI 生成记录模型：generation_records 表（第一期「生成不丢失」规格实现）
# 记录一次 AI 调用本身：使用了什么上下文、提示词、模型、参数，生成了什么结果。
# action_type 枚举：outline / draft / continue / rewrite / polish / summarize / extract_state / check / plan_check
# status 枚举：
#   通用（规划/大纲类）：pending / succeeded / failed / cancelled
#   正文生成链路（draft/continue/rewrite/expand/polish）：
#     completed       —— 生成完成且无风险信号
#     over_target     —— 正文超长（> target*1.2）且不再静默截断，保留完整原文
#     incomplete      —— 触达 max_tokens（finish_reason=length）或流未正常结束
#     postprocess_failed —— 后处理（标题提取/字数评估）异常，raw_content 仍保留
#     failed          —— 响应为空或低于最小阈值
# needs_review 为独立风险标记（存 review_flags），不改变 status。
#
# 第一批数据完整性（v6）：
#   attribution_status —— 归属语义，project_bound / pre_project / unattributed；
#     project_bound 必须有 project_id；pre_project 仅用于作品创建前的规划类调用；
#     unattributed 只允许出现在历史或异常数据上，不作为普通写入路径的默认值。
#   consumed_at / consumed_chapter_id / consumed_version_id —— 消费标记；
#     一条生成结果默认只能被接受为正式正文一次（条件更新 consumed_at IS NULL）。
# chapter_id / volume_id / scene_id 关联 outlines 表（volume/chapter/scene 大纲节点），可为空。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 动作类型（第一期规格）
ACTION_TYPES = [
    "outline",
    "draft",
    "continue",
    "rewrite",
    "polish",
    "summarize",
    "extract_state",
    "check",
    "plan_check",
]

# 生成记录归属状态（v6）
ATTRIBUTION_STATUSES = [
    "project_bound",
    "pre_project",
    "unattributed",
]

# 生成状态
GENERATION_STATUSES = [
    "pending",
    "succeeded",
    "failed",
    "cancelled",
    "completed",
    "over_target",
    "incomplete",
    "postprocess_failed",
]


class GenerationRecord(Base):
    __tablename__ = "generation_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    volume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    scene_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # 「已确认大纲版本化」追溯字段：生成正文所依据的已确认大纲版本与章节计划
    outline_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outline_plan_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    chapter_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        "chapter_plan_id",
        String(64),
        nullable=True,
        index=True,
    )

    # 生成时所依据的已确认 ChapterPlan 快照（JSON）
    chapter_plan_snapshot: Mapped[str | None] = mapped_column(
        Text
    )

    # 生成时所依据的已确认章节标题（与 outlines.title 可能不一致时的判定基准）
    title_snapshot: Mapped[str | None] = mapped_column(
        Text
    )

    action_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True
    )

    model_provider: Mapped[str | None] = mapped_column(
        String(50)
    )

    model_name: Mapped[str | None] = mapped_column(
        String(100)
    )

    temperature: Mapped[float | None] = mapped_column(
        Float
    )

    max_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    system_prompt: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    user_prompt: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 本次实际注入的设定、摘要、人物状态等 JSON 字符串
    context_snapshot: Mapped[str | None] = mapped_column(
        Text
    )

    response_content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # ===== 第三批「生成链路数据一致性/可追溯性」字段 =====
    # 模型完整原始响应（只追加不改写，后处理失败时仍完整保留）
    raw_content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 清洗后结果（标题提取/规范化后的正文中间态）
    processed_content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 最终章节正文（落库/返回给前端的正文）
    final_content: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False
    )

    # 后处理操作记录（JSON 文本，如 ["normalize_title","keep_full_text"]）
    postprocess_actions: Mapped[str | None] = mapped_column(
        Text
    )

    # ---------- v6 第一批数据完整性字段 ----------
    # 归属状态：project_bound / pre_project / unattributed（见 ATTRIBUTION_STATUSES）
    attribution_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        index=True,
    )

    # 消费标记：被接受为正式正文的时间与目标章节版本（条件更新写入）
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    consumed_chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )
    consumed_version_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    # 模型返回的 finish_reason（stop / length 等）
    finish_reason: Mapped[str | None] = mapped_column(
        String(30)
    )

    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    completion_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    raw_content_length: Mapped[int | None] = mapped_column(
        Integer
    )

    final_content_length: Mapped[int | None] = mapped_column(
        Integer
    )

    # 是否发生过正文截断（本批次关闭静默截断后，正常链路恒为 0；仅显式截断时置 1）
    was_truncated: Mapped[int | None] = mapped_column(
        Integer,
        default=0
    )

    # 截断原因（如 truncated_by_max_tokens / manual / none）
    truncation_reason: Mapped[str | None] = mapped_column(
        Text
    )

    # 完成性风险标记（JSON 数组文本，如 ["unclosed_quote","non_sentence_end","below_minimum"]）
    review_flags: Mapped[str | None] = mapped_column(
        Text
    )

    input_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    output_tokens: Mapped[int | None] = mapped_column(
        Integer
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True
    )

    error_message: Mapped[str | None] = mapped_column(
        Text
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
