# 批次 5（2026-11-17）：chapters_fts（FTS5 + 字符 bigram）建表、触发器同步与检索测试。
# 使用独立临时库，不依赖应用引擎。
import sqlite3

from app.core.text_utils import bigrams, term_to_query

_SCHEMA_SQL = [
    "CREATE TABLE outlines (id TEXT PRIMARY KEY, project_id TEXT, title TEXT)",
    "CREATE TABLE chapter_contents ("
    "id TEXT PRIMARY KEY, outline_id TEXT, content TEXT, content_bigrams TEXT NOT NULL DEFAULT '')",
    # 与迁移 v10 相同的 FTS 表与触发器
    "CREATE VIRTUAL TABLE chapters_fts USING fts5("
    "title, content_bigrams, project_id UNINDEXED, tokenize = 'unicode61')",
    "CREATE TRIGGER chapters_fts_ai AFTER INSERT ON chapter_contents BEGIN "
    "INSERT INTO chapters_fts(rowid, title, content_bigrams, project_id) "
    "SELECT new.rowid, COALESCE((SELECT o.title FROM outlines o WHERE o.id = new.outline_id), ''), "
    "COALESCE(new.content_bigrams, ''), "
    "(SELECT o.project_id FROM outlines o WHERE o.id = new.outline_id) "
    "WHERE COALESCE(new.content_bigrams, '') != ''; END",
    "CREATE TRIGGER chapters_fts_au AFTER UPDATE ON chapter_contents BEGIN "
    "DELETE FROM chapters_fts WHERE rowid = old.rowid; "
    "INSERT INTO chapters_fts(rowid, title, content_bigrams, project_id) "
    "SELECT new.rowid, COALESCE((SELECT o.title FROM outlines o WHERE o.id = new.outline_id), ''), "
    "COALESCE(new.content_bigrams, ''), "
    "(SELECT o.project_id FROM outlines o WHERE o.id = new.outline_id) "
    "WHERE COALESCE(new.content_bigrams, '') != ''; END",
    "CREATE TRIGGER chapters_fts_ad AFTER DELETE ON chapter_contents BEGIN "
    "DELETE FROM chapters_fts WHERE rowid = old.rowid; END",
]


def _fresh_db():
    con = sqlite3.connect(":memory:")
    for s in _SCHEMA_SQL:
        con.execute(s)
    return con


def _insert_chapter(con, cid, oid, title, pid, content):
    con.execute("INSERT INTO outlines VALUES (?, ?, ?)", (oid, pid, title))
    con.execute(
        "INSERT INTO chapter_contents (id, outline_id, content, content_bigrams) VALUES (?, ?, ?, ?)",
        (cid, oid, content, bigrams(content)),
    )


def test_bigrams_basic():
    assert bigrams("陈九") == "陈九 陈 九"
    assert bigrams("账房") == "账房 账 房"


def test_term_to_query_phrase():
    assert term_to_query("陈九") == '"陈九"'
    assert term_to_query("账房") == '"账房"'
    assert term_to_query("沈") == '"沈"'


def test_fts_finds_2char_and_3char_terms():
    con = _fresh_db()
    _insert_chapter(con, "c1", "o1", "第3章：砒霜", "p1", "陈九在药房后院发现砒霜与毒酒同源。")
    _insert_chapter(con, "c2", "o2", "第80章：军械", "p1", "陈九比对军械账册，发现去向对不上。")
    # 2 字词条
    rows = con.execute(
        "SELECT f.title FROM chapters_fts f WHERE chapters_fts MATCH ? AND f.project_id='p1'",
        (term_to_query("砒霜"),),
    ).fetchall()
    assert [r[0] for r in rows] == ["第3章：砒霜"], rows
    # 3 字词条（跨 bigram 短语）
    rows = con.execute(
        "SELECT f.title FROM chapters_fts f WHERE chapters_fts MATCH ? AND f.project_id='p1'",
        (term_to_query("军械账册"),),
    ).fetchall()
    assert [r[0] for r in rows] == ["第80章：军械"], rows


def test_fts_sync_on_update_and_delete():
    con = _fresh_db()
    # 注意：标题也进索引，检索词要用「只在正文里出现」的词，避免与标题撞车
    _insert_chapter(con, "c1", "o1", "第1章：验契", "p1", "顾长宁翻着旧卷。")
    rows = con.execute(
        "SELECT count(*) FROM chapters_fts WHERE chapters_fts MATCH ?",
        (term_to_query("顾长宁"),),
    ).fetchone()[0]
    assert rows == 1
    new_content = "陈九推门而入，账房空无一人。"
    con.execute(
        "UPDATE chapter_contents SET content = ?, content_bigrams = ? WHERE id='c1'",
        (new_content, bigrams(new_content)),
    )
    assert con.execute(
        "SELECT count(*) FROM chapters_fts WHERE chapters_fts MATCH ?",
        (term_to_query("账房"),),
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM chapters_fts WHERE chapters_fts MATCH ?",
        (term_to_query("顾长宁"),),
    ).fetchone()[0] == 0
    con.execute("DELETE FROM chapter_contents WHERE id='c1'")
    assert con.execute("SELECT count(*) FROM chapters_fts WHERE project_id='p1'").fetchone()[0] == 0


def test_fts_project_isolation():
    con = _fresh_db()
    _insert_chapter(con, "c1", "o1", "第1章", "p1", "砒霜来源不明。")
    _insert_chapter(con, "c2", "o2", "第1章", "p2", "砒霜来源不明。")
    n = con.execute(
        "SELECT count(*) FROM chapters_fts WHERE chapters_fts MATCH ? AND project_id='p1'",
        (term_to_query("砒霜"),),
    ).fetchone()[0]
    assert n == 1
