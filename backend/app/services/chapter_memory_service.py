# 章节记忆服务（第二期「章节能被记住」基础 + 文26 P1「章节摘要和续写记忆闭环」补齐）
# 职责：
# 1. 接受正文后自动生成章节摘要（action_type=summarize 落 generation_records）；
# 2. 生成短摘要 + 文26 P1 structuredMemory 结构化记忆 JSON，
#    保存为 chapter_memory（status=draft，generation_record_id 关联生成记录）；
# 3. 项目级设置读取（auto_summarize 开关本期生效，人物/伏笔/时间线确认策略随第三期生效）。
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_memory import ChapterMemory
from app.models.project_setting import ProjectSetting
from app.services.ai_service import chat_with_ai, _chat_with_ai_retry_on_truncation, is_ai_configured
from app.services.generation_service import (
    record_generation,
    resolve_ai_provider,
    serialize_context,
)
from app.services.model_policy import TaskType

# memory_json 默认结构（文26 P1 规格 structuredMemory，与 models/chapter_memory.py 顶部约定一致）
EMPTY_MEMORY = {
    "timeLabel": "",
    "locations": [],
    "charactersPresent": [],
    "characterChanges": [],
    "relationshipChanges": [],
    "timelineEvents": [],
    "foreshadowsAdded": [],
    "foreshadowsResolved": [],
    "importantItems": [],
    "openThreads": [],
    "nextChapterRequirements": [],
}

DEFAULT_PROJECT_SETTING = {
    "auto_summarize": True,
    "auto_check": True,
    "character_confirm": "require",
    "foreshadow_confirm": "require",
    "timeline_confirm": "require",
}


