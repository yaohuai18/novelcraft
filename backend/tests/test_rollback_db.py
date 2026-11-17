"""Synthetic tests for scripts/rollback-db.py (read-only inventory, v1).

Every fixture lives under pytest's tmp_path. Real operations records,
the formal database, snapshots and staging directories are never read.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rollback-db.py"

OP = "20261101-120000-abc123"
OP2 = "20261101-130000-def456"
OP3 = "20261101-140000-aaa111"

STOPPED = lambda: {
    "backend_running": False,
    "port_8000_listening": False,
    "db_access_process": False,
}


@pytest.fixture(scope="module")
def mod():
    assert SCRIPT.is_file(), f"missing tool: {SCRIPT}"
    spec = importlib.util.spec_from_file_location("rollback_db_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch, mod):
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    ops = tmp_path / "NovelCraft-backups" / "operations"
    ops.mkdir(parents=True)
    (backend / "novelcraft.db").write_bytes(b"LIVE-DB")
    monkeypatch.setattr(mod, "_expected_operations_root", lambda: ops)
    return SimpleNamespace(repo=repo, backend=backend, ops=ops, tmp=tmp_path)


def canon(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(env, rel: str, data: bytes = b"X") -> Path:
    path = env.backend / rel
    path.write_bytes(data)
    return path


def _role_for_name(name: str, op: str) -> str | None:
    table = {
        "novelcraft.db": "main",
        "novelcraft.db-wal": "wal",
        "novelcraft.db-shm": "shm",
        "novelcraft.db-journal": "journal",
        f"novelcraft.db.stale-{op}": "stale_main",
        f"novelcraft.db-wal.stale-{op}": "stale_wal",
        f"novelcraft.db-shm.stale-{op}": "stale_shm",
        f"novelcraft.db-journal.stale-{op}": "stale_journal",
        f"novelcraft.db.failed-{op}": "failed_new",
        f"novelcraft.db-wal.failed-{op}": "failed_wal",
        f"novelcraft.db-shm.failed-{op}": "failed_shm",
        f"novelcraft.db-journal.failed-{op}": "failed_journal",
        f"novelcraft.db.restore-{op}.tmp": "temp_restore",
    }
    return table.get(name)


def _scene_files(env, op: str) -> list:
    items = []
    for path in sorted(env.backend.iterdir()):
        role = _role_for_name(path.name, op)
        if role is None:
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            continue
        live = role in {"main", "wal", "shm", "journal"}
        if live:
            digest = None
        else:
            try:
                digest = sha(path.read_bytes())
            except OSError:
                continue
        items.append(
            {
                "role": role,
                "original_path": path.name,
                "actual_path": path.name,
                "exists": True,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "sha256": digest,
            }
        )
    return items


def record(
    env,
    op: str = OP,
    *,
    state: str = "S1",
    step: int = 7,
    files: list | None = None,
    extra: dict | None = None,
    hash_ok: bool = True,
    schema: bool = True,
    op_field: str | None = None,
    pre_rollback: dict | None = None,
) -> dict:
    if files is None:
        files = _scene_files(env, op)
    rec = {
        "record_schema_version": "1.0",
        "operation_id": op_field if op_field is not None else op,
        "created_at": "2026-11-02T00:00:00+08:00",
        "operator": "fixture",
        "authorization_ref": "fixture-auth",
        "current_step": step,
        "step_status": "failed",
        "state": state,
        "head_at_start": "5913343",
        "source_snapshot_dir": "novelcraft-db-20261101-142559-e7ed8fb",
        "pre_restore_snapshot_dir": None,
        "files": files or [],
        "groups": {},
        "renames": [],
        "failures": [],
        "pre_rollback": pre_rollback,
        "tool_version": "1.0.0",
    }
    if not schema:
        rec.pop("files")
    if extra:
        rec.update(extra)
    raw = canon(rec)
    (env.ops / f"{op}.json").write_bytes(raw)
    digest = sha(raw) if hash_ok else "0" * 64
    (env.ops / f"{op}.json.sha256").write_text(digest + "\n", encoding="utf-8")
    return rec


def run(mod, env, capsys, *args, probe=STOPPED):
    code = mod.main(
        list(args),
        repo_root=env.repo,
        operations_root=env.ops,
        probe_service=probe,
    )
    out = capsys.readouterr().out
    data = None
    if "--json" in args:
        data = json.loads(out)
    return code, data, out


def snapshot_tree(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file():
            result[rel] = (path.stat().st_size, sha(path.read_bytes()))
        else:
            result[rel] = ("dir",)
    return result


# ---------------------------------------------------------------- CLI basics


def test_help_exits_zero(mod, env, capsys):
    code, _, out = run(mod, env, capsys, "--help")
    assert code == 0
    assert "--operation-id" in out
    assert "--dry-run" in out


def test_version_exits_zero(mod, env, capsys):
    code, _, out = run(mod, env, capsys, "--version")
    assert code == 0
    assert "1.3.0" in out


def test_inventory_without_id_returns_6_and_lists_ids_sorted(mod, env, capsys):
    record(env, OP)
    record(env, OP2, state="S3")
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert data["MODE"] == "inventory"
    assert data["OPERATION_ID"] is None
    assert data["OPERATION_IDS"] == sorted([OP, OP2])


def test_inventory_with_no_groups_still_returns_6(mod, env, capsys):
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert data["OPERATION_IDS"] == []


def test_invalid_operation_id_rejected(mod, env, capsys):
    code, data, _ = run(mod, env, capsys, "--operation-id", "not-an-id", "--json")
    assert code == 2
    assert data["ERROR_CATEGORY"] == "invalid_operation_id"


def test_unknown_argument_rejected(mod, env, capsys):
    code, data, _ = run(mod, env, capsys, "--whatever", "--json")
    assert code == 2
    assert data["ERROR_CATEGORY"] == "unsupported_argument"


def test_operation_id_without_value_rejected(mod, env, capsys):
    code, data, _ = run(mod, env, capsys, "--operation-id", "--json")
    assert code == 2


@pytest.mark.parametrize(
    "flag",
    [
        "--restore",
        "--replace",
        "--target-database",
        "--source-database",
        "--database",
        "--db",
        "--snapshot-dir",
        "--staging-dir",
        "--clean",
        "--delete",
        "--force",
        "--retry",
        "--confirm",
        "--apply",
        "--start-service",
        "--health-check",
        "--migrate",
    ],
)
def test_forbidden_flags_return_2(mod, env, capsys, flag):
    code, data, out = run(mod, env, capsys, flag, "--json")
    assert code == 2
    assert data["ERROR_CATEGORY"] == "unsupported_argument"
    assert str(env.tmp) not in out


# ------------------------------------------------------------- path utilities


def test_is_remote_paths(mod):
    for raw in [r"\\server\share\x", "//server/share/x", r"\\.\C:\x", r"\\?\UNC\s\s\x"]:
        assert mod._is_remote(raw) is True, raw
    for raw in [r"C:\work\db", "/tmp/x", r"\\?\C:\work\x"]:
        assert mod._is_remote(raw) is False, raw


def test_no_reparse_components_rejects_symlinked_root(mod, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    with pytest.raises(mod.ToolError) as excinfo:
        mod._no_reparse_components(link, tmp_path, "root")
    assert excinfo.value.code == 3


def test_operations_root_reparse_is_rejected(mod, env, capsys, tmp_path):
    real_ops = tmp_path / "real-ops"
    real_ops.mkdir()
    link = tmp_path / "ops-link"
    try:
        os.symlink(real_ops, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    record(env, OP)
    code = mod.main(
        ["--operation-id", OP, "--json"],
        repo_root=env.repo,
        operations_root=link,
        probe_service=STOPPED,
    )
    capsys.readouterr()
    assert code == 3


# --------------------------------------------------------------- record guard


def test_missing_sidecar_requires_adjudication(mod, env, capsys):
    record(env, OP)
    (env.ops / f"{OP}.json.sha256").unlink()
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


def test_record_hash_mismatch_requires_adjudication(mod, env, capsys):
    record(env, OP, hash_ok=False)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


def test_record_schema_missing_requires_adjudication(mod, env, capsys):
    record(env, OP, schema=False)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_record_id_mismatch_requires_adjudication(mod, env, capsys):
    record(env, OP, op_field=OP2)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_record_missing_is_adjudication_not_crash(mod, env, capsys):
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


# ------------------------------------------------------------------- states


def test_s1_absent_target_cannot_produce_a_plan(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, "novelcraft.db-wal")
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S1"
    assert data["GUARDS_PASS"] is False
    assert data["GUARDS"]["formal_target_non_reparse"]["status"] == "unknown"
    assert data["BLOCKERS"]
    assert code == 6


def test_s3_all_members_stale_no_target(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db-wal.stale-{OP}")
    write(env, f"novelcraft.db-shm.stale-{OP}")
    record(env, OP, state="S3", step=8)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S3"
    assert data["GUARDS_PASS"] is False
    assert code == 6


def test_s2_mixed_sidecar_state(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db-wal.stale-{OP}")
    write(env, "novelcraft.db-shm")
    record(env, OP, state="S2", step=8)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S2"


def test_s4_partial_temp(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    stale = write(env, f"novelcraft.db.stale-{OP}", b"OLD-DB-CONTENT")
    write(env, f"novelcraft.db.restore-{OP}.tmp", b"PART")
    record(env, OP, state="S4", step=9)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S4"


def test_s5_complete_temp(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-DB-CONTENT")
    write(env, f"novelcraft.db.restore-{OP}.tmp", b"OLD-DB-CONTENT")
    record(env, OP, state="S5", step=9)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S5"


def test_s6_rename_failed(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-DB-CONTENT")
    write(env, f"novelcraft.db.restore-{OP}.tmp", b"OLD-DB-CONTENT")
    record(env, OP, state="S6", step=10)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S6"


def test_s7_without_pre_rollback_returns_7(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S7", step=11)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S7"
    assert code == 7


def test_s8_failed_group_without_pre_rollback_returns_7(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.failed-{OP}", b"NEW")
    record(env, OP, state="S8", step=11)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S8"
    assert data["FAILED_GROUP"]
    assert code == 7


def test_s9_with_pre_rollback_can_plan(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.failed-{OP}", b"NEW")
    record(
        env,
        OP,
        state="S9",
        step=12,
        pre_rollback=pre_ok(env),
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S9"
    assert code == 0


def test_s0_returns_7(mod, env, capsys):
    record(env, OP, state="S0", step=3)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["STATE"] == "S0"
    assert code == 7


def test_state_contradicting_filesystem_requires_adjudication(mod, env, capsys):
    record(env, OP, state="S3", step=8)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


# -------------------------------------------------------------- group guards


def test_cross_id_temp_rejected(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.restore-{OP2}.tmp")
    record(env, OP, state="S5", step=9)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["BLOCKERS"]


def test_failed_group_must_not_mix_with_stale(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.failed-{OP2}", b"NEW")
    record(env, OP, state="S8", step=11, pre_rollback=pre_ok(env))
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["BLOCKERS"]
    assert data["GUARDS_PASS"] is False
    assert data["NEEDS_HUMAN_ADJUDICATION"] or data["MANUAL_CONFIRMATIONS"]
    assert code == 6


def test_multiple_groups_listed_sorted(mod, env, capsys):
    for op in (OP3, OP, OP2):
        record(env, op)
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert data["OPERATION_IDS"] == sorted([OP, OP2, OP3])


def test_target_name_conflict_blocks(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}", b"OLD")
    record(env, OP, state="S3", step=8)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["BLOCKERS"]


def test_nonempty_wal_requires_manual_adjudication(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db-wal.stale-{OP}", b"WAL-DATA")
    record(env, OP, state="S3", step=8)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["MANUAL_CONFIRMATIONS"]


# ------------------------------------------------------------- output contract


def test_json_contract_fields(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    for key in [
        "RESULT",
        "MODE",
        "STATE",
        "OPERATION_ID",
        "OPERATION_IDS",
        "GROUPS",
        "MISSING_MEMBERS",
        "NEEDS_HUMAN_ADJUDICATION",
        "FAILED_GROUP",
        "TEMP_FILES",
        "GUARDS",
        "GUARDS_PASS",
        "BLOCKERS",
        "MANUAL_CONFIRMATIONS",
        "ROLLBACK_PLAN_ONLY",
        "SIDE_EFFECTS",
        "TOOL_VERSION",
    ]:
        assert key in data, key
    assert data["ROLLBACK_PLAN_ONLY"] is True
    assert data["SIDE_EFFECTS"] is False
    assert data["TOOL_VERSION"] == "1.3.0"


def test_output_contains_no_absolute_paths_or_secrets(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    _, _, out = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert str(env.tmp) not in out
    for needle in ("JWT_SECRET", "DATABASE_URL", "Bearer ", "sk-", "Traceback"):
        assert needle not in out


def test_text_mode_matches_json_keys(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    code, _, out = run(mod, env, capsys, "--operation-id", OP)
    assert code == 6
    for key in ("RESULT=", "MODE=", "STATE=", "ROLLBACK_PLAN_ONLY=true", "SIDE_EFFECTS=false"):
        assert key in out


def test_service_state_unknown_is_not_a_pass(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json", probe=None)
    assert data["GUARDS_PASS"] is False
    assert code == 6


def test_tool_never_writes_to_fixture_tree(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    before = snapshot_tree(env.tmp)
    run(mod, env, capsys, "--operation-id", OP, "--json")
    run(mod, env, capsys, "--json")
    assert snapshot_tree(env.tmp) == before


# --------------------------------------------------------------- safety audit


def test_ast_has_no_destructive_or_runtime_apis():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden_attrs = {
        "rename",
        "replace",
        "unlink",
        "rmdir",
        "remove",
        "move",
        "copy",
        "copy2",
        "rmtree",
        "connect",
        "execute",
        "executemany",
        "popen",
        "system",
        "spawn",
    }
    forbidden_modules = {"sqlite3", "subprocess", "shutil", "socket", "urllib", "requests", "http"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or "").split(".")[0]]
            for name in names:
                assert name not in forbidden_modules, name
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attrs, node.attr


# ---------------------------------------------- record/scene section 16.14 coverage


def pre_ok(env, op: str = OP) -> dict:
    path = f"novelcraft.db.pre-rollback-{op}"
    digest = "a" * 64
    payload = {"operation_id": op, "path": path, "sha256": digest, "size": 8192}
    raw = canon(payload)
    (env.ops / f"pre-rollback-{op}.json").write_bytes(raw)
    (env.ops / f"pre-rollback-{op}.json.sha256").write_text(sha(raw) + "\n", encoding="utf-8")
    return {"present": True, "path": path, "sha256": digest, "size": 8192}


def scene(env, op: str = OP, *, files=None, record_path=None, record_sha256=None, **over):
    payload = {
        "scene_schema_version": "1.0",
        "operation_id": op,
        "created_at": "2026-11-02T00:00:00+08:00",
        "files": files or [],
        "record_path": record_path,
        "record_sha256": record_sha256,
    }
    payload.update(over)
    raw = canon(payload)
    (env.ops / f"{op}-scene.json").write_bytes(raw)
    (env.ops / f"{op}-scene.json.sha256").write_text(sha(raw) + "\n", encoding="utf-8")


def junction(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    try:
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    except OSError:
        return False
    return done.returncode == 0


def test_scene_is_read_and_validated(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db-wal.stale-{OP}")
    record(env, OP, state="S3", step=8)
    scene(env, OP, record_path=f"{OP}.json")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"] == []
    assert data["GUARDS_PASS"] is False


def test_scene_sidecar_missing_is_adjudication(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    scene(env, OP)
    (env.ops / f"{OP}-scene.json.sha256").unlink()
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


def test_scene_hash_mismatch_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    scene(env, OP)
    (env.ops / f"{OP}-scene.json.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_scene_id_mismatch_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    scene(env, OP, operation_id=OP2)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_scene_record_hash_mismatch_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    scene(env, OP, record_sha256="b" * 64)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_scene_record_path_traversal_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    scene(env, OP, record_path="../outside.json")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_record_over_size_limit_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    (env.ops / f"{OP}.json").write_bytes(b"{" + b"x" * (mod.MAX_RECORD_BYTES + 1) + b"}")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["NEEDS_HUMAN_ADJUDICATION"]


def test_record_wrong_field_types_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, extra={"current_step": "seven"})
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_record_json_replaced_by_directory_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    (env.ops / f"{OP}.json").unlink()
    (env.ops / f"{OP}.json").mkdir()
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_missing_record_with_scene_does_not_infer(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    scene(env, OP)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert "state_indeterminate" in data["NEEDS_HUMAN_ADJUDICATION"]


def test_operations_parent_reparse_is_rejected(mod, env, capsys, tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "ops").mkdir()
    link = tmp_path / "linked-parent"
    if not junction(link, real_parent):
        pytest.skip("link creation is not permitted in this environment")
    record(env, OP, state="S1", step=7)
    code = mod.main(
        ["--operation-id", OP, "--json"],
        repo_root=env.repo,
        operations_root=link / "ops",
        probe_service=STOPPED,
    )
    capsys.readouterr()
    assert code == 3


# ------------------------------------------------------- pre_rollback trust


@pytest.mark.parametrize(
    "value",
    [
        {"present": True, "path": "../escape.db", "sha256": "a" * 64},
        {"present": True, "path": f"novelcraft.db.pre-rollback-{OP}", "sha256": "not-a-hash"},
        {"present": True, "path": f"novelcraft.db.pre-rollback-{OP2}", "sha256": "a" * 64},
        {"present": False, "path": f"novelcraft.db.pre-rollback-{OP}", "sha256": "a" * 64},
        {"present": True, "path": f"novelcraft.db.pre-rollback-{OP}"},
    ],
)
def test_incomplete_pre_rollback_returns_7(mod, env, capsys, value):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S7", step=11, pre_rollback=value)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 7
    assert "pre_rollback_record_incomplete" in data["BLOCKERS"]


# -------------------------------------------------- state/scene consistency


def test_s8_without_failed_group_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S8", step=11, pre_rollback=pre_ok(env))
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert "state_requires_failed_group" in data["NEEDS_HUMAN_ADJUDICATION"] or data["NEEDS_HUMAN_ADJUDICATION"]


def test_s5_temp_mismatch_is_adjudication(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-CONTENT")
    write(env, f"novelcraft.db.restore-{OP}.tmp", b"OTHER")
    record(env, OP, state="S5", step=9)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_s7_without_live_target_is_adjudication(mod, env, capsys):
    (env.backend / "novelcraft.db").unlink()
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S7", step=11, pre_rollback=pre_ok(env))
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_s1_without_stale_main_is_adjudication(mod, env, capsys):
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_recorded_missing_sidecar_is_reported(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(
        env,
        OP,
        state="S1",
        step=7,
        files=[{"role": "stale_wal", "exists": False, "actual_path": f"novelcraft.db-wal.stale-{OP}"}],
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert "stale_wal" in data["MISSING_MEMBERS"]


def test_record_actual_path_mismatch_is_a_mismatch(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(
        env,
        OP,
        state="S1",
        step=7,
        files=[{"role": "stale_main", "exists": True, "actual_path": "backend/other.db"}],
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert any(value.startswith("actual_path_invalid:") for value in data["RECORD_MISMATCHES"])


# ------------------------------------------------------ path / volume guards


def test_volume_mismatch_is_guard_failure(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7, extra={"groups": {"stale": {"stale_main": {"volume_id": 123456789}}}})
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 3
    assert data["GUARDS"]["volume_matches_record"]["status"] == "fail"


def test_directory_object_change_is_guard_failure(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7, extra={"dir_object": {"mtime_ns": 1}})
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 3
    assert data["GUARDS"]["directory_object_stable"]["status"] == "fail"


def test_stale_main_reparse_is_rejected(mod, env, capsys, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    link = env.backend / f"novelcraft.db.stale-{OP}"
    if not junction(link, victim):
        pytest.skip("link creation is not permitted in this environment")
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 3
    assert "reparse_present" in data["BLOCKERS"]


def test_failed_member_reparse_is_rejected(mod, env, capsys, tmp_path):
    victim = tmp_path / "victim2"
    victim.mkdir()
    link = env.backend / f"novelcraft.db.failed-{OP}"
    if not junction(link, victim):
        pytest.skip("link creation is not permitted in this environment")
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S8", step=11, pre_rollback=pre_ok(env))
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 3


def test_fingerprint_skipped_large_is_reported(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-CONTENT")
    record(env, OP, state="S1", step=7)
    monkeypatch.setattr(mod, "MAX_HASH_BYTES", 1)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["FINGERPRINTS"]["stale.stale_main"] == "skipped_large"
    assert data["GUARDS"]["fingerprints_available"]["status"] == "fail"
    assert code == 6


def test_ast_forbids_write_and_runtime_apis():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden_attrs = {"write_text", "write_bytes", "mkdir", "makedirs", "dump", "touch", "chmod"}
    forbidden_modules = {"tempfile", "sqlite3", "subprocess", "shutil", "socket", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or "").split(".")[0]]
            for name in names:
                assert name not in forbidden_modules, name
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attrs, node.attr
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            assert len(node.args) >= 2, "open() without explicit mode is not allowed"
            mode = node.args[1]
            assert isinstance(mode, ast.Constant) and isinstance(mode.value, str)


def test_formal_target_reparse_blocks(mod, env, capsys, monkeypatch):
    original = mod._inspect_target

    def fake(backend):
        info = original(backend)
        info["reparse"] = True
        return info

    monkeypatch.setattr(mod, "_inspect_target", fake)
    record(env, OP, state="S1", step=7)
    write(env, f"novelcraft.db.stale-{OP}")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["GUARDS"]["formal_target_non_reparse"]["status"] == "fail"
    assert code == 3


def test_formal_target_must_be_a_regular_file(mod, env, capsys):
    target = env.backend / "novelcraft.db"
    target.unlink()
    target.mkdir()
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["GUARDS"]["formal_target_regular_file"]["status"] == "fail"
    assert code == 3


def test_record_size_mismatch_returns_5(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-CONTENT")
    record(env, OP, state="S1", step=7, files=[{"role": "stale_main", "exists": True, "size": 999}])
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert "size:stale_main" in data["RECORD_MISMATCHES"]


def test_record_sha_mismatch_returns_5(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}", b"OLD-CONTENT")
    record(
        env,
        OP,
        state="S1",
        step=7,
        files=[{"role": "stale_main", "exists": True, "size": 11, "sha256": "b" * 64}],
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert "sha256:stale_main" in data["RECORD_MISMATCHES"]


def test_sidecar_with_extra_tokens_is_adjudication(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    sidecar = env.ops / f"{OP}.json.sha256"
    sidecar.write_text(sidecar.read_text(encoding="utf-8").strip() + " extra\n", encoding="utf-8")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert any("sidecar" in item for item in data["NEEDS_HUMAN_ADJUDICATION"])


def test_raw_json_sidecar_is_rejected(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    payload = record(env, OP, state="S1", step=7)
    pretty = json.dumps(payload, indent=2)
    raw = pretty.encode("utf-8")
    (env.ops / f"{OP}.json").write_bytes(raw)
    (env.ops / f"{OP}.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert any("hash_mismatch" in item for item in data["NEEDS_HUMAN_ADJUDICATION"])


def test_inventory_rejects_record_without_sidecar(mod, env, capsys):
    record(env, OP, state="S1", step=7)
    (env.ops / f"{OP}.json.sha256").unlink()
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert data["INVENTORY_REJECTED"]
    assert OP not in data["OPERATION_IDS"]


def test_duplicate_record_role_is_mismatch(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(
        env,
        OP,
        state="S1",
        step=7,
        files=[
            {"role": "stale_main", "exists": True},
            {"role": "stale_main", "exists": True},
        ],
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert any(item.startswith("duplicate_record_role") for item in data["RECORD_MISMATCHES"])


def test_inventory_lists_backend_only_operation_ids(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.failed-{OP2}")
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert OP in data["OPERATION_IDS"]
    assert OP2 in data["OPERATION_IDS"]
    assert data["GROUPS"][OP] == ["stale_main"]
    assert data["GROUPS"][OP2] == ["failed_new"]


def test_inventory_rejects_oversized_record(mod, env, capsys):
    record(env, OP, state="S1", step=7)
    (env.ops / f"{OP}.json").write_bytes(b"{" + b" " * (2_097_152 + 1))
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert any(item.startswith("too_large:") for item in data["INVENTORY_REJECTED"])


def test_inventory_rejects_oversized_sidecar(mod, env, capsys):
    record(env, OP, state="S1", step=7)
    (env.ops / f"{OP}.json.sha256").write_text("a" * 64 + "\n" + "b" * 5000, encoding="utf-8")
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert any(item.startswith("sidecar_too_large:") for item in data["INVENTORY_REJECTED"])


def test_inventory_checks_sidecar_reparse_before_reading(mod, env, capsys, monkeypatch):
    record(env, OP, state="S1", step=7)
    real = mod._is_reparse_point
    monkeypatch.setattr(
        mod,
        "_is_reparse_point",
        lambda path: path.name.endswith(".json.sha256") or real(path),
    )
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert any(item.startswith("sidecar_reparse:") for item in data["INVENTORY_REJECTED"])


def test_missing_file_fields_are_mismatch(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7, files=[{"role": "stale_main", "exists": True}])
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert "record_field_invalid:stale_main:size" in data["RECORD_MISMATCHES"]
    assert "record_field_invalid:stale_main:sha256" in data["RECORD_MISMATCHES"]


def test_invalid_sha_format_is_mismatch(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(
        env,
        OP,
        state="S1",
        step=7,
        files=[
            {
                "role": "stale_main",
                "original_path": f"novelcraft.db.stale-{OP}",
                "actual_path": f"novelcraft.db.stale-{OP}",
                "exists": True,
                "size": 1,
                "mtime_ns": 1,
                "sha256": "not-hex",
            }
        ],
    )
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 5
    assert "record_field_invalid:stale_main:sha256" in data["RECORD_MISMATCHES"]


def test_pre_rollback_size_mismatch_returns_7(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.failed-{OP}")
    pre = pre_ok(env)
    pre["size"] = 4096
    record(env, OP, state="S9", step=12, pre_rollback=pre)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 7
    assert "pre_rollback_record_incomplete" in data["BLOCKERS"]


def test_live_role_volume_mismatch_is_guard_failure(mod, env, capsys):
    record(env, OP, state="S1", step=7, extra={"groups": {"live": {"main": {"volume_id": 987654321}}}})
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["GUARDS"]["volume_matches_record"]["status"] == "fail"
    assert code == 3


def test_temp_actual_path_outside_backend_is_blocked(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, f"novelcraft.db.restore-{OP}.tmp", b"TMP")
    record(env, OP, state="S5", step=10, files=[])
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["GUARDS"]["temp_in_backend_directory"]["status"] == "fail"
    assert code == 6


def test_dir_object_index_is_unknown_not_fail(mod, env, capsys):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7, extra={"dir_object": {"index": 5}})
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert data["GUARDS"]["directory_object_stable"]["status"] == "unknown"
    assert "unverified:directory_object_stable" in data["BLOCKERS"]
    assert code == 6


def test_internal_error_has_its_own_exit_code(mod, env, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_run", boom)
    code = mod.main(["--json"], repo_root=env.repo, operations_root=env.ops)
    assert code == 8
    payload = json.loads(capsys.readouterr().out)
    assert payload["ERROR_CATEGORY"] == "internal_error"






def test_live_files_are_never_hashed(mod, env, monkeypatch):
    write(env, "novelcraft.db", b"LIVE")
    write(env, "novelcraft.db-wal", b"W")

    def boom(path):
        raise AssertionError(f"hashed live file: {path.name}")

    monkeypatch.setattr(mod, "_sha256_file", boom)
    _entries, _groups, live, _dups = mod._scan(env.backend)
    assert live["main"]["sha256"] is None
    assert live["main"]["fingerprint_status"] == "not_allowed_formal_db"
    assert live["wal"]["sha256"] is None


def test_record_live_sha_is_not_comparable(mod, env):
    live_entry = {
        "name": "novelcraft.db",
        "role": "main",
        "kind": "live",
        "operation_id": None,
        "exists": True,
        "is_file": True,
        "size": 4,
        "mtime_ns": 0,
        "volume_id": 1,
        "reparse": False,
        "sha256": None,
        "fingerprint_status": "not_allowed_formal_db",
    }
    rec = {
        "files": [
            {
                "role": "main",
                "original_path": "novelcraft.db",
                "actual_path": "novelcraft.db",
                "exists": True,
                "size": 4,
                "mtime_ns": 0,
                "sha256": "a" * 64,
            }
        ]
    }
    mismatches, unverifiable = mod._record_mismatches(
        rec, {"stale": {}, "failed": {}, "live": {}, "temp": []}, OP, {"main": live_entry}
    )
    assert unverifiable == ["live_sha_not_comparable:main"]
    assert mismatches == []


def test_output_fingerprints_exclude_live(mod, env, capsys):
    write(env, "novelcraft.db", b"LIVE")
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert all(not key.startswith("live.") for key in data["FINGERPRINTS"])


def test_stale_files_are_still_hashed(mod, env):
    write(env, f"novelcraft.db.stale-{OP}", b"STALE")
    entries, _groups, _live, _dups = mod._scan(env.backend)
    stale = next(entry for entry in entries if entry["kind"] == "stale")
    assert stale["fingerprint_status"] == "computed"
    assert isinstance(stale["sha256"], str) and len(stale["sha256"]) == 64


def test_scan_and_target_never_call_path_is_file(mod, env, monkeypatch):
    from pathlib import Path

    write(env, "novelcraft.db", b"LIVE")
    write(env, "novelcraft.db-wal", b"W")
    write(env, f"novelcraft.db.stale-{OP}", b"STALE")

    def boom(self):
        raise AssertionError(f"Path.is_file() must not be called: {self}")

    monkeypatch.setattr(Path, "is_file", boom)
    entries, _groups, live, _dups = mod._scan(env.backend)
    target = mod._inspect_target(env.backend)
    assert live["main"]["sha256"] is None
    assert target["is_file"] is True
    assert any(entry["kind"] == "stale" for entry in entries)


def test_guarded_file_uses_lstat_not_is_file(mod, env, monkeypatch):
    from pathlib import Path

    record(env, OP, state="S1", step=7)

    def boom(self):
        raise AssertionError("Path.is_file() must not be called")

    monkeypatch.setattr(Path, "is_file", boom)
    parsed, problem = mod._guarded_file(
        env.ops / f"{OP}.json", env.ops / f"{OP}.json.sha256", env.ops, "record"
    )
    assert problem is None
    assert parsed["operation_id"] == OP


def test_operations_root_uses_lstat_not_is_dir(mod, env, monkeypatch):
    from pathlib import Path

    def boom(self):
        raise AssertionError("Path.is_dir() must not be called")

    monkeypatch.setattr(Path, "is_dir", boom)
    mod._operations_root(env.ops)


def test_formal_target_directory_is_not_regular(mod, env):
    target = env.backend / "novelcraft.db"
    if target.exists():
        target.unlink()
    target.mkdir()
    target = mod._inspect_target(env.backend)
    assert target["exists"] is True
    assert target["is_file"] is False


def _stub_identity(size=1, mtime=1):
    return (1, 1, size, mtime, True)


def test_protected_read_rejects_handle_identity_change(mod, env, monkeypatch):
    target = write(env, "probe.bin", b"DATA")
    monkeypatch.setattr(mod, "_fd_identity", lambda fd: _stub_identity(size=99))
    with pytest.raises(mod.ToolError) as caught:
        mod._protected_read(target, env.backend, "probe")
    assert caught.value.code == 6
    assert caught.value.category == "probe_identity_changed"


def test_protected_read_rejects_change_while_reading(mod, env, monkeypatch):
    target = write(env, "probe.bin", b"DATA")
    calls = {"n": 0}
    real = mod._fd_identity

    def flaky(fd):
        calls["n"] += 1
        current = real(fd)
        if calls["n"] == 1:
            return current
        return (current[0], current[1], current[2] + 1, current[3], current[4])

    monkeypatch.setattr(mod, "_fd_identity", flaky)
    with pytest.raises(mod.ToolError) as caught:
        mod._protected_read(target, env.backend, "probe")
    assert caught.value.code == 6
    assert caught.value.category == "probe_changed_while_reading"


def test_protected_read_rejects_replacement_after_read(mod, env, monkeypatch):
    target = write(env, "probe.bin", b"DATA")
    real_close = mod.os.close

    def closing(fd):
        real_close(fd)
        mod.os.utime(target, (1, 2))

    monkeypatch.setattr(mod.os, "close", closing)
    with pytest.raises(mod.ToolError) as caught:
        mod._protected_read(target, env.backend, "probe")
    assert caught.value.code == 6
    assert caught.value.category in {"probe_replaced", "probe_changed_while_reading"}


def test_protected_read_hides_paths_on_failure(mod, env, capsys, monkeypatch):
    monkeypatch.setattr(mod, "_fd_identity", lambda fd: _stub_identity(size=7))
    write(env, "probe.bin", b"DATA")
    code = mod.main(["--json"], repo_root=env.repo, operations_root=env.ops)
    assert code == 6
    out = capsys.readouterr()
    combined = out.out + out.err
    assert str(env.repo) not in combined
    assert "Traceback" not in combined


def test_record_swapped_to_link_is_blocked(mod, env, monkeypatch):
    record(env, OP, state="S1", step=7)
    target = env.ops / f"{OP}.json"
    outside = write(env, "outside.json", b"{}")
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code in {3, 6}
    assert data["SIDE_EFFECTS"] is False


def test_source_uses_no_write_or_link_apis():
    source = Path(__file__).resolve().parents[2] / "scripts" / "rollback-db.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    banned_calls = {
        "rename", "replace", "remove", "unlink", "rmdir", "mkdir", "makedirs",
        "write_text", "write_bytes", "copy", "copy2", "copyfile",
        "move", "rmtree", "connect", "system", "run", "Popen",
    }
    banned_modules = {"shutil", "tempfile", "sqlite3", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned_modules
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned_modules
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in (banned_calls | {"open"}), node.func.id
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in banned_calls, node.func.attr


def test_backend_directory_replaced_during_scan_is_blocked(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real = mod._dir_identity
    hits = {"n": 0}

    def flaky(path, label):
        ident = real(path, label)
        if label == "backend":
            hits["n"] += 1
            if hits["n"] > 1:
                return {**ident, "ino": ident["ino"] + 1}
        return ident

    monkeypatch.setattr(mod, "_dir_identity", flaky)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert "backend_directory_replaced" in data["BLOCKERS"]


def test_operations_root_replaced_before_read_is_blocked(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real = mod._dir_identity
    hits = {"n": 0}

    def flaky(path, label):
        ident = real(path, label)
        if label == "operations":
            hits["n"] += 1
            if hits["n"] > 1:
                return {**ident, "ino": ident["ino"] + 1}
        return ident

    monkeypatch.setattr(mod, "_dir_identity", flaky)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert "operations_directory_replaced" in data["BLOCKERS"]


def test_operations_root_swapped_during_record_read_is_blocked(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real_read = mod._read_record

    def swapping(root, op):
        result = real_read(root, op)
        moved = env.ops.parent / "operations-old"
        env.ops.rename(moved)
        env.ops.mkdir()
        return result

    monkeypatch.setattr(mod, "_read_record", swapping)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert any("operations_directory_" in item for item in data["BLOCKERS"])


def test_inventory_operations_root_identity_change_is_blocked(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real = mod._dir_identity
    hits = {"n": 0}

    def flaky(path, label):
        ident = real(path, label)
        if label == "operations":
            hits["n"] += 1
            if hits["n"] > 1:
                return {**ident, "mtime_ns": ident["mtime_ns"] + 1}
        return ident

    monkeypatch.setattr(mod, "_dir_identity", flaky)
    code, data, _ = run(mod, env, capsys, "--json")
    assert code == 6
    assert "operations_directory_modified" in data["BLOCKERS"]
    assert any("operations_directory_modified" in item for item in data["INVENTORY_REJECTED"])


def test_directory_identity_cannot_be_read_is_adjudication(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)

    def broken(path, label):
        raise mod.ToolError(6, f"{label}_identity_unavailable")

    monkeypatch.setattr(mod, "_dir_identity", broken)
    code, _, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6


def test_identity_failure_leaves_fixture_untouched(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    write(env, "sentinel.txt", b"KEEP")
    record(env, OP, state="S1", step=7)
    before = sorted((p.relative_to(env.repo).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in env.repo.rglob("*") if p.is_file())
    real = mod._dir_identity
    hits = {"n": 0}

    def flaky(path, label):
        ident = real(path, label)
        if label == "backend":
            hits["n"] += 1
            if hits["n"] > 1:
                return {**ident, "ino": ident["ino"] + 1}
        return ident

    monkeypatch.setattr(mod, "_dir_identity", flaky)
    code = mod.main(["--json"], repo_root=env.repo, operations_root=env.ops)
    assert code == 6
    after = sorted((p.relative_to(env.repo).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                   for p in env.repo.rglob("*") if p.is_file())
    assert before == after
    out = capsys.readouterr()
    assert str(env.repo) not in (out.out + out.err)
    assert "Traceback" not in (out.out + out.err)


def test_operations_swapped_after_last_read_blocks_plan(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real = mod._pre_rollback

    def swapping(root, rec, op):
        result = real(root, rec, op)
        moved = env.ops.parent / "operations-old"
        env.ops.rename(moved)
        env.ops.mkdir()
        return result

    monkeypatch.setattr(mod, "_pre_rollback", swapping)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["RESULT"] == "BLOCKED"
    assert data["GUARDS_PASS"] is False
    assert any("operations_directory_" in item for item in data["BLOCKERS"])


def test_backend_swapped_after_last_read_blocks_plan(mod, env, capsys, monkeypatch):
    write(env, f"novelcraft.db.stale-{OP}")
    record(env, OP, state="S1", step=7)
    real = mod._pre_rollback

    def swapping(root, rec, op):
        result = real(root, rec, op)
        moved = env.backend.parent / "backend-old"
        env.backend.rename(moved)
        env.backend.mkdir()
        return result

    monkeypatch.setattr(mod, "_pre_rollback", swapping)
    code, data, _ = run(mod, env, capsys, "--operation-id", OP, "--json")
    assert code == 6
    assert data["RESULT"] == "BLOCKED"
    assert data["GUARDS_PASS"] is False
    assert any("backend_directory_" in item for item in data["BLOCKERS"])


class _NoIno:
    st_mode = 0o040000
    st_dev = 1
    st_size = 1
    st_mtime_ns = 1


class _NoDev:
    st_mode = 0o100644
    st_ino = 1
    st_size = 1
    st_mtime_ns = 1


def test_identity_missing_ino_is_adjudication(mod):
    with pytest.raises(mod.ToolError) as caught:
        mod._identity(_NoIno())
    assert caught.value.code == 6
    assert caught.value.category == "identity_unavailable"


def test_identity_missing_dev_is_adjudication(mod):
    with pytest.raises(mod.ToolError) as caught:
        mod._identity(_NoDev())
    assert caught.value.code == 6
    assert caught.value.category == "identity_unavailable"


def test_fd_identity_unavailable_is_adjudication(mod, monkeypatch):
    monkeypatch.setattr(mod.os, "fstat", lambda fd: _NoIno())
    with pytest.raises(mod.ToolError) as caught:
        mod._fd_identity(3)
    assert caught.value.code == 6


def test_dir_identity_without_ino_is_adjudication(mod, env, monkeypatch):
    monkeypatch.setattr(mod, "_is_reparse_point", lambda path: False)
    monkeypatch.setattr(type(env.ops), "lstat", lambda self: _NoIno())
    with pytest.raises(mod.ToolError) as caught:
        mod._dir_identity(env.ops, "operations")
    assert caught.value.code == 6
    assert caught.value.category == "operations_identity_unavailable"


def test_operations_root_outside_fixed_location_is_rejected(mod, env, capsys):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    code = mod.main(["--json"], repo_root=env.repo, operations_root=elsewhere)
    assert code == 3
    out = capsys.readouterr()
    assert str(elsewhere) not in (out.out + out.err)
    assert "Traceback" not in (out.out + out.err)


def test_inventory_checks_operations_root_before_backend(mod, env, monkeypatch):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    seen = []
    real = mod._dir_identity

    def recording(path, label):
        seen.append(label)
        return real(path, label)

    monkeypatch.setattr(mod, "_dir_identity", recording)
    code = mod.main(["--json"], repo_root=env.repo, operations_root=elsewhere)
    assert code == 3
    assert "backend" not in seen


def _rejected_root(mod, env, capsys, candidate):
    code = mod.main(["--json"], repo_root=env.repo, operations_root=candidate)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 3
    assert str(candidate) not in text
    assert "Traceback" not in text
    return code


def test_same_tail_under_other_parent_is_rejected(mod, env, capsys):
    other = env.tmp / "other-root" / "NovelCraft-backups" / "operations"
    other.mkdir(parents=True)
    _rejected_root(mod, env, capsys, other)


def test_repo_local_fake_operations_root_is_rejected(mod, env, capsys):
    fake = env.repo / "NovelCraft-backups" / "operations"
    fake.mkdir(parents=True)
    _rejected_root(mod, env, capsys, fake)


def test_other_user_home_is_rejected(mod, env, capsys):
    other_user = env.tmp / "Users" / "someone-else" / "NovelCraft-backups" / "operations"
    other_user.mkdir(parents=True)
    _rejected_root(mod, env, capsys, other_user)


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive path semantics")
def test_case_difference_is_accepted_on_windows(mod, env):
    mod._operations_root(Path(str(env.ops).upper()))


def test_exact_expected_root_is_accepted(mod, env):
    mod._operations_root(env.ops)


def test_expected_operations_root_missing_returns_6(mod, env, capsys, monkeypatch):
    missing = env.tmp / "missing-home" / "NovelCraft-backups" / "operations"
    monkeypatch.setattr(mod, "_expected_operations_root", lambda: missing)
    before = sorted(p.relative_to(env.tmp).as_posix() for p in env.tmp.rglob("*"))
    with pytest.raises(mod.ToolError) as caught:
        mod._operations_root(env.ops)
    assert caught.value.code == 6
    assert caught.value.category == "expected_root_unavailable"
    assert str(missing) not in str(caught.value)
    code = mod.main(["--json"], repo_root=env.repo, operations_root=env.ops)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 6
    assert str(missing) not in text and str(env.ops) not in text
    assert "Traceback" not in text
    after = sorted(p.relative_to(env.tmp).as_posix() for p in env.tmp.rglob("*"))
    assert after == before
    assert not missing.exists()


def test_default_expected_operations_root_is_user_home(mod):
    assert mod._expected_operations_root() == (
        Path.home() / "NovelCraft-backups" / "operations"
    )
