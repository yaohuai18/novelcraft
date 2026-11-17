# 创作页轻量一致性校验（2026-11-17 批次 2）
# 无数据库依赖、确定性规则、零 AI 成本；保存前自动跑，问题以黄条提示，不阻断写作。
# 第一期只做 4 个高价值维度（对标 InkOS 但不追 33 维）：
#   1. foreshadow_order    伏笔回收章早于/等于埋设章
#   2. timeline_reversal   章节间绝对日期倒流
#   3. character_state     人物在前章死亡/遇害，后章又作为行动主体出场
#   4. rule_violation      正文命中设定限制（内置血腥词表 + 用户规则关键词提取）
import re
from datetime import datetime

_SEVERITY = "warning"

# ---------- 规则词表 ----------

_GORE_WORDS = [
    "脑浆", "肠子", "内脏", "肢解", "血肉模糊", "分尸", "血浆迸",
    "砍下头颅", "挖出眼球", "开膛破肚", "剁成肉泥",
]

_DEATH_VERBS = "死了|身亡|遇害|被杀|遇难|殒命|身死|咽了气|断了气"

# 人物作为行动主体出场的强信号（动词）
_ACTION_VERBS = "说|道|问|答|喊|喝|走|站|坐|笑|看|望|回头|点头|摇头|开口|起身|进门|推门|抽出|拔出|抬头|低喝|冷笑|皱眉"

_RULE_KEYWORD_RE = re.compile(
    r"(?:不出现|禁止出现|不得出现|禁止|不得|避免|不写|杜绝)\s*[「“\"]?([^「”，。；、\"\n]+)"
)

