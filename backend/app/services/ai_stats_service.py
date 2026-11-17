# AI 使用统计与失败分析服务（第十七批「统计分析与运营后台」规格十/十一）
# AIUsageDailyStat：按 用户+项目+供应商+模型+任务类型+日期 每日聚合。
# AIFailureStat：失败原因统计（超时/限频/上下文过长/服务异常/审核拒绝/余额不足/参数错误），
#                后台展示失败率/平均重试次数/失败最多模型/失败最多任务类型/近 24h 趋势。
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage_daily_stat import AI_FAILURE_REASONS, AIFailureStat, AIUsageDailyStat
from app.models.project import Project
from app.models.user import User

# 简单成本估算（美元/千 token），可按 provider 扩展
_COST_INPUT_PER_K = 0.0001
_COST_OUTPUT_PER_K = 0.0002


class AIStatsError(ValueError):
    pass


def estimate_cost(input_tokens: int | None, output_tokens: int | None) -> float:
    """按 token 估算成本（美元）。

    阶段 1 token 口径最小修复（2026-10）：未知（None）表示"无真实 usage"，
    按 0 贡献成本，禁止用字符数折算替代；单价与计费规则本轮不变。
    """
    in_tok = 0 if input_tokens is None else int(input_tokens)
    out_tok = 0 if output_tokens is None else int(output_tokens)
    return round(
        in_tok / 1000 * _COST_INPUT_PER_K
        + out_tok / 1000 * _COST_OUTPUT_PER_K,
        6,
    )


async def record_usage(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    provider: str,
    model: str,
    task_type: str,
    success: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost: float | None = None,
    failure_reason: str | None = None,
    retry_count: int = 0,
    stat_date: date | None = None,
) -> AIUsageDailyStat:
    """记录一次 AI 调用；自动更新每日聚合与失败统计。

    阶段 1 token 口径最小修复（2026-10）：token 字段只接受供应商真实 usage。
    未取得真实 usage（None）时不得用字符数折算，也不得被当作"真实 0 token"计费：
      - 日聚合列（非空整数）落 0，仅表示"当日未取得真实 token、未计入"；
      - 成本只按已知真实 token 折算（未知侧贡献 0），单价与计费规则本轮不变。
    """
    stat_date = stat_date or date.today()
    if failure_reason and failure_reason not in AI_FAILURE_REASONS:
        raise AIStatsError(f"未知失败原因: {failure_reason}")
    # 未知（None）→ 聚合列落 0，且不参与成本折算；字符数一律不折算为 token。
    in_tok = 0 if input_tokens is None else int(input_tokens)
    out_tok = 0 if output_tokens is None else int(output_tokens)
    cost = (
        estimated_cost
        if estimated_cost is not None
        else estimate_cost(in_tok, out_tok)
    )

    # upsert 每日用量
    stat = (
        await db.execute(
            select(AIUsageDailyStat).where(
                AIUsageDailyStat.user_id == user_id,
                AIUsageDailyStat.project_id == project_id,
                AIUsageDailyStat.provider == provider,
                AIUsageDailyStat.model == model,
                AIUsageDailyStat.task_type == task_type,
                AIUsageDailyStat.stat_date == stat_date,
            )
        )
    ).scalar_one_or_none()
    if stat is None:
        stat = AIUsageDailyStat(
            user_id=user_id,
            project_id=project_id,
            provider=provider,
            model=model,
            task_type=task_type,
            request_count=1,
            success_count=1 if success else 0,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost=cost,
            stat_date=stat_date,
        )
        db.add(stat)
    else:
        stat.request_count += 1
        if success:
            stat.success_count += 1
        stat.input_tokens += in_tok
        stat.output_tokens += out_tok
        stat.estimated_cost = round(stat.estimated_cost + cost, 6)
    await db.flush()

    if not success and failure_reason:
        fail = (
            await db.execute(
                select(AIFailureStat).where(
                    AIFailureStat.stat_date == stat_date,
                    AIFailureStat.project_id == project_id,
                    AIFailureStat.model == model,
                    AIFailureStat.task_type == task_type,
                    AIFailureStat.failure_reason == failure_reason,
                )
            )
        ).scalar_one_or_none()
        if fail is None:
            fail = AIFailureStat(
                stat_date=stat_date,
                project_id=project_id,
                model=model,
                task_type=task_type,
                failure_reason=failure_reason,
                request_count=1,
                retry_count=retry_count,
            )
            db.add(fail)
        else:
            fail.request_count += 1
            fail.retry_count += retry_count
        await db.flush()

    return stat


