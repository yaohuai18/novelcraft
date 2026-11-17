# -*- coding: utf-8 -*-
"""SQL echo 必须由独立开关控制，不得跟随 debug。

约束：
- 不在进程内 import app.core.config：那会在 backend/ 下读取真实 .env；
- 每次都用子进程、临时工作目录（无 .env）、显式环境变量探测；
- 不打印 SQL、参数、token、密码或连接串。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BACKEND_DIR / "app" / "core" / "config.py"
DATABASE_PATH = BACKEND_DIR / "app" / "core" / "database.py"

_PROBE = (
    "import json;"
    "from app.core.config import settings;"
    "from app.core.database import engine;"
    "print(json.dumps({"
    "'debug': bool(settings.debug),"
    "'sql_echo': bool(settings.sql_echo),"
    "'engine_echo': bool(engine.sync_engine.echo),"
    "}))"
)


def _base_env() -> dict[str, str]:
    env = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "JWT_SECRET_KEY": "test-only-secret",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(BACKEND_DIR),
    }
    for key in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "WINDIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _probe(tmp_path: Path, **overrides: str) -> dict:
    env = _base_env()
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _engine_echo_kwarg() -> ast.AST:
    """在 database.py 里找到 _engine_kwargs 的 echo 值节点（不执行模块）。"""
    tree = ast.parse(DATABASE_PATH.read_text(encoding="utf-8"))
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "echo":
                found.append(value)
    assert found, "database.py 中未找到 echo 配置"
    assert len(found) == 1, "database.py 中存在多处 echo 配置"
    return found[0]


def test_sql_echo_defaults_to_false(tmp_path):
    data = _probe(tmp_path)
    assert data["debug"] is True
    assert data["sql_echo"] is False


def test_debug_true_does_not_enable_sql_echo(tmp_path):
    data = _probe(tmp_path, DEBUG="true")
    assert data["debug"] is True
    assert data["sql_echo"] is False


def test_explicit_sql_echo_switch_enables_it(tmp_path):
    data = _probe(tmp_path, DEBUG="true", SQL_ECHO="true")
    assert data["sql_echo"] is True
    assert data["engine_echo"] is True


def test_engine_echo_uses_sql_echo_setting():
    value = _engine_echo_kwarg()
    assert isinstance(value, ast.Attribute)
    assert isinstance(value.value, ast.Name) and value.value.id == "settings"
    assert value.attr == "sql_echo", f"engine echo 仍使用 settings.{value.attr}"


def test_config_field_default_is_false():
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    defaults: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "sql_echo":
                defaults.append(node.value)
    assert defaults, "config.py 中缺少 sql_echo 字段"
    assert isinstance(defaults[0], ast.Constant) and defaults[0].value is False
