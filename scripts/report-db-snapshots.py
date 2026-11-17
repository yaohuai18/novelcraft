#!/usr/bin/env python3
"""NovelCraft 数据库快照清单报告（只读）。

本工具只读取快照目录的文件系统元数据与 manifest.json 的允许字段，
输出 KEEP / REVIEW / DELETE_CANDIDATE 分类，供人工复核。

设计边界：
- 没有删除、移动、复制、重命名或写入能力；
- 不打开任何 SQLite 数据库，不执行 integrity_check/quick_check/backup；
- 不计算任何文件哈希；
- 不读取 .env、凭据、日志正文或业务数据；
- 不跟随目录符号链接、junction 或 reparse point；
- 不创建目录、不创建计划任务。

退出码：
  0  报告成功（含存在 REVIEW / DELETE_CANDIDATE 的情况）
  2  参数、root 路径不合法或无法安全扫描
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_DIR_RE = re.compile(r"^novelcraft-db-(\d{8}-\d{6})-([0-9a-f]{7})$")
EXPECTED_FILES = ("manifest.json", "novelcraft.db")
SNAPSHOT_FILE = "novelcraft.db"
SNAPSHOT_METHOD = "sqlite-backup"

# 只投影这些字段；backup_sha256 与任何未知字段一律不读取、不输出。
MANIFEST_FIELD_TYPES: dict[str, type] = {
    "created_at": str,
    "head": str,
    "head_short": str,
    "source_path": str,
    "snapshot_file": str,
    "snapshot_size": int,
    "method": str,
    "service_stopped": bool,
    "integrity_check": str,
    "quick_check": str,
    "tool_version": str,
    "label": str,
}

KEEP = "KEEP"
REVIEW = "REVIEW"
DELETE_CANDIDATE = "DELETE_CANDIDATE"

DEFAULT_KEEP = 10
DEFAULT_MAX_AGE_DAYS = 180

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ReportError(Exception):
    """参数或 root 路径层面的错误，转为退出码 2。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _is_remote_path(raw: str) -> bool:
    r"""判断 UNC / 远程路径；\\?\C: 这类本地长路径不算远程。"""
    if raw.startswith("\\\\?\\UNC\\"):
        return True
    if raw.startswith("\\\\?\\"):
        return False
    if raw.startswith("\\\\.\\"):
        return True
    return raw.startswith("\\\\") or raw.startswith("//")


def _is_link(entry: os.DirEntry[str]) -> bool:
    """符号链接、junction、其他 reparse point 一律视为链接，不跟随。"""
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return True
    if os.name == "nt":
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            return True
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scan_entries(directory: Path) -> dict[str, int]:
    """返回 name -> size（目录记为 0，链接/不可读记为 -1）。"""
    sizes: dict[str, int] = {}
    with os.scandir(directory) as it:
        for entry in it:
            if _is_link(entry):
                sizes[f"<link> {entry.name}"] = -1
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                sizes[f"<unreadable> {entry.name}"] = -1
                continue
            sizes[entry.name] = 0 if entry.is_dir(follow_symlinks=False) else int(st.st_size)
    return sizes


def _is_temp_name(name: str) -> bool:
    return (
        name.startswith(".novelcraft.db.tmp-")
        or name.endswith("-wal")
        or name.endswith("-shm")
        or name.endswith("-journal")
        or ".tmp-" in name
    )


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, ["manifest_missing"]
    except OSError:
        return None, ["manifest_unreadable"]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, ["manifest_invalid_json"]
    if not isinstance(data, dict):
        return None, ["manifest_not_object"]
    return data, []


def _contract_issues(manifest: dict[str, Any] | None, sizes: dict[str, int]) -> list[str]:
    issues: list[str] = []
    if manifest is None:
        return issues

    for field, expected_type in MANIFEST_FIELD_TYPES.items():
        if field not in manifest:
            issues.append(f"manifest_missing_{field}")
            continue
        value = manifest[field]
        if expected_type is int:
            if not _is_int(value):
                issues.append(f"manifest_type_{field}")
        elif not isinstance(value, expected_type):
            issues.append(f"manifest_type_{field}")

    if _parse_created_at(manifest.get("created_at")) is None:
        issues.append("created_at_unparseable")

    if manifest.get("snapshot_file") != SNAPSHOT_FILE:
        issues.append("snapshot_file_mismatch")
    if manifest.get("method") != SNAPSHOT_METHOD:
        issues.append("method_mismatch")

    integrity = manifest.get("integrity_check")
    quick = manifest.get("quick_check")
    if integrity != "ok":
        issues.append("integrity_not_ok")
    if quick != "ok":
        issues.append("quick_check_not_ok")

    actual_size = sizes.get(SNAPSHOT_FILE)
    if actual_size is None:
        issues.append("snapshot_file_missing")
    elif _is_int(manifest.get("snapshot_size")) and actual_size != manifest["snapshot_size"]:
        issues.append("snapshot_size_mismatch")

    return issues