async def get_ai_usage(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    task_type: str | None = None,
    group_by: str = "day",
) -> dict:
    """AI 使用统计；group_by 支持 day/model/task_type/provider/project/user。"""
    from_date = from_date or date.today() - timedelta(days=29)
    to_date = to_date or date.today()

    filters = [
        AIUsageDailyStat.stat_date >= from_date,
        AIUsageDailyStat.stat_date <= to_date,
    ]
    if project_id is not None:
        filters.append(AIUsageDailyStat.project_id == project_id)
    if user_id is not None:
        filters.append(AIUsageDailyStat.user_id == user_id)
    if task_type:
        filters.append(AIUsageDailyStat.task_type == task_type)

    rows = (
        await db.execute(
            select(AIUsageDailyStat).where(*filters).order_by(AIUsageDailyStat.stat_date.asc())
        )
    ).scalars().all()

    # 汇总 + 分组
    total = {
        "request_count": 0,
        "success_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0.0,
    }
    groups: dict[str, dict] = defaultdict(lambda: {
        "request_count": 0,
        "success_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0.0,
    })

    for r in rows:
        total["request_count"] += r.request_count
        total["success_count"] += r.success_count
        total["input_tokens"] += r.input_tokens
        total["output_tokens"] += r.output_tokens
        total["estimated_cost"] = round(total["estimated_cost"] + r.estimated_cost, 6)

        if group_by == "day":
            key = r.stat_date.isoformat()
        elif group_by == "model":
            key = r.model
        elif group_by == "task_type":
            key = r.task_type
        elif group_by == "provider":
            key = r.provider
        elif group_by == "project":
            key = str(r.project_id)
        elif group_by == "user":
            key = str(r.user_id)
        else:
            raise AIStatsError(f"不支持的分组: {group_by}")
        g = groups[key]
        g["request_count"] += r.request_count
        g["success_count"] += r.success_count
        g["input_tokens"] += r.input_tokens
        g["output_tokens"] += r.output_tokens
        g["estimated_cost"] = round(g["estimated_cost"] + r.estimated_cost, 6)

    group_items = [
        {"key": k, **v, "failure_count": v["request_count"] - v["success_count"],
         "failure_rate": round((v["request_count"] - v["success_count"]) / v["request_count"], 4)
         if v["request_count"] else 0.0}
        for k, v in sorted(groups.items())
    ]

    total["failure_count"] = total["request_count"] - total["success_count"]
    total["failure_rate"] = round(
        total["failure_count"] / total["request_count"], 4
    ) if total["request_count"] else 0.0

    # 阶段 1 token 口径标注（读取侧，最小修复，不改计费规则）：
    # - token 字段只接受供应商真实 usage，无真实 usage 时按未知（0，未知不得用字符数折算）；
    # - 修复日之前的日聚合记录可能含历史「字符数折半估算」污染口径，按任务书不迁移、不回填；
    # - 当前表结构下「真实 0 token」与「未取得真实 usage」均落 0、无法区分
    #   （需后续迁移新增口径字段才能区分），故合计值只作趋势参考。
    token_data_note = (
        "token 口径说明：token 字段仅记录供应商真实 usage，未知一律按 0 计（不再用字符数折算）；"
        "修复日之前的历史记录可能混入字符数口径且未回填、未迁移；"
        "当前表结构下「真实 0 token」与「未取得真实 usage」均落 0、无法区分（待后续迁移补口径字段）；"
        "故合计 token / estimated_cost 仅供趋势参考，不可作为计费依据。"
    )
    cost_billable = False
    total["token_data_note"] = token_data_note
    total["cost_billable"] = cost_billable

    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "group_by": group_by,
        "total": total,
        "groups": group_items,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
        "token_data_note": token_data_note,
        "cost_billable": cost_billable,
    }


