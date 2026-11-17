# 二次确认令牌服务（第十八批「性能优化、可靠性与安全加固」规格十九）
# 高风险操作二次确认：删除项目 / 覆盖恢复 / 改所有者 / 导出协作数据 / 查看私人内容 / 修改 API 密钥。
# 流程：业务方生成确认令牌（服务端仅存哈希 + 操作类型 + 归属用户），用户确认时校验令牌、
# 动作与归属匹配，单次使用后立即作废；令牌带过期时间。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security_utils import generate_secure_token, sha256_hex
from app.models.confirmation_token import ConfirmationToken

HIGH_RISK_ACTIONS = {
    "delete_project",
    "overwrite_restore",
    "change_owner",
    "export_collaboration_data",
    "view_private_content",
    "change_api_key",
    "delete_account",
}


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    action: str,
) -> tuple[ConfirmationToken, str]:
    """创建二次确认令牌：返回 (记录, 明文令牌)，服务端仅存哈希。"""
    if action not in HIGH_RISK_ACTIONS:
        raise ValueError(f"未知高风险操作: {action}")
    plain = generate_secure_token(32)
    row = ConfirmationToken(
        user_id=user_id,
        action=action,
        token_hash=sha256_hex(plain),
        expires_at=datetime.utcnow() + timedelta(
            minutes=settings.confirmation_token_expire_minutes
        ),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, plain


async def confirm(
    db: AsyncSession,
    *,
    token: str,
    action: str,
    user_id: uuid.UUID,
) -> ConfirmationToken | None:
    """校验并消费确认令牌：动作 + 归属必须匹配，单次使用后作废。"""
    row = (
        await db.execute(
            select(ConfirmationToken).where(
                ConfirmationToken.token_hash == sha256_hex(token),
                ConfirmationToken.action == action,
                ConfirmationToken.user_id == user_id,
                ConfirmationToken.used_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    await db.commit()
    await db.refresh(row)
    return row
