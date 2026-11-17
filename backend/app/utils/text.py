# 文本工具：统一字数统计服务（第十七批「统计分析与运营后台」规格四）
# 正文统计与界面展示使用同一套计数服务，避免不同页面出现不一致。
# 规则：
#   - 中文：每个汉字计 1 字；中文标点默认计入（可通过 include_punctuation 关闭）。
#   - 英文：连续字母序列按单词计（hello world = 2 词）。
#   - 数字：连续数字串按 1 个词计。
#   - 空格：默认不计，include_spaces=True 时每个空格计 1。
#   - Markdown 标记：默认不计（# 标题、* 强调、` 代码、> 引用、- 列表、[text](url) 链接），
#     count_markdown=True 时按普通字符计入。
import re
from dataclasses import dataclass

# 常用 Markdown 行级/行内标记模式
_MD_TITLE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MD_LIST = re.compile(r"^\s{0,3}([-*+]|\d+[.)])\s+", re.MULTILINE)
_MD_FENCE = re.compile(r"^\s{0,3}(```|~~~).*$", re.MULTILINE)
_MD_TABLE = re.compile(r"^\s*\|", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_INLINE = re.compile(r"(`[^`]*`|\*\*[^*]*\*\*|\*[^*]*\*|__[^_]*__|_[^_]*_)")

# 汉字
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 英文单词（连续字母）
_EN_WORD = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
# 数字串
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
# 标点（中英文标点符号）
_PUNCT = re.compile(r"[\u3000-\u303f\uff00-\uffef\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e\u2010-\u2027\u2030-\u205e]")


@dataclass(frozen=True)
class WordCountOptions:
    """字数统计规则配置。"""

    include_spaces: bool = False       # 是否统计空格
    include_punctuation: bool = True   # 是否统计标点
    count_markdown: bool = False       # 是否统计 Markdown 标记


DEFAULT_OPTIONS = WordCountOptions()


def _strip_markdown(text: str) -> str:
    """去除 Markdown 标记（行级结构 + 行内强调/代码/链接）。"""
    lines = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s{0,3}(```|~~~)", line):
            in_fence = not in_fence
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = _MD_TITLE.sub("", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_LIST.sub("", text)
    text = _MD_FENCE.sub("", text)
    text = _MD_TABLE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE.sub(r"\1", text)
    return text


def count_words(text: str, options: WordCountOptions | None = None) -> int:
    """按统一规则统计文本字数。

    中文字符逐字计数，英文/数字按整词计数；标点/空格/Markdown 标记按配置计入。
    """
    if not text:
        return 0

    opts = options or DEFAULT_OPTIONS
    src = text

    if not opts.count_markdown:
        src = _strip_markdown(src)

    total = 0
    # 中文字符
    total += len(_CJK.findall(src))
    # 英文单词
    total += len(_EN_WORD.findall(src))
    # 数字串（与英文单词不重叠，单独计数）
    total += len(_NUM.findall(src))

    if opts.include_spaces:
        total += src.count(" ")

    if opts.include_punctuation:
        # 标点不计入上面的 CJK/单词/数字，单独按字符计数
        total += len(_PUNCT.findall(src))

    return total


def count_stats(text: str, options: WordCountOptions | None = None) -> dict:
    """返回字数统计明细（中文/英文/数字/标点/空格/Markdown 标记字数）。"""
    opts = options or DEFAULT_OPTIONS
    src = text or ""
    raw = src

    if not opts.count_markdown:
        src = _strip_markdown(raw)

    cjk = len(_CJK.findall(src))
    en = len(_EN_WORD.findall(src))
    num = len(_NUM.findall(src))
    spaces = src.count(" ")
    punct = len(_PUNCT.findall(src))
    markdown_chars = len(raw) - len(src) if not opts.count_markdown else 0

    total = cjk + en + num
    if opts.include_spaces:
        total += spaces
    if opts.include_punctuation:
        total += punct
    if opts.count_markdown:
        total += markdown_chars

    return {
        "total": total,
        "cjk": cjk,
        "english_words": en,
        "numbers": num,
        "spaces": spaces,
        "punctuation": punct,
        "markdown_chars": markdown_chars,
        "include_spaces": opts.include_spaces,
        "include_punctuation": opts.include_punctuation,
        "count_markdown": opts.count_markdown,
    }


def mask_email(email: str | None) -> str | None:
    """邮箱脱敏：a***@example.com（报表导出/用户列表用）。"""
    if not email:
        return None
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        return f"*@{domain}"
    return f"{name[0]}***@{domain}"


def mask_username(username: str | None) -> str | None:
    """用户名脱敏：保留首字符，其余打码。"""
    if not username:
        return None
    if len(username) <= 1:
        return "*"
    return f"{username[0]}***"
