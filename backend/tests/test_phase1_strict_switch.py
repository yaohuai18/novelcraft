# -*- coding: utf-8 -*-
"""阶段 E 批次 1：strict_entity_check 开关行为固化。

本测试只固化 `app.services.ai_service.strict_entity_check_enabled()`
的当前实现语义，不改变实现、不扩展关闭值集合。

冻结口径（与当前实现一致）：
    - 环境变量不存在            -> True
    - "0"                       -> False
    - "false"                   -> False
    - "off"                     -> False
    - "no"                      -> False
    - 空字符串                  -> True
    - 未知值                    -> True

边界说明：
    - 不导入 state_extraction_service / chapter_lifecycle / app.main / 路由；
    - 不连接任何数据库，仅构造内存环境的 Settings 所需最小变量。
"""

import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 仅为让 app.core.config.Settings 可在无 .env 的隔离环境中完成构造；
# 不建立任何连接，也不写入任何库。
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "phase1-strict-switch-test-secret")

from app.services.ai_service import strict_entity_check_enabled  # noqa: E402

ENV_KEY = "NOVELCRAFT_STRICT_ENTITY_CHECK"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),      # 环境变量缺失
        ("0", False),
        ("false", False),
        ("off", False),
        ("no", False),
        ("", True),        # 空字符串不视为关闭
        ("unknown", True),  # 未知值回落到开启
    ],
)
def test_strict_switch_contract(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(ENV_KEY, raising=False)
    else:
        monkeypatch.setenv(ENV_KEY, raw)

    assert strict_entity_check_enabled() is expected


def test_env_value_is_normalized(monkeypatch):
    """当前实现先 strip+lower 再比对，固化大小写与首尾空白行为。"""
    monkeypatch.setenv(ENV_KEY, "  FALSE  ")
    assert strict_entity_check_enabled() is False

    monkeypatch.setenv(ENV_KEY, "  No ")
    assert strict_entity_check_enabled() is False

    monkeypatch.setenv(ENV_KEY, "  ON  ")
    assert strict_entity_check_enabled() is True
