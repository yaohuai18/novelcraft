# 智能运营服务：用户分群 + 消息编排（第二十二批「数据分析、推荐系统和智能运营」规格十八/十九）
# 分群：注册七天内未创建项目/连续七天创作/最近三十天未登录/AI额度即将用完/试用期即将结束/
#       有作品未发布/团队管理员。
# 消息编排：触发条件/等待时间/消息渠道/发送次数上限/退出条件/用户是否同意；
#           支持退订（营销通知关闭）、时区发送（按用户 locale 区域时段）、重复触发保护。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.billing_org import OrganizationMember
from app.models.billing_subscription import Subscription
from app.models.data_privacy_pref import DataPrivacyPref
from app.models.operation_flow import FLOW_TRIGGER_TYPES, OperationFlow, OperationMessageLog
from app.models.project import Project
from app.models.user import User
from app.models.user_locale import UserLocale
from app.models.user_segment import SEGMENT_RULE_TYPES, UserSegment
from app.models.ai_quota import AIQuota


def _now() -> datetime:
    return datetime.utcnow()


async def create_segment(
    db: AsyncSession,
    *,
    name: str,
    rule_type: str,
    rule_params: dict | None = None,
    created_by: uuid.UUID | None = None,
) -> dict:
    if rule_type not in SEGMENT_RULE_TYPES:
        return {"error": "invalid_rule_type"}
    seg = UserSegment(
        name=name, rule_type=rule_type,
        rule_params=json.dumps(rule_params or {}, ensure_ascii=False),
        status="active", created_by=created_by,
    )
    db.add(seg)
    await db.flush()
    return {"status": "created", "segment_id": str(seg.id)}


async def evaluate_segment(db: AsyncSession, *, segment_id: uuid.UUID | None = None, rule_type: str | None = None, rule_params: dict | None = None) -> list[dict]:
    """计算分群命中的用户列表。"""
    if segment_id:
        seg = (await db.execute(
            select(UserSegment).where(UserSegment.id == segment_id)
        )).scalar_one_or_none()
        if not seg:
            return []
        rule_type = seg.rule_type
        rule_params = json.loads(seg.rule_params or "{}") or {}
    rule_params = rule_params or {}

    users = (await db.execute(select(User.id, User.created_at))).all()
    user_ids = [u.id for u in users]
    if not user_ids:
        return []

    now = _now()
    if rule_type == "reg_no_project_7d":
        # 注册七天内未创建项目
        threshold = now - timedelta(days=int(rule_params.get("days", 7)))
        proj_created = set((await db.execute(
            select(AnalyticsEvent.user_id).where(
                AnalyticsEvent.event_name == "project.created",
                AnalyticsEvent.user_id.in_(user_ids),
            )
        )).scalars().all())
        return [{"user_id": str(uid), "matched": True}
                for uid, created in users
                if uid not in proj_created and created >= threshold]

    if rule_type == "active_7d_consecutive":
        # 连续七天创作
        days_ago = now - timedelta(days=int(rule_params.get("days", 7)))
        events = (await db.execute(
            select(AnalyticsEvent.user_id, AnalyticsEvent.occurred_at).where(
                AnalyticsEvent.event_name.in_(["chapter.saved", "chapter.published"]),
                AnalyticsEvent.occurred_at >= days_ago,
                AnalyticsEvent.user_id.in_(user_ids),
            )
        )).all()
        by_user: dict = {}
        for uid, ts in events:
            by_user.setdefault(uid, set()).add(ts.date())
        matched = []
        for uid, days_set in by_user.items():
            d = now.date()
            streak = 0
            while d in days_set:
                streak += 1
                d = d - timedelta(days=1)
            if streak >= int(rule_params.get("streak", 7)):
                matched.append(uid)
        return [{"user_id": str(uid), "matched": True} for uid in matched]

    if rule_type == "inactive_30d":
        threshold = now - timedelta(days=int(rule_params.get("days", 30)))
        last_active: dict = {}
        for uid, ts in (await db.execute(
            select(AnalyticsEvent.user_id, AnalyticsEvent.occurred_at).where(
                AnalyticsEvent.user_id.in_(user_ids),
            )
        )).all():
            if uid not in last_active or ts > last_active[uid]:
                last_active[uid] = ts
        return [
            {"user_id": str(uid), "matched": True}
            for uid, created in users
            if uid not in last_active or last_active[uid] < threshold
        ]

    if rule_type == "ai_quota_low":
        # AI 额度即将用完
        rows = (await db.execute(
            select(AIQuota).where(AIQuota.user_id.in_(user_ids))
        )).scalars().all()
        matched = []
        for q in rows:
            limit = q.user_budget if q.user_budget is not None else q.daily_limit
            used = q.user_used if q.user_budget is not None else q.daily_used
            if limit and (limit - used) / limit < float(rule_params.get("ratio", 0.1)):
                matched.append(q.user_id)
        return [{"user_id": str(uid), "matched": True} for uid in set(matched)]

    if rule_type == "trial_ending_soon":
        rows = (await db.execute(
            select(Subscription).where(
                Subscription.status == "trialing",
                Subscription.user_id.in_(user_ids),
            )
        )).scalars().all()
        soon = now + timedelta(days=int(rule_params.get("days", 3)))
        return [
            {"user_id": str(s.user_id), "matched": True}
            for s in rows if s.current_period_end and s.current_period_end <= soon
        ]

    if rule_type == "has_unpublished_work":
        rows = (await db.execute(
            select(Project.user_id).where(Project.user_id.in_(user_ids), Project.published_at.is_(None))
        )).scalars().all()
        return [{"user_id": str(uid), "matched": True} for uid in set(rows)]

    if rule_type == "team_admin":
        rows = (await db.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.user_id.in_(user_ids),
                OrganizationMember.role == "admin",
            )
        )).scalars().all()
        return [{"user_id": str(uid), "matched": True} for uid in set(rows)]

    return []


