# 时间线事件模型：timeline_events 表（第十六批「数据导入、导出与备份恢复」规格实现）
# 完整项目 JSON 结构中的 timeline_events 数组来源；支持 CSV 表格导入。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    description: Mapped[str | None] = mapped_column(Text)

    # 事件时间：可自由格式文本（如「边境历 1024 年秋」），CSV/JSON 均按字符串导入
    event_time: Mapped[str | None] = mapped_column(String(255))

    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )

    importance: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False
    )

    # ---------- 阶段A「事实权威与锁定」新增字段 ----------
    # 权威等级：low（低，模型候选/推测）| normal（默认，自动提取）| high（高，人工确认/已批准设定）
    authority_level: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        server_default=text("'normal'"),
        nullable=False
    )
    # 置信度 0-1（NULL=未标注）
    confidence: Mapped[float | None] = mapped_column(Float)
    # 人工锁定（True=事实重算时跳过，不被自动提取覆盖）
    locked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False
    )
    # 来源记录 ID（关联 generation_record / 人工操作记录，可空）
    source_record_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    # ---------- 第三期「时间线事件结构化」新增字段 ----------
    # 来源章节（可空：手工全局事件可无章节）
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    # 事件日期标签（如「景历二十三年秋」）
    event_date_label: Mapped[str | None] = mapped_column(String(255))
    # 时间标签（文26 P2，兼容 event_date_label 的语义别名）
    time_label: Mapped[str | None] = mapped_column(String(255))
    # 章内排序序号
    sequence_order: Mapped[int | None] = mapped_column(Integer)
    # 事件发生地点（location 表外键，可空）
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True
    )
    # 参与人物（JSON 数组字符串，如 ["陈九","边军大营都尉"]）
    participants: Mapped[str | None] = mapped_column(Text)
    # 来源：manual（手工录入）| ai_extracted（AI 提取后自动确认）
    source: Mapped[str] = mapped_column(
        String(20),
        default="manual",
        nullable=False
    )
    # 是否已确认（AI 提取候选默认 False，确认后 True）
    confirmed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    # 确认状态（文26 P2：draft|confirmed|archived，以 status 为准）
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        nullable=False,
        index=True
    )

    # ---------- 第一批「事实数据库与规则校验」新增结构化字段 ----------
    # 绝对时间（可排序的时间标签，如「景历1024年秋」；NULL 表示未知）
    absolute_time: Mapped[str | None] = mapped_column(String(255))
    # 相对时间（如「第3章后三天」；NULL 表示未知）
    relative_time: Mapped[str | None] = mapped_column(String(255))
    # 参与人物 ID（JSON UUID 数组字符串；与 participants 名称列表并存）
    participant_ids: Mapped[str | None] = mapped_column(Text)
    # 因果前驱事件（cause_event_id 指向更早发生的事件，用于时间先后校验）
    cause_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("timeline_events.id", ondelete="SET NULL"),
        nullable=True
    )
    # 事件结果/影响描述
    result: Mapped[str | None] = mapped_column(Text)
    # 是否公开事件（False=秘密/私密事件，公开校验时可排除）
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("1"),
        nullable=False
    )

    # ---------- 阶段G「时间与认知系统」新增字段 ----------
    # 时间类型：exact（精确）| range（区间）| relative（相对）|
    #          approximate（约略）| unknown（未知，默认兜底）
    # 存量行默认 unknown：不参与结构化日期先后校验，旧行为保留。
    time_type: Mapped[str] = mapped_column(
        String(20),
        default="unknown",
        server_default=text("'unknown'"),
        nullable=False
    )
    # 结构化时间区间起点/终点（文本，如「景历1024年春」；仅 time_type=range 必填）
    time_start: Mapped[str | None] = mapped_column(String(255))
    time_end: Mapped[str | None] = mapped_column(String(255))
    # 叙述时间（三通道之二）：事件在正文中被叙述的时间点/章节（如「第3章」「景历1024年秋」）
    narrated_at: Mapped[str | None] = mapped_column(String(255))
    # 获知时间（三通道之三）：角色何时得知该事件（如「第5章」；NULL=未知）
    known_at: Mapped[str | None] = mapped_column(String(255))
    # 事实认知类型：fact（客观事实，默认）| belief（角色相信）| rumor（传闻）|
    #              lie（谎言）| uncertain（叙事不确定）
    # 默认 fact 保持旧行为；belief/rumor 等认知类型启用时 secret.early_knowledge
    # 等规则做细化判定（角色听说≠客观知晓）。
    fact_kind: Mapped[str] = mapped_column(
        String(20),
        default="fact",
        server_default=text("'fact'"),
        nullable=False
    )
    # 认知主体（belief/rumor/lie/uncertain 类事件建议填写：该认知属于哪个角色）
    holder_character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
