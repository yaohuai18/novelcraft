# AI 额度账户服务（第二十批「商业化、订阅与计费系统」规格九/十）
# QuotaAccount + QuotaLedger：
#   - grant   发放/充值额度
#   - reserve 预扣（提交 AI 任务时估算最大消耗）
#   - consume 结算实际消耗（将预扣转为已用）
#   - release 任务成功多扣部分释放 / 可恢复错误释放预扣
#   - refund  退款回补
#   - expire  过期回收
#   - adjust  管理员调整
# 预扣机制保证并发请求无法绕过额度限制。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing_quota import QuotaAccount, QuotaLedger
from app.models.billing_finance import LedgerEntry

QUOTA_ALIASES = {
    "tokens": "ai_tokens",
    "ai_tokens": "ai_tokens",
    "tasks": "ai_tasks",
    "ai_tasks": "ai_tasks",
    "storage": "storage_bytes",
    "storage_bytes": "storage_bytes",
    "exports": "exports",
    "export": "exports",
}


def normalize_quota_type(qtype: str) -> str:
    return QUOTA_ALIASES.get(qtype, qtype)


async def _ensure_account(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    expires_at: datetime | None = None,
) -> QuotaAccount:
    qtype = normalize_quota_type(quota_type)
    result = await db.execute(
        select(QuotaAccount).where(
            QuotaAccount.owner_type == owner_type,
            QuotaAccount.owner_id == owner_id,
            QuotaAccount.quota_type == qtype,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        account = QuotaAccount(
            owner_type=owner_type,
            owner_id=owner_id,
            quota_type=qtype,
            total_amount=0,
            used_amount=0,
            reserved_amount=0,
            expires_at=expires_at,
        )
        db.add(account)
        await db.flush()
    return account


async def _append_ledger(
    db: AsyncSession,
    account: QuotaAccount,
    action: str,
    amount: int,
    reference_type: str | None = None,
    reference_id: str | None = None,
    note: str | None = None,
) -> None:
    db.add(
        QuotaLedger(
            account_id=account.id,
            reference_type=reference_type,
            reference_id=str(reference_id) if reference_id else None,
            action=action,
            amount=amount,
            balance_after=account.available_amount,
            note=note,
        )
    )


async def get_account(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
) -> QuotaAccount | None:
    result = await db.execute(
        select(QuotaAccount).where(
            QuotaAccount.owner_type == owner_type,
            QuotaAccount.owner_id == owner_id,
            QuotaAccount.quota_type == normalize_quota_type(quota_type),
        )
    )
    return result.scalar_one_or_none()


async def grant_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    *,
    expires_at: datetime | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    note: str | None = None,
    operator_id: uuid.UUID | None = None,
) -> QuotaAccount:
    """发放/充值额度（订阅权益、优惠赠送、管理员补发）。"""
    if amount < 0:
        raise ValueError("发放额度不能为负")
    qtype = normalize_quota_type(quota_type)
    account = await _ensure_account(
        db, owner_type, owner_id, qtype, expires_at=expires_at
    )
    account.total_amount += amount
    if expires_at is not None:
        account.expires_at = expires_at
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "grant", amount,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
    )
    if operator_id is not None and amount > 0:
        db.add(
            LedgerEntry(
                entry_type="quota_adjust",
                user_id=owner_id if owner_type == "user" else operator_id,
                organization_id=owner_id if owner_type == "org" else None,
                amount=0,
                currency="CNY",
                direction="out",
                reference_id=str(reference_id) if reference_id else None,
                operator_id=operator_id,
                reason=note or f"发放额度 {qtype} +{amount}",
            )
        )
    await db.commit()
    await db.refresh(account)
    return account


async def reserve_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    reference_id: str,
    *,
    note: str | None = None,
) -> QuotaAccount:
    """预扣额度（提交 AI 任务时）。预扣失败抛 ValueError，防止并发绕过限制。"""
    if amount < 0:
        raise ValueError("预扣额度不能为负")
    account = await _ensure_account(db, owner_type, owner_id, quota_type)
    if account.available_amount < amount:
        raise ValueError(
            f"额度不足: 可用 {account.available_amount}, 需要 {amount}"
        )
    account.reserved_amount += amount
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "reserve", amount,
        reference_type="ai_task",
        reference_id=reference_id,
        note=note,
    )
    await db.commit()
    await db.refresh(account)
    return account


async def consume_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    reference_id: str,
    *,
    reserved: int = 0,
    note: str | None = None,
) -> QuotaAccount:
    """结算实际消耗：把预扣转为已用（reserved 为本次预扣量，释放多余部分）。"""
    account = await _ensure_account(db, owner_type, owner_id, quota_type)
    if reserved < 0:
        raise ValueError("reserved 不能为负")
    if amount > account.reserved_amount + account.available_amount:
        raise ValueError("实际消耗超出账户额度")
    account.used_amount += amount
    release_back = max(0, reserved - amount)
    if reserved > 0:
        account.reserved_amount -= reserved
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "consume", amount,
        reference_type="ai_task",
        reference_id=reference_id,
        note=note,
    )
    if release_back > 0:
        await _append_ledger(
            db, account, "release", release_back,
            reference_type="ai_task",
            reference_id=reference_id,
            note="任务结算多扣释放",
        )
    await db.commit()
    await db.refresh(account)
    return account