def _build_item(directory: Path, name: str) -> dict[str, Any]:
    sizes = _scan_entries(directory)
    file_names = sorted(sizes)
    extra_files = [n for n in file_names if n not in EXPECTED_FILES]
    contains_temp = any(_is_temp_name(n) for n in extra_files)

    manifest_path = directory / "manifest.json"
    manifest, manifest_issues = _load_manifest(manifest_path)
    issues = list(manifest_issues)
    if manifest is not None:
        issues.extend(_contract_issues(manifest, sizes))
    if extra_files:
        issues.append("extra_files")
    if contains_temp:
        issues.append("temp_sidecars")

    manifest_valid = manifest is not None and not manifest_issues
    created_raw = manifest.get("created_at") if manifest else None
    created = _parse_created_at(created_raw)
    snapshot_size_actual = sizes.get(SNAPSHOT_FILE)

    return {
        "directory_name": name,
        "created_at": created_raw if isinstance(created_raw, str) else None,
        "head": manifest.get("head") if manifest else None,
        "head_short": manifest.get("head_short") if manifest else None,
        "snapshot_file": manifest.get("snapshot_file") if manifest else None,
        "snapshot_size_manifest": manifest.get("snapshot_size") if manifest else None,
        "snapshot_size_actual": snapshot_size_actual,
        "age_days": None,
        "method": manifest.get("method") if manifest else None,
        "integrity_check": manifest.get("integrity_check") if manifest else None,
        "quick_check": manifest.get("quick_check") if manifest else None,
        "manifest_valid": manifest_valid,
        "contract_issues": issues,
        "extra_files": extra_files,
        "contains_temp_sidecars": contains_temp,
        "classification": REVIEW,
        "_created": created,
    }


def _classify(items: list[dict[str, Any]], keep: int, max_age_days: int) -> None:
    valid = [it for it in items if it["_created"] is not None and not it["contract_issues"]]
    valid.sort(key=lambda it: it["_created"], reverse=True)
    rank = {id(it): idx for idx, it in enumerate(valid)}
    total = len(items)

    for it in items:
        created = it["_created"]
        issues = it["contract_issues"]
        if created is None or issues:
            it["age_days"] = None if created is None else round(
                (datetime.now(timezone.utc) - created).total_seconds() / 86400.0, 2
            )
            it["classification"] = REVIEW
            continue
        age = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
        it["age_days"] = round(age, 2)
        if total <= keep:
            it["classification"] = KEEP
        elif rank[id(it)] < keep:
            it["classification"] = KEEP
        elif age <= max_age_days:
            it["classification"] = KEEP
        else:
            it["classification"] = DELETE_CANDIDATE


def _root_label(root: Path) -> str:
    """root 只以目录名出现，不输出盘符、用户目录或绝对路径。"""
    name = root.name
    return name if name else "root"