async def get_failure_stats(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """AI 任务失败统计：失败率/平均重试/失败最多模型与类型/近 24h 趋势。"""
    from_date = from_date or date.today() - timedelta(days=6)
    to_date = to_date or date.today()

    filters = [
        AIFailureStat.stat_date >= from_date,
        AIFailureStat.stat_date <= to_date,
    ]
    if project_id is not None:
        filters.append(AIFailureStat.project_id == project_id)

    rows = (
        await db.execute(select(AIFailureStat).where(*filters))
    ).scalars().all()

    by_reason: dict[str, dict] = defaultdict(lambda: {"request_count": 0, "retry_count": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"request_count": 0, "retry_count": 0})
    by_task: dict[str, dict] = defaultdict(lambda: {"request_count": 0, "retry_count": 0})
    total_fail = 0
    total_retry = 0
    for r in rows:
        total_fail += r.request_count
        total_retry += r.retry_count
        by_reason[r.failure_reason]["request_count"] += r.request_count
        by_reason[r.failure_reason]["retry_count"] += r.retry_count
        by_model[r.model]["request_count"] += r.request_count
        by_model[r.model]["retry_count"] += r.retry_count
        by_task[r.task_type]["request_count"] += r.request_count
        by_task[r.task_type]["retry_count"] += r.retry_count

    # 总体失败率（需从用量表取总请求数）
    total_req = (
        await db.execute(
            select(func.coalesce(func.sum(AIUsageDailyStat.request_count), 0)).where(
                AIUsageDailyStat.stat_date >= from_date,
                AIUsageDailyStat.stat_date <= to_date,
                *([AIUsageDailyStat.project_id == project_id] if project_id else []),
            )
        )
    ).scalar_one()

    most_failed_model = max(by_model.items(), key=lambda kv: kv[1]["request_count"])[0] if by_model else None
    most_failed_task = max(by_task.items(), key=lambda kv: kv[1]["request_count"])[0] if by_task else None

    # 近 24h 趋势（按小时，基于 created_at）
    since = datetime.utcnow() - timedelta(hours=24)
    recent = (
        await db.execute(
            select(AIFailureStat).where(
                AIFailureStat.created_at >= since,
                *([AIFailureStat.project_id == project_id] if project_id else []),
            )
        )
    ).scalars().all()
    hourly: dict[str, int] = defaultdict(int)
    for r in recent:
        key = r.created_at.strftime("%Y-%m-%dT%H:00")
        hourly[key] += r.request_count
    trend_24h = [{"hour": k, "failure_count": v} for k, v in sorted(hourly.items())]

    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "total_requests": total_req,
        "total_failures": total_fail,
        "failure_rate": round(total_fail / total_req, 4) if total_req else 0.0,
        "average_retry_count": round(total_retry / total_fail, 2) if total_fail else 0.0,
        "most_failed_model": most_failed_model,
        "most_failed_task_type": most_failed_task,
        "by_reason": [
            {"reason": k, **v} for k, v in sorted(by_reason.items())
        ],
        "by_model": [
            {"model": k, **v} for k, v in sorted(
                by_model.items(), key=lambda kv: kv[1]["request_count"], reverse=True
            )
        ],
        "by_task_type": [
            {"task_type": k, **v} for k, v in sorted(
                by_task.items(), key=lambda kv: kv[1]["request_count"], reverse=True
            )
        ],
        "trend_24h": trend_24h,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "is_realtime": True,
        "is_estimated": False,
    }


async def delete_user_ai_stats(db: AsyncSession, user_id: uuid.UUID) -> None:
    """用户删除权：清除个人 AI 使用统计。"""
    from sqlalchemy import delete

    await db.execute(delete(AIUsageDailyStat).where(AIUsageDailyStat.user_id == user_id))
