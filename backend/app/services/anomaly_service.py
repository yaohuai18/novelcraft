# 异常检测服务（第二十二批「数据分析、推荐系统和智能运营」规格二十五）
# 识别：异常登录/刷阅读量/异常评论增长/批量注册/异常AI消耗/恶意调用/异常支付/推荐内容突增。
# 处理：记录风险事件（RiskEvent）/降低权限/限制频率/暂时冻结/人工审核/恢复解除。
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.billing_finance import RiskEvent
from app.models.comment import Comment
from app.models.user import User

ANOMALY_TYPES = [
    "abnormal_login", "view_inflation", "comment_surge", "batch_registration",
    "ai_consumption_spike", "malicious_call", "payment_anomaly", "recommend_surge",
]

ANOMALY_STATUSES = ["open", "reviewed", "appealed", "resolved"]
ANOMALY_ACTIONS = ["flag", "freeze", "review", "delay", "verify"]

SEVERITY_MAP = {
    "abnormal_login": "high",
    "view_inflation": "high",
    "comment_surge": "medium",
    "batch_registration": "high",
    "ai_consumption_spike": "medium",
    "malicious_call": "high",
    "payment_anomaly": "high",
    "recommend_surge": "low",
}


def _now() -> datetime:
    return datetime.utcnow()


async def detect_anomalies(db: AsyncSession, *, window_hours: int = 1) -> list[dict]:
    """规则式异常检测，返回风险事件列表（记录到 RiskEvent）。"""
    now = _now()
    start = now - timedelta(hours=window_hours)
    events = (await db.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.received_at >= start)
    )).scalars().all()

    findings: list[dict] = []
    by_user: dict = defaultdict(int)
    by_type: dict = defaultdict(int)
    view_users: dict = defaultdict(int)
    comment_users: dict = defaultdict(int)
    login_users: dict = defaultdict(int)
    ai_users: dict = defaultdict(int)
    payment_users: dict = defaultdict(int)

    for e in events:
        by_user[e.user_id] += 1 if e.user_id else 0
        by_type[e.event_name] += 1
        if e.event_name == "reading.page_view" and e.user_id:
            view_users[e.user_id] += 1
        if e.event_name == "comment.created" and e.user_id:
            comment_users[e.user_id] += 1
        if e.event_name == "user.login" and e.user_id:
            login_users[e.user_id] += 1
        if e.event_name.startswith("ai.") and e.user_id:
            ai_users[e.user_id] += 1
        if e.event_name.startswith("payment.") and e.user_id:
            payment_users[e.user_id] += 1

    # 异常登录：1h 内登录 >10 次
    for uid, cnt in login_users.items():
        if cnt > 10:
            findings.append(_make_finding(db, "abnormal_login", uid, {"logins": cnt}))
    # 刷阅读量：1h 内 >200 次阅读
    for uid, cnt in view_users.items():
        if cnt > 200:
            findings.append(_make_finding(db, "view_inflation", uid, {"views": cnt}))
    # 异常评论增长：1h 内 >50 条评论
    for uid, cnt in comment_users.items():
        if cnt > 50:
            findings.append(_make_finding(db, "comment_surge", uid, {"comments": cnt}))
    # 批量注册：窗口内新注册 >20
    new_users = int((await db.execute(
        select(func.count(User.id)).where(User.created_at >= start)
    )).scalar_one() or 0)
    if new_users > 20:
        findings.append(_make_finding(db, "batch_registration", None, {"new_users": new_users}))
    # 异常 AI 消耗：1h 内 >100 次 AI 调用
    for uid, cnt in ai_users.items():
        if cnt > 100:
            findings.append(_make_finding(db, "ai_consumption_spike", uid, {"ai_calls": cnt}))
    # 恶意调用：1h 内 >500 事件（多事件轰炸）
    for uid, cnt in by_user.items():
        if cnt > 500:
            findings.append(_make_finding(db, "malicious_call", uid, {"events": cnt}))
    # 异常支付：同用户 1h 内 >5 支付事件
    for uid, cnt in payment_users.items():
        if cnt > 5:
            findings.append(_make_finding(db, "payment_anomaly", uid, {"payment_events": cnt}))
    # 推荐内容突增：推荐曝光事件 1h 内 >2000
    if by_type.get("recommend.displayed", 0) > 2000:
        findings.append(_make_finding(db, "recommend_surge", None, {"exposed": by_type["recommend.displayed"]}))

    await db.flush()
    return findings


def _make_finding(db, anomaly_type: str, user_id, params: dict) -> dict:
    r = RiskEvent(
        event_type=anomaly_type,
        user_id=user_id,
        severity=SEVERITY_MAP.get(anomaly_type, "medium"),
        action_taken="flag",
        status="open",
        evidence=json.dumps(params, ensure_ascii=False),
    )
    db.add(r)
    return {"type": anomaly_type, "user_id": str(user_id) if user_id else None, "params": params}


async def list_risk_events(db: AsyncSession, *, status: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(limit)
    if status:
        stmt = select(RiskEvent).where(RiskEvent.status == status).order_by(RiskEvent.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id), "type": r.event_type,
            "user_id": str(r.user_id) if r.user_id else None,
            "severity": r.severity,
            "params": json.loads(r.evidence or "{}"),
            "action_taken": r.action_taken,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def handle_risk(
    db: AsyncSession,
    *,
    risk_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID,
) -> dict:
    """处理风险事件：限制频率/降权/冻结/人工审核/恢复。"""
    r = (await db.execute(
        select(RiskEvent).where(RiskEvent.id == risk_id)
    )).scalar_one_or_none()
    if not r:
        return {"error": "risk_event_not_found"}
    if action in ("rate_limit", "demote", "delay", "verify"):
        r.action_taken = action
        r.status = "reviewed"
    elif action == "freeze":
        r.action_taken = "freeze"
        r.status = "reviewed"
        if r.user_id:
            u = (await db.execute(select(User).where(User.id == r.user_id))).scalar_one_or_none()
            if u:
                u.status = "frozen"
    elif action == "review":
        r.action_taken = "review"
        r.status = "reviewed"
    elif action == "restore":
        r.status = "resolved"
        if r.user_id:
            u = (await db.execute(select(User).where(User.id == r.user_id))).scalar_one_or_none()
            if u and u.status == "frozen":
                u.status = "active"
    else:
        return {"error": "invalid_action"}
    await db.flush()
    return {"status": "ok", "risk_id": str(risk_id), "action": action}