def _display_head(item: dict[str, Any]) -> str:
    """head_short/head 都不是字符串时显示 '-'，避免单个损坏 manifest 打断整份文本报告。"""
    for key in ("head_short", "head"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value[:7]
    return "-"


def build_report(root: Path, *, keep: int, max_age_days: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    ignored = 0
    skipped_links = 0

    if root.exists() and not root.is_dir():
        raise ReportError("root 不是目录")
    if not root.exists():
        return _empty_report(root, keep, max_age_days, note="root 不存在")

    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        raise ReportError(f"无法扫描 root: {type(exc).__name__}") from exc

    for entry in entries:
        if _is_link(entry):
            skipped_links += 1
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                ignored += 1
                continue
        except OSError:
            ignored += 1
            continue
        if not SNAPSHOT_DIR_RE.match(entry.name):
            ignored += 1
            continue
        try:
            items.append(_build_item(Path(entry.path), entry.name))
        except OSError:
            items.append(
                {
                    "directory_name": entry.name,
                    "created_at": None,
                    "head": None,
                    "head_short": None,
                    "snapshot_file": None,
                    "snapshot_size_manifest": None,
                    "snapshot_size_actual": None,
                    "age_days": None,
                    "method": None,
                    "integrity_check": None,
                    "quick_check": None,
                    "manifest_valid": False,
                    "contract_issues": ["directory_unreadable"],
                    "extra_files": [],
                    "contains_temp_sidecars": False,
                    "classification": REVIEW,
                    "_created": None,
                }
            )

    _classify(items, keep, max_age_days)

    for it in items:
        it.pop("_created", None)

    items.sort(key=lambda it: (it["created_at"] or "", it["directory_name"]), reverse=True)
    counts = {
        "total": len(items),
        "keep": sum(1 for it in items if it["classification"] == KEEP),
        "review": sum(1 for it in items if it["classification"] == REVIEW),
        "delete_candidate": sum(1 for it in items if it["classification"] == DELETE_CANDIDATE),
        "ignored_entries": ignored,
        "skipped_links": skipped_links,
    }
    return {
        "root": _root_label(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {"keep": keep, "max_age_days": max_age_days},
        "items": items,
        "summary": counts,
        "note": "本报告只读，不删除任何文件；DELETE_CANDIDATE 仅表示可人工复核，必须另行授权。",
    }


def _empty_report(root: Path, keep: int, max_age_days: int, *, note: str) -> dict[str, Any]:
    return {
        "root": _root_label(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {"keep": keep, "max_age_days": max_age_days},
        "items": [],
        "summary": {
            "total": 0,
            "keep": 0,
            "review": 0,
            "delete_candidate": 0,
            "ignored_entries": 0,
            "skipped_links": 0,
        },
        "note": f"{note}；本报告只读，不删除任何文件。",
    }


def _format_text(report: dict[str, Any]) -> str:
    lines = [
        "NovelCraft 数据库快照清单（只读报告，不删除任何文件）",
        f"root: {report['root']}",
        f"generated_at: {report['generated_at']}",
        f"keep={report['thresholds']['keep']} max_age_days={report['thresholds']['max_age_days']}",
        "",
        f"{'目录':<44} {'created_at':<26} {'HEAD':<9} {'大小(MB)':>9} {'年龄(天)':>8} {'契约':<6} 分类",
    ]
    for it in report["items"]:
        size_mb = "-"
        if _is_int(it["snapshot_size_actual"]):
            size_mb = f"{it['snapshot_size_actual'] / 1048576:.1f}"
        age = "-" if it["age_days"] is None else f"{it['age_days']:.1f}"
        contract = "OK" if not it["contract_issues"] else "异常"
        head = _display_head(it)
        lines.append(
            f"{it['directory_name']:<44} {str(it['created_at']):<26} {head:<9} "
            f"{size_mb:>9} {age:>8} {contract:<6} {it['classification']}"
        )
        if it["contract_issues"]:
            lines.append(f"    契约问题: {', '.join(it['contract_issues'])}")
        if it["extra_files"]:
            lines.append(f"    额外文件: {', '.join(it['extra_files'])}")
    s = report["summary"]
    lines.extend(
        [
            "",
            f"合计: {s['total']} | KEEP={s['keep']} REVIEW={s['review']} "
            f"DELETE_CANDIDATE={s['delete_candidate']} | 忽略={s['ignored_entries']} "
            f"链接跳过={s['skipped_links']}",
            report["note"],
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="report-db-snapshots.py",
        description=(
            "只读汇报数据库快照清单与保留建议；不删除、不移动、不写入，不打开数据库。"
        ),
    )
    parser.add_argument(
        "--root",
        default=str(Path.home() / "NovelCraft-backups" / "database"),
        help="快照根目录（默认：用户主目录下 NovelCraft-backups/database）",
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="最近 N 个视为保留（默认 10）")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help="年龄不超过该天数的快照视为保留（默认 180）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.keep < 0:
        print("参数错误: --keep 不能为负数", file=sys.stderr)
        return 2
    if args.max_age_days < 0:
        print("参数错误: --max-age-days 不能为负数", file=sys.stderr)
        return 2
    if _is_remote_path(str(args.root)):
        print("参数错误: root 不能是 UNC 或远程路径", file=sys.stderr)
        return 2

    root = Path(args.root)
    try:
        report = build_report(root, keep=args.keep, max_age_days=args.max_age_days)
    except ReportError as exc:
        print(f"错误: {exc.message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