async def get_or_create_project_setting(
    db: AsyncSession, project_id: uuid.UUID
) -> ProjectSetting:
    """读取项目设置；不存在时按默认值创建（幂等）。"""
    row = (
        await db.execute(
            select(ProjectSetting).where(ProjectSetting.project_id == project_id)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = ProjectSetting(
        project_id=project_id,
        auto_summarize=DEFAULT_PROJECT_SETTING["auto_summarize"],
        auto_check=DEFAULT_PROJECT_SETTING["auto_check"],
        character_confirm=DEFAULT_PROJECT_SETTING["character_confirm"],
        foreshadow_confirm=DEFAULT_PROJECT_SETTING["foreshadow_confirm"],
        timeline_confirm=DEFAULT_PROJECT_SETTING["timeline_confirm"],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


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


def build_mock_memory(chapter_title: str, content: str) -> tuple[str, dict]:
    """演示模式（未配置 AI 密钥）的合理 mock 摘要与结构化记忆。

    摘要：取正文开头约 120 字 + 模板后缀，控制在 100-250 字；
    结构化记忆：基于标题/正文给出一组可读的占位结构（字段值尽量呼应正文开头）。
    """
    clean = (content or "").replace("\n", " ").replace("\r", " ").strip()
    lead = clean[:120].strip()
    if len(lead) < 20:
        lead = (lead + "（正文内容较少，建议补充后重新生成摘要）").strip()
    summary = f"{lead}。本章围绕《{chapter_title or '本章'}》推进主线，埋设后续情节线索，并留下待回收的悬念。"
    if len(summary) > 250:
        summary = summary[:247] + "…"

    location_hint = ""
    chars_hint = ""
    if clean:
        # 从正文开头截取一小段作为地点/人物的提示性占位
        seg = clean[:60]
        location_hint = f"（正文提及场景：{seg[:24]}…）"
        chars_hint = f"（正文涉及角色：{seg[:20]}…）"

    memory = {
        "timeLabel": "接续上一章的时间推进",
        "locations": [f"（模拟）本章主要地点 {location_hint}".strip()],
        "charactersPresent": [f"（模拟）本章出场人物 {chars_hint}".strip()],
        "characterChanges": [
            {
                "character": "（模拟）核心人物",
                "changes": ["本章目标有所推进，关系出现新变化"],
            }
        ],
        "relationshipChanges": [],
        "timelineEvents": [
            {
                "timeLabel": "接续上一章",
                "location": "（模拟）本章主要地点",
                "title": "本章核心事件",
                "description": "推进主线并埋设后续情节线索",
            }
        ],
        "foreshadowsAdded": [
            {
                "content": "（模拟）本章新增伏笔",
                "plannedPayoff": "后续章节回收并影响主线",
            }
        ],
        "foreshadowsResolved": [],
        "importantItems": ["（模拟）本章出现的关键物品"],
        "openThreads": ["（模拟）本章遗留的未解决冲突"],
        "nextChapterRequirements": [
            "（模拟）下章承接：延续本章结尾的冲突继续推进"
        ],
    }
    return summary, memory


async def _save_chapter_memory(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    summary: str,
    memory: dict,
    source: str = "ai",
    status: str = "draft",
    generation_record_id: uuid.UUID | None = None,
) -> ChapterMemory:
    """按 chapter_id upsert 章节记忆并提交。"""
    row = (
        await db.execute(
            select(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ChapterMemory(
            project_id=project_id,
            chapter_id=chapter_id,
            summary=summary,
            memory_json=json.dumps(memory, ensure_ascii=False),
            source=source,
            status=status,
            generation_record_id=generation_record_id,
        )
        db.add(row)
    else:
        row.summary = summary
        row.memory_json = json.dumps(memory, ensure_ascii=False)
        row.source = source
        row.status = status
        if generation_record_id is not None:
            row.generation_record_id = generation_record_id
    await db.commit()
    await db.refresh(row)
    return row


async def summarize_chapter(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
) -> ChapterMemory | None:
    """为单章生成摘要+结构化记忆（真实模型 / 演示 mock），落 generation_records 与 chapter_memory。

    返回 None 表示真实模型调用/解析失败（已记录 failed 的 generation_record），
    调用方不应因此阻断主流程。
    """
    provider, model = resolve_ai_provider()
    real = is_ai_configured()

    system_prompt = (
        "你是小说章节记忆提取器。请阅读用户给出的章节正文，输出一段 100-250 字的"
        "短摘要，以及结构化记忆 JSON。必须只输出 JSON，不要任何多余文字。"
    )
    user_prompt = (
        "章节标题：《" + (chapter_title or "") + "》\n"
        "章节正文：\n" + (content or "") + "\n\n"
        "输出 JSON 格式（文26 structuredMemory 规范）：\n"
        '{"summary": "100-250字短摘要", "memory": {'
        '"timeLabel": "相对时间标签（如 第3天 夜）", '
        '"locations": ["本章地点"], '
        '"charactersPresent": ["出场人物"], '
        '"characterChanges": [{"character": "人物", "changes": ["变化"], "status": "当前状态"}], '
        '"relationshipChanges": [{"source": "人物A", "target": "人物B", "change": "关系变化"}], '
        '"timelineEvents": [{"timeLabel": "时间", "location": "地点", "title": "事件标题", "description": "事件描述"}], '
        '"foreshadowsAdded": [{"content": "新埋伏笔", "plannedPayoff": "计划回收方式"}], '
        '"foreshadowsResolved": ["本章回收的伏笔"], '
        '"importantItems": ["关键物品"], '
        '"openThreads": ["未解决冲突"], '
        '"nextChapterRequirements": ["下章必须承接的要点"]}}'
    )

    summary = ""
    memory: dict = {}
    status = "succeeded"
    error_message: str | None = None

    if real:
        try:
            raw = await _chat_with_ai_retry_on_truncation(
                    system_prompt,
                    user_prompt,
                    task_type=TaskType.CHAPTER_MEMORY.value,
                )
            raw = _strip_code_fence(raw)
            parsed = json.loads(raw)
            summary = str(parsed.get("summary") or "").strip()
            memory = parsed.get("memory") or {}
            if not isinstance(memory, dict):
                memory = {}
        except Exception as exc:  # noqa: BLE001 - 摘要失败不应阻断保存正文
            status = "failed"
            error_message = str(exc)[:500]
    else:
        summary, memory = build_mock_memory(chapter_title, content)

    # 补齐 memory 缺省字段，保证结构稳定
    full_memory = {**EMPTY_MEMORY, **memory}
    for key in EMPTY_MEMORY:
        if key not in full_memory:
            full_memory[key] = EMPTY_MEMORY[key]

    record = await record_generation(
        db,
        attribution_status="project_bound",
        project_id=project_id,
        chapter_id=chapter_id,
        action_type="summarize",
        model_provider=(provider or "mock") if real else "mock",
        model_name=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context_snapshot=serialize_context(
            {
                "chapter_title": chapter_title,
                "content_length": len(content or ""),
                "trigger": "auto_summarize" if status == "succeeded" else "manual",
            }
        ),
        response_content=json.dumps(
            {"summary": summary, "memory": full_memory}, ensure_ascii=False
        ),
        status=status,
        error_message=error_message,
    )

    if status == "failed":
        return None

    return await _save_chapter_memory(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        summary=summary,
        memory=full_memory,
        source="ai",
        status="draft",
        generation_record_id=record.id if record else None,
    )


async def trigger_chapter_summary(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    chapter_title: str,
    content: str,
    force: bool = False,
) -> ChapterMemory | None:
    """接受正文后自动摘要入口。

    - force=True（手动重新生成）：忽略 auto_summarize 开关；
    - force=False：读取项目设置，auto_summarize=False 时跳过。
    任何异常都不向上抛出，保证正文保存/恢复流程不受影响。
    """
    try:
        if not force:
            setting = await get_or_create_project_setting(db, project_id)
            if not setting.auto_summarize:
                return None
        return await summarize_chapter(
            db, project_id, chapter_id, chapter_title, content
        )
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
        return None
