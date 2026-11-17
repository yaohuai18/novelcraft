# FTS5 章节正文检索（2026-11-17 批次 5）：零 API 成本的全文检索升级。
# 替代原方案的向量 RAG（竞品 InkOS 实测同样走 SQLite FTS5/BM25 路线；
# 本机单书库无需 embedding 成本）。
# 中文切词：SQLite 自带 tokenizer 不切中文，索引列存字符 bigram（text_utils.bigrams），
# 查询词同样转 bigram 短语。索引表 chapters_fts 由迁移 v10 创建 + 触发器同步，
# 存量回填由 ensure_chapters_fts_index 在启动时幂等完成。
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text_utils import bigrams, term_to_query

logger = logging.getLogger(__name__)


async def fts_search_chapters(
    db: AsyncSession,
    project_id: str,
    terms: list[str],
    limit: int = 3,
) -> list[dict]:
    """按词条检索项目内章节正文（bm25 排序），返回带章题出处的原文片段。

    索引缺失时返回空列表（调用方自行回退到旧关键词检索）。
    """
    clean = [str(t).strip() for t in (terms or []) if str(t).strip()]
    if not clean:
        return []
    match_q = " OR ".join(term_to_query(t) for t in clean)
    sql = text(
        "SELECT f.rowid, f.title, cc.content, bm25(chapters_fts) AS score "
        "FROM chapters_fts f JOIN chapter_contents cc ON cc.rowid = f.rowid "
        "WHERE chapters_fts MATCH :q AND f.project_id = :pid "
        "ORDER BY score LIMIT :lim"
    )
    try:
        rows = (
            await db.execute(sql, {"q": match_q, "pid": str(project_id), "lim": limit})
        ).all()
    except Exception:  # noqa: BLE001 —— 索引缺失/版本差异时静默回退
        logger.warning("fts_search_chapters failed, fallback to legacy search", exc_info=True)
        return []
    out = []
    for _rowid, title, raw, score in rows:
        snippet = _snippet_around(raw or "", clean)
        out.append({"title": (title or "").strip(), "text": snippet, "score": score})
    return out


def _snippet_around(content: str, terms: list[str], window: int = 60) -> str:
    """在原文里定位第一个命中词条，截取前后窗口作为展示片段。"""
    text = content or ""
    pos = -1
    for t in terms:
        pos = text.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return text[: window * 2]
    start = max(0, pos - window // 2)
    end = min(len(text), pos + window + window // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


async def ensure_chapters_fts_index(conn) -> dict:
    """启动时幂等维护：补列 → 回填 content_bigrams → 补齐 FTS 索引行。

    迁移 v10 只建 FTS 表与触发器；content_bigrams 列由本函数按需补建
    （create_all 新库已有该列，老库则 ALTER 补上），存量数据一次性回填。
    失败不影响启动（检索与保存会静默回退）。
    """
    try:
        cols = [
            r[1]
            for r in (
                await conn.execute(text("PRAGMA table_info(chapter_contents)"))
            ).all()
        ]
        if "content_bigrams" not in cols:
            await conn.execute(
                text(
                    "ALTER TABLE chapter_contents ADD COLUMN content_bigrams "
                    "TEXT NOT NULL DEFAULT ''"
                )
            )
        rows = (
            await conn.execute(
                text(
                    "SELECT rowid, content FROM chapter_contents "
                    "WHERE content IS NOT NULL AND content != '' "
                    "AND (content_bigrams IS NULL OR content_bigrams = '')"
                )
            )
        ).all()
        for rowid, content in rows:
            await conn.execute(
                text(
                    "UPDATE chapter_contents SET content_bigrams = :b WHERE rowid = :r"
                ),
                {"b": bigrams(content or ""), "r": rowid},
            )
        ins = await conn.execute(
            text(
                "INSERT INTO chapters_fts(rowid, title, content_bigrams, project_id) "
                "SELECT cc.rowid, COALESCE(o.title, ''), cc.content_bigrams, o.project_id "
                "FROM chapter_contents cc JOIN outlines o ON o.id = cc.outline_id "
                "WHERE COALESCE(cc.content_bigrams, '') != '' "
                "AND cc.rowid NOT IN (SELECT rowid FROM chapters_fts)"
            )
        )
        return {"backfilled": len(rows), "indexed": ins.rowcount}
    except Exception:  # noqa: BLE001
        logger.warning("ensure_chapters_fts_index failed", exc_info=True)
        return {"backfilled": 0, "indexed": 0}
