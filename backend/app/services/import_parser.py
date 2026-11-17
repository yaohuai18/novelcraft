# 导入解析服务（第十六批「数据导入、导出与备份恢复」规格实现）
# 支持 Markdown / 纯文本 / HTML / JSON / CSV 五类格式解析为统一中间结构：
#   title / root_type / structure(层级列表) / characters / locations / factions /
#   world_rules / timeline_events / relationships / tags / warnings / format_warnings /
#   unrecognized / duplicates / estimated_word_count / volume_count / chapter_count / total_items
# Markdown 解析规则（规格三）：一级标题=分卷、二级标题=章节、普通文本=正文段落、
#   引用=引用块、列表=列表；无标题时提供拆分选项（按文件/按一级标题/按二级标题/单章）。
# CSV（规格一/二）：人物、地点、势力、世界观规则、时间线、人物关系、标签等表格数据。
# 解析阶段不写任何业务数据（预览先行）；承担导入安全限制（大小、章节长度、条目数量、脚本剥离）。
import csv
import hashlib
import html as html_mod
import io
import json
import re
from html.parser import HTMLParser

# ============ 安全限制常量（规格十三） ============
MAX_IMPORT_LENGTH = 2 * 1024 * 1024      # 单次导入内容最大 2MB（字符数）
MAX_CHAPTER_LENGTH = 200_000             # 单个章节正文最大 20 万字符
MAX_PROJECT_ITEMS = 2000                 # 单次导入条目数量上限（大纲+正文+知识+标签）

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

# 纯文本章节标题识别：第X卷 / 第X章 / 序章 / 楔子 / 尾声 / 番外
_PLAIN_TITLE_RE = re.compile(
    r"^\s*(第[0-9一二三四五六七八九十百千零两]+[卷章节回]|序章|楔子|尾声|番外[^\s]*)[\s：:、.．\-—]*\s*(.*)$"
)

_CN_NUM_RE = re.compile(r"[0-9一二三四五六七八九十百千零两]+")

# 拆分方式（规格三：无标题时的导入选项）
SPLIT_MODES = ["file", "volume", "chapter", "single"]


