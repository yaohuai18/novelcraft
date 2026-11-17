# -*- coding: utf-8 -*-
"""生成链路数据一致性/可追溯性修复回归测试（backend/tests/）。

（阶段 1 冻结期：本文件自 backend/temp/ 迁入 backend/tests/，纳入正式可追踪回归；
   用例内容与源脚本等价，仅调整文件位置与运行路径，测试逻辑未改动。）


覆盖：
  1. 4201 / 4500 / 8000 字正文不会被静默截断（_finalize_chapter 关闭 allow_truncate）；
  2. 超长状态为 over_target，且 raw_content 与 final_content 一致；
  3. 触达 max_tokens（finish_reason=length）状态为 incomplete 而非 succeeded；
  4. 流式 chunk 完整拼接后才进入后处理/评估；
  5. 后处理失败不覆盖 raw_content（raw_content 恒为模型原始响应）；
  6. 非句末结尾触发 review 标记但不删除内容；
  7. 旧数据（缺新列的历史库）读取新字段不报错（迁移幂等补列）。

运行：cd backend && python -m pytest tests/test_phase1_generation_consistency.py -q
"""
import asyncio
import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app.services.ai_service import (  # noqa: E402
    _SENTENCE_END_CHARS,
    _tail_quality_flags,
    assemble_stream_chunks,
    count_words,
    evaluate_chapter_result,
)
from app.routers.ai_novel_generation import _finalize_chapter  # noqa: E402


