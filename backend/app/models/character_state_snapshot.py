# 人物状态快照模型：character_state_snapshots 表（第三期「人物和时间不漂」）
# 记录人物在某章节时的状态切片，用于构建「陈九第12章前在北关城、第13章加入边军大营…」
# 这类逐章状态轨迹。
# source 取值：ai_extracted（自动提取后直接写入）| user_confirmed（确认写入）| manual（手工创建）
# status 取值：draft（待确认）| confirmed（已确认）| rejected（已拒绝）
# 第一批「事实数据库与规则校验」扩展：age/birth_year/is_alive/location_id/
# possessions/known_secrets/injuries 结构化列，供 fact_validator 做
# timeline 年龄、state 死亡重现/瞬移、possession 物品归属、secret 提前知情校验。
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CharacterStateSnapshot(Base):
    __tablename__ = "character_state_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    character_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 状态切片 JSON：如 {"current_location": "北关城", "status": "在世",
    #   "current_timeline_status": "加入边军大营", "goals": ["寻父"], ...}
    state_json: Mapped[str] = mapped_column(Text, nullable=False)

    source: Mapped[str] = mapped_column(
        String(20),
        default="ai_extracted",
        nullable=False
    )

    # 确认状态（文26 P2：draft|confirmed|rejected）
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        nullable=False,
        index=True
    )

    # ---------- 第一批「事实数据库与规则校验」新增结构化字段 ----------
    # 年龄（整数；NULL 表示正文未给出）
    age: Mapped[int | None] = mapped_column(Integer)
    # 出生年份（整数，如 1000；NULL 表示未知）
    birth_year: Mapped[int | None] = mapped_column(Integer)
    # 是否在世（True=在世；NULL 表示未提及）
    is_alive: Mapped[bool | None] = mapped_column(Boolean)
    # 本章所在地点（locations 表外键）
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    # 持有物品（JSON 数组字符串，如 ["玄铁剑","地图"]）
    possessions: Mapped[str | None] = mapped_column(Text)
    # 已知秘密（JSON 数组字符串，如 ["陈九身世之谜"]）
    known_secrets: Mapped[str | None] = mapped_column(Text)
    # 伤势/异常状态（JSON 数组字符串，如 ["左臂重伤"]）
    injuries: Mapped[str | None] = mapped_column(Text)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

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

    # ---------- 阶段G「时间与认知系统」新增字段 ----------
    # 事实认知类型：fact（客观事实，默认）| belief（角色相信）| rumor（传闻）|
    #              lie（谎言）| uncertain（叙事不确定）
    # 默认 fact 保持旧行为；belief/rumor 等认知类型启用时 secret.early_knowledge
    # 等规则做细化判定（角色听说≠客观知晓，避免误报）。
    fact_kind: Mapped[str] = mapped_column(
        String(20),
        default="fact",
        server_default=text("'fact'"),
        nullable=False
    )
    # 认知主体（belief/rumor/lie/uncertain 类快照建议填写：该认知属于哪个角色）
    holder_character_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