async def release_reserved(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    reference_id: str,
    *,
    note: str | None = None,
) -> QuotaAccount:
    """释放预扣额度（任务失败可恢复错误 / 系统补偿）。"""
    account = await _ensure_account(db, owner_type, owner_id, quota_type)
    actual = min(amount, account.reserved_amount)
    account.reserved_amount -= actual
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "release", actual,
        reference_type="ai_task",
        reference_id=reference_id,
        note=note,
    )
    await db.commit()
    await db.refresh(account)
    return account


async def refund_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    *,
    reference_type: str | None = None,
    reference_id: str | None = None,
    note: str | None = None,
) -> QuotaAccount:
    """退款回补已使用额度。"""
    account = await _ensure_account(db, owner_type, owner_id, quota_type)
    actual = min(amount, account.used_amount)
    account.used_amount -= actual
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "refund", actual,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
    )
    await db.commit()
    await db.refresh(account)
    return account


async def adjust_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    amount: int,
    operator_id: uuid.UUID,
    *,
    reason: str = "管理员调整额度",
    expires_at: datetime | None = None,
) -> QuotaAccount:
    """管理员调整额度（正数补发，负数回收已用）。"""
    if amount >= 0:
        return await grant_quota(
            db, owner_type, owner_id, quota_type, amount,
            expires_at=expires_at,
            reference_type="admin",
            operator_id=operator_id,
            note=reason,
        )
    account = await _ensure_account(db, owner_type, owner_id, quota_type)
    absorb = min(-amount, account.used_amount)
    account.used_amount -= absorb
    account.updated_at = datetime.utcnow()
    await _append_ledger(
        db, account, "adjust", -amount,
        reference_type="admin",
        note=reason,
    )
    db.add(
        LedgerEntry(
            entry_type="quota_adjust",
            user_id=owner_id if owner_type == "user" else operator_id,
            organization_id=owner_id if owner_type == "org" else None,
            amount=0,
            currency="CNY",
            direction="out",
            operator_id=operator_id,
            reason=reason,
        )
    )
    await db.commit()
    await db.refresh(account)
    return account


async def expire_quota(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
) -> QuotaAccount | None:
    """到期回收：将过期账户的可用与预扣额度清零，保留已用流水。"""
    account = await get_account(db, owner_type, owner_id, quota_type)
    if not account or not account.expires_at or account.expires_at > datetime.utcnow():
        return account
    leftover = account.available_amount + account.reserved_amount
    if leftover > 0:
        account.total_amount -= leftover
        account.reserved_amount = 0
        account.updated_at = datetime.utcnow()
        await _append_ledger(
            db, account, "expire", leftover,
            note="额度到期回收",
        )
        await db.commit()
        await db.refresh(account)
    return account


async def ai_task_settlement(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
    quota_type: str,
    task_id: uuid.UUID,
    *,
    estimated: int,
    actual: int,
    success: bool,
    recoverable: bool = True,
) -> QuotaAccount:
    """AI 任务预扣-结算闭环：
    - 任务成功：按实际消耗结算，释放多扣部分
    - 任务失败可恢复：释放全部预扣
    - 任务失败不可恢复：按实际消耗结算
    """
    if not success and recoverable:
        return await release_reserved(
            db, owner_type, owner_id, quota_type, estimated,
            reference_id=str(task_id),
            note="AI 任务失败（可恢复错误），释放预扣额度",
        )
    return await consume_quota(
        db, owner_type, owner_id, quota_type, actual,
        reference_id=str(task_id),
        reserved=estimated if success else 0,
        note="AI 任务结算" if success else "AI 任务失败（不可恢复），按实际消耗处理",
    )


async def get_usage_summary(
    db: AsyncSession,
    owner_type: str,
    owner_id: uuid.UUID,
) -> list[dict]:
    """查询用户/组织的额度汇总（含可用/已用/预扣/到期时间）。"""
    result = await db.execute(
        select(QuotaAccount).where(
            QuotaAccount.owner_type == owner_type,
            QuotaAccount.owner_id == owner_id,
        )
    )
    accounts = result.scalars().all()
    summary = []
    for acc in accounts:
        summary.append(
            {
                "quota_type": acc.quota_type,
                "total": acc.total_amount,
                "used": acc.used_amount,
                "reserved": acc.reserved_amount,
                "available": acc.available_amount,
                "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
            }
        )
    return summary


async def list_ledger(
    db: AsyncSession,
    account_id: uuid.UUID,
    limit: int = 100,
) -> list[QuotaLedger]:
    result = await db.execute(
        select(QuotaLedger)
        .where(QuotaLedger.account_id == account_id)
        .order_by(QuotaLedger.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def add_trial_quota(db: AsyncSession, user_id: uuid.UUID) -> None:
    """试用额度：新用户默认发放（免费套餐每月额度已在 entitlement 中体现，
    此处为「试用期」体验额度，7 天有效）。"""
    await grant_quota(
        db, "user", user_id, "ai_tokens", 5000,
        expires_at=datetime.utcnow() + timedelta(days=7),
        reference_type="trial",
        note="新用户试用额度",
    )
