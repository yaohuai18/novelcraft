# 安全工具扩展（第十八批「性能优化、可靠性与安全加固」规格十八~二十三）
# 通用安全能力：令牌哈希、IP 哈希、错误脱敏、HTML 白名单、MIME/文件名校验等。
import hashlib
import html
import re
import secrets

# 敏感模式：DB 异常堆栈 / 服务器路径 / 内部地址 / 密钥
_SENSITIVE_PATTERNS = [
    re.compile(r"(Traceback \(most recent call last\)|File \"[^\"]+\", line \d+)", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\\\[^\s\"']+|/[A-Za-z0-9_./-]+/(?:backend|app)/", re.IGNORECASE),
    re.compile(r"http://(?:127\.0\.0\.1|localhost|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", re.IGNORECASE),
    re.compile(r'(api[_-]?key|secret|password|token)["\']?\s*[:=]\s*["\']?[A-Za-z0-9_\-\.]{6,}', re.IGNORECASE),
]

# HTML 白名单标签（内容安全：恶意 HTML / XSS 过滤）
_ALLOWED_HTML_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li",
    "blockquote", "code", "pre", "a", "img", "h1", "h2", "h3", "h4",
    "span", "div", "table", "thead", "tbody", "tr", "td", "th",
}
_ALLOWED_HTML_ATTRS = {"href", "title", "src", "alt", "width", "height", "target", "rel", "class", "style"}
_BLOCKED_URL_SCHEMES = {"javascript:", "data:text/html", "vbscript:"}

# 命令注入危险字符（内容安全：命令注入防护）
_COMMAND_META = re.compile(r"[;&|`$<>]|&&|\|\||\$\(|\b(rm|del|format|shutdown|mkfs)\b", re.IGNORECASE)


def sha256_hex(value: str) -> str:
    """SHA-256 十六进制哈希（用于令牌存储 / IP 脱敏）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_secure_token(byte_length: int = 32) -> str:
    """生成加密安全随机令牌（hex）。"""
    return secrets.token_hex(byte_length)


def sanitize_error_message(message: str | None, *, max_length: int = 500) -> str:
    """错误信息脱敏：剥离堆栈、路径、内部地址、疑似密钥，并限制长度。"""
    if not message:
        return "发生未知错误"
    text = str(message)
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    # 再兜一层：任何包含文件路径特征的行直接截断
    text = re.sub(r"\n.*?(?:Traceback|at .+\(.+\.py:)", "\n[REDACTED]", text)
    return text[:max_length]


def sanitize_html(content: str) -> str:
    """HTML 白名单过滤：只保留白名单标签与属性，其余转义；阻止 javascript: 等危险协议。"""
    # 快速路径：无标签直接转义返回
    if "<" not in content:
        return content
    tag_re = re.compile(r"<\s*(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
    def _replace(match: re.Match) -> str:
        closing = match.group(1)
        tag = match.group(2).lower()
        attrs = match.group(3)
        if tag not in _ALLOWED_HTML_TAGS:
            return html.escape(match.group(0))
        if closing:
            return f"</{tag}>"
        safe_attrs = []
        for name, value in re.findall(r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*"([^"]*)"', attrs):
            name = name.lower()
            value = value.strip()
            lowered = value.lower()
            if name in _ALLOWED_HTML_ATTRS and not any(lowered.startswith(s) for s in _BLOCKED_URL_SCHEMES):
                safe_attrs.append(f'{name}="{html.escape(value, quote=True)}"')
        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        return f"<{tag}{attr_str}>"
    out = tag_re.sub(_replace, content)
    return out


def looks_like_command_injection(text: str) -> bool:
    """检测命令注入风险（用于 AI 提示词/导入内容等输入校验提示）。"""
    return bool(_COMMAND_META.search(text))


def safe_filename(original_name: str) -> str:
    """清洗原始文件名，仅保留安全的基名与扩展名。"""
    base = original_name.replace("\\", "/").split("/")[-1]
    base = re.sub(r'[^\w.\-]', "_", base)
    return base[:120]


def random_stored_name(original_name: str) -> str:
    """生成随机存储名：<32位hex>.<扩展名>（文件存储安全：随机存储名）。"""
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "bin"
    if not re.fullmatch(r"[a-z0-9]{1,10}", ext):
        ext = "bin"
    return f"{secrets.token_hex(16)}.{ext}"