_DATE_RE = re.compile(
    r"([〇零一二三四五六七八九十百千两\d]{1,6})\s*年\s*"
    r"([一二三四五六七八九十\d]{1,3})\s*月\s*"
    r"([一二三四五六七八九十\d]{1,3})\s*日"
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn2int(s: str) -> int | None:
    """中文数字/阿拉伯数字 → int（支持 三/十二/二十一/三百零五 等）。"""
    s = str(s).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    cur = 0
    for ch in s:
        if ch in _CN_DIGITS:
            cur = _CN_DIGITS[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        elif ch == "千":
            total += (cur or 1) * 1000
            cur = 0
        else:
            return None
    return total + cur


_ERA_RE = re.compile(r"([一-鿿]{1,6})\s*$")


def _issue(itype: str, message: str, location: str, suggestion: str) -> dict:
    return {
        "severity": _SEVERITY,
        "type": itype,
        "location": location,
        "message": message,
        "suggestion": suggestion,
        "data": {},
    }


def _chapter_no(ch: dict) -> int:
    try:
        return int(ch.get("index") or ch.get("chapter_index") or 0)
    except (TypeError, ValueError):
        return 0


def _chapter_text(ch: dict) -> str:
    title = str(ch.get("title") or "")
    content = str(ch.get("content") or "")
    return f"{title}\n{content}"


# ---------- 维度 1：伏笔回收顺序 ----------

def _check_foreshadow_order(plan: dict, issues: list[dict]) -> None:
    for f in plan.get("foreshadowing") or []:
        if not isinstance(f, dict):
            continue
        item = str(f.get("item") or "未命名伏笔")
        planted = f.get("planted_chapter")
        payoff = f.get("payoff_chapter")
        try:
            planted_i = int(planted)
            payoff_i = int(payoff)
        except (TypeError, ValueError):
            continue
        if payoff_i <= planted_i:
            issues.append(
                _issue(
                    "foreshadow_order",
                    f"伏笔《{item}》回收章（第{payoff_i}章）早于或等于埋设章（第{planted_i}章），"
                    "读者会在回收前就得知结局，伏笔作废。",
                    f"第{planted_i}章",
                    f"把回收章改到埋设章之后（建议 ≥ 埋设章+5），或调整规划中该伏笔的 payoff_chapter。",
                )
            )


# ---------- 维度 2：时间线倒流 ----------

def _check_timeline(chapters: list[dict], issues: list[dict]) -> None:
    """按章节顺序取每章第一个绝对日期，检测后章日期早于前章。

    支持阿拉伯数字与中文数字纪年（如「崇宁三年三月五日」）；年号不同（如崇宁→天启）
    时不比较（无法排序），只比较同年号内的日期。
    """
    last: tuple[str, int, int, int] | None = None
    last_no = 0
    last_raw = ""
    for ch in chapters or []:
        text = _chapter_text(ch)
        m = _DATE_RE.search(text)
        if not m:
            continue
        y = _cn2int(m.group(1))
        mo = _cn2int(m.group(2))
        d = _cn2int(m.group(3))
        if y is None or mo is None or d is None:
            continue
        era_m = _ERA_RE.search(text[max(0, m.start() - 6): m.start()])
        era = era_m.group(1) if era_m else ""
        no = _chapter_no(ch)
        if last is not None and era == last[0] and (y, mo, d) < last[1:]:
            issues.append(
                _issue(
                    "timeline_reversal",
                    f"第{no}章出现的时间（{era}{m.group(0)}）早于第{last_no}章（{last_raw}），时间线倒流。",
                    f"第{no}章",
                    "核对两章的时间表述；若本章是回忆/倒叙，请补上「多年前/回忆」等明确标记。",
                )
            )
        last = (era, y, mo, d)
        last_no = no
        last_raw = f"{era}{m.group(0)}"


# ---------- 维度 3：人物状态冲突（死亡后又出场） ----------

def _collect_names(plan: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    protagonist = str(plan.get("protagonist") or "")
    for raw in [protagonist] + [str(c.get("name") or "") for c in (plan.get("characters") or [])] + [str(c.get("name") or "") for c in (plan.get("supporting_characters") or [])]:
        for part in re.split(r"[，,、；;\s\n]+", raw):
            part = part.strip("「」《》\"'：:（）() ")
            if len(part) >= 2 and len(part) <= 4 and part not in seen:
                seen.add(part)
                names.append(part)
    return names


def _check_character_state(plan: dict, chapters: list[dict], issues: list[dict]) -> None:
    names = _collect_names(plan)
    if not names or len(chapters or []) < 2:
        return
    chs = chapters or []
    for name in names:
        death_re = re.compile(rf"{name}[^。！？\n]{{0,12}}(?:{_DEATH_VERBS})")
        alive_re = re.compile(rf"{name}({_ACTION_VERBS})")
        death_no = 0
        for ch in chs:
            text = _chapter_text(ch)
            no = _chapter_no(ch)
            if death_no == 0 and death_re.search(text):
                death_no = no
            elif death_no > 0 and no > death_no and alive_re.search(text):
                m = alive_re.search(text)
                issues.append(
                    _issue(
                        "character_state_conflict",
                        f"第{death_no}章写明「{name}」已死亡，第{no}章又以行动主体出场"
                        f"（出现「{name}{m.group(1)}」等动作）。",
                        f"第{no}章",
                        "若这是回忆/闪回/替身/假死，请在正文里明确点出；否则需要改掉其中一处。",
                    )
                )
                break  # 同一人物只报一次


# ---------- 维度 4：设定规则违反 ----------

def _check_rules(chapters: list[dict], rules: list[str], issues: list[dict]) -> None:
    chs = chapters or []
    for rule in rules or []:
        rule_s = str(rule).strip()
        if not rule_s:
            continue
        keywords: list[str] = []
        if "血腥" in rule_s or "猎奇" in rule_s:
            keywords = _GORE_WORDS
        else:
            m = _RULE_KEYWORD_RE.search(rule_s)
            if m:
                raw = m.group(1).strip()
                # 提取词+前缀退化（如「穿越情节」也命中「穿越」），按长到短逐个试
                keywords = []
                for p in re.split(r"[、/，,|]+", raw):
                    p = p.strip()
                    for end in range(len(p), 1, -1):
                        if len(p[:end]) >= 2:
                            keywords.append(p[:end])
                keywords = list(dict.fromkeys(keywords))
        for kw in keywords:
            for ch in chs:
                text = _chapter_text(ch)
                if kw in text:
                    issues.append(
                        _issue(
                            "rule_violation",
                            f"正文命中设定限制「{rule_s[:40]}」：第{_chapter_no(ch)}章出现「{kw}」。",
                            f"第{_chapter_no(ch)}章",
                            "删除或改写该处描写，与锁定设定保持一致。",
                        )
                    )
                    break  # 同一关键词只报一次


# ---------- 入口 ----------

def quick_check(
    plan: dict | None,
    chapters: list[dict] | None,
    rules: list[str] | None,
) -> dict:
    """对创作页内存态做 4 维轻校验，返回 {issues, dimensions, checked_at}。"""
    plan = plan or {}
    chapters = chapters or []
    rules = rules or []
    issues: list[dict] = []
    _check_foreshadow_order(plan, issues)
    _check_timeline(chapters, issues)
    _check_character_state(plan, chapters, issues)
    _check_rules(chapters, rules, issues)
    return {
        "issues": issues,
        "dimensions": 4,
        "checked_at": datetime.utcnow().isoformat(),
    }
