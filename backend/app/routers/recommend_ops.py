# 第二十二批「数据分析、推荐系统和智能运营」- 推荐/建议/分群/消息编排/A/B 测试/推荐监控/运营后台 路由
# 推荐 GET /api/recommendations（过滤：权限/地区/年龄/可见性/违规/屏蔽/重复曝光）
# 推荐交互 POST /api/recommendations/actions、block、blocked
# 创作建议 POST /api/suggestions/generate、GET /api/suggestions、POST /api/suggestions/{id}/feedback
# 用户分群 /api/segments；消息编排 /api/flows；A/B 测试 /api/experiments
# 推荐监控/运营后台 /api/admin/batch22/*（admin + 脱敏 + 审计）
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import require_admin_roles
from app.models.user import User
from app.services import (
    aggregation_service,
    experiment_service,
    recommend_monitor_service,
    recommend_service,
    segment_service,
    suggestion_service,
)

router = APIRouter(prefix="/api", tags=["batch22-recommend-ops", "实验"])


# ---------- 推荐系统 ----------
@router.get("/recommendations")
async def get_recommendations(
    scene: str = Query("homepage"),
    limit: int = Query(10, ge=1, le=50),
    region: str | None = None,
    age: int | None = None,
    language: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """主推荐入口：热门/标签匹配/最近阅读/相似作品/关注更新/编辑精选 + 过滤与排序融合。"""
    result = await recommend_service.recommend(
        db,
        user_id=current_user.id,
        scene=scene,
        limit=limit,
        region=region,
        age=age,
        language=language,
    )
    await db.commit()
    return result


class ActionIn(BaseModel):
    log_id: str | None = None
    action: str
    recommend_type: str | None = None
    recommend_id: str | None = None


@router.post("/recommendations/actions")
async def record_recommend_action(
    body: ActionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """记录推荐交互：click/complete/hide/block/report。"""
    result = await recommend_service.record_action(
        db,
        user_id=current_user.id,
        log_id=uuid.UUID(body.log_id) if body.log_id else None,
        action=body.action,
        recommend_type=body.recommend_type,
        recommend_id=body.recommend_id,
    )
    await db.commit()
    return result


class BlockIn(BaseModel):
    target_type: str
    target_id: str


@router.post("/recommendations/block")
async def block_recommendation(
    body: BlockIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await recommend_service.block_recommend(
        db, user_id=current_user.id, target_type=body.target_type, target_id=body.target_id
    )
    await db.commit()
    return result


@router.get("/recommendations/blocked")
async def list_blocked_recommendations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await recommend_service.list_blocked(db, current_user.id)


@router.get("/recommendations/metrics")
async def recommendation_metrics(
    hours: int = Query(24, ge=1, le=168),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """推荐效果监控：点击率/完成率/多样性/重复曝光/屏蔽/投诉/响应/错误/地区差异。"""
    return await recommend_monitor_service.recommend_metrics(db, hours=hours)


# ---------- 智能创作建议 ----------
class SuggestionGenIn(BaseModel):
    project_id: str | None = None


@router.post("/suggestions/generate")
async def generate_suggestions(
    body: SuggestionGenIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成创作建议（只建议不自动修改）。"""
    result = await suggestion_service.generate_suggestions(
        db, user_id=current_user.id,
        project_id=uuid.UUID(body.project_id) if body.project_id else None,
    )
    await db.commit()
    return result


@router.get("/suggestions")
async def list_suggestions(
    project_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await suggestion_service.list_suggestions(
        db, user_id=current_user.id,
        project_id=uuid.UUID(project_id) if project_id else None,
    )


class SuggestionFeedbackIn(BaseModel):
    action: str  # accept / ignore / dismiss_category / useless


@router.post("/suggestions/{suggestion_id}/feedback")
async def suggestion_feedback(
    suggestion_id: str,
    body: SuggestionFeedbackIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await suggestion_service.feedback(
        db, user_id=current_user.id,
        suggestion_id=uuid.UUID(suggestion_id), action=body.action,
    )
    await db.commit()
    return result


# ---------- 用户分群 ----------
class SegmentIn(BaseModel):
    name: str
    rule_type: str
    rule_params: dict | None = None


@router.post("/segments")
async def create_segment(
    body: SegmentIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    result = await segment_service.create_segment(
        db, name=body.name, rule_type=body.rule_type,
        rule_params=body.rule_params, created_by=current_user.id,
    )
    await db.commit()
    return result


class SegmentEvalIn(BaseModel):
    segment_id: str | None = None
    rule_type: str | None = None
    rule_params: dict | None = None


@router.post("/segments/evaluate")
async def evaluate_segment(
    body: SegmentEvalIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """分群引擎评估：注册七天未建项目/连续七天创作/三十天未登录/AI额度/试用/未发布/团队管理员。"""
    return await segment_service.evaluate_segment(
        db,
        segment_id=uuid.UUID(body.segment_id) if body.segment_id else None,
        rule_type=body.rule_type,
        rule_params=body.rule_params,
    )


# ---------- 消息编排 ----------
class FlowIn(BaseModel):
    name: str
    trigger_type: str
    trigger_params: dict | None = None
    wait_seconds: int = 0
    channel: str = "in_app"
    template: str | None = None
    max_sends_per_user: int = 3
    exit_condition: dict | None = None
    consent_required: bool = True


@router.post("/flows")
async def create_flow(
    body: FlowIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    result = await segment_service.create_flow(
        db,
        name=body.name,
        trigger_type=body.trigger_type,
        trigger_params=body.trigger_params,
        wait_seconds=body.wait_seconds,
        channel=body.channel,
        template=body.template,
        max_sends_per_user=body.max_sends_per_user,
        exit_condition=body.exit_condition,
        consent_required=body.consent_required,
        created_by=current_user.id,
    )
    await db.commit()
    return result


@router.get("/flows")
async def list_flows(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await segment_service.list_flows(db)


class FlowRunIn(BaseModel):
    user_id: str
    trigger_type: str | None = None


@router.post("/flows/run")
async def run_flows(
    body: FlowRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发用户相关运营流程（等待时间/发送上限/退出条件/退订/时区发送/重复触发保护）。"""
    result = await segment_service.run_flows_for_user(
        db, user_id=uuid.UUID(body.user_id), trigger_type=body.trigger_type
    )
    await db.commit()
    return result


# ---------- A/B 测试 ----------
class ExperimentIn(BaseModel):
    name: str
    target: str
    variants: list[str]
    allocation: dict | None = None
    start_at: str | None = None
    end_at: str | None = None
    status: str = "draft"


@router.post("/experiments")
async def create_experiment(
    body: ExperimentIn,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    result = await experiment_service.create_experiment(
        db,
        name=body.name,
        target=body.target,
        variants=body.variants,
        allocation=body.allocation,
        status=body.status,
        created_by=current_user.id,
    )
    await db.commit()
    return result


@router.post("/experiments/{experiment_id}/start")
async def start_experiment(
    experiment_id: str,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    result = await experiment_service.start_experiment(db, experiment_id=uuid.UUID(experiment_id))
    await db.commit()
    return result


class AssignIn(BaseModel):
    user_id: str


@router.post("/experiments/{experiment_id}/assign")
async def assign_experiment_user(
    experiment_id: str,
    body: AssignIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """hash 分桶分配实验组：同一用户实验期间分组稳定不漂移。"""
    result = await experiment_service.assign_user(
        db, experiment_id=uuid.UUID(experiment_id), user_id=uuid.UUID(body.user_id)
    )
    await db.commit()
    return result


class ExperimentEventIn(BaseModel):
    user_id: str
    event_type: str  # exposure / conversion
    value: float | None = None


@router.post("/experiments/{experiment_id}/events")
async def record_experiment_event(
    experiment_id: str,
    body: ExperimentEventIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await experiment_service.record_event(
        db,
        experiment_id=uuid.UUID(experiment_id),
        user_id=uuid.UUID(body.user_id),
        event_type=body.event_type,
        value=body.value,
    )
    await db.commit()
    return result


@router.get("/experiments/{experiment_id}/report")
async def experiment_report(
    experiment_id: str,
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    return await experiment_service.experiment_report(db, experiment_id=uuid.UUID(experiment_id))


# ---------- 管理员分析后台（batch22） ----------
@router.get("/admin/batch22/dashboard")
async def batch22_admin_dashboard(
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """运营报表：用户趋势/创作趋势/内容增长/阅读/AI使用/套餐转化/地区分布/性能/推荐/运营活动。"""
    await recommend_monitor_service.record_audit(
        db, actor_id=current_user.id, action="view_dashboard",
        target_type="analytics", reason="batch22 admin dashboard",
    )
    await db.commit()
    return await aggregation_service.admin_dashboard(db)


@router.get("/admin/batch22/recommend")
async def batch22_recommend_monitor(
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    await recommend_monitor_service.record_audit(
        db, actor_id=current_user.id, action="view_recommend_monitor",
        target_type="recommend", reason="batch22 recommend monitor",
    )
    await db.commit()
    return await recommend_monitor_service.admin_dashboard(db)


@router.get("/admin/batch22/users")
async def batch22_admin_users(
    include_sensitive: bool = Query(False),
    current_user: User = Depends(require_admin_roles()),
    db: AsyncSession = Depends(get_db),
):
    """用户列表：最小化展示 + 字段脱敏；敏感字段仅 admin 角色可见。"""
    await recommend_monitor_service.record_audit(
        db, actor_id=current_user.id, action="view_users",
        target_type="users", reason="batch22 admin users",
        metadata_json=f'{{"include_sensitive": {str(include_sensitive).lower()}}}',
    )
    await db.commit()
    return await recommend_monitor_service.list_admin_users(
        db, include_sensitive=include_sensitive, current_role=current_user.role
    )
