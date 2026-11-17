# 状态变更提取服务（第三期「变更确认流程」核心）
# 职责：
# 1. extract_state_changes：接受正文后从正文提取人物状态变化/新增关系/时间线事件候选
#    （action_type=extract_state 落 generation_records），按项目确认策略：
#      - character_confirm=auto / timeline_confirm=auto / foreshadow_confirm=auto
#        → 直接写入正式库并标记 source=ai_extracted，不生成待确认建议；
#      - require（默认）→ 生成 pending 变更建议，等待用户确认。
# 2. confirm_suggestion：确认/修改确认/忽略建议，确认后写入正式人物卡状态、
#    character_state_snapshots(user_confirmed)、关系表、timeline_events、章节记忆伏笔。
import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_suggestion import ChangeSuggestion
from app.models.character import Character
from app.models.character_relationship import CharacterRelationship
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.chapter_content import ChapterContent
from app.models.chapter_memory import ChapterMemory
from app.models.outline import Outline
from app.models.timeline_event import TimelineEvent
from app.models.project_setting import ProjectSetting
from app.services.ai_service import (
    chat_with_ai,
    _chat_with_ai_retry_on_truncation,
    is_ai_configured,
    strict_entity_check_enabled,
)
from app.services.entity_reference_validator import validate_entity_references
from app.services.chapter_memory_service import get_or_create_project_setting
from app.services.generation_service import (
    record_generation,
    resolve_ai_provider,
    serialize_context,
)
from app.services.model_policy import TaskType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 提取 ----------

async def _match_character(
    db: AsyncSession, project_id: uuid.UUID, name: str
) -> Character | None:
    """按名字/别名匹配项目内人物卡，找不到返回 None。"""
    if not name:
        return None
    name = str(name).strip()
    rows = (
        await db.execute(
            select(Character).where(Character.project_id == project_id)
        )
    ).scalars().all()
    exact = None
    for c in rows:
        if c.name == name:
            return c
        try:
            aliases = json.loads(c.aliases or "[]")
        except Exception:  # noqa: BLE001
            aliases = []
        if name in aliases:
            exact = c
    # 阶段E修复（E-02）：名字解析只认精确 name / 别名，禁止包含匹配作为命中依据。
    # 包含匹配会把「陈」误判成「陈默」，把不认识的名字硬绑到已有实体，
    # 导致写路径把事实写到错误人物卡上；解析不到即返回 None，交由调用方记为待解析。
    return exact