def _make_body(n_words: int) -> str:
    """生成 n_words 字左右的正文（以句号结尾）。"""
    unit = "这是用来测试生成链路完整性的填充句子，确保字数统计与截断判定可靠。"
    repeats = max(1, n_words // len(unit))
    body = (unit * repeats)
    if len(body) > n_words:
        body = body[:n_words]
    return body.rstrip("，") + "。"


def run(coro):
    return asyncio.run(coro)


# ---------- 1. 超长正文不会被静默截断 ----------
@pytest.mark.parametrize("n_words", [4201, 4500, 8000])
def test_no_silent_truncation_long_body(n_words):
    body = _make_body(n_words)
    content = f"第1章：测试章节\n{body}"
    title, final_body, word_count = run(
        _finalize_chapter(content, 1, target_words=3500)
    )
    assert word_count == count_words(final_body)
    # 不再被 _smart_truncate 截到 3500：最终正文应保留全部填充句子
    assert final_body == body, "超长正文被静默截断（截断了）"
    assert count_words(final_body) > 3500, "超长正文被截短到目标字数附近"


# ---------- 2. 超长 → over_target 且原文与最终正文一致 ----------
def test_over_target_status_and_raw_matches_final():
    body = _make_body(4500)
    title = "第1章：超长章节"
    raw = f"{title}\n{body}"
    ev = evaluate_chapter_result(
        raw_content=raw,
        title=title,
        body=body,
        target_words=3500,
        finish_reason="stop",
    )
    assert ev["status"] == "over_target"
    assert "over_target" in ev["review_flags"]
    assert "keep_full_text_no_truncate" in ev["postprocess_actions"]
    # 原文（含标题行）字数 >= 最终正文字数，最终正文完整保留未被截断
    assert ev["raw_content_length"] >= ev["final_content_length"]
    assert ev["final_content_length"] == count_words(body)


# ---------- 3. 触达 max_tokens → incomplete 而非 succeeded ----------
def test_finish_reason_length_is_incomplete():
    body = _make_body(3000)
    ev = evaluate_chapter_result(
        raw_content=f"第1章：截断\n{body}",
        title="第1章：截断",
        body=body,
        target_words=3500,
        finish_reason="length",
    )
    assert ev["status"] == "incomplete"
    assert ev["status"] != "succeeded"
    assert ev["truncation_reason"] == "truncated_by_max_tokens"
    assert "truncated_by_max_tokens" in ev["review_flags"]


def test_stream_not_completed_is_incomplete():
    body = _make_body(3000)
    ev = evaluate_chapter_result(
        raw_content=f"第1章：流断\n{body}",
        title="第1章：流断",
        body=body,
        target_words=3500,
        finish_reason="stop",
        stream_completed=False,
    )
    assert ev["status"] == "incomplete"
    assert ev["truncation_reason"] == "stream_not_completed"


# ---------- 4. 流式 chunk 完整拼接后才后处理 ----------
def test_stream_chunks_assembled_before_postprocess():
    chunks = ["第1章：拼接", "\n第一段没有说完", "。第二段。", "第三段。"]
    full = assemble_stream_chunks(chunks)
    assert full == "第1章：拼接\n第一段没有说完。第二段。第三段。"

    title, body, _ = run(_finalize_chapter(full, 1, target_words=1000))
    # 拼接完整后：正文以句号结尾 → 无 non_sentence_end 风险标记
    flags = _tail_quality_flags(body)
    assert "non_sentence_end" not in flags

    # 若只取半截 chunk 则会被判为残缺（模拟未拼接完成就后处理）
    half = assemble_stream_chunks(chunks[:2])
    _, half_body, _ = run(_finalize_chapter(half, 1, target_words=1000))
    half_flags = _tail_quality_flags(half_body)
    assert "non_sentence_end" in half_flags


# ---------- 5. 后处理失败不覆盖 raw_content ----------
def test_raw_content_preserved_regardless_of_status():
    from app.routers.ai_novel_generation import _build_generation_eval

    body = _make_body(3500)
    raw = f"第1章：保留\n{body}"
    for finish_reason in ("stop", "length"):
        ev = _build_generation_eval(raw, "第1章：保留", body, 3500, finish_reason=finish_reason)
        # raw_content 字段恒为模型原始完整响应（只追加不改写）
        assert ev["raw_content"] == raw
        # 后处理字段与状态解耦：即使 incomplete/failed，raw 也不被清空或截断
        assert ev["raw_content_length"] == count_words(raw)
        assert ev["final_content_length"] == count_words(body)


# ---------- 6. 非句末结尾触发 review 标记但不删除内容 ----------
def test_non_sentence_end_flags_review_but_keeps_content():
    body = _make_body(2000) + "话说到一半"  # 非句末标点结尾
    ev = evaluate_chapter_result(
        raw_content=f"第1章：未完\n{body}",
        title="第1章：未完",
        body=body,
        target_words=3500,
        finish_reason="stop",
    )
    assert "non_sentence_end" in ev["review_flags"]
    # 不删除内容：final_content 保持完整
    assert ev["final_content_length"] == count_words(body)
    assert ev["status"] != "failed"  # 仅风险信号，不判失败


def test_unclosed_quote_flags_review():
    flags = _tail_quality_flags("他说：“今天天气不错")
    assert any(f.startswith("unclosed_delimiter") for f in flags)


# ---------- 7. 旧数据读取新字段不报错（迁移幂等补列） ----------
def test_phase11_migration_backfills_columns():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.db_migrations import ensure_phase11_schema

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    conn = None
    try:
        async def _scenario():
            async with engine.begin() as c:
                # 构造"历史库"：旧 generation_records 表（无 phase11 新列）
                await c.execute(text(
                    "CREATE TABLE generation_records ("
                    " id CHAR(32) PRIMARY KEY, action_type VARCHAR(30) NOT NULL,"
                    " status VARCHAR(20) DEFAULT 'pending' NOT NULL,"
                    " response_content TEXT DEFAULT '', created_at DATETIME)"
                ))
                # 执行幂等迁移补列
                await ensure_phase11_schema(c)
            async with engine.connect() as c:
                cols = {row[0] for row in (await c.execute(
                    text("SELECT name FROM pragma_table_info('generation_records')")
                )).all()}
                assert "raw_content" in cols
                assert "processed_content" in cols
                assert "final_content" in cols
                assert "finish_reason" in cols
                assert "was_truncated" in cols
                assert "review_flags" in cols
                # 旧行写入后读取新字段不报错（带默认值）
                await c.execute(text(
                    "INSERT INTO generation_records (id, action_type, status, response_content)"
                    " VALUES ('00000000000000000000000000000001', 'continue', 'succeeded', 'old text')"
                ))
                row = (await c.execute(text(
                    "SELECT raw_content, final_content, was_truncated, review_flags"
                    " FROM generation_records WHERE id='00000000000000000000000000000001'"
                ))).fetchone()
                assert row is not None
                assert row[0] == "" and row[1] == "" and row[2] == 0 and row[3] is None
                # 幂等：重复执行不报错
                async with engine.begin() as c2:
                    await ensure_phase11_schema(c2)
        asyncio.run(_scenario())
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
