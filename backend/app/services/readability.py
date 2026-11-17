# 追读力分析（2026-11-17 批次 4）：规则版、零 AI 成本、只读。
# 三个指标：钩子覆盖率（章末悬念）、爽点密度、伏笔债务（未回收清单）。
# 对标 Webnovel Writer 的追读力系统，但先做生成侧可用的轻量报告。
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.foreshadow import Foreshadow
from app.models.outline import Outline
from app.models.project import Project

_HOOK_TAIL_LEN = 80
_SENTENCE_END = "。！？”’」』…"

# 爽点信号词（打脸/反转/收获/震慑类）
_THRILL_WORDS = [
    "反转", "打脸", "震撼", "震惊", "没想到", "竟", "原来如此", "翻身",
    "扬眉吐气", "大快人心", "满座皆惊", "心服口服", "刮目相看",
    "恍然大悟", "不寒而栗", "倒吸一口凉气", "鸦雀无声",
]


def has_hook_ending(content: str) -> bool:
    """章末是否留了钩子：悬念问句 / 省略号 / 破折号 / 未收束的句子。"""
    tail = (content or "").rstrip()[-_HOOK_TAIL_LEN:]
    if not tail.strip():
        return False
    if "？" in tail or "…" in tail:
        return True
    if tail.rstrip().endswith("——"):
        return True
    # 结尾不是句末标点 → 句子未收束，视为钩子
    if tail[-1] not in _SENTENCE_END:
        return True
    return False


def thrill_hits(content: str) -> int:
    """爽点信号词命中次数。"""
    text = content or ""
    return sum(text.count(w) for w in _THRILL_WORDS)


async def compute_readability_report(
    db: AsyncSession, project_id: uuid.UUID
) -> dict:
    """对项目全部已写章节出追读力摘要（只读）。"""
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    outlines = (
        await db.execute(
            select(Outline)
            .where(Outline.project_id == project_id, Outline.outline_type == "chapter")
            .order_by(Outline.sort_order)
        )
    ).scalars().all()

    # 正文在 chapter_contents 表（outlines.content 通常为空），逐章取
    content_rows = (
        await db.execute(
            select(ChapterContent.outline_id, ChapterContent.content).where(
                ChapterContent.outline_id.in_([o.id for o in outlines])
            )
        )
    ).all()
    content_map = {str(oid): (text or "") for oid, text in content_rows}

    chapters = []
    for o in outlines:
        content = content_map.get(str(o.id), o.content or "")
        chapters.append(
            {
                "id": str(o.id),
                "sort_order": o.sort_order or 0,
                "title": o.title or f"第{o.sort_order or '?'}章",
                "content": content,
            }
        )

    hook_chapters: list[str] = []
    thrill_by_chapter: list[dict] = []
    total_hits = 0
    total_chars = 0
    for ch in chapters:
        text = ch["content"]
        hits = thrill_hits(text)
        total_hits += hits
        chars = len((text or "").replace(" ", "").replace("\n", ""))
        total_chars += chars
        if has_hook_ending(text):
            hook_chapters.append(ch["title"])
        thrill_by_chapter.append(
            {"title": ch["title"], "hits": hits, "chars": chars}
        )

    foreshadows = (
        await db.execute(
            select(Foreshadow).where(
                Foreshadow.project_id == project_id,
                Foreshadow.resolved_chapter_id.is_(None),
            )
        )
    ).scalars().all()
    debt_items = [
        {
            "title": f.title or "未命名伏笔",
            "status": f.status or "planned",
            "importance": f.importance or "medium",
            "planned_payoff": f.planned_payoff or "",
        }
        for f in foreshadows
    ]

    total = len(chapters)
    with_hook = len(hook_chapters)
    return {
        "project_id": str(project_id),
        "project_title": project.title,
        "chapters_total": total,
        "hook_coverage": {
            "chapters_with_hook": with_hook,
            "rate": round(with_hook / total, 3) if total else 0,
            "without_hook": [c["title"] for c in chapters if c["title"] not in hook_chapters][:20],
        },
        "thrill_density": {
            "total_hits": total_hits,
            "per_1000_chars": round(total_hits * 1000 / total_chars, 2) if total_chars else 0,
            "top_chapters": sorted(thrill_by_chapter, key=lambda x: -x["hits"])[:5],
        },
        "foreshadow_debt": {
            "open_count": len(debt_items),
            "items": debt_items[:30],
        },
        "checked_at": datetime.utcnow().isoformat(),
    }
