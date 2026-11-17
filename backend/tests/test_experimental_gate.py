# -*- coding: utf-8 -*-
"""任务 A：实验接口闸门三态固化。

口径（与 app/core/experimental.py 当前实现一致）：
    - debug=True  且 experimental_open_in_debug=False -> 普通用户 403
    - debug=True  且 experimental_open_in_debug=True  -> 普通用户放行
    - 管理员 / 超管                                  -> 任意开关组合都放行
    - debug=False 时即使开关=True，普通用户仍 403

边界说明：
    - 只对 require_experimental_access 做纯单元调用，不建库、不连库、不联网；
    - 用 monkeypatch 直接替换模块内 settings，不依赖 .env 与环境变量；
    - 不导入 app.main / 路由，不触发任何启动副作用。
"""

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 仅让 app.core.config.Settings 在无 .env 的隔离环境里能完成构造；不建立连接。
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "experimental-gate-test-secret")

from app.core import experimental  # noqa: E402


def _user(role: str):
    """构造闸门只需要的最小用户对象（只读 role 字段）。"""
    return SimpleNamespace(role=role)


def _settings(*, debug: bool, flag: bool):
    return SimpleNamespace(debug=debug, experimental_open_in_debug=flag)


@pytest.mark.parametrize(
    ("debug", "flag", "role", "expected"),
    [
        # 三态主口径
        (True, False, "writer", "deny"),      # debug 开、显式开关关 -> 403
        (True, True, "writer", "allow"),      # debug 开、显式开关开 -> 放行
        (True, False, "admin", "allow"),      # 管理员不受开关限制
        # 边界
        (True, False, "superadmin", "allow"), # 超管同样放行
        (False, True, "writer", "deny"),      # 非 debug 时开关无效
        (False, False, "admin", "allow"),     # 非 debug 时管理员仍放行
        (False, False, "writer", "deny"),     # 非 debug 普通用户 403
    ],
)
def test_require_experimental_access_matrix(monkeypatch, debug, flag, role, expected):
    monkeypatch.setattr(experimental, "settings", _settings(debug=debug, flag=flag))

    if expected == "allow":
        assert experimental.require_experimental_access(_user(role)) is None
    else:
        with pytest.raises(HTTPException) as exc:
            experimental.require_experimental_access(_user(role))
        assert exc.value.status_code == 403


def test_flag_defaults_to_false():
    """配置项默认必须是 False（防止默认把实验接口暴露出去）。"""
    from app.core.config import Settings

    assert Settings.model_fields["experimental_open_in_debug"].default is False
