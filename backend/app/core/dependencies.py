# 兼容别名：get_current_user 已迁移至 core/auth.py（入口与规格一致）
# 保留本文件仅用于兼容旧引用，新代码请从 app.core.auth 导入。
from app.core.auth import bearer_scheme, get_current_user

__all__ = ["bearer_scheme", "get_current_user"]
