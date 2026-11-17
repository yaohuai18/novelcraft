# 第二十二批「数据分析、推荐系统和智能运营」- 数据分析/聚合/漏斗/目标/隐私/质量/异常 路由
# 事件采集 POST /api/analytics/events（event_id 幂等去重、敏感字段清洗）
# 聚合统计 GET /api/analytics/stats/{kind}（user/creation/content/commercial）
# 运营后台 GET /api/analytics/dashboard（admin）
# 用户阶段与转化 GET /api/analytics/funnel、/api/analytics/users/{user_id}/stage
# 个人创作分析 GET /api/analytics/personal；作品分析 GET /api/analytics/projects/{project_id}（权限判断）
# 创作目标 /api/analytics/goals（CRUD + 自动累计 + 提醒）
# 数据隐私 /api/analytics/privacy（偏好/导出/删除/撤回/说明）
# 数据质量 /api/analytics/quality（快照/历史/回放/死信重放）
# 异常检测 /api/analytics/anomalies（检测/列表/处理）
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import require_admin_roles
from app.models.project import Project
from app.models.user import User
from app.services import (
    aggregation_service,
    analytics_service,
    anomaly_service,
    funnel_service,
    goal_service,
    personal_analytics_service,
    privacy_control_service,
    quality_service,
)

router = APIRouter(prefix="/api/analytics", tags=["batch22-analytics", "实验"])


# ---------- 事件采集 ----------
class EventIn(BaseModel):
    event_name: str
    event_id: str
    user_id: str | None = None
    anonymous_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    properties: dict[str, Any] | None = None
    device_type: str | None = None
    app_version: str | None = None
    locale: str | None = None
    region: str | None = None
    occurred_at: datetime | None = None


