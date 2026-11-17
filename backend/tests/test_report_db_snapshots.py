"""只读快照清单报告工具（scripts/report-db-snapshots.py）的契约测试。

全部使用 tmp_path 下的合成目录，不引用真实快照根、不打开任何数据库。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "report-db-snapshots.py"


def _load_tool_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("report_db_snapshots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _manifest(**overrides):
    data = {
        "backup_sha256": "SHOULD-NOT-LEAK",
        "created_at": _iso(1),
        "head": "abcdef0123456789",
        "head_short": "abcdef0",
        "integrity_check": "ok",
        "label": "synthetic",
        "method": "sqlite-backup",
        "quick_check": "ok",
        "service_pid_after": None,
        "service_pid_before": None,
        "service_stopped": False,
        "snapshot_file": "novelcraft.db",
        "snapshot_size": 1024,
        "source_path": "backend/novelcraft.db",
        "tool_version": "1.0.0",
        "unknown_secret": "MUST-NOT-LEAK",
    }
    data.update(overrides)
    return data


def _make_snapshot(
    root: Path,
    name: str,
    *,
    size: int = 1024,
    manifest: dict | None = None,
    manifest_text: str | None = None,
    with_db: bool = True,
    db_name: str = "novelcraft.db",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    if with_db:
        payload = b"\x00" * size
        (directory / db_name).write_bytes(payload)
    if manifest_text is not None:
        (directory / "manifest.json").write_text(manifest_text, encoding="utf-8")
    elif manifest is not None:
        (directory / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    return directory


def _valid_name(index: int, *, days_ago: float | None = None, head_short: str = "abcdef0") -> str:
    stamp = (datetime(2026, 1, 1, 0, 0, index) if days_ago is None else datetime.now() - timedelta(days=days_ago))
    return f"novelcraft-db-{stamp.strftime('%Y%m%d-%H%M%S')}-{head_short}"


def _make_dir_link(link: Path, target: Path) -> str:
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return "junction"
    pytest.skip("neither symlink nor junction could be created in this environment")


def test_missing_root_returns_empty_report_and_creates_nothing(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "absent"
    report = module.build_report(root, keep=10, max_age_days=180)

    assert report["items"] == []
    assert report["summary"]["total"] == 0
    assert "不删除任何文件" in report["note"]
    assert not root.exists(), "报告不得创建 root 目录"


def test_root_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root.txt"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(module.ReportError):
        module.build_report(root, keep=10, max_age_days=180)
    assert module.main(["--root", str(root)]) == 2


def test_unc_root_is_rejected_without_scanning() -> None:
    module = _load_tool_module()
    assert module.main(["--root", r"\\server\share\database"]) == 2


def test_only_direct_subdirectories_are_scanned(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    root.mkdir()
    outer = _make_snapshot(root, "novelcraft-db-20260101-000001-abcdef0", manifest=_manifest())
    nested = outer / "nested" / "novelcraft-db-20260101-000002-abcdef0"
    nested.mkdir(parents=True)

    report = module.build_report(root, keep=10, max_age_days=180)

    assert [it["directory_name"] for it in report["items"]] == ["novelcraft-db-20260101-000001-abcdef0"]
    assert "nested" in report["items"][0]["extra_files"]


def test_invalid_directory_names_are_ignored(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    for name in ("random", "novelcraft-db-bad", "novelcraft-db-20260101-000000-ABCDEF0"):
        (root / name).mkdir(parents=True)
    (root / "not-a-dir.txt").write_text("x", encoding="utf-8")

    report = module.build_report(root, keep=10, max_age_days=180)

    assert report["items"] == []
    assert report["summary"]["ignored_entries"] == 4


def test_linked_directories_are_not_followed(tmp_path: Path) -> None:
    module = _load_tool_module()
    target = tmp_path / "outside" / "novelcraft-db-20260101-000001-abcdef0"
    _make_snapshot(target.parent, target.name, manifest=_manifest())
    root = tmp_path / "root"
    root.mkdir()
    _make_dir_link(root / "novelcraft-db-20260101-000002-abcdef0", target)

    report = module.build_report(root, keep=10, max_age_days=180)

    assert report["items"] == []
    assert report["summary"]["skipped_links"] == 1


def test_manifest_fields_are_projected_without_unknown_fields(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(created_at=_iso(2), label="alpha"),
    )

    report = module.build_report(root, keep=10, max_age_days=180)
    item = report["items"][0]

    assert item["directory_name"] == "novelcraft-db-20260101-000001-abcdef0"
    assert item["created_at"] == _iso(2)
    assert item["head"] == "abcdef0123456789"
    assert item["head_short"] == "abcdef0"
    assert item["snapshot_file"] == "novelcraft.db"
    assert item["snapshot_size_manifest"] == 1024
    assert item["snapshot_size_actual"] == 1024
    assert item["method"] == "sqlite-backup"
    assert item["integrity_check"] == "ok"
    assert item["quick_check"] == "ok"
    assert item["manifest_valid"] is True
    assert item["contract_issues"] == []
    assert item["extra_files"] == []
    assert item["contains_temp_sidecars"] is False
    assert item["classification"] == "KEEP"
    assert "backup_sha256" not in item
    assert "unknown_secret" not in item


def test_manifest_with_utf8_bom_is_accepted(tmp_path: Path) -> None:
    """Windows 工具可能写出带 BOM 的 UTF-8；manifest 读取必须容忍 BOM。"""
    module = _load_tool_module()
    root = tmp_path / "root"
    directory = _make_snapshot(root, "novelcraft-db-20260101-000001-abcdef0")
    payload = json.dumps(_manifest()).encode("utf-8")
    (directory / "manifest.json").write_bytes(b"\xef\xbb\xbf" + payload)

    report = module.build_report(root, keep=10, max_age_days=180)
    item = report["items"][0]

    assert item["manifest_valid"] is True
    assert item["contract_issues"] == []
    assert item["classification"] == "KEEP"


def test_json_output_excludes_hash_and_unknown_fields(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(label="beta"),
    )

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(["--root", str(root), "--json"])
    assert code == 0

    payload = buffer.getvalue()
    assert "backup_sha256" not in payload
    assert "unknown_secret" not in payload
    parsed = json.loads(payload)
    assert parsed["thresholds"] == {"keep": 10, "max_age_days": 180}
    assert set(parsed["summary"]) >= {"total", "keep", "review", "delete_candidate"}
    assert "label" not in parsed["items"][0]
    assert parsed["items"][0]["classification"] == "KEEP"


def test_corrupt_manifest_is_review_and_other_items_continue(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest_text="{ not json",
    )
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000002-abcdef0",
        manifest=_manifest(),
    )

    report = module.build_report(root, keep=10, max_age_days=180)
    by_name = {it["directory_name"]: it for it in report["items"]}

    assert report["summary"]["total"] == 2
    corrupt = by_name["novelcraft-db-20260101-000001-abcdef0"]
    assert corrupt["manifest_valid"] is False
    assert "manifest_invalid_json" in corrupt["contract_issues"]
    assert corrupt["classification"] == "REVIEW"
    assert by_name["novelcraft-db-20260101-000002-abcdef0"]["classification"] == "KEEP"


def test_missing_snapshot_size_mismatch_and_missing_manifest_are_review(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"

    _make_snapshot(root, "novelcraft-db-20260101-000001-abcdef0", manifest=_manifest(), with_db=False)
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000002-abcdef0",
        size=999,
        manifest=_manifest(snapshot_size=1024),
    )
    _make_snapshot(root, "novelcraft-db-20260101-000003-abcdef0", with_db=True, manifest=None)

    report = module.build_report(root, keep=10, max_age_days=180)
    by_name = {it["directory_name"]: it for it in report["items"]}

    assert "snapshot_file_missing" in by_name["novelcraft-db-20260101-000001-abcdef0"]["contract_issues"]
    assert "snapshot_size_mismatch" in by_name["novelcraft-db-20260101-000002-abcdef0"]["contract_issues"]
    assert "manifest_missing" in by_name["novelcraft-db-20260101-000003-abcdef0"]["contract_issues"]
    assert all(it["classification"] == "REVIEW" for it in report["items"])


def test_temp_sidecars_are_detected_and_force_review(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    directory = _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(),
    )
    (directory / "novelcraft.db-wal").write_bytes(b"")
    (directory / ".novelcraft.db.tmp-123-shm").write_bytes(b"")
    (directory / "novelcraft.db-journal").write_bytes(b"")

    report = module.build_report(root, keep=10, max_age_days=180)
    item = report["items"][0]

    assert item["contains_temp_sidecars"] is True
    assert "temp_sidecars" in item["contract_issues"]
    assert item["classification"] == "REVIEW"
    assert set(item["extra_files"]) >= {"novelcraft.db-wal", "novelcraft.db-journal"}


def test_keep_and_age_boundaries(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    # 最近 10 个（1..10 天前），再加两个超过 180 天的。
    for index in range(1, 11):
        _make_snapshot(
            root,
            _valid_name(index, days_ago=index),
            manifest=_manifest(created_at=_iso(index)),
        )
    for index, days in ((11, 200), (12, 210)):
        _make_snapshot(
            root,
            _valid_name(index, days_ago=days),
            manifest=_manifest(created_at=_iso(days)),
        )

    report = module.build_report(root, keep=10, max_age_days=180)
    by_age = sorted(report["items"], key=lambda it: it["age_days"])

    assert report["summary"]["total"] == 12
    assert all(it["classification"] == "KEEP" for it in by_age[:10])
    assert [it["classification"] for it in by_age[10:]] == ["DELETE_CANDIDATE", "DELETE_CANDIDATE"]
    assert report["summary"]["delete_candidate"] == 2


def test_no_delete_candidate_when_total_not_above_keep(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    for index in range(1, 4):
        _make_snapshot(
            root,
            _valid_name(index, days_ago=200 + index),
            manifest=_manifest(created_at=_iso(200 + index)),
        )

    report = module.build_report(root, keep=10, max_age_days=180)

    assert report["summary"]["delete_candidate"] == 0
    assert all(it["classification"] == "KEEP" for it in report["items"])


def test_unparseable_created_at_is_review_and_never_candidate(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    for index in range(1, 13):
        _make_snapshot(
            root,
            _valid_name(index, days_ago=index),
            manifest=_manifest(created_at=_iso(index)),
        )
    _make_snapshot(
        root,
        "novelcraft-db-20260101-235959-abcdef0",
        manifest=_manifest(created_at="not-a-date"),
    )

    report = module.build_report(root, keep=10, max_age_days=180)
    bad = next(it for it in report["items"] if it["created_at"] == "not-a-date")

    assert bad["classification"] == "REVIEW"
    assert "created_at_unparseable" in bad["contract_issues"]


def test_report_run_does_not_modify_fixtures(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    directory = _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(),
    )
    before = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }
    manifest_before = (directory / "manifest.json").read_bytes()

    module.build_report(root, keep=10, max_age_days=180)

    after = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }
    assert before == after
    assert (directory / "manifest.json").read_bytes() == manifest_before


def test_script_has_no_destructive_or_write_api() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        "os.remove",
        "os.unlink",
        "shutil.rmtree",
        "shutil.move",
        "os.rename",
        "os.replace",
        "os.makedirs",
        "mkdir(",
        "write_text",
        "write_bytes",
        "--delete",
        "--remove",
        "--clean",
        "sqlite3.connect",
    )
    for token in forbidden:
        assert token not in source, f"脚本不应包含 {token!r}"


def test_cli_help_lists_read_only_options() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "--root" in proc.stdout
    assert "--keep" in proc.stdout
    assert "--max-age-days" in proc.stdout
    assert "--json" in proc.stdout
    assert "--delete" not in proc.stdout


def test_json_mode_reports_root_and_counts(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(),
    )

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert module.main(["--root", str(root), "--json"]) == 0

    raw = buffer.getvalue()
    payload = json.loads(raw)
    assert payload["root"] == root.name
    assert str(root) not in raw
    assert str(root.parent) not in raw
    assert "\\\\" not in raw
    username = os.environ.get("USERNAME") or ""
    if username:
        assert username not in raw
    assert payload["summary"]["total"] == 1
    assert payload["items"][0]["classification"] == "KEEP"
    assert "generated_at" in payload

    text_buffer = io.StringIO()
    with contextlib.redirect_stdout(text_buffer):
        assert module.main(["--root", str(root)]) == 0
    text = text_buffer.getvalue()
    assert f"root: {root.name}" in text
    assert str(root) not in text
    assert str(root.parent) not in text


def test_malformed_head_type_does_not_break_text_report(tmp_path: Path) -> None:
    module = _load_tool_module()
    root = tmp_path / "root"
    broken = _make_snapshot(
        root,
        "novelcraft-db-20260101-000001-abcdef0",
        manifest=_manifest(head=123, head_short=None),
    )
    _make_snapshot(
        root,
        "novelcraft-db-20260101-000002-abcdef0",
        manifest=_manifest(),
    )

    report = module.build_report(root, keep=10, max_age_days=180)
    broken_item = next(it for it in report["items"] if it["directory_name"] == broken.name)
    assert broken_item["classification"] == "REVIEW"
    assert "manifest_type_head" in broken_item["contract_issues"]
    assert module._display_head(broken_item) == "-"

    import contextlib
    import io

    text_buffer = io.StringIO()
    with contextlib.redirect_stdout(text_buffer):
        assert module.main(["--root", str(root)]) == 0
    text = text_buffer.getvalue()
    assert broken.name in text
    assert "novelcraft-db-20260101-000002-abcdef0" in text
    assert "REVIEW" in text
    assert "KEEP" in text

    json_buffer = io.StringIO()
    with contextlib.redirect_stdout(json_buffer):
        assert module.main(["--root", str(root), "--json"]) == 0
    parsed = json.loads(json_buffer.getvalue())
    assert any(it["classification"] == "REVIEW" for it in parsed["items"])
