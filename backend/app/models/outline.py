# 大纲模型：outlines 表（第四批规格实现）
# 分层结构：summary（故事梗概）→ volume（分卷）→ chapter（章节）→ scene（场景）
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 建议大纲类型常量（与规格一致）
OUTLINE_TYPES = [
    "summary",  # 故事梗概
    "volume",   # 分卷
    "chapter",  # 章节
    "scene",    # 场景
]

# 大纲层级父级类型约束（第六批规格实现）
# summary 无父 / volume 父为 summary / chapter 父为 volume / scene 父为 chapter
ALLOWED_PARENT_TYPES = {
    "summary": {None},
    "volume": {"summary"},
    "chapter": {"volume"},
    "scene": {"chapter"},
}


class Outline(Base):
    __tablename__ = "outlines"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    outline_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )

    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)

    # 章节计划（ChapterPlan，仅 outline_type=chapter 使用）：
    # JSON 文本，结构见 schemas/chapter_plan.py ChapterPlanData；
    # 为 NULL 时回退兼容旧数据（title/summary/content 即 legacy 计划）。
    chapter_plan: Mapped[str | None] = mapped_column(Text)

    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    # 第十三批「发布与阅读模式」：章节可见性与公开链接
    # 仅 outline_type=chapter 使用；volume 的 status 表示分卷是否已发布
    visibility: Mapped[str] = mapped_column(
        String(30),
        default="private",
        nullable=False
    )
    chapter_slug: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    # ---------- 第一批「事实数据库与规则校验」新增结构化字段（仅 chapter 使用） ----------
    # 章节时间标签（如「景历1024年秋」；NULL=未标注）
    chapter_time: Mapped[str | None] = mapped_column(String(255))
    # 章节主要发生地点（locations 表外键）
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    # 本章出场人物（JSON 数组字符串，如 ["陈九","边军大营都尉"]）
    present_characters: Mapped[str | None] = mapped_column(Text)
    # 本章目标（计划层面）
    chapter_goal: Mapped[str | None] = mapped_column(Text)
    # 本章结局/落点（事实层面）
    chapter_outcome: Mapped[str | None] = mapped_column(Text)
    # 最近一次事实/元数据落库时间（NULL=从未提取）
    state_facts_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ---------- 第一阶段「生命周期与可追溯元数据」新增字段（仅 chapter 使用） ----------
    # 章节生命周期：draft → generated → validation_failed / pending_review → approved → published。
    # 生成失败/截断不写入 lifecycle_status，独立记录在 generation_records（incomplete/failed）。
    lifecycle_status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    # 生成元数据（JSON 文本：模型/参数/生成时间/生成状态/generation_record_id 等）
    generation_meta: Mapped[str | None] = mapped_column(Text)
    # 校验报告（JSON 文本：fact_validator 问题清单 + needs_recheck 标记等）
    validation_report: Mapped[str | None] = mapped_column(Text)
    # 修复记录（JSON 文本；第一阶段只建字段，不实现自动修复）
    repair_records: Mapped[str | None] = mapped_column(Text)
    # 生成所依据的已确认大纲版本（outline_plan_versions.id）
    outline_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outline_plan_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 故事事实版本号：每次事实重算成功 +1（默认 0）
    facts_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
