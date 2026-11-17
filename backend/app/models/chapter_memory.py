# 章节记忆模型：chapter_memories 表（第二期「章节能被记住」规格实现）
# 每个章节至多一条记忆记录：短摘要(100-250字) + 结构化记忆 JSON。
# source: ai（AI 自动生成）/ manual（人工编辑）；status: draft（待确认）/ confirmed（已确认）。
# 已确认的 chapter_memory 在续写 Context Builder 中作为「剧情承接摘要」优先引用。
import json
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CHAPTER_MEMORY_SOURCES = ["ai", "manual"]
CHAPTER_MEMORY_STATUSES = ["draft", "confirmed"]

# memory_json 约定结构（示例）：
# {
#   "timeline": {"relative_day": "第 3 天", "location": ["北境要塞"]},
#   "characters_present": ["主角", "向导"],
#   "character_updates": [{"character": "主角", "changes": ["得知真相"]}],
#   "foreshadows_added": [{"content": "神秘铜钥匙", "planned_payoff": "第三卷开启密室"}],
#   "open_threads": ["守卫失踪之谜"],
#   "important_items": ["铜钥匙"],
#   "next_chapter_hint": "下章承接：主角动身前往深渊"
# }


class ChapterMemory(Base):
    __tablename__ = "chapter_memories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlines.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 结构化记忆 JSON 字符串，见模块顶部约定
    memory_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # 文26 P1 规格 generationRecordId：摘要/状态提取对应的生成记录
    generation_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_records.id", ondelete="SET NULL"),
        nullable=True,
    )

    source: Mapped[str] = mapped_column(
        String(10), default="ai", nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(10), default="draft", nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def memory(self) -> dict:
        """供响应 Schema 的 memory 字段映射：将 memory_json 字符串解析为 dict。"""
        try:
            return json.loads(self.memory_json) if self.memory_json else {}
        except Exception:
            return {}

    @property
    def structured_memory(self) -> dict:
        """文26 P1 structuredMemory 视图：新结构字段优先，二期旧结构自动映射兜底。"""
        data = self.memory or {}
        legacy = data.get("timeline") or {}
        if any(k in data for k in (
            "timeLabel", "locations", "charactersPresent", "characterChanges",
            "relationshipChanges", "timelineEvents", "foreshadowsAdded",
            "foreshadowsResolved", "importantItems", "openThreads",
            "nextChapterRequirements",
        )):
            return data
        return {
            "timeLabel": legacy.get("relative_day") or legacy.get("timeLabel"),
            "locations": data.get("locations") or legacy.get("location") or [],
            "charactersPresent": (
                data.get("charactersPresent")
                or data.get("characters_present")
                or []
            ),
            "characterChanges": (
                data.get("characterChanges")
                or [
                    {"character": u.get("character"), "changes": u.get("changes") or []}
                    for u in (data.get("character_updates") or [])
                ]
            ),
            "relationshipChanges": data.get("relationshipChanges") or [],
            "timelineEvents": data.get("timelineEvents") or [],
            "foreshadowsAdded": (
                data.get("foreshadowsAdded")
                or [
                    {"content": f.get("content"), "plannedPayoff": f.get("planned_payoff")}
                    for f in (data.get("foreshadows_added") or [])
                ]
            ),
            "foreshadowsResolved": data.get("foreshadowsResolved") or [],
            "importantItems": data.get("importantItems") or data.get("important_items") or [],
            "openThreads": data.get("openThreads") or data.get("open_threads") or [],
            "nextChapterRequirements": (
                data.get("nextChapterRequirements")
                or ([data["next_chapter_hint"]] if data.get("next_chapter_hint") else [])
            ),
        }
