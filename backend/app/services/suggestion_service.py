# 智能创作建议服务（第二十二批「数据分析、推荐系统和智能运营」规格十六）
# 类型：写作目标/章节长度/更新频率/长期未完成/人物资料缺失/情节连续性/重复表达/章节结构。
# 原则：只建议不自动修改；用户可接受/忽略/关闭某类/标记无用/查看依据。
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.chapter_content import ChapterContent
from app.models.character import Character
from app.models.creation_suggestion import (
    SUGGESTION_STATUSES,
    SUGGESTION_TYPES,
    CreationSuggestion,
    SuggestionFeedback,
    SuggestionPreference,
)
from app.models.outline import Outline
from app.models.project import Project
from app.models.writing_goal import WritingGoal


def _now() -> datetime:
    return datetime.utcnow()


async def _chapter_word_rows(db: AsyncSession, *, project_id: uuid.UUID, limit: int | None = None):
    """章节字数（Outline + ChapterContent.word_count）。"""
    stmt = (
        select(Outline.id, Outline.title, Outline.sort_order, ChapterContent.word_count)
        .join(ChapterContent, ChapterContent.outline_id == Outline.id)
        .where(
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
        )
        .order_by(Outline.sort_order.asc())
    )
    rows = (await db.execute(stmt)).all()
    if limit:
        rows = rows[-limit:]
    return rows