def _sanitize_text(text: str) -> str:
    """导入文本基础清洗：去空白/控制字符。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "").strip()


def _normalize_title(title: str) -> str:
    title = html_mod.unescape(title or "").strip()
    return title[:255]


def _check_length(content: str) -> None:
    if len(content) > MAX_IMPORT_LENGTH:
        raise ValueError(
            f"单次导入内容超过大小限制（{MAX_IMPORT_LENGTH} 字符）"
        )


def _check_items_limit(total: int) -> None:
    if total > MAX_PROJECT_ITEMS:
        raise ValueError(
            f"导入条目数量超过单作品上限（{MAX_PROJECT_ITEMS}）"
        )


def _check_chapter_length(content: str, title: str, warnings: list[str]) -> str:
    """章节长度限制：超限返回截断文本并追加警告。"""
    if len(content) > MAX_CHAPTER_LENGTH:
        warnings.append(f"章节「{title}」正文超过长度限制，已截断至 {MAX_CHAPTER_LENGTH} 字符")
        return content[:MAX_CHAPTER_LENGTH]
    return content


def _append_line(existing: str, line: str) -> str:
    if existing:
        return existing + "\n" + line
    return line


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


def _detect_duplicates(structure: list[dict], warnings: list[str]) -> list[dict]:
    """按 (kind,title) 与正文哈希检测重复。"""
    seen_titles: dict[tuple[str, str], int] = {}
    seen_hashes: dict[str, str] = {}
    duplicates: list[dict] = []
    for node in structure:
        if node["kind"] not in ("volume", "chapter"):
            continue
        title = node.get("title") or ""
        key = (node["kind"], title)
        if title:
            seen_titles[key] = seen_titles.get(key, 0) + 1
            if seen_titles[key] > 1:
                duplicates.append({
                    "item": title,
                    "match_by": "title",
                    "kind": node["kind"],
                })
        content = node.get("content") or ""
        if content.strip():
            h = _content_hash(content)
            if h in seen_hashes:
                duplicates.append({
                    "item": title or f"未命名{node['kind']}",
                    "match_by": "content_hash",
                    "kind": node["kind"],
                })
            else:
                seen_hashes[h] = title
    return duplicates


# ============ Markdown 解析（规格三：一级=分卷 / 二级=章节） ============

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_markdown(content: str, file_name: str, split_mode: str = "chapter") -> dict:
    lines = content.splitlines()
    raw_nodes: list[dict] = []
    cur = None
    title = _normalize_title(file_name)
    warnings: list[str] = []
    for line in lines:
        m = _MD_HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = _normalize_title(m.group(2))
            kind = {1: "volume", 2: "chapter", 3: "scene"}.get(level, "scene")
            node = {"kind": kind, "level": min(level, 4), "title": text, "content": ""}
            raw_nodes.append(node)
            cur = node
        else:
            stripped = line.strip()
            if cur is None:
                raw_nodes.append({
                    "kind": "chapter", "level": 2,
                    "title": "未分类内容", "content": "",
                })
                cur = raw_nodes[-1]
                warnings.append("发现 Markdown 标题前的正文，已归入「未分类内容」")
            if stripped.startswith(">"):
                cur["content"] = _append_line(cur["content"], "> " + stripped.lstrip(">").strip())
            elif stripped.startswith(("- ", "* ", "+ ")):
                cur["content"] = _append_line(cur["content"], "- " + stripped[2:].strip())
            else:
                cur["content"] = _append_line(cur["content"], line)

    if not raw_nodes:
        raw_nodes.append({
            "kind": "chapter", "level": 2,
            "title": "未分类内容", "content": content.strip(),
        })
        warnings.append("未检测到任何标题层级，全部内容归入「未分类内容」")

    structure = _apply_split_mode(raw_nodes, split_mode, title, warnings, "markdown")
    return _assemble(structure, title, "markdown", warnings, root_type="outline")


def _apply_split_mode(
    raw_nodes: list[dict], split_mode: str, title: str,
    warnings: list[str], file_type: str,
) -> list[dict]:
    """按拆分方式重组标题节点（规格三）。"""
    if split_mode not in SPLIT_MODES:
        split_mode = "chapter"
    has_any_heading = any(n.get("title") not in ("未分类内容", "") for n in raw_nodes)
    if not has_any_heading:
        split_mode = "single"

    if split_mode == "single":
        body = "\n".join(n.get("content", "") for n in raw_nodes).strip()
        return [{"kind": "chapter", "level": 2, "title": "未命名章节", "content": body}]

    if split_mode == "file":
        body = "\n".join(
            (f"# {n['title']}\n" if n["kind"] == "volume" else f"## {n['title']}\n" if n["kind"] == "chapter" else "") + (n.get("content") or "")
            for n in raw_nodes
        ).strip()
        return [{"kind": "chapter", "level": 2, "title": title, "content": body}]

    if split_mode == "volume":
        # 每个一级标题作为一个章节
        result: list[dict] = []
        for n in raw_nodes:
            if n["kind"] == "volume":
                result.append({
                    "kind": "chapter", "level": 2,
                    "title": n.get("title") or "未命名章节",
                    "content": n.get("content") or "",
                })
            elif n["kind"] == "chapter":
                if result:
                    result[-1]["content"] = _append_line(
                        result[-1]["content"],
                        ("## " + n["title"] + "\n" if n.get("title") and n["title"] != "未分类内容" else "") + (n.get("content") or ""),
                    )
                else:
                    result.append({
                        "kind": "chapter", "level": 2,
                        "title": n.get("title") or "未命名章节",
                        "content": n.get("content") or "",
                    })
        if not result:
            result = [{"kind": "chapter", "level": 2, "title": "未命名章节", "content": ""}]
        return result

    # split_mode == "chapter"：一级=分卷 / 二级=章节 / 三级=场景
    result = []
    for n in raw_nodes:
        node = dict(n)
        if n["kind"] == "chapter" and n.get("title") == "未分类内容" and not n.get("content"):
            continue
        result.append(node)
    if not result:
        result = [{"kind": "chapter", "level": 2, "title": "未命名章节", "content": ""}]
    return result


# ============ 纯文本解析 ============

def parse_text(content: str, file_name: str, split_mode: str = "chapter") -> dict:
    structure: list[dict] = []
    cur = None
    title = _normalize_title(file_name)
    warnings: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            if cur is not None:
                cur["content"] = _append_line(cur["content"], "")
            continue
        m = _PLAIN_TITLE_RE.match(line)
        if m and _is_title_line(m.group(1)):
            raw_head = m.group(1)
            rest = m.group(2)
            if "卷" in raw_head:
                kind = "volume"
                level = 1
            else:
                kind = "chapter"
                level = 2
            node_title = raw_head + (f"：{rest}" if rest else "")
            node = {
                "kind": kind, "level": level,
                "title": _normalize_title(node_title), "content": "",
            }
            structure.append(node)
            cur = node
        else:
            if cur is None:
                structure.append({
                    "kind": "chapter", "level": 2,
                    "title": "未分类内容", "content": "",
                })
                cur = structure[-1]
                warnings.append("发现无法识别的段落，已归入「未分类内容」")
            cur["content"] = _append_line(cur["content"], line)
    if not structure:
        structure.append({
            "kind": "chapter", "level": 2,
            "title": "未分类内容", "content": content.strip(),
        })
        warnings.append("未识别到章节标题，全部内容归入「未分类内容」")
    structure = _apply_split_mode(structure, split_mode, title, warnings, "text")
    return _assemble(structure, title, "text", warnings, root_type="outline")


def _is_title_line(raw_head: str) -> bool:
    # 排除误匹配：如正文首字恰好是"第…"但后续无章节词
    return bool(_CN_NUM_RE.search(raw_head) or raw_head in ("序章", "楔子", "尾声") or raw_head.startswith("番外"))


# ============ HTML 解析 ============

class _ImportHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.structure: list[dict] = []
        self.cur: dict | None = None
        self._in_script = False
        self._in_title = False
        self._pending_title_text = False
        self.warnings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._in_script = True
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3", "h4"):
            level = int(tag[1])
            kind = {1: "volume", 2: "chapter", 3: "scene"}.get(level, "scene")
            self.cur = {"kind": kind, "level": level, "title": "", "content": ""}
            self.structure.append(self.cur)
            self._pending_title_text = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._in_script = False
        elif tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3", "h4"):
            if self.cur is not None and not self.cur["title"]:
                self.cur["title"] = "未命名标题"
            self._pending_title_text = False

    def handle_data(self, data: str) -> None:
        if self._in_script or self._in_title:
            if self._in_title:
                self.title = (self.title or "") + data.strip()
            return
        if self._pending_title_text and self.cur is not None:
            self.cur["title"] = _normalize_title(data)
            self._pending_title_text = False
            return
        if self.cur is None:
            if data.strip():
                self.structure.append({
                    "kind": "chapter", "level": 2,
                    "title": "未分类内容", "content": "",
                })
                self.cur = self.structure[-1]
                self.warnings.append("发现 HTML 标题前的正文，已归入「未分类内容」")
        if self.cur is not None and data.strip():
            self.cur["content"] = _append_line(self.cur["content"], data.strip())


def parse_html(content: str, file_name: str, split_mode: str = "chapter") -> dict:
    parser = _ImportHTMLParser()
    parser.feed(content)
    parser.close()
    structure = parser.structure
    title = parser.title or _normalize_title(file_name)
    if not structure:
        structure.append({
            "kind": "chapter", "level": 2,
            "title": "未分类内容", "content": content.strip(),
        })
        parser.warnings.append("未检测到任何标题层级，全部内容归入「未分类内容」")
    structure = _apply_split_mode(structure, split_mode, title, parser.warnings, "html")
    return _assemble(structure, title, "html", parser.warnings, root_type="outline")


# ============ JSON 解析（完整项目结构，规格十一） ============

_JSON_LEVEL_MAP = {
    "summary": 1,
    "volume": 2,
    "chapter": 3,
    "scene": 4,
}


def parse_json(content: str, file_name: str, split_mode: str = "chapter") -> dict:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 解析失败：{error.msg}") from error
    if not isinstance(data, dict):
        raise ValueError("JSON 导入格式错误：顶层必须是对象")

    schema_version = str(data.get("schema_version", "1.0"))
    warnings: list[str] = []
    if schema_version not in ("1.0", "1", "0.9"):
        warnings.append(f"schema_version={schema_version} 非当前支持版本（1.0），按兼容方式解析")
        if _version_greater(schema_version, "1.0"):
            raise ValueError(f"备份 schema_version={schema_version} 过高，当前不支持")

    project = data.get("project") or {}
    title = _normalize_title(project.get("title") or file_name)

    structure: list[dict] = []
    seen_titles: dict[tuple[str, str], int] = {}

    # 优先解析规格十一结构：volumes / chapters
    volumes = data.get("volumes") or []
    chapters = data.get("chapters") or []
    if volumes or chapters:
        vol_by_id: dict[str, dict] = {}
        for v in volumes:
            if not isinstance(v, dict):
                continue
            vol_by_id[str(v.get("id") or "")] = v
        for v in volumes:
            if not isinstance(v, dict):
                continue
            node = {
                "kind": "volume", "level": 1,
                "title": _normalize_title(v.get("title") or "未命名分卷"),
                "content": v.get("summary") or "",
            }
            if v.get("id"):
                node["key"] = str(v["id"])
            structure.append(node)
            seen_titles[("volume", node["title"])] = seen_titles.get(("volume", node["title"]), 0) + 1
        for c in chapters:
            if not isinstance(c, dict):
                continue
            node = {
                "kind": "chapter", "level": 2,
                "title": _normalize_title(c.get("title") or "未命名章节"),
                "content": c.get("content") or "",
            }
            if c.get("id"):
                node["key"] = str(c["id"])
            if c.get("volume_id"):
                node["parent_key"] = str(c["volume_id"])
            structure.append(node)
            seen_titles[("chapter", node["title"])] = seen_titles.get(("chapter", node["title"]), 0) + 1
        for key, count in seen_titles.items():
            if count > 1:
                warnings.append(f"检测到重复标题：{key[1]}")

    # 兼容第十批：outlines / chapter_contents
    else:
        outlines = data.get("outlines") or []
        for item in outlines:
            if not isinstance(item, dict):
                continue
            kind = item.get("outline_type") or item.get("type") or "chapter"
            if kind not in _JSON_LEVEL_MAP:
                kind = "chapter"
            node = {
                "kind": kind,
                "level": _JSON_LEVEL_MAP[kind],
                "title": _normalize_title(item.get("title") or "未命名标题"),
                "content": item.get("content") or "",
            }
            summary = item.get("summary")
            if summary:
                node["summary"] = summary
            if item.get("parent_id"):
                node["parent_key"] = str(item["parent_id"])
            if item.get("id"):
                node["key"] = str(item["id"])
            structure.append(node)
            key = (kind, node["title"])
            seen_titles[key] = seen_titles.get(key, 0) + 1
            if seen_titles[key] > 1:
                warnings.append(f"检测到重复标题：{node['title']}")

        chapter_contents = data.get("chapter_contents") or []
        content_map: dict[str, str] = {}
        for cc in chapter_contents:
            if isinstance(cc, dict) and cc.get("outline_id"):
                content_map[str(cc["outline_id"])] = cc.get("content") or ""

        # 章节正文合并回 structure（按 outline_id key 匹配）
        for node in structure:
            key = node.get("key")
            if key and key in content_map:
                body = _check_chapter_length(content_map[key], node["title"], warnings)
                node["content"] = body

    # 人物
    characters: list[dict] = []
    for ch in data.get("characters") or []:
        if isinstance(ch, dict):
            characters.append({
                "name": _normalize_title(ch.get("name") or "未命名人物"),
                "identity": ch.get("identity"),
                "personality": ch.get("personality"),
                "appearance": ch.get("appearance"),
                "background": ch.get("background"),
                "goal": ch.get("goal"),
                "conflict": ch.get("conflict"),
                "relationship": ch.get("relationship"),
                "character_status": ch.get("character_status"),
            })

    # 地点
    locations: list[dict] = []
    for loc in data.get("locations") or []:
        if isinstance(loc, dict):
            locations.append({
                "name": _normalize_title(loc.get("name") or "未命名地点"),
                "category": loc.get("category"),
                "description": loc.get("description"),
                "importance": loc.get("importance") or "normal",
            })

    # 势力
    factions: list[dict] = []
    for f in data.get("factions") or []:
        if isinstance(f, dict):
            factions.append({
                "name": _normalize_title(f.get("name") or "未命名势力"),
                "description": f.get("description"),
                "leader": f.get("leader"),
                "goal": f.get("goal"),
            })

    # 世界观规则
    world_rules: list[dict] = []
    for r in data.get("world_rules") or []:
        if isinstance(r, dict):
            world_rules.append({
                "name": _normalize_title(r.get("name") or "未命名规则"),
                "category": r.get("category") or "rule",
                "content": r.get("content") or "",
                "importance": r.get("importance") or "normal",
            })

    # 时间线事件
    timeline_events: list[dict] = []
    for te in data.get("timeline_events") or []:
        if isinstance(te, dict):
            timeline_events.append({
                "title": _normalize_title(te.get("title") or "未命名事件"),
                "description": te.get("description"),
                "event_time": te.get("event_time"),
                "sort_order": te.get("sort_order") or 0,
                "importance": te.get("importance") or "normal",
            })

    # 人物关系
    relationships: list[dict] = []
    for rel in data.get("relationships") or []:
        if isinstance(rel, dict):
            relationships.append({
                "from_name": rel.get("from_name") or rel.get("from") or "",
                "to_name": rel.get("to_name") or rel.get("to") or "",
                "relation_type": rel.get("relation_type") or rel.get("type") or "related",
                "description": rel.get("description"),
            })

    # 知识条目（兼容字段）
    knowledge_items: list[dict] = []
    raw_knowledge = data.get("knowledge_items") or []
    for item in raw_knowledge:
        if not isinstance(item, dict):
            continue
        ktype = item.get("type") or item.get("kind") or "character"
        if ktype == "character":
            characters.append({
                "name": _normalize_title(item.get("name") or "未命名人物"),
                "identity": item.get("identity"),
                "personality": item.get("personality"),
                "background": item.get("background"),
                "goal": item.get("goal"),
                "conflict": item.get("conflict"),
                "relationship": item.get("relationship"),
            })
        else:
            world_rules.append({
                "name": _normalize_title(item.get("name") or "未命名条目"),
                "category": str(item.get("category") or "rule"),
                "content": item.get("content") or "",
                "importance": item.get("importance") or "normal",
            })
    for ws in data.get("world_settings") or []:
        if isinstance(ws, dict):
            world_rules.append({
                "name": _normalize_title(ws.get("name") or "未命名条目"),
                "category": str(ws.get("category") or "rule"),
                "content": ws.get("content") or "",
                "importance": ws.get("importance") or "normal",
            })

    # 标签
    tags: list[dict] = []
    for t in data.get("tags") or []:
        if isinstance(t, dict):
            tags.append({
                "name": _normalize_title(t.get("name") or ""),
                "color": t.get("color") or "#4F6EF7",
                "tag_type": t.get("tag_type"),
            })

    # 评论 / 版本信息（规格二：导入范围扩展）
    comments: list[dict] = []
    for c in data.get("comments") or []:
        if isinstance(c, dict):
            comments.append({
                "target_type": c.get("target_type") or "chapter",
                "target_title": c.get("target_title"),
                "content": c.get("content") or "",
                "type": c.get("type") or "comment",
                "author_name": c.get("author_name"),
            })
    versions: list[dict] = []
    for v in data.get("version_history") or data.get("versions") or []:
        if isinstance(v, dict):
            versions.append({
                "target_type": v.get("target_type") or "chapter",
                "target_title": v.get("target_title"),
                "version_number": v.get("version_number") or 1,
                "content": v.get("content") or "",
                "source": v.get("source") or "import",
            })

    empty_chapters = sum(1 for n in structure if n["kind"] == "chapter" and not n["content"])
    if empty_chapters:
        warnings.append(f"发现 {empty_chapters} 个没有正文的章节")

    return _assemble(
        structure, title, "json", warnings,
        characters=characters, locations=locations, factions=factions,
        world_rules=world_rules, timeline_events=timeline_events,
        relationships=relationships, tags=tags, comments=comments,
        versions=versions, root_type="project", schema_version=schema_version,
    )


def _version_greater(v: str, base: str) -> bool:
    try:
        parts_v = [int(x) for x in re.split(r"[^0-9]+", v) if x.isdigit()] or [0]
        parts_b = [int(x) for x in re.split(r"[^0-9]+", base) if x.isdigit()] or [0]
        return tuple(parts_v[:3]) > tuple(parts_b[:3])
    except Exception:
        return False


# ============ CSV 解析（规格一/二：表格数据） ============

_CSV_TYPE_KEYWORDS = {
    "characters": ["人物", "角色", "name", "姓名", "名字"],
    "locations": ["地点", "位置", "场所"],
    "factions": ["势力", "组织", "阵营"],
    "world_rules": ["规则", "设定", "世界观"],
    "timeline_events": ["时间线", "事件", "时间"],
    "relationships": ["关系", "from", "to"],
    "tags": ["标签", "tag"],
}

_CSV_FIELD_ALIASES = {
    "name": ["name", "名称", "名字", "姓名", "人物名", "角色名", "标题", "title", "事件", "标签名"],
    "identity": ["身份", "identity"],
    "personality": ["性格", "personality"],
    "appearance": ["外貌", "appearance"],
    "background": ["背景", "background", "简介"],
    "goal": ["目标", "goal"],
    "conflict": ["冲突", "conflict"],
    "relationship": ["关系", "relationship"],
    "description": ["描述", "说明", "description", "内容", "content", "详情"],
    "category": ["类别", "分类", "category", "类型", "type"],
    "importance": ["重要度", "importance", "重要程度"],
    "color": ["颜色", "color"],
    "tag_type": ["标签类型", "tag_type"],
    "leader": ["首领", "领袖", "leader"],
    "event_time": ["时间", "事件时间", "time", "日期"],
    "relation_type": ["关系类型", "relation_type", "关系", "type"],
    "from_name": ["from", "来源", "from_name", "角色A", "人物A"],
    "to_name": ["to", "目标", "to_name", "角色B", "人物B"],
}


def _pick_field(row: dict, field: str) -> str | None:
    aliases = _CSV_FIELD_ALIASES.get(field, [field])
    for alias in aliases:
        if alias in row:
            value = (row[alias] or "").strip()
            if value:
                return value
    return None


def _detect_csv_type(header: list[str], file_name: str) -> str:
    header_lower = [h.strip().lower() for h in header]
    joined = ",".join(header_lower) + "," + (file_name or "").lower()
    scores: dict[str, int] = {}
    for ctype, keywords in _CSV_TYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in joined:
                score += 1
        scores[ctype] = score
    # 特殊：relationships 需同时有 from/to 两个方向字段
    if ("from" in joined or "来源" in joined) and ("to" in joined or "目标" in joined):
        scores["relationships"] += 3
    # 人物特有字段（身份/外貌/性格）显著提升 characters，避免与 locations 混淆
    if any(kw in joined for kw in ("identity", "appearance", "personality", "身份", "外貌", "性格")):
        scores["characters"] += 2
    # 类别字段（category/类别/分类）是地点/规则表的典型特征，提升 locations
    if any(kw in joined for kw in ("category", "类别", "分类")):
        scores["locations"] += 2
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "characters"


def parse_csv(content: str, file_name: str) -> dict:
    """CSV 表格导入：人物/地点/势力/世界观规则/时间线/关系/标签。"""
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValueError("CSV 导入失败：无法识别表头（需要至少一列表头）")
    header = [h.strip() for h in reader.fieldnames if h is not None]

    warnings: list[str] = []
    ctype = _detect_csv_type(header, file_name)
    characters: list[dict] = []
    locations: list[dict] = []
    factions: list[dict] = []
    world_rules: list[dict] = []
    timeline_events: list[dict] = []
    relationships: list[dict] = []
    tags: list[dict] = []
    unrecognized: list[dict] = []

    row_no = 0
    for raw in reader:
        row_no += 1
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        try:
            if ctype == "locations":
                name = _pick_field(row, "name")
                if not name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少名称"})
                    continue
                locations.append({
                    "name": _normalize_title(name),
                    "category": _pick_field(row, "category"),
                    "description": _pick_field(row, "description"),
                    "importance": _pick_field(row, "importance") or "normal",
                })
            elif ctype == "factions":
                name = _pick_field(row, "name")
                if not name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少名称"})
                    continue
                factions.append({
                    "name": _normalize_title(name),
                    "description": _pick_field(row, "description"),
                    "leader": _pick_field(row, "leader"),
                    "goal": _pick_field(row, "goal"),
                })
            elif ctype == "world_rules":
                name = _pick_field(row, "name")
                if not name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少名称"})
                    continue
                world_rules.append({
                    "name": _normalize_title(name),
                    "category": _pick_field(row, "category") or "rule",
                    "content": _pick_field(row, "description") or "",
                    "importance": _pick_field(row, "importance") or "normal",
                })
            elif ctype == "timeline_events":
                title = _pick_field(row, "name")
                if not title:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少事件标题"})
                    continue
                timeline_events.append({
                    "title": _normalize_title(title),
                    "description": _pick_field(row, "description"),
                    "event_time": _pick_field(row, "event_time"),
                    "sort_order": row_no,
                    "importance": _pick_field(row, "importance") or "normal",
                })
            elif ctype == "relationships":
                from_name = _pick_field(row, "from_name")
                to_name = _pick_field(row, "to_name")
                if not from_name or not to_name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "关系缺少一端人物"})
                    continue
                relationships.append({
                    "from_name": _normalize_title(from_name),
                    "to_name": _normalize_title(to_name),
                    "relation_type": _pick_field(row, "relation_type") or "related",
                    "description": _pick_field(row, "description"),
                })
            elif ctype == "tags":
                name = _pick_field(row, "name")
                if not name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少标签名"})
                    continue
                tags.append({
                    "name": _normalize_title(name),
                    "color": _pick_field(row, "color") or "#4F6EF7",
                    "tag_type": _pick_field(row, "tag_type"),
                })
            else:
                name = _pick_field(row, "name")
                if not name:
                    unrecognized.append({"item": f"CSV 第 {row_no} 行", "message": "缺少人物名"})
                    continue
                characters.append({
                    "name": _normalize_title(name),
                    "identity": _pick_field(row, "identity"),
                    "personality": _pick_field(row, "personality"),
                    "appearance": _pick_field(row, "appearance"),
                    "background": _pick_field(row, "background"),
                    "goal": _pick_field(row, "goal"),
                    "conflict": _pick_field(row, "conflict"),
                    "relationship": _pick_field(row, "relationship"),
                })
        except Exception as error:  # 单行失败不中断（规格七）
            warnings.append(f"CSV 第 {row_no} 行解析失败：{error}")

    if unrecognized:
        warnings.append(f"CSV 存在 {len(unrecognized)} 行无法识别内容")

    return _assemble(
        [], "", "csv", warnings,
        characters=characters, locations=locations, factions=factions,
        world_rules=world_rules, timeline_events=timeline_events,
        relationships=relationships, tags=tags,
        root_type="knowledge",
        unrecognized=unrecognized,
    )


# ============ 统一组装 ============

def _assemble(
    structure: list[dict],
    title: str,
    file_type: str,
    warnings: list[str],
    characters: list[dict] | None = None,
    locations: list[dict] | None = None,
    factions: list[dict] | None = None,
    world_rules: list[dict] | None = None,
    timeline_events: list[dict] | None = None,
    relationships: list[dict] | None = None,
    tags: list[dict] | None = None,
    comments: list[dict] | None = None,
    versions: list[dict] | None = None,
    knowledge_items: list[dict] | None = None,
    root_type: str = "outline",
    schema_version: str | None = None,
    unrecognized: list[dict] | None = None,
) -> dict:
    # 长度限制（章节正文）
    empty_chapters = 0
    for node in structure:
        node["content"] = _check_chapter_length(node["content"], node["title"], warnings)
        if node["kind"] == "chapter" and not node["content"].strip():
            empty_chapters += 1
    if empty_chapters:
        warnings.append(f"发现 {empty_chapters} 个没有正文的章节")

    total = (
        len(structure)
        + len(characters or [])
        + len(locations or [])
        + len(factions or [])
        + len(world_rules or [])
        + len(timeline_events or [])
        + len(relationships or [])
        + len(tags or [])
        + len(comments or [])
        + len(versions or [])
    )
    _check_items_limit(total)

    volume_count = sum(1 for n in structure if n["kind"] == "volume")
    chapter_count = sum(1 for n in structure if n["kind"] == "chapter")
    estimated_word_count = sum(len((n.get("content") or "")) for n in structure)
    for extra in (
        characters or [], locations or [], factions or [], world_rules or [],
        timeline_events or [], relationships or [], tags or [],
    ):
        estimated_word_count += sum(
            len(str(v or "")) for v in extra.values()
        ) if isinstance(extra, dict) else 0

    duplicates = _detect_duplicates(structure, warnings)

    return {
        "title": title,
        "file_type": file_type,
        "root_type": root_type,
        "structure": structure,
        "characters": characters or [],
        "locations": locations or [],
        "factions": factions or [],
        "world_rules": world_rules or [],
        "timeline_events": timeline_events or [],
        "relationships": relationships or [],
        "tags": tags or [],
        "comments": comments or [],
        "versions": versions or [],
        "knowledge_items": knowledge_items or [],
        "warnings": warnings,
        "unrecognized": unrecognized or [],
        "duplicates": duplicates,
        "total_items": total,
        "volume_count": volume_count,
        "chapter_count": chapter_count,
        "estimated_word_count": estimated_word_count,
        "schema_version": schema_version,
    }


def parse_import(
    file_name: str, file_type: str, content: str,
    split_mode: str = "chapter",
) -> dict:
    """按 file_type 分发解析；返回统一 parsed 结构。非法类型/超限抛 ValueError。"""
    _check_length(content)
    if file_type == "markdown":
        return parse_markdown(content, file_name, split_mode)
    if file_type == "text":
        return parse_text(content, file_name, split_mode)
    if file_type == "html":
        return parse_html(content, file_name, split_mode)
    if file_type == "json":
        return parse_json(content, file_name, split_mode)
    if file_type == "csv":
        return parse_csv(content, file_name)
    raise ValueError(f"不支持的导入格式：{file_type}")


def strip_script_tags(text: str) -> str:
    """剥除脚本/样式块，防止脚本注入（导入阶段兜底）。"""
    return _SCRIPT_STYLE_RE.sub("", text or "")