def _strip_code_fence(text: str) -> str:
    """去掉模型输出中可能存在的 ```json 围栏，提取 JSON 主体。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def build_mock_state_changes(chapter_title: str, content: str) -> dict:
    """演示模式（未配置 AI 密钥）的合理 mock 状态变更提取结果。

    输出结构与真实模式一致：character_updates / relationships / timeline_events / foreshadows。
    字段尽量呼应正文开头，便于全链路自测。
    """
    clean = (content or "").replace("\n", " ").replace("\r", " ").strip()
    seg = clean[:60].strip()
    hint = f"（正文提及：{seg[:20]}…）" if seg else ""
    return {
        "character_updates": [
            {
                "character": "陈九",
                "changes": [f"本章发生人物状态变化 {hint}"],
                "current_location": "边军大营",
                "current_timeline_status": "加入边军大营",
                "status": "在世",
            }
        ],
        "relationships": [
            {
                "source": "陈九",
                "target": "边军大营都尉",
                "relation_type": "同盟",
                "description": "陈九加入边军大营，与都尉结为同盟，共同应对北关城追兵。",
            }
        ],
        "timeline_events": [
            {
                "title": "陈九加入边军大营",
                "event_date_label": "景历二十三年秋",
                "sequence_order": 1,
                "participants": ["陈九", "边军大营都尉"],
                "description": "陈九离开北关城，正式加入边军大营，与都尉结盟。",
            }
        ],
        "foreshadows": [
            {
                "content": "边军大营都尉隐瞒了与陈九父亲的旧怨",
                "planned_payoff": "后续章节揭示都尉的真实动机",
            }
        ],
    }


async def extract_state_changes(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> dict:
    """接受正文后：提取状态变更候选并按确认策略落地。

    返回统计 dict：{suggestions: int, auto_applied: dict}，任何异常不向上抛出。
    """
    result = {"suggestions": 0, "auto_applied": {"character": 0, "relationship": 0, "timeline": 0, "foreshadow": 0}}
    try:
        provider, model = resolve_ai_provider()
        real = is_ai_configured()

        system_prompt = (
            "你是小说状态变更提取器。请阅读用户给出的章节正文，提取本章发生的"
            "人物状态变化、新增人物关系、时间线事件候选与新增伏笔。必须只输出 JSON，不要任何多余文字。"
        )
        user_prompt = (
            "章节标题：《" + (chapter_title or "") + "》\n"
            "章节正文：\n" + (content or "") + "\n\n"
            "输出 JSON 格式：\n"
            '{"character_updates": [{"character": "人物名", "changes": ["变化描述"],'
            ' "current_location": "新地点", "current_timeline_status": "新时间线状态", "status": "在世|死亡|失踪|未知"}], '
            '"relationships": [{"source": "人物A", "target": "人物B", "relation_type": "师徒|敌对|同盟|亲属|怀疑|爱慕|主仆|同僚|其他",'
            ' "description": "关系说明"}], '
            '"timeline_events": [{"title": "事件标题", "event_date_label": "日期标签如 景历二十三年秋",'
            ' "sequence_order": 1, "participants": ["人物"], "description": "事件描述"}], '
            '"foreshadows": [{"content": "伏笔内容", "planned_payoff": "计划回收方式"}]}'
        )

        raw = None
        status = "succeeded"
        error_message: str | None = None
        if real:
            try:
                raw = await _chat_with_ai_retry_on_truncation(
                    system_prompt,
                    user_prompt,
                    task_type=TaskType.FACT_EXTRACTION.value,
                )
                raw = _strip_code_fence(raw)
                parsed = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                error_message = str(exc)[:500]
                parsed = None
        else:
            # 阶段E修复（E-03）：未配置 AI 时不再返回固定虚构事实（mock）兜底，
            # 生产链禁止 build_mock_state_changes 产生"看起来成功"的假数据。
            parsed = None

        if parsed is None or not isinstance(parsed, dict):
            parsed = {}

        # 未配置 AI / 调用失败 / 输出不可解析 / 空 payload：不产生任何业务写入
        if not parsed:
            result["skipped_reason"] = (
                "ai_not_configured" if not real else
                ("ai_error" if status == "failed" else "empty_payload")
            )
            return result

        # 阶段 E：写路径实体引用校验（默认开启；失败丢弃模型输出，不落库）
        _validation = await validate_entity_references(
            db, project_id, parsed, strict=strict_entity_check_enabled()
        )
        if not _validation.ok:
            result["rejected"] = _validation.as_dict()
            logger.warning(
                "extract_state_changes: 实体引用校验失败，丢弃本模型输出（不落库）: %s",
                _validation.as_dict(),
            )
            _rejected_json = json.dumps(
                {"rejected": _validation.as_dict()}, ensure_ascii=False
            )
            # 阶段 1 token 口径最小修复（2026-10）：本路径无供应商真实 usage，
            # 禁止把字符数按折半估算伪装写入 prompt/completion/input/output token 字段；
            # 字符维度改记在 context_snapshot 的 prompt_chars / refusal_chars 安全字段。
            await record_generation(
                db,
                attribution_status="project_bound",
                project_id=project_id,
                chapter_id=chapter_id,
                action_type="extract_state",
                model_provider=(provider or "mock") if real else "mock",
                model_name=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                context_snapshot=serialize_context(
                    {"chapter_title": chapter_title, "content_length": len(content or ""),
                     "trigger": "auto_extract", "rejected": True,
                     "prompt_chars": len(system_prompt or "") + len(user_prompt or ""),
                     "refusal_chars": len(_rejected_json),
                     "token_note": "无供应商真实 usage：被丢弃输出不写 token 字段（NULL），字符数仅记 prompt_chars/refusal_chars"}
                ),
                response_content=_rejected_json,
                status="succeeded",
                error_message="实体引用校验失败：输出被丢弃，未落库",
                finish_reason="stop",
            )
            await db.commit()
            return result

        setting: ProjectSetting = await get_or_create_project_setting(db, project_id)

        character_auto = setting.character_confirm == "auto"
        timeline_auto = setting.timeline_confirm == "auto"
        foreshadow_auto = setting.foreshadow_confirm == "auto"
        relationship_auto = character_auto  # 关系归属人物确认策略（默认与人物一致）

        # 人物状态变更
        for upd in parsed.get("character_updates") or []:
            if not isinstance(upd, dict):
                continue
            char_name = str(upd.get("character") or "").strip()
            char = await _match_character(db, project_id, char_name)
            if character_auto:
                if char is not None:
                    await _apply_character_update(
                        db, project_id, char, chapter_id, upd, source="ai_extracted"
                    )
                    result["auto_applied"]["character"] += 1
            else:
                await _create_suggestion(
                    db,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    category="character",
                    character_id=char.id if char else None,
                    target_character_id=None,
                    title=f"人物状态变更：{char_name or '未知人物'}",
                    description=(
                        "；".join(upd.get("changes") or [])
                        or json.dumps(upd, ensure_ascii=False)[:300]
                    ),
                    payload=upd,
                )
                result["suggestions"] += 1

        # 新增关系
        for rel in parsed.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            src_name = str(rel.get("source") or "").strip()
            tgt_name = str(rel.get("target") or "").strip()
            src = await _match_character(db, project_id, src_name)
            tgt = await _match_character(db, project_id, tgt_name)
            if relationship_auto:
                if src is not None and tgt is not None:
                    await _apply_relationship(
                        db, project_id, src, tgt, chapter_id, rel
                    )
                    result["auto_applied"]["relationship"] += 1
            else:
                await _create_suggestion(
                    db,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    category="relationship",
                    character_id=src.id if src else None,
                    target_character_id=tgt.id if tgt else None,
                    title=f"新增关系：{src_name or '未知人物'} × {tgt_name or '未知人物'}",
                    description=rel.get("description") or rel.get("relation_type") or "",
                    payload=rel,
                )
                result["suggestions"] += 1

        # 时间线事件
        for ev in parsed.get("timeline_events") or []:
            if not isinstance(ev, dict):
                continue
            if timeline_auto:
                await _apply_timeline(db, project_id, chapter_id, ev)
                result["auto_applied"]["timeline"] += 1
            else:
                await _create_suggestion(
                    db,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    category="timeline",
                    character_id=None,
                    target_character_id=None,
                    title=f"时间线事件：{ev.get('title') or '（未命名事件）'}",
                    description=ev.get("description") or ev.get("event_date_label") or "",
                    payload=ev,
                )
                result["suggestions"] += 1

        # 伏笔
        for fh in parsed.get("foreshadows") or []:
            if not isinstance(fh, dict):
                continue
            if foreshadow_auto:
                await _apply_foreshadow(db, project_id, chapter_id, fh)
                result["auto_applied"]["foreshadow"] += 1
            else:
                await _create_suggestion(
                    db,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    category="foreshadow",
                    character_id=None,
                    target_character_id=None,
                    title=f"新增伏笔：{str(fh.get('content') or '')[:60]}",
                    description=fh.get("planned_payoff") or "",
                    payload=fh,
                )
                result["suggestions"] += 1

        await record_generation(
            db,
            attribution_status="project_bound",
            project_id=project_id,
            chapter_id=chapter_id,
            action_type="extract_state",
            model_provider=(provider or "mock") if real else "mock",
            model_name=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_snapshot=serialize_context(
                {
                    "chapter_title": chapter_title,
                    "content_length": len(content or ""),
                    "trigger": "auto_extract",
                    "result": result,
                }
            ),
            response_content=json.dumps(
                {"extracted": parsed, "result": result}, ensure_ascii=False
            ),
            status=status,
            error_message=error_message,
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
    return result


async def trigger_state_extraction(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> dict:
    """接受正文后自动状态提取入口（失败不阻断正文保存流程）。"""
    try:
        return await extract_state_changes(
            db, project_id, chapter_id, chapter_title, content
        )
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
        return {"suggestions": 0, "auto_applied": {}}


# ---------------------------------------------------------------- 建议创建 ----------

async def _create_suggestion(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    category: str,
    character_id: uuid.UUID | None,
    target_character_id: uuid.UUID | None,
    title: str,
    description: str | None,
    payload: dict,
) -> None:
    db.add(
        ChangeSuggestion(
            project_id=project_id,
            chapter_id=chapter_id,
            category=category,
            character_id=character_id,
            target_character_id=target_character_id,
            title=title,
            description=description,
            payload=json.dumps(payload, ensure_ascii=False),
            status="pending",
        )
    )


# ---------------------------------------------------------------- 正式库写入 ----------

async def _apply_character_update(
    db: AsyncSession,
    project_id: uuid.UUID,
    character: Character,
    chapter_id: uuid.UUID,
    payload: dict,
    source: str = "user_confirmed",
) -> None:
    """更新人物卡当前状态并写快照（source 区分 ai_extracted / user_confirmed）。"""
    if payload.get("current_location"):
        character.current_location = str(payload["current_location"])[:255]
    if payload.get("current_timeline_status"):
        character.current_timeline_status = str(payload["current_timeline_status"])
    if payload.get("status"):
        character.status = str(payload["status"])
    if payload.get("goals"):
        character.goals = (
            "；".join(payload["goals"])
            if isinstance(payload["goals"], list)
            else str(payload["goals"])
        )
    # 首末出场章节维护
    if character.first_chapter_id is None:
        character.first_chapter_id = chapter_id
    character.last_chapter_id = chapter_id

    db.add(
        CharacterStateSnapshot(
            character_id=character.id,
            chapter_id=chapter_id,
            state_json=json.dumps(
                {
                    "current_location": character.current_location,
                    "current_timeline_status": character.current_timeline_status,
                    "status": character.status,
                    "goals": character.goals,
                    "changes": payload.get("changes") or [],
                },
                ensure_ascii=False,
            ),
            source=source,
            confirmed_at=(
                None if source == "ai_extracted" else _now_utc()
            ),
        )
    )


async def _apply_relationship(
    db: AsyncSession,
    project_id: uuid.UUID,
    src: Character,
    tgt: Character,
    chapter_id: uuid.UUID,
    payload: dict,
) -> None:
    """写入关系（确认后 status=confirmed；自动提取也视为已确认事实）。"""
    rel_type = payload.get("relation_type") or "其他"
    db.add(
        CharacterRelationship(
            project_id=project_id,
            source_character_id=src.id,
            target_character_id=tgt.id,
            relation_type=rel_type,
            description=payload.get("description"),
            start_chapter_id=chapter_id,
            end_chapter_id=None,
            status="confirmed",
        )
    )


async def _apply_timeline(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    payload: dict,
) -> None:
    """写入时间线事件（确认后 confirmed=True；自动提取 source=ai_extracted）。"""
    db.add(
        TimelineEvent(
            project_id=project_id,
            title=str(payload.get("title") or "（未命名事件）")[:255],
            description=payload.get("description"),
            chapter_id=chapter_id,
            event_date_label=payload.get("event_date_label"),
            event_time=payload.get("event_date_label"),
            sequence_order=payload.get("sequence_order"),
            participants=json.dumps(
                payload.get("participants") or [], ensure_ascii=False
            ),
            source="ai_extracted",
            confirmed=True,
        )
    )


async def _apply_foreshadow(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    payload: dict,
) -> None:
    """回写伏笔到章节记忆 memory_json.foreshadows_added（幂等）。

    第四期「写错能发现」：同时同步写入伏笔表（foreshadows），状态为 planted，
    便于伏笔追踪与回收提醒；重复内容不重复插入。
    """
    row = (
        await db.execute(
            select(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    try:
        mem = json.loads(row.memory_json or "{}")
    except Exception:  # noqa: BLE001
        mem = {}
    foreshadows = mem.get("foreshadows_added") or []
    content = str(payload.get("content") or "")
    exists = any(
        isinstance(f, dict) and f.get("content") == content for f in foreshadows
    )
    if content and not exists:
        foreshadows.append(
            {
                "content": content,
                "planned_payoff": payload.get("planned_payoff") or "",
            }
        )
    mem["foreshadows_added"] = foreshadows
    row.memory_json = json.dumps(mem, ensure_ascii=False)

    # 第四期：同步写入伏笔表（已埋设状态 planted）
    if content:
        from app.models.foreshadow import Foreshadow

        dup = (
            await db.execute(
                select(Foreshadow).where(
                    Foreshadow.project_id == project_id,
                    Foreshadow.content == content,
                )
            )
        ).scalar_one_or_none()
        if dup is None:
            db.add(
                Foreshadow(
                    project_id=project_id,
                    content=content,
                    planted_chapter_id=chapter_id,
                    planned_payoff=payload.get("planned_payoff") or "",
                    status="planted",
                    source="chapter_memory",
                )
            )


# ---------------------------------------------------------------- 建议确认 ----------

async def confirm_suggestion(
    db: AsyncSession,
    suggestion_id: uuid.UUID,
    action: str = "confirm",
    payload: dict | None = None,
) -> ChangeSuggestion:
    """确认/修改确认/忽略变更建议。

    - action=confirm：按建议 payload 写入正式库，status=confirmed；
    - action=modified：用传入 payload 覆盖后写入正式库，status=modified；
    - action=ignore：仅标记 status=ignored，不写入。
    """
    row = (
        await db.execute(
            select(ChangeSuggestion).where(ChangeSuggestion.id == suggestion_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("suggestion_not_found")

    if action == "confirm":
        # 原子认领：仅 pending 可确认。条件 UPDATE 保证并发双击只有一个请求胜出，
        # 防止重复写入 CharacterStateSnapshot / CharacterRelationship / TimelineEvent
        claimed = await db.execute(
            update(ChangeSuggestion)
            .where(
                ChangeSuggestion.id == suggestion_id,
                ChangeSuggestion.status == "pending",
            )
            .values(status="confirmed")
        )
        if claimed.rowcount == 0:
            raise ValueError("suggestion_already_handled")

    if action == "ignore":
        row.status = "ignored"
        await db.commit()
        await db.refresh(row)
        return row

    use_payload = payload if isinstance(payload, dict) and payload else (
        json.loads(row.payload or "{}")
    )
    if not isinstance(use_payload, dict):
        use_payload = {}

    if row.category == "character" and row.character_id is not None:
        character = (
            await db.execute(
                select(Character).where(Character.id == row.character_id)
            )
        ).scalar_one_or_none()
        if character is not None:
            await _apply_character_update(
                db, row.project_id, character, row.chapter_id, use_payload,
                source="user_confirmed",
            )

    elif row.category == "relationship":
        if row.character_id is not None and row.target_character_id is not None:
            src = (
                await db.execute(
                    select(Character).where(Character.id == row.character_id)
                )
            ).scalar_one_or_none()
            tgt = (
                await db.execute(
                    select(Character).where(Character.id == row.target_character_id)
                )
            ).scalar_one_or_none()
            if src is not None and tgt is not None:
                await _apply_relationship(
                    db, row.project_id, src, tgt, row.chapter_id, use_payload
                )

    elif row.category == "timeline":
        await _apply_timeline(
            db, row.project_id, row.chapter_id, use_payload
        )

    elif row.category == "foreshadow":
        await _apply_foreshadow(
            db, row.project_id, row.chapter_id, use_payload
        )

    row.status = "confirmed" if action == "confirm" else "modified"
    row.confirmed_at = _now_utc()
    if action == "modified":
        row.payload = json.dumps(use_payload, ensure_ascii=False)
        row.description = use_payload.get("description") or use_payload.get("changes") and "；".join(use_payload["changes"]) or row.description

    await db.commit()
    await db.refresh(row)
    return row


def _now_utc():
    from datetime import datetime

    return datetime.utcnow()


# ============================================================================
# 第一批「事实数据库与规则校验」（第三节）：结构化事实提取
# 在既有「状态变更候选+确认流程」之上，新增按章节的结构化事实提取与落库：
#   - 提取：章节元数据 / 人物快照（年龄/生死/地点/持有物品/已知秘密/伤势）/
#           时间线事件 / 物品 / 秘密知情关系，统一 JSON 结构；
#   - 落库：按章节幂等重写（先删本章 ai_extracted 事实，再写入新值）；
#   - 失败不阻断正文保存（trigger_state_facts 兜底）；
#   - 不触碰既有正文、不删除任何用户手工确认数据。
# ============================================================================

def build_mock_state_facts(chapter_title: str, content: str) -> dict:
    """演示模式（未配置 AI 密钥）的合理 mock 结构化事实提取结果。

    结构与真实模式一致：chapter_meta / snapshots / timeline_events / items / secrets，
    字段尽量呼应正文开头，便于全链路自测与回归测试。
    """
    clean = (content or "").replace("\n", " ").replace("\r", " ").strip()
    seg = clean[:60].strip()
    hint = f"（正文提及：{seg[:20]}…）" if seg else ""
    return {
        "chapter_meta": {
            "chapter_time": "景历二十三年秋",
            "location": "边军大营",
            "present_characters": ["陈九", "边军大营都尉"],
            "goal": "本章推进陈九加入边军大营",
            "outcome": f"陈九正式加入边军大营，与都尉结盟 {hint}",
        },
        "snapshots": [
            {
                "character": "陈九",
                "age": 23,
                "birth_year": 1000,
                "is_alive": True,
                "location": "边军大营",
                "possessions": ["玄铁剑"],
                "known_secrets": [],
                "injuries": [],
                "timeline_status": "加入边军大营",
            }
        ],
        "timeline_events": [
            {
                "title": "陈九加入边军大营",
                "event_date_label": "景历二十三年秋",
                "absolute_time": "景历二十三年秋",
                "sequence_order": 1,
                "participants": ["陈九", "边军大营都尉"],
                "description": "陈九离开北关城，正式加入边军大营，与都尉结盟。",
                "is_public": True,
            }
        ],
        "items": [
            {
                "name": "玄铁剑",
                "category": "item",
                "owner": "陈九",
                "description": "陈九随身佩剑",
                "first_chapter": True,
            }
        ],
        "secrets": [
            {
                "name": "陈九身世之谜",
                "known_by": ["陈九"],
                "learned_chapter": True,
                "description": "陈九自己知晓的身世秘密",
            }
        ],
    }


async def _match_location(
    db: AsyncSession, project_id: uuid.UUID, name: str
):
    """按名称匹配项目内地点，找不到返回 None。"""
    from app.models.location import Location

    if not name:
        return None
    name = str(name).strip()
    rows = (
        await db.execute(
            select(Location).where(Location.project_id == project_id)
        )
    ).scalars().all()
    for loc in rows:
        if loc.name == name:
            return loc
    # 阶段E修复（E-02）：禁止包含匹配，解析不到就是解析不到。
    return None


# --------------------------------------- 阶段E 写路径 E-01/E-02：名字归一化 ----------


@dataclass(frozen=True)
class _NameResolution:
    """单条名字引用的解析结果（可直接序列化进 unresolved 列表）。"""

    path: str
    entity_type: str
    name: str
    status: str
    entity_id: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def blocking(self) -> bool:
        """unknown/ambiguous/malformed/conflict 阻塞严格模式；empty 不阻塞但需上报。"""
        return self.status in {"unknown", "ambiguous", "malformed", "conflict"}

    def as_unresolved(self) -> dict:
        return {
            "path": self.path,
            "entity_type": self.entity_type,
            "name": self.name,
            "reason": self.status,
            "candidates": list(self.candidates),
        }


@dataclass
class _EntityIndex:
    """项目内实体的名字索引（一次查询，供整批名字解析复用）。"""

    character_by_name: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    character_by_alias: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    location_by_name: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    item_by_key: dict[tuple[str, str], list[uuid.UUID]] = field(default_factory=dict)
    display_name: dict[uuid.UUID, str] = field(default_factory=dict)


def _split_aliases(raw: Any) -> list[str]:
    """解析 Character.aliases（JSON 文本或列表）为去重后的别名列表。"""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data: Any = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = [raw]
    else:
        data = raw
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return []
    out: list[str] = []
    for item in data:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def _build_entity_index(
    db: AsyncSession, project_id: uuid.UUID
) -> _EntityIndex:
    """构建项目内人物/地点/物品的名字索引（只读查询）。"""
    from app.models.item import Item
    from app.models.location import Location

    index = _EntityIndex()
    char_rows = (
        await db.execute(
            select(Character).where(Character.project_id == project_id)
        )
    ).scalars().all()
    for row in char_rows:
        name = str(row.name or "").strip()
        if name:
            index.character_by_name.setdefault(name, []).append(row.id)
            index.display_name[row.id] = name
        for alias in _split_aliases(row.aliases):
            bucket = index.character_by_alias.setdefault(alias, [])
            if row.id not in bucket:
                bucket.append(row.id)
    loc_rows = (
        await db.execute(
            select(Location).where(Location.project_id == project_id)
        )
    ).scalars().all()
    for row in loc_rows:
        name = str(row.name or "").strip()
        if name:
            index.location_by_name.setdefault(name, []).append(row.id)
            index.display_name[row.id] = name
    item_rows = (
        await db.execute(select(Item).where(Item.project_id == project_id))
    ).scalars().all()
    for row in item_rows:
        name = str(row.name or "").strip()
        if not name:
            continue
        category = str(row.category or "item").strip() or "item"
        index.item_by_key.setdefault((category, name), []).append(row.id)
        index.display_name[row.id] = name
    return index


def _names_of(index: _EntityIndex, ids: list[uuid.UUID]) -> tuple[str, ...]:
    return tuple(index.display_name.get(i, str(i)) for i in ids)


def _resolve_name(
    index: _EntityIndex,
    entity_type: str,
    raw_name: Any,
    path: str,
    category: str | None = None,
) -> _NameResolution:
    """把单个名字引用解析成实体 id（只认精确 name / 精确别名，禁止包含匹配）。"""
    if raw_name is None:
        return _NameResolution(path, entity_type, "", "empty")
    if not isinstance(raw_name, str):
        return _NameResolution(path, entity_type, str(raw_name), "malformed")
    name = raw_name.strip()
    if not name:
        return _NameResolution(path, entity_type, "", "empty")

    if entity_type == "character":
        exact = list(index.character_by_name.get(name) or [])
        if len(exact) == 1:
            return _NameResolution(path, entity_type, name, "resolved", str(exact[0]))
        if len(exact) > 1:
            return _NameResolution(
                path, entity_type, name, "ambiguous", None, _names_of(index, exact)
            )
        alias = list(index.character_by_alias.get(name) or [])
        if len(alias) == 1:
            return _NameResolution(path, entity_type, name, "resolved", str(alias[0]))
        if len(alias) > 1:
            return _NameResolution(
                path, entity_type, name, "ambiguous", None, _names_of(index, alias)
            )
        return _NameResolution(path, entity_type, name, "unknown")

    if entity_type == "location":
        hits = list(index.location_by_name.get(name) or [])
        if len(hits) == 1:
            return _NameResolution(path, entity_type, name, "resolved", str(hits[0]))
        if len(hits) > 1:
            return _NameResolution(
                path, entity_type, name, "ambiguous", None, _names_of(index, hits)
            )
        return _NameResolution(path, entity_type, name, "unknown")

    # item / secret 走 (category, name) 精确键
    key = ((category or "item").strip() or "item", name)
    hits = list(index.item_by_key.get(key) or [])
    if len(hits) == 1:
        return _NameResolution(path, entity_type, name, "resolved", str(hits[0]))
    if len(hits) > 1:
        return _NameResolution(
            path, entity_type, name, "ambiguous", None, _names_of(index, hits)
        )
    return _NameResolution(path, entity_type, name, "unknown")


def _collect(
    resolutions: list[_NameResolution], resolution: _NameResolution
) -> _NameResolution:
    """空引用不入 unresolved（只在整批空 payload 时单独上报）。"""
    if resolution.status != "empty":
        resolutions.append(resolution)
    return resolution


def _apply_ref(
    index: _EntityIndex,
    container: dict,
    entity_type: str,
    name_field: str,
    id_field: str,
    path: str,
) -> _NameResolution:
    """把 container[name_field] 解析成 id，写入 payload 副本的 id_field。"""
    resolution = _resolve_name(index, entity_type, container.get(name_field), path)
    if resolution.resolved:
        container[id_field] = resolution.entity_id
        return resolution
    if not resolution.blocking:
        return resolution

    # 名字解析不到、但 payload 已显式给出 *_id（历史调用方/兼容负载）：
    # 用实体索引反查裁决，避免把"名字已给但库里没有"的脏数据蒙混进库。
    explicit = container.get(id_field)
    explicit_text = str(explicit).strip() if isinstance(explicit, (str, uuid.UUID)) else ""
    if explicit_text:
        try:
            parsed = uuid.UUID(explicit_text)
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is not None and parsed in index.display_name:
            if index.display_name[parsed] == resolution.name:
                # 名字与 id 指向同一实体：视为已解析
                container[id_field] = str(parsed)
                return _NameResolution(
                    path, entity_type, resolution.name, "resolved", str(parsed)
                )
            # 张冠李戴：名字与 id 指向不同实体，清掉 id 并计冲突
            container.pop(id_field, None)
            return _NameResolution(
                path,
                entity_type,
                resolution.name,
                "conflict",
                None,
                (index.display_name[parsed],),
            )
        # id 在库中不存在：保留原 id 不动，交给实体引用校验器裁决（此处不判 blocking）
        return _NameResolution(
            path, entity_type, resolution.name, "id_unverified", None
        )
    container.pop(id_field, None)
    return resolution


async def _normalize_state_facts(
    db: AsyncSession, project_id: uuid.UUID, facts: dict
) -> tuple[dict, list[_NameResolution]]:
    """把 AI payload 的名字引用归一化成实体 id（返回副本，绝不原地改原始 payload）。

    为什么需要：真实 LLM 输出只有人名/地名/物名字符串、没有 *_id 字段，而校验器
    白名单只覆盖 *_id，导致 checked=0 / ok=True 恒成立（E-01）。本函数在写路径
    之前完成名字 → id 解析，并把解析失败项结构化上报（E-02）。
    列表型引用（participants / known_by）由本归一化器自行校验，不依赖校验器白名单。
    """
    index = await _build_entity_index(db, project_id)
    normalized = copy.deepcopy(facts)
    resolutions: list[_NameResolution] = []

    meta = normalized.get("chapter_meta")
    if isinstance(meta, dict):
        _collect(
            resolutions,
            _apply_ref(
                index, meta, "location", "location", "location_id",
                "chapter_meta.location",
            ),
        )

    for i, snap in enumerate(normalized.get("snapshots") or []):
        if not isinstance(snap, dict):
            continue
        _collect(
            resolutions,
            _apply_ref(
                index, snap, "character", "character", "character_id",
                f"snapshots[{i}].character",
            ),
        )
        _collect(
            resolutions,
            _apply_ref(
                index, snap, "location", "location", "location_id",
                f"snapshots[{i}].location",
            ),
        )

    for i, ev in enumerate(normalized.get("timeline_events") or []):
        if not isinstance(ev, dict):
            continue
        participants = ev.get("participants")
        if not isinstance(participants, (list, tuple)):
            continue
        participant_ids: list[str] = []
        for j, pname in enumerate(participants):
            res = _collect(
                resolutions,
                _resolve_name(
                    index, "character", pname,
                    f"timeline_events[{i}].participants[{j}]",
                ),
            )
            if res.resolved:
                participant_ids.append(res.entity_id or "")
        if participant_ids:
            ev["participant_ids"] = participant_ids
        else:
            ev.pop("participant_ids", None)

    for i, it in enumerate(normalized.get("items") or []):
        if not isinstance(it, dict):
            continue
        _collect(
            resolutions,
            _apply_ref(
                index, it, "character", "owner", "owner_character_id",
                f"items[{i}].owner",
            ),
        )

    for i, sk in enumerate(normalized.get("secrets") or []):
        if not isinstance(sk, dict):
            continue
        knowers = sk.get("known_by")
        if not isinstance(knowers, (list, tuple)):
            continue
        knower_ids: list[str] = []
        for j, kname in enumerate(knowers):
            res = _collect(
                resolutions,
                _resolve_name(
                    index, "character", kname,
                    f"secrets[{i}].known_by[{j}]",
                ),
            )
            if res.resolved:
                knower_ids.append(res.entity_id or "")
        if knower_ids:
            sk["knower_character_ids"] = knower_ids
        else:
            sk.pop("knower_character_ids", None)

    return normalized, resolutions


def _ref_id(
    refs: dict[str, _NameResolution], path: str
) -> str | None:
    res = refs.get(path)
    return res.entity_id if res is not None and res.resolved else None


def _meta_has_payload(facts: dict) -> bool:
    """chapter_meta 是否存在可写字段（写入函数会据此更新章节元数据）。"""
    meta = facts.get("chapter_meta")
    if not isinstance(meta, dict):
        return False
    if meta.get("present_characters"):
        return True
    for key in ("chapter_time", "location", "goal", "outcome"):
        if str(meta.get(key) or "").strip():
            return True
    return False


def _facts_have_payload(facts: dict) -> bool:
    """payload 是否有任何可写内容（空 payload 不阻塞严格模式，但版本不递增）。"""
    for key in ("snapshots", "timeline_events", "items", "secrets"):
        if facts.get(key):
            return True
    return _meta_has_payload(facts)


def _has_writable_item(
    facts: dict, resolutions: list[_NameResolution]
) -> bool:
    """非严格模式下是否仍有可写内容（用于判定"整批引用都解析不到"）。

    列表型事实本身即使引用未解析也会落行（事件/物品/秘密），因此只要存在这些
    列表即视为可写；快照则必须人物引用已解析才可写。
    """
    for key in ("timeline_events", "items", "secrets"):
        if facts.get(key):
            return True
    meta = facts.get("chapter_meta")
    if isinstance(meta, dict) and _meta_has_payload(facts) and not str(
        meta.get("location") or ""
    ).strip():
        return True
    for res in resolutions:
        if res.resolved and res.path.startswith("snapshots[") and res.path.endswith(
            "].character"
        ):
            return True
    return False


async def _write_structured_facts(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    facts: dict,
    resolutions: list[_NameResolution] | None = None,
) -> dict:
    """把结构化事实按章节幂等写入各表（items / item_possessions / secret_knowers /
    character_state_snapshots / timeline_events / outlines 元数据）。

    幂等策略（可重复执行、不重复堆积、不误删手工数据）：
    1) 先删本章自动提取（source=ai_extracted）的快照与时间线事件；
    2) 先删本章建立的持有登记（since_chapter_id=本章）与知情登记
       （learned_chapter_id=本章）；
    3) items 只 upsert（不删除，保护历史引用），first_chapter_id 只增不减；
    4) outlines 章节元数据直接覆盖（本章最新事实为准）。
    返回写入统计。
    """
    from app.models.item import Item
    from app.models.item_possession import ItemPossession
    from app.models.location import Location
    from app.models.outline import Outline
    from app.models.secret_knower import SecretKnower

    # 阶段E：写路径只接受已完成名字归一化的 payload；单独调用时先就地补归一化，
    # 保证任何入口都不会跳过名字 → id 解析（E-01）而让校验器空转。
    if resolutions is None:
        facts, resolutions = await _normalize_state_facts(db, project_id, facts)
    refs = {r.path: r for r in resolutions}

    stats = {
        "snapshots": 0,
        "timeline_events": 0,
        "items": 0,
        "possessions": 0,
        "secrets": 0,
        "secret_knowers": 0,
    }

    # ---- 0) 幂等清理：本章自动提取的旧事实（阶段A：跳过人工锁定行） ----
    old_snaps = (
        await db.execute(
            select(CharacterStateSnapshot).where(
                CharacterStateSnapshot.chapter_id == chapter_id,
                CharacterStateSnapshot.source == "ai_extracted",
                CharacterStateSnapshot.locked == False,  # noqa: E712
            )
        )
    ).scalars().all()
    for s in old_snaps:
        await db.delete(s)
    old_events = (
        await db.execute(
            select(TimelineEvent).where(
                TimelineEvent.chapter_id == chapter_id,
                TimelineEvent.source == "ai_extracted",
                TimelineEvent.locked == False,  # noqa: E712
            )
        )
    ).scalars().all()
    for e in old_events:
        await db.delete(e)
    old_poss = (
        await db.execute(
            select(ItemPossession).where(
                ItemPossession.since_chapter_id == chapter_id,
                ItemPossession.locked == False,  # noqa: E712
            )
        )
    ).scalars().all()
    for p in old_poss:
        await db.delete(p)
    old_knowers = (
        await db.execute(
            select(SecretKnower).where(
                SecretKnower.learned_chapter_id == chapter_id,
                SecretKnower.locked == False,  # noqa: E712
            )
        )
    ).scalars().all()
    for k in old_knowers:
        await db.delete(k)

    # ---- 1) 章节元数据 ----
    meta = facts.get("chapter_meta") or {}
    outline = (
        await db.execute(
            select(Outline).where(Outline.id == chapter_id)
        )
    ).scalar_one_or_none()
    if outline is not None and isinstance(meta, dict) and _meta_has_payload(facts):
        # 阶段E修复：空 payload 或未提供的 meta 字段一律保留原值，禁止用空值清空既有设定
        loc_id = _ref_id(refs, "chapter_meta.location")
        if meta.get("chapter_time") is not None:
            outline.chapter_time = str(meta.get("chapter_time") or "")[:255] or None
        if loc_id is not None:
            outline.location_id = loc_id
        if meta.get("present_characters") is not None:
            outline.present_characters = json.dumps(
                meta.get("present_characters") or [], ensure_ascii=False
            )
        if meta.get("goal") is not None:
            outline.chapter_goal = meta.get("goal") or None
        if meta.get("outcome") is not None:
            outline.chapter_outcome = meta.get("outcome") or None
        outline.state_facts_updated_at = _now_utc()

    # ---- 2) 人物快照 ----
    for i, snap in enumerate(facts.get("snapshots") or []):
        if not isinstance(snap, dict):
            continue
        char_id = _ref_id(refs, f"snapshots[{i}].character")
        if char_id is None:
            # 名字未解析（或缺失）：已由归一化器记入 unresolved，这里只跳过写入
            continue
        loc_id = _ref_id(refs, f"snapshots[{i}].location")
        age = snap.get("age")
        birth_year = snap.get("birth_year")
        if isinstance(age, str):
            try:
                age = int(age)
            except Exception:  # noqa: BLE001
                age = None
        if isinstance(birth_year, str):
            try:
                birth_year = int(birth_year)
            except Exception:  # noqa: BLE001
                birth_year = None
        db.add(
            CharacterStateSnapshot(
                character_id=uuid.UUID(char_id),
                chapter_id=chapter_id,
                state_json=json.dumps(
                    {
                        "timeline_status": snap.get("timeline_status") or "",
                        "changes": snap.get("changes") or [],
                    },
                    ensure_ascii=False,
                ),
                age=age,
                birth_year=birth_year,
                is_alive=snap.get("is_alive"),
                location_id=uuid.UUID(loc_id) if loc_id else None,
                possessions=json.dumps(
                    snap.get("possessions") or [], ensure_ascii=False
                ),
                known_secrets=json.dumps(
                    snap.get("known_secrets") or [], ensure_ascii=False
                ),
                injuries=json.dumps(
                    snap.get("injuries") or [], ensure_ascii=False
                ),
                source="ai_extracted",
            )
        )
        stats["snapshots"] += 1

    # ---- 3) 时间线事件 ----
    for ev in facts.get("timeline_events") or []:
        if not isinstance(ev, dict):
            continue
        # 阶段E：参与人 id 由归一化器解析（列表引用不在校验器白名单内，必须自行校验）
        participant_ids = list(ev.get("participant_ids") or [])
        db.add(
            TimelineEvent(
                project_id=project_id,
                title=str(ev.get("title") or "（未命名事件）")[:255],
                description=ev.get("description"),
                chapter_id=chapter_id,
                event_date_label=ev.get("event_date_label"),
                event_time=ev.get("event_date_label"),
                sequence_order=ev.get("sequence_order"),
                participants=json.dumps(
                    ev.get("participants") or [], ensure_ascii=False
                ),
                absolute_time=ev.get("absolute_time"),
                relative_time=ev.get("relative_time"),
                participant_ids=json.dumps(participant_ids, ensure_ascii=False)
                if participant_ids
                else None,
                is_public=(
                    bool(ev["is_public"])
                    if ev.get("is_public") is not None
                    else True
                ),
                source="ai_extracted",
                confirmed=True,
            )
        )
        stats["timeline_events"] += 1

    # ---- 4) 物品与持有关系 ----
    for i, it in enumerate(facts.get("items") or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        category = str(it.get("category") or "item").strip() or "item"
        existing = (
            await db.execute(
                select(Item).where(
                    Item.project_id == project_id,
                    Item.name == name,
                    Item.category == category,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            item = Item(
                project_id=project_id,
                name=name,
                category=category,
                first_chapter_id=chapter_id,
                description=it.get("description") or None,
                status="active",
            )
            db.add(item)
            await db.flush()
            stats["items"] += 1
        else:
            item = existing
            # 阶段A：人工锁定行不自动更新首次登场章节（锁定设定优先）
            if item.first_chapter_id is None and not item.locked:
                item.first_chapter_id = chapter_id
        owner_id = _ref_id(refs, f"items[{i}].owner")
        if owner_id:
            db.add(
                ItemPossession(
                    item_id=item.id,
                    owner_character_id=uuid.UUID(owner_id),
                    since_chapter_id=chapter_id,
                    until_chapter_id=None,
                    status="active",
                )
            )
            stats["possessions"] += 1

    # ---- 5) 秘密与知情关系 ----
    for sk in facts.get("secrets") or []:
        if not isinstance(sk, dict):
            continue
        name = str(sk.get("name") or "").strip()
        if not name:
            continue
        existing = (
            await db.execute(
                select(Item).where(
                    Item.project_id == project_id,
                    Item.name == name,
                    Item.category == "secret",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            item = Item(
                project_id=project_id,
                name=name,
                category="secret",
                first_chapter_id=chapter_id,
                description=sk.get("description") or None,
                status="active",
            )
            db.add(item)
            await db.flush()
            stats["secrets"] += 1
        else:
            item = existing
            # 阶段A：人工锁定行不自动更新首次登场章节（锁定设定优先）
            if item.first_chapter_id is None and not item.locked:
                item.first_chapter_id = chapter_id
        # 阶段E：知情者 id 由归一化器解析（列表引用不在校验器白名单内，必须自行校验）
        for knower_id in sk.get("knower_character_ids") or []:
            db.add(
                SecretKnower(
                    secret_id=item.id,
                    character_id=uuid.UUID(str(knower_id)),
                    learned_chapter_id=chapter_id,
                    source="ai_extracted",
                )
            )
            stats["secret_knowers"] += 1

    return stats


async def record_chapter_fact_dependencies(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    facts: dict,
) -> int:
    """阶段H「依赖图增量回归」：记录本章引用的事实键到 chapter_fact_dependencies。

    幂等策略：先删本章旧依赖行，再按本章最新提取结果重写（与 _write_structured_facts
    同一事务内调用）。实体可匹配到项目内卡片（人物/地点/物品/秘密）时记录实体级键
    （entity_type + entity_id）；匹配不到时记录名字级键（entity_id=NULL，
    fact_key 内嵌名字），保证未来按名字也能命中。
    返回写入行数；本函数只读写依赖表，不触碰正文与既有事实表。
    """
    from app.models.chapter_fact_dependency import ChapterFactDependency
    from app.models.item import Item

    old_deps = (
        await db.execute(
            select(ChapterFactDependency).where(
                ChapterFactDependency.chapter_id == chapter_id
            )
        )
    ).scalars().all()
    for d in old_deps:
        await db.delete(d)

    rows: list[ChapterFactDependency] = []

    def _add(
        entity_type: str,
        entity_id: uuid.UUID | None,
        fact_key: str,
        snapshot: Any,
    ) -> None:
        rows.append(
            ChapterFactDependency(
                chapter_id=chapter_id,
                project_id=project_id,
                entity_type=entity_type,
                entity_id=(
                    str(entity_id).replace("-", "")
                    if entity_id is not None
                    else None
                ),
                fact_key=fact_key,
                dep_snapshot=(
                    json.dumps(snapshot, ensure_ascii=False, default=str)
                    if snapshot is not None
                    else None
                ),
            )
        )

    def _cid(entity_id: uuid.UUID | None) -> str:
        """实体 UUID → 32 位 hex 键片段（与 entity_id 列存储格式一致）。"""
        return str(entity_id).replace("-", "") if entity_id is not None else ""

    # ---- 1) 章节元数据依赖 ----
    meta = facts.get("chapter_meta") or {}
    loc_name = str(meta.get("location") or "").strip()
    if loc_name:
        loc = await _match_location(db, project_id, loc_name)
        if loc is not None:
            _add("location", loc.id, f"location:{_cid(loc.id)}:presence", loc_name)
        else:
            _add("location", None, f"location:name:{loc_name}", loc_name)
    for cname in meta.get("present_characters") or []:
        cname = str(cname or "").strip()
        if not cname:
            continue
        c = await _match_character(db, project_id, cname)
        if c is not None:
            _add("character", c.id, f"character:{_cid(c.id)}:present", cname)
        else:
            _add("character", None, f"character:name:{cname}:present", cname)

    # ---- 2) 人物快照依赖 ----
    for snap in facts.get("snapshots") or []:
        if not isinstance(snap, dict):
            continue
        cname = str(snap.get("character") or "").strip()
        if not cname:
            continue
        c = await _match_character(db, project_id, cname)
        if c is None:
            _add("character", None, f"character:name:{cname}:snapshot", cname)
            continue
        cid = c.id
        _add("character", cid, f"character:{_cid(cid)}:snapshot", cname)
        if snap.get("is_alive") is not None:
            _add("character", cid, f"character:{_cid(cid)}:is_alive", snap.get("is_alive"))
        if snap.get("age") is not None:
            _add("character", cid, f"character:{_cid(cid)}:age", snap.get("age"))
        sloc = str(snap.get("location") or "").strip()
        if sloc:
            loc = await _match_location(db, project_id, sloc)
            if loc is not None:
                _add("location", loc.id, f"location:{_cid(loc.id)}:presence", sloc)
            else:
                _add("location", None, f"location:name:{sloc}", sloc)
        for pname in snap.get("possessions") or []:
            pname = str(pname or "").strip()
            if pname:
                _add("item", None, f"item:name:{pname}:possessed", pname)
        for sname in snap.get("known_secrets") or []:
            sname = str(sname or "").strip()
            if sname:
                _add("secret", None, f"secret:name:{sname}:known", sname)

    # ---- 3) 时间线事件参与角色依赖 ----
    for ev in facts.get("timeline_events") or []:
        if not isinstance(ev, dict):
            continue
        for pname in ev.get("participants") or []:
            pname = str(pname or "").strip()
            if not pname:
                continue
            c = await _match_character(db, project_id, pname)
            if c is not None:
                _add("character", c.id, f"character:{_cid(c.id)}:participant", pname)
            else:
                _add(
                    "character",
                    None,
                    f"character:name:{pname}:participant",
                    pname,
                )

    # ---- 4) 物品依赖：匹配到项目物品卡 → 实体键，否则名字级键 ----
    for it in facts.get("items") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        category = str(it.get("category") or "item").strip() or "item"
        item = (
            await db.execute(
                select(Item).where(
                    Item.project_id == project_id,
                    Item.name == name,
                    Item.category == category,
                )
            )
        ).scalar_one_or_none()
        if item is not None:
            _add("item", item.id, f"item:{_cid(item.id)}:active", name)
        else:
            _add("item", None, f"item:name:{name}:{category}", name)

    # ---- 5) 秘密依赖 ----
    for sk in facts.get("secrets") or []:
        if not isinstance(sk, dict):
            continue
        name = str(sk.get("name") or "").strip()
        if not name:
            continue
        secret = (
            await db.execute(
                select(Item).where(
                    Item.project_id == project_id,
                    Item.name == name,
                    Item.category == "secret",
                )
            )
        ).scalar_one_or_none()
        if secret is not None:
            _add("secret", secret.id, f"secret:{_cid(secret.id)}:known", name)
        else:
            _add("secret", None, f"secret:name:{name}", name)

    for row in rows:
        db.add(row)
    if rows:
        await db.flush()
    return len(rows)


async def extract_state_facts(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
    status_out: dict | None = None,
) -> dict | None:
    """结构化事实提取（不落库）：调用 LLM 按固定 JSON 结构提取，失败返回 None。

    返回结构（与既有事实结构一致）：
      chapter_meta / snapshots / timeline_events / items / secrets。

    返回 None 表示"没有可用于写库的结果"，原因写入 status_out["reason"]：
      ai_not_configured（未配置 AI，禁止用固定虚构事实兜底）/ ai_error / invalid_payload。
    """
    if status_out is not None:
        status_out.clear()
    provider, model = resolve_ai_provider()
    real = is_ai_configured()

    system_prompt = (
        "你是小说结构化事实提取器。请阅读用户给出的章节正文，提取本章的事实，"
        "包括：章节元数据（时间/地点/出场人物/目标/落点）、人物状态快照"
        "（年龄/出生年/生死/地点/持有物品/已知秘密/伤势）、时间线事件、"
        "物品（含持有人）与秘密（含本章起知晓该秘密的人物）。"
        "只输出 JSON，不要任何多余文字。只提取正文明确提及的事实，"
        "不要编造。"
    )
    user_prompt = (
        "章节标题：《" + (chapter_title or "") + "》\n"
        "章节正文：\n" + (content or "") + "\n\n"
        "输出 JSON 格式：\n"
        '{"chapter_meta": {"chapter_time": "时间标签或空", "location": "主要地点名或空",'
        ' "present_characters": ["人物名"], "goal": "本章目标或空", "outcome": "本章落点或空"}, '
        '"snapshots": [{"character": "人物名", "age": 整数或空, "birth_year": 整数或空,'
        ' "is_alive": true, "location": "地点名或空", "possessions": ["物品名"],'
        ' "known_secrets": ["秘密名"], "injuries": ["伤势"], "timeline_status": "状态或空",'
        ' "changes": ["变化描述"]}], '
        '"timeline_events": [{"title": "事件标题", "event_date_label": "日期标签或空",'
        ' "absolute_time": "可排序时间或空", "relative_time": "相对时间或空",'
        ' "sequence_order": 1, "participants": ["人物名"], "description": "事件描述",'
        ' "is_public": true}], '
        '"items": [{"name": "物品名", "category": "item", "owner": "持有人名或空",'
        ' "description": "描述或空", "first_chapter": true}], '
        '"secrets": [{"name": "秘密名", "known_by": ["本章已知晓的人物"],'
        ' "learned_chapter": true, "description": "描述或空"}]}'
    )

    if not real:
        # 阶段E修复（E-03）：未配置 AI 时禁止返回固定虚构事实（mock）落库，
        # 直接放弃本次写入，由调用方记录 skipped_reason。
        if status_out is not None:
            status_out["reason"] = "ai_not_configured"
        logger.info("extract_state_facts: 未配置 AI，跳过结构化事实提取（不落库）")
        return None

    try:
        raw = await _chat_with_ai_retry_on_truncation(
                    system_prompt,
                    user_prompt,
                    task_type=TaskType.FACT_EXTRACTION.value,
                )
        raw = _strip_code_fence(raw)
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        if status_out is not None:
            status_out["reason"] = "ai_error"
        return None
    if not isinstance(parsed, dict):
        if status_out is not None:
            status_out["reason"] = "invalid_payload"
        return None
    return parsed


async def trigger_state_facts(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> dict:
    """保存链路入口：结构化事实提取 + 按章节幂等落库（失败不阻断正文保存）。

    返回统计 dict；任何异常都被吞掉并回滚本次提取写入（正文已在保存接口提交，
    不受影响）。
    """
    result = {
        "ok": False,
        "extracted": False,
        "written": {},
        "written_total": 0,
        "rejected": None,
        "error": None,
        "skipped_reason": None,
        "facts_version_status": None,
        "unresolved": [],
        "blocking_unresolved": 0,
    }
    extract_status: dict = {}
    try:
        facts = await extract_state_facts(
            db, project_id, chapter_id, chapter_title, content,
            status_out=extract_status,
        )
        result["extracted"] = facts is not None
        if facts is None:
            # AI 未配置 / 调用异常 / 输出不可解析：一律不写库、不动版本（E-03）
            result["skipped_reason"] = extract_status.get("reason") or "no_payload"
            if result["skipped_reason"] == "ai_not_configured":
                result["unresolved"] = [
                    {
                        "path": "<payload>",
                        "entity_type": "payload",
                        "name": "",
                        "reason": "ai_not_configured",
                        "candidates": [],
                    }
                ]
            return result

        # 空 payload：不阻塞严格模式，但必须出现在 unresolved 中；不写库、不递增版本
        if not _facts_have_payload(facts):
            result["skipped_reason"] = "empty_payload"
            result["facts_version_status"] = "not_incremented"
            result["unresolved"] = [
                {
                    "path": "<payload>",
                    "entity_type": "payload",
                    "name": "",
                    "reason": "empty",
                    "candidates": [],
                }
            ]
            return result

        # E-01/E-02：名字 → 实体 id 归一化（返回副本，不改原始 AI payload）
        normalized, resolutions = await _normalize_state_facts(
            db, project_id, facts
        )
        unresolved = [r for r in resolutions if not r.resolved]
        blocking = [r for r in unresolved if r.blocking]
        result["unresolved"] = [r.as_unresolved() for r in unresolved]
        result["blocking_unresolved"] = len(blocking)

        strict = strict_entity_check_enabled()
        if strict and blocking:
            # 严格模式：任何未解析引用都拒绝整批写入（含 participants / known_by 列表）
            result["rejected"] = {
                "ok": False,
                "checked": len(resolutions),
                "violations": [r.as_unresolved() for r in blocking],
                "reason": "unresolved_entity_references",
            }
            result["skipped_reason"] = "rejected_entity_reference"
            result["facts_version_status"] = "not_incremented"
            logger.warning(
                "trigger_state_facts: 严格模式下存在未解析引用，丢弃本章事实输出（不落库）: %s",
                result["rejected"],
            )
            return result

        if blocking and not _has_writable_item(normalized, resolutions):
            # 非严格模式且整批引用都解析不到、也没有无需引用的可写内容：
            # 只上报结构化 rejected，不写入任何业务数据，版本保持不变。
            result["rejected"] = {
                "ok": False,
                "checked": len(resolutions),
                "violations": [r.as_unresolved() for r in blocking],
                "reason": "unresolved_entity_references",
            }
            result["skipped_reason"] = "rejected_entity_reference"
            result["facts_version_status"] = "not_incremented"
            logger.warning(
                "trigger_state_facts: 非严格模式下整批引用未解析，跳过写入（不落库）: %s",
                result["rejected"],
            )
            return result

        # 阶段 E：写路径实体引用校验（归一化后 *_id 才真正可校验）
        _validation = await validate_entity_references(
            db, project_id, normalized, strict=strict
        )
        if not _validation.ok:
            result["rejected"] = _validation.as_dict()
            result["skipped_reason"] = "rejected_entity_reference"
            result["facts_version_status"] = "not_incremented"
            logger.warning(
                "trigger_state_facts: 实体引用校验失败，丢弃本章事实输出（不落库）: %s",
                _validation.as_dict(),
            )
            return result

        # Q5：版本承载行 fail-closed —— 在任何清理 / 元数据 / 事实写入之前判定，
        # 否则会出现「事实已更新、版本缓存无法同步」的半完成状态。
        outline_row = await db.get(Outline, chapter_id)
        if outline_row is None:
            result["skipped_reason"] = "missing_version_anchor"
            result["facts_version_status"] = "missing_outline"
            logger.warning(
                "trigger_state_facts: 缺少 outline 版本承载行，放弃本章事实写入（不落库）: chapter=%s",
                chapter_id,
            )
            return result
        cc_row = (
            await db.execute(
                select(ChapterContent).where(
                    ChapterContent.outline_id == chapter_id
                )
            )
        ).scalar_one_or_none()
        if cc_row is None:
            result["skipped_reason"] = "missing_version_anchor"
            result["facts_version_status"] = "missing_chapter_content"
            logger.warning(
                "trigger_state_facts: 缺少 chapter_content 版本承载行，放弃本章事实写入（不落库）: chapter=%s",
                chapter_id,
            )
            return result

        if not strict and blocking:
            # 非严格模式：只写已解析项，未解析项完整上报（不阻断其余写入）
            result["rejected"] = {
                "ok": False,
                "checked": len(resolutions),
                "violations": [r.as_unresolved() for r in blocking],
                "reason": "partial_unresolved_entity_references",
            }
            logger.warning(
                "trigger_state_facts: 非严格模式，跳过 %d 条未解析引用: %s",
                len(blocking),
                [r.as_unresolved() for r in blocking],
            )

        stats = await _write_structured_facts(
            db, project_id, chapter_id, normalized, resolutions
        )
        if sum(stats.values()) <= 0 and not _meta_has_payload(normalized):
            # 无可写内容：回滚（含写入函数内的旧行清理），不提交任何业务写入，不递增版本
            await db.rollback()
            result["written"] = {}
            result["skipped_reason"] = "nothing_writable"
            result["facts_version_status"] = "not_incremented"
            return result
        result["written"] = stats
        result["written_total"] = sum(stats.values())

        # 阶段H「依赖图增量回归」：记录本章引用的事实键（失败不阻断事实落库）
        try:
            dep_count = await record_chapter_fact_dependencies(
                db, project_id, chapter_id, normalized
            )
            result["dependency_count"] = dep_count
        except Exception as dep_exc:  # noqa: BLE001
            logger.warning(
                "trigger_state_facts: 依赖图记录失败（回退全量标记兜底）: %s",
                str(dep_exc)[:300],
            )
            result["dependency_error"] = str(dep_exc)[:300]

        if result["written_total"] <= 0 and not _meta_has_payload(normalized):
            # 契约兜底：ok=True 必须至少有一项事实或元数据写入（提交前判定）
            await db.rollback()
            result["written"] = {}
            result["written_total"] = 0
            result["skipped_reason"] = "nothing_writable"
            result["facts_version_status"] = "not_incremented"
            return result

        # 第一阶段「可追溯元数据」：事实重算成功 → 两处版本同步 +1（两个承载行已确认存在）
        outline_row.facts_version = (outline_row.facts_version or 0) + 1
        cc_row.facts_version = outline_row.facts_version
        await db.commit()
        result["facts_version_status"] = "synced"
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
        result["error"] = str(exc)[:500]
    return result