async def generate_suggestions(db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID | None = None) -> list[dict]:
    """按规则生成创作建议（不修改用户内容）。"""
    disabled = set((await db.execute(
        select(SuggestionPreference.category).where(
            SuggestionPreference.user_id == user_id,
            SuggestionPreference.disabled.is_(True),
        )
    )).scalars().all())

    suggestions: list[dict] = []

    # 1) 写作目标建议
    if "writing_goal" not in disabled:
        goal = (await db.execute(
            select(WritingGoal).where(
                WritingGoal.user_id == user_id,
                WritingGoal.status == "active",
            ).order_by(WritingGoal.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if goal is None:
            suggestions.append({
                "type": "writing_goal", "title": "设置每日写作目标",
                "description": "建议设定每日字数目标并开启提醒，保持创作节奏。",
                "evidence": {"active_goals": 0},
            })
        elif goal.current_value < goal.target_value * 0.6:
            suggestions.append({
                "type": "writing_goal", "title": "写作目标进度偏慢",
                "description": f"当前进度 {goal.current_value}/{goal.target_value}，低于 60%，建议调整目标或增加频次。",
                "evidence": {"current": goal.current_value, "target": goal.target_value},
            })

    # 2) 章节长度建议（字数取自 ChapterContent）
    if "chapter_length" not in disabled and project_id:
        rows = await _chapter_word_rows(db, project_id=project_id, limit=5)
        if rows:
            lens = [row[3] or 0 for row in rows]
            avg_len = sum(lens) / max(len(lens), 1)
            if avg_len < 800:
                suggestions.append({
                    "type": "chapter_length", "title": "章节篇幅偏短",
                    "description": "近 5 章平均字数不足 800，建议补充场景与细节。",
                    "evidence": {"avg_words": round(avg_len, 0)},
                })

    # 3) 更新频率提醒
    if "update_frequency" not in disabled and project_id:
        pub_count = (await db.execute(
            select(func.count(AnalyticsEvent.id)).where(
                AnalyticsEvent.project_id == project_id,
                AnalyticsEvent.event_name == "chapter.published",
            )
        )).scalar_one() or 0
        if pub_count == 0:
            suggestions.append({
                "type": "update_frequency", "title": "作品尚未发布更新",
                "description": "该作品还没有发布章节，建议完成第一章并发布，让读者可见。",
                "evidence": {"published": 0},
            })

    # 4) 长期未完成项目提醒
    if "long_unfinished" not in disabled:
        if project_id:
            projects = (await db.execute(
                select(Project).where(Project.id == project_id)
            )).scalars().all()
        else:
            projects = (await db.execute(
                select(Project).where(Project.user_id == user_id)
            )).scalars().all()
        for p in projects:
            if p.updated_at and _now() - p.updated_at > timedelta(days=14) and not p.published_at:
                suggestions.append({
                    "type": "long_unfinished", "title": "长期未完成项目",
                    "description": f"《{p.title}》已 14 天以上未更新，考虑继续创作或归档。",
                    "evidence": {"project": str(p.id), "days_inactive": (_now() - p.updated_at).days},
                })

    # 5) 人物资料缺失
    if "character_profile_missing" not in disabled and project_id:
        chars = (await db.execute(
            select(Character).where(Character.project_id == project_id)
        )).scalars().all()
        missing = [c.name for c in chars if not (c.description or "").strip()]
        if missing:
            suggestions.append({
                "type": "character_profile_missing", "title": "人物资料缺失",
                "description": f"以下角色缺少背景设定：{', '.join(missing[:5])}。",
                "evidence": {"characters": missing[:10]},
            })

    # 6) 情节连续性/重复表达/章节结构：基于章节标题与字数启发
    if "plot_continuity" not in disabled and project_id:
        titles = [row[1] or "" for row in await _chapter_word_rows(db, project_id=project_id)]
        if len(titles) >= 3 and any(not t.strip() for t in titles[-3:]):
            suggestions.append({
                "type": "plot_continuity", "title": "部分章节缺少标题",
                "description": "最近章节缺少标题，建议补充便于读者导航与情节梳理。",
                "evidence": {"untitled_recent": sum(1 for t in titles[-3:] if not t.strip())},
            })

    if "repeated_expression" not in disabled and project_id:
        titles = [row[1] or "" for row in await _chapter_word_rows(db, project_id=project_id)]
        if len(titles) >= 4 and len(set(titles)) < len(titles):
            dup = [t for t in titles if titles.count(t) > 1]
            suggestions.append({
                "type": "repeated_expression", "title": "章节标题重复",
                "description": f"检测到重复章节标题：{', '.join(sorted(set(dup))[:5])}，注意避免混淆。",
                "evidence": {"duplicates": sorted(set(dup))[:10]},
            })

    if "chapter_structure" not in disabled and project_id:
        rows = await _chapter_word_rows(db, project_id=project_id)
        lens = [row[3] or 0 for row in rows]
        if lens and max(lens) > min(lens) * 3 + 2000:
            suggestions.append({
                "type": "chapter_structure", "title": "章节长度差异较大",
                "description": "章节字数差异明显，可考虑拆分长章或合并短章以均衡节奏。",
                "evidence": {"max_words": max(lens), "min_words": min(lens)},
            })

    # 落库（去重：同类型同项目仅保留最近一条；project_id 可空），并把 id 回填到返回结果
    created: list[tuple[dict, CreationSuggestion]] = []
    for s in suggestions:
        dup_stmt = select(CreationSuggestion).where(
            CreationSuggestion.user_id == user_id,
            CreationSuggestion.suggestion_type == s["type"],
            CreationSuggestion.status == "active",
        )
        if project_id:
            dup_stmt = dup_stmt.where(CreationSuggestion.project_id == project_id)
        else:
            dup_stmt = dup_stmt.where(CreationSuggestion.project_id.is_(None))
        dup = (await db.execute(dup_stmt)).scalar_one_or_none()
        if dup:
            s["id"] = str(dup.id)
            continue
        obj = CreationSuggestion(
            user_id=user_id,
            project_id=project_id,
            suggestion_type=s["type"],
            title=s["title"],
            description=s["description"],
            evidence=json.dumps(s["evidence"], ensure_ascii=False),
            status="active",
        )
        db.add(obj)
        created.append((s, obj))
    await db.flush()
    for s, obj in created:
        s["id"] = str(obj.id)
    return suggestions


async def list_suggestions(db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID | None = None) -> list[dict]:
    stmt = select(CreationSuggestion).where(CreationSuggestion.user_id == user_id)
    if project_id:
        stmt = stmt.where(CreationSuggestion.project_id == project_id)
    rows = (await db.execute(stmt.order_by(CreationSuggestion.created_at.desc()))).scalars().all()
    return [
        {
            "id": str(s.id), "project_id": str(s.project_id) if s.project_id else None,
            "type": s.suggestion_type, "title": s.title, "description": s.description,
            "evidence": json.loads(s.evidence or "{}"), "status": s.status,
        }
        for s in rows
    ]


async def feedback(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    action: str,
) -> dict:
    """接受/忽略/关闭某类/标记无用。"""
    if action not in ("accept", "ignore", "dismiss_category", "useless"):
        return {"error": "invalid_action"}
    sug = (await db.execute(
        select(CreationSuggestion).where(
            CreationSuggestion.id == suggestion_id,
            CreationSuggestion.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not sug:
        return {"error": "suggestion_not_found"}

    db.add(SuggestionFeedback(suggestion_id=suggestion_id, user_id=user_id, action=action))
    if action == "accept":
        sug.status = "accepted"
    elif action == "ignore":
        sug.status = "ignored"
    elif action == "useless":
        sug.status = "useless"
    elif action == "dismiss_category":
        sug.status = "dismissed"
        pref = (await db.execute(
            select(SuggestionPreference).where(
                SuggestionPreference.user_id == user_id,
                SuggestionPreference.category == sug.suggestion_type,
            )
        )).scalar_one_or_none()
        if pref:
            pref.disabled = True
        else:
            db.add(SuggestionPreference(user_id=user_id, category=sug.suggestion_type, disabled=True))
    await db.flush()
    return {"status": "ok", "action": action, "suggestion_id": str(suggestion_id)}
