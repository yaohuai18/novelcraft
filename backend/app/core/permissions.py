# 运营后台权限（第十七批「统计分析与运营后台」规格十二 + 第二十批「商业化、订阅与计费系统」规格十八）
# 角色：
#   super_admin     管理系统配置和管理员
#   operator        管理用户、内容和运营数据
#   support         查看用户问题和处理工单
#   auditor         查看审计日志
#   analyst         查看脱敏统计数据
#   billing_admin   管理订单和订阅（第二十批）
#   finance_admin   查看账务和对账（第二十批，默认无私人正文查看权限）
#   support_admin   处理退款申请（第二十批）
#   promotion_admin 管理优惠活动（第二十批）
# 普通用户（user）无后台权限；私有内容访问必须走 PrivateAccessGrant 临时授权。
import hashlib
import uuid
from datetime import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.private_access_grant import PrivateAccessGrant
from app.models.user import User

ADMIN_ROLES = {
    "super_admin",
    "operator",
    "support",
    "auditor",
    "analyst",
    "billing_admin",
    "finance_admin",
    "support_admin",
    "promotion_admin",
}

ROLE_PERMISSIONS = {
    "super_admin": {"users", "content", "system", "audit", "reports", "tickets", "settings", "billing", "finance", "refunds", "promotions"},
    "operator": {"users", "content", "reports", "tickets"},
    "support": {"tickets"},
    "auditor": {"audit"},
    "analyst": {"reports"},
    "billing_admin": {"billing"},
    "finance_admin": {"finance"},
    "support_admin": {"refunds"},
    "promotion_admin": {"promotions"},
}

# 商业化权限点（第二十批规格十八）
BILLING_PERMISSIONS = {
    "billing.orders",          # 查看/管理订单
    "billing.subscriptions",   # 查看/管理订阅
    "billing.plans",           # 管理套餐与价格
    "billing.payments",        # 处理支付回调/异常支付
    "finance.ledger",          # 查看账务
    "finance.reconciliation",  # 对账管理
    "finance.reports",         # 财务报表
    "refunds.process",         # 处理退款申请
    "promotions.manage",       # 管理优惠活动
    "quota.adjust",            # 额度调整
}


def require_admin_roles(*roles: str):
    """依赖工厂：要求当前用户具备指定后台角色之一（不传则任意后台角色）。"""
    allowed = set(roles) or ADMIN_ROLES

    async def dependency(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if current_user.role not in ADMIN_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无运营后台权限"
            )
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前角色无权执行该操作"
            )
        return current_user

    return dependency


def can_access_permission(role: str, permission: str) -> bool:
    """判断角色是否拥有指定权限点。"""
    return permission in ROLE_PERMISSIONS.get(role, set())


async def check_private_access(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
) -> bool:
    """校验用户是否拥有目标项目的私有内容临时授权（未过期且为 active）。"""
    if user.role == "super_admin":
        return True
    result = await db.execute(
        select(PrivateAccessGrant).where(
            PrivateAccessGrant.granted_to == user.id,
            PrivateAccessGrant.project_id == project_id,
            PrivateAccessGrant.status == "active",
        )
    )
    grants = result.scalars().all()
    now = datetime.utcnow()
    for grant in grants:
        if grant.expires_at and grant.expires_at > now:
            return True
        if grant.expires_at and grant.expires_at <= now:
            grant.status = "expired"
    if grants:
        await db.commit()
    return False


def hash_ip(ip: str | None) -> str | None:
    """IP 地址脱敏：SHA-256 哈希，不保存原始 IP。"""
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def mask_email(email: str | None) -> str | None:
    """邮箱脱敏：a***@example.com。"""
    if not email:
        return None
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        return f"*@ {domain}" if False else f"*@{domain}"
    return f"{name[0]}***@{domain}"


def mask_username(username: str | None) -> str | None:
    """用户名脱敏：保留首字符，其余打码。"""
    if not username:
        return None
    if len(username) <= 1:
        return "*"
    return f"{username[0]}***"


def mask_text(text: str | None, keep_head: int = 2, keep_tail: int = 0) -> str | None:
    """正文/理由等长文本脱敏：仅保留首尾少量字符。"""
    if not text:
        return None
    if len(text) <= keep_head + keep_tail + 3:
        return "***"
    return f"{text[:keep_head]}...{text[-keep_tail:] if keep_tail else ''}"


async def get_db_session() -> AsyncSession:
    """兼容别名：直接使用 get_db 依赖。"""
    async for session in get_db():
        yield session