@router.post("/events")
async def ingest_event(
    body: EventIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """采集行为事件：event_id 幂等去重、事件名校验、敏感字段清洗。"""
    result = await analytics_service.ingest_event(
        db,
        event_name=body.event_name,
        event_id=body.event_id,
        user_id=uuid.UUID(body.user_id) if body.user_id else current_user.id,
        anonymous_id=body.anonymous_id,
        session_id=body.session_id,
        project_id=uuid.UUID(body.project_id) if body.project_id else None,
        object_type=body.object_type,
        object_id=body.object_id,
        properties=body.properties,
        device_type=body.device_type,
        app_version=body.app_version,
        locale=body.locale,
        region=body.region,
        occurred_at=body.occurred_at,
    )
    await db.commit()
    return result


# ---------- 聚合统计 ----------
@router.get("/stats/{kind}")
async def aggregated_stats(
    kind: str,
    days: int = Query(30, ge=1, le=365),
    region: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """数据分层聚合统计：user/creation/content/commercial。"""
    if kind == "user":
        data = await aggregation_service.user_metrics(db, days=days, region=region)
    elif kind == "creation":
        data = await aggregation_service.creation_metrics(
            db, user_id=uuid.UUID(user_id) if user_id else None
        )
    elif kind == "content":
        data = await aggregation_service.content_metrics(
            db, project_id=uuid.UUID(project_id) if project_id else None, days=days
        )
    elif kind == "commercial":
        data = await aggregation_service.commercial_metrics(db, days=days)
    else:
        raise HTTPException(status_code=400, detail="kind 仅支持 user/creation/content/commercial")
    return data


@router.get("/dashboard")
async def analytics_dashboard(
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """管理员分析后台（用户/创作/内容/阅读/AI/商业化/地区/系统性能）。"""
    return await aggregation_service.admin_dashboard(db)


# ---------- 用户阶段与转化 ----------
@router.get("/funnel")
async def user_funnel(
    days: int = Query(90, ge=1, le=365),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """用户阶段转化漏斗（注册→创建→完成→发布→稳定/高频→付费→团队→沉默/流失）。"""
    return await funnel_service.user_funnel(db, days=days)


@router.get("/users/{user_id}/stage")
async def user_stage(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """单个用户当前阶段判定。"""
    return await funnel_service.user_stage(db, user_id=uuid.UUID(user_id))


# ---------- 个人创作分析与作品分析 ----------
@router.get("/personal")
async def personal_creation_analysis(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """个人创作统计：每日/每周/每月字数、时长、连续天数、目标完成率、趋势对比。"""
    return await personal_analytics_service.personal_creation_analysis(db, current_user.id)


@router.get("/projects/{project_id}")
async def project_analysis(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """作品分析；对外公开数据必须权限判断：非公开作品仅属主/后台角色可查看。"""
    project = (await db.execute(
        select(Project).where(Project.id == uuid.UUID(project_id))
    )).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    is_owner = project.user_id == current_user.id
    is_admin = current_user.role in ("super_admin", "operator", "analyst", "auditor", "support")
    if not is_owner and not is_admin and project.publication_status != "published":
        raise HTTPException(status_code=403, detail="无权查看私人作品数据")
    return await personal_analytics_service.project_analysis(db, uuid.UUID(project_id))


# ---------- 创作目标 ----------
class GoalIn(BaseModel):
    goal_type: str
    target_value: int | float
    project_id: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


@router.post("/goals")
async def create_goal(
    body: GoalIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await goal_service.create_goal(
        db,
        user_id=current_user.id,
        goal_type=body.goal_type,
        target_value=body.target_value,
        project_id=uuid.UUID(body.project_id) if body.project_id else None,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )
    await db.commit()
    return result


@router.get("/goals")
async def list_goals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await goal_service.list_goals(db, user_id=current_user.id)


class AccumulateIn(BaseModel):
    project_id: str | None = None
    goal_type: str
    delta: int


@router.post("/goals/{goal_id}/accumulate")
async def accumulate_goal(
    goal_id: str,
    body: AccumulateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await goal_service.accumulate_goal(
        db,
        user_id=current_user.id,
        project_id=uuid.UUID(body.project_id) if body.project_id else None,
        goal_type=body.goal_type,
        delta=body.delta,
    )
    await db.commit()
    return result


@router.get("/goals/reminders")
async def goal_reminders(
    hour: int = Query(20, ge=0, le=23),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提醒策略：低于进度/接近截止/完成提醒；仅开启提醒的用户，频率受控。"""
    return await goal_service.goal_reminders(db, user_id=current_user.id, hour=hour)


class GoalStatusIn(BaseModel):
    status: str


@router.patch("/goals/{goal_id}/status")
async def update_goal_status(
    goal_id: str,
    body: GoalStatusIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await goal_service.update_goal_status(
        db, user_id=current_user.id, goal_id=uuid.UUID(goal_id), status=body.status
    )
    await db.commit()
    return result


# ---------- 数据隐私 ----------
class PrivacyPrefsIn(BaseModel):
    personalized_recommendation: bool | None = None
    behavior_analysis: bool | None = None
    marketing_notification: bool | None = None
    data_sharing: bool | None = None
    ai_data_usage: bool | None = None


@router.get("/privacy")
async def get_privacy(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await privacy_control_service.get_preferences(db, user_id=current_user.id)


@router.put("/privacy")
async def update_privacy(
    body: PrivacyPrefsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = {k: v for k, v in body.model_dump().items() if v is not None}
    result = await privacy_control_service.update_preferences(
        db, user_id=current_user.id, prefs=prefs
    )
    await db.commit()
    return result


@router.post("/privacy/export")
async def export_behavior_data(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出个人行为数据。"""
    return await privacy_control_service.export_behavior_data(db, user_id=current_user.id)


@router.post("/privacy/delete")
async def delete_analysis_data(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除分析数据（保留必要安全/计费/审计记录）。"""
    result = await privacy_control_service.delete_analysis_data(db, user_id=current_user.id)
    await db.commit()
    return result


@router.post("/privacy/revoke-marketing")
async def revoke_marketing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """撤回运营授权（营销通知）。"""
    result = await privacy_control_service.revoke_marketing(db, user_id=current_user.id)
    await db.commit()
    return result


@router.get("/privacy/policy")
async def data_usage_policy():
    """查看数据使用说明。"""
    return await privacy_control_service.data_usage_policy()


# ---------- 数据质量 ----------
@router.post("/quality/snapshot")
async def quality_snapshot(
    period: str = Query("1h", pattern="^(15m|1h|24h)$"),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """监控事件丢失/重复/缺失/延迟/类型错误等质量指标。"""
    return await quality_service.record_quality_snapshot(db, period=period)


@router.get("/quality/history")
async def quality_history(
    limit: int = Query(10, ge=1, le=100),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    return await quality_service.quality_history(db, limit=limit)


class ReplayIn(BaseModel):
    event_ids: list[str] | None = None
    dead_letter: bool = True


@router.post("/quality/replay")
async def replay_events(
    body: ReplayIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """数据回放（支持指定事件/死信队列重放）。"""
    return await quality_service.replay_events(
        db,
        event_ids=[uuid.UUID(x) for x in (body.event_ids or [])],
        dead_letter=body.dead_letter,
    )


@router.post("/quality/dead-letters/replay")
async def replay_dead_letters(
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    return await analytics_service.replay_dead_letters(db, limit=limit)


# ---------- 异常检测 ----------
@router.post("/anomalies/detect")
async def detect_anomalies(
    window_hours: int = Query(1, ge=1, le=72),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """规则式异常检测：异常登录/刷阅读/评论突增/批量注册/AI消耗/恶意调用/异常支付/推荐突增。"""
    result = await anomaly_service.detect_anomalies(db, window_hours=window_hours)
    await db.commit()
    return result


@router.get("/anomalies")
async def list_anomalies(
    risk_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    return await anomaly_service.list_risk_events(db, status=risk_status, limit=limit)


class RiskHandleIn(BaseModel):
    action: str


@router.post("/anomalies/{risk_id}/handle")
async def handle_anomaly(
    risk_id: str,
    body: RiskHandleIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """处理：记录/降权/限频/冻结/人工审核/恢复解除。"""
    result = await anomaly_service.handle_risk(
        db, risk_id=uuid.UUID(risk_id), action=body.action, actor_id=current_user.id
    )
    await db.commit()
    return result