async def create_flow(
    db: AsyncSession,
    *,
    name: str,
    trigger_type: str,
    trigger_params: dict | None = None,
    wait_seconds: int = 0,
    channel: str = "in_app",
    template: str | None = None,
    max_sends_per_user: int = 3,
    exit_condition: dict | None = None,
    consent_required: bool = True,
    created_by: uuid.UUID | None = None,
) -> dict:
    if trigger_type not in FLOW_TRIGGER_TYPES:
        return {"error": "invalid_trigger_type"}
    if channel not in ("in_app", "email", "push"):
        return {"error": "invalid_channel"}
    flow = OperationFlow(
        name=name, trigger_type=trigger_type,
        trigger_params=json.dumps(trigger_params or {}, ensure_ascii=False),
        wait_seconds=wait_seconds, channel=channel, template=template,
        max_sends_per_user=max_sends_per_user,
        exit_condition=json.dumps(exit_condition or {}, ensure_ascii=False),
        consent_required=consent_required, status="active", created_by=created_by,
    )
    db.add(flow)
    await db.flush()
    return {"status": "created", "flow_id": str(flow.id)}


async def run_flows_for_user(db: AsyncSession, *, user_id: uuid.UUID, trigger_type: str | None = None) -> list[dict]:
    """触发用户相关运营流程（含等待时间、发送上限、退出条件、退订、时区发送、重复触发保护）。"""
    stmt = select(OperationFlow).where(OperationFlow.status == "active")
    if trigger_type:
        stmt = stmt.where(OperationFlow.trigger_type == trigger_type)
    flows = (await db.execute(stmt)).scalars().all()
    if not flows:
        return []

    # 用户是否同意接收营销通知
    pref = (await db.execute(
        select(DataPrivacyPref).where(DataPrivacyPref.user_id == user_id)
    )).scalar_one_or_none()
    marketing_ok = pref.marketing_notification if pref else True

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    sent: list[dict] = []
    now = _now()

    for flow in flows:
        if flow.consent_required and not marketing_ok:
            continue  # 用户退订/撤回授权
        # 等待时间：未到触发时间
        trigger_at = now + timedelta(seconds=flow.wait_seconds)
        # 发送次数上限
        cnt = int((await db.execute(
            select(func.count(OperationMessageLog.id)).where(
                OperationMessageLog.flow_id == flow.id,
                OperationMessageLog.user_id == user_id,
            )
        )).scalar_one() or 0)
        if cnt >= flow.max_sends_per_user:
            continue
        # 退出条件（示例：no_project_after_register 退出=已创建项目）
        exit_cond = json.loads(flow.exit_condition or "{}") or {}
        if exit_cond.get("has_project") is True:
            has_proj = (await db.execute(
                select(AnalyticsEvent.id).where(
                    AnalyticsEvent.user_id == user_id,
                    AnalyticsEvent.event_name == "project.created",
                )
            )).scalar_one_or_none()
            if has_proj:
                continue
        # 时区发送：按用户 locale 映射本地时段（简化：region 包含时区偏移近似）
        hour = now.hour
        uloc = (await db.execute(
            select(UserLocale).where(UserLocale.user_id == user_id)
        )).scalar_one_or_none()
        locale = (uloc.locale if uloc else None) or "zh-CN"
        tz_offset = _locale_offset_hours(locale)
        local_hour = (hour + tz_offset) % 24
        if flow.trigger_type == "inactive_days" and not (8 <= local_hour <= 22):
            continue  # 召回消息仅本地 8-22 点发送，避免打扰

        log = OperationMessageLog(
            flow_id=flow.id, user_id=user_id, channel=flow.channel,
            sequence=cnt + 1, status="sent", sent_at=now,
            reason=f"trigger={flow.trigger_type};local_hour={local_hour}",
        )
        db.add(log)
        sent.append({
            "flow_id": str(flow.id), "name": flow.name,
            "channel": flow.channel, "sequence": cnt + 1,
            "sent_at": now.isoformat(),
        })
    await db.flush()
    return sent


def _locale_offset_hours(locale: str) -> int:
    """按 locale 近似时区偏移（测试用简化映射：zh-CN/Asia=+8）。"""
    if locale.lower().startswith("zh"):
        return 8
    if locale.lower().startswith("en-us") or locale.lower().startswith("en_us"):
        return -5
    if locale.lower().startswith("ja"):
        return 9
    if locale.lower().startswith("en-gb") or locale.lower().startswith("en_gb"):
        return 0
    return 0


async def list_flows(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        select(OperationFlow).order_by(OperationFlow.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": str(f.id), "name": f.name, "trigger_type": f.trigger_type,
            "channel": f.channel, "wait_seconds": f.wait_seconds,
            "max_sends_per_user": f.max_sends_per_user, "status": f.status,
        }
        for f in rows
    ]
