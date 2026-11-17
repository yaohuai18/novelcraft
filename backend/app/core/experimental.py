# 实验接口访问控制（P1 第二批收尾）：
# 实验接口默认关闭，仅「开发模式 + 显式开关（settings.experimental_open_in_debug=True）」
# 或平台管理员（user.role in admin/superadmin）可访问，其余一律 403。
# 仅 debug=True 不再自动放行，避免开发默认值把实验接口带到生产。
# OpenAPI 分组通过各 router 的 tags 追加「实验」实现。
from fastapi import Depends, HTTPException, status

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.user import User

EXPERIMENTAL_TAG = "实验"

# 平台管理员角色（区别于组织内 owner/admin）
ADMIN_ROLES = {"admin", "superadmin"}


def require_experimental_access(
    current_user: User = Depends(get_current_user),
) -> None:
    """实验接口访问闸门：需「开发模式 + 显式开关」，或平台管理员。"""
    if settings.debug and settings.experimental_open_in_debug:
        return
    if current_user.role in ADMIN_ROLES:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="实验接口未开放：仅开发模式（需显式开启 experimental_open_in_debug）或平台管理员可访问",
    )
