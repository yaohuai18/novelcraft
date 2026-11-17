# 文本工具（2026-11-17 批次 5）：中文 n-gram 生成，供 FTS5 索引与检索共用。
# SQLite 自带 tokenizer 对中文不切词（整句成一个 token），字符 bigram 是零依赖解法：
# 索引文本与查询词都转成 2 字滑动窗口，短语查询即可命中任意长度词条。


def bigrams(text: str) -> str:
    """把文本转成空格分隔的字符 bigram + unigram 串。

    「陈九在账房」→ 「陈九 九在 在账 账房 陈 九 在 账 房」
    （先全部 bigram 保持相邻性，再全部 unigram 支持单字词条。）
    """
    s = "".join((text or "").split())
    if not s:
        return ""
    grams = [s[i : i + 2] for i in range(len(s) - 1)] if len(s) > 1 else []
    grams += list(s)
    return " ".join(grams)


def term_to_query(term: str) -> str:
    """把检索词条转成 FTS5 查询片段（短语形式）。

    - 1 字：「陈」→ "陈"
    - 2 字：「陈九」→ "陈九"
    - 3+ 字：「账房」→ "账房"（相邻 bigram 的短语）
    """
    t = "".join(str(term).split())
    if not t:
        return ""
    if len(t) == 1:
        return f'"{t}"'
    bgs = [t[i : i + 2] for i in range(len(t) - 1)]
    return '"' + " ".join(bgs) + '"'
