"""模块开关（feature flag）：为未上线/高风险模块提供统一的关闭手段。

第三批 e 项。规则：

- `DISABLED_MODULES` 逗号分隔，例如 `enterprise,billing,analytics`。
- 默认空 → 全部开启，**不改变现有行为**（避免"改造即停机"）。
- 关闭一个模块只影响它的路由挂载：路径返回 404，不会半开半闭。
- 关闭是**启动时**决定的（include_router 阶段）；运行中改环境变量无效，
  这是刻意的：路由表在进程生命周期内保持稳定，便于排查。
- 该开关不是权限系统：它用于"把未验证模块整体下线"，不用于替代授权校验。

已登记的模块名见 `KNOWN_MODULES`；拼错的名字会在启动时打印警告，
避免 `DISABLED_MODULES=enterpirse` 这种静默失效。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.flags")

KNOWN_MODULES = (
    "enterprise",   # 企业版：组织/成员/文件分享/审阅
    "billing",      # 计费与订阅
    "i18n",         # 多语言内容
    "ops",          # 运营后台
    "analytics",    # 统计分析
    "security",     # 安全中心
    "openapi",      # 开放平台（token/app/webhook/移动草稿）
)


def parse_disabled_modules(raw: str | None) -> set[str]:
    if not raw:
        return set()
    names = {item.strip().lower() for item in raw.split(",") if item.strip()}
    unknown = sorted(names - set(KNOWN_MODULES))
    if unknown:
        logger.warning(
            "DISABLED_MODULES 含未登记的模块名：%s（已登记：%s），这些名字不会生效",
            unknown,
            list(KNOWN_MODULES),
        )
    return names & set(KNOWN_MODULES)


def module_enabled(name: str, *, disabled: set[str] | None = None) -> bool:
    """模块是否启用。`disabled` 显式传入时优先，便于测试。"""
    if disabled is None:
        try:
            from app.core.config import settings

            disabled = parse_disabled_modules(getattr(settings, "disabled_modules", ""))
        except Exception:  # noqa: BLE001 - 配置缺失时按"全部启用"处理
            disabled = set()
    return name not in disabled
