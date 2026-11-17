# 数据隐私控制服务（第二十二批「数据分析、推荐系统和智能运营」规格二十一）
# 用户可关闭：个性化推荐/行为分析/营销通知/数据共享/AI数据使用；
# 提供：导出个人行为数据、删除分析数据、撤回运营授权、查看数据使用说明；
# 关闭分析后仍保留必要安全/计费/审计记录（AuditLog 保留，行为事件不删安全类）。
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.data_privacy_pref import DataPrivacyPref

SAFE_EVENT_NAMES = {"user.login", "user.login_failed", "payment.order_created", "audit.admin_action"}


def _now() -> datetime:
    return datetime.utcnow()


async def get_or_create_pref(db: AsyncSession, *, user_id: uuid.UUID) -> DataPrivacyPref:
    pref = (await db.execute(
        select(DataPrivacyPref).where(DataPrivacyPref.user_id == user_id)
    )).scalar_one_or_none()
    if not pref:
        pref = DataPrivacyPref(user_id=user_id)
        db.add(pref)
        await db.flush()
    return pref


async def update_preferences(db: AsyncSession, *, user_id: uuid.UUID, prefs: dict) -> dict:
    p = await get_or_create_pref(db, user_id=user_id)
    # 契约字段 -> 模型字段 映射（外部使用 personalized_recommendation/behavior_analysis）
    field_map = {
        "personalized_recommendation": "personal_recommendation",
        "behavior_analysis": "behavior_analytics",
        "behavior_analytics": "behavior_analytics",
        "marketing_notification": "marketing_notification",
        "data_sharing": "data_sharing",
        "ai_data_usage": "ai_data_usage",
    }
    for key, value in prefs.items():
        attr = field_map.get(key)
        if attr:
            setattr(p, attr, bool(value))
    await db.flush()
    return {"status": "updated"}


async def get_preferences(db: AsyncSession, *, user_id: uuid.UUID) -> dict:
    p = await get_or_create_pref(db, user_id=user_id)
    return {
        "personalized_recommendation": p.personal_recommendation,
        "behavior_analytics": p.behavior_analytics,
        "marketing_notification": p.marketing_notification,
        "data_sharing": p.data_sharing,
        "ai_data_usage": p.ai_data_usage,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def export_behavior_data(db: AsyncSession, *, user_id: uuid.UUID) -> dict:
    """导出个人行为数据（不含正文/密码/凭证/令牌，事件 properties 已脱敏）。"""
    events = (await db.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.user_id == user_id
        ).order_by(AnalyticsEvent.occurred_at.asc())
    )).scalars().all()
    items = [
        {
            "event_name": e.event_name,
            "occurred_at": e.occurred_at.isoformat(),
            "received_at": e.received_at.isoformat(),
            "session_id": str(e.session_id) if e.session_id else None,
            "project_id": str(e.project_id) if e.project_id else None,
            "object_type": e.object_type,
            "object_id": str(e.object_id) if e.object_id else None,
            "properties": json.loads(e.properties or "{}"),
            "device_type": e.device_type,
            "app_version": e.app_version,
            "locale": e.locale,
            "region": e.region,
        }
        for e in events
    ]
    return {"user_id": str(user_id), "count": len(items), "events": items}


async def delete_analysis_data(db: AsyncSession, *, user_id: uuid.UUID) -> dict:
    """删除分析数据：删除行为事件；保留安全/计费/审计类事件。"""
    events = (await db.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.user_id == user_id)
    )).scalars().all()
    deleted = 0
    kept = 0
    for e in events:
        if e.event_name in SAFE_EVENT_NAMES:
            kept += 1
            continue
        await db.delete(e)
        deleted += 1
    # 关闭所有分析类偏好
    p = await get_or_create_pref(db, user_id=user_id)
    p.behavior_analytics = False
    p.personalized_recommendation = False
    p.data_sharing = False
    await db.flush()
    return {"status": "deleted", "deleted_events": deleted, "kept_safe_events": kept}


async def revoke_marketing(db: AsyncSession, *, user_id: uuid.UUID) -> dict:
    """撤回运营授权（退订营销通知）。"""
    p = await get_or_create_pref(db, user_id=user_id)
    p.marketing_notification = False
    await db.flush()
    return {"status": "revoked"}


async def data_usage_policy() -> dict:
    """数据使用说明。"""
    return {
        "purpose": ["产品优化", "用户引导", "创作建议", "内容推荐", "商业化运营", "系统容量规划", "安全和风控"],
        "what_we_collect": "行为事件（事件名/时间/来源/统计特征），不采集正文、密码、支付凭证、令牌",
        "retention": "分析数据可在用户请求时删除；安全/计费/审计记录依法保留",
        "controls": [
            "关闭个性化推荐", "关闭行为分析", "关闭营销通知", "关闭数据共享", "关闭AI数据使用",
            "导出个人行为数据", "删除分析数据", "撤回运营授权",
        ],
    }
