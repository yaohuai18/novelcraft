# AI 生成路由（第六批规格实现）
# 1) POST /api/projects/{project_id}/ai/generate-summary
#    故事总纲生成：返回预览内容，不写入数据库（生成预览—用户确认—保存流程）。
# 2) POST /api/projects/{project_id}/ai/generate-volumes
#    分卷生成：返回 VolumeGenerateResponse 预览，不落库；确认后经 outlines/batch 保存。
# 3) POST /api/projects/{project_id}/ai/generate-chapters
#    章节生成：返回 ChapterGenerateResponse 预览，不落库；确认后经 outlines/batch 保存。
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.models.world_setting import WorldSetting
from app.schemas.ai import (
    ChapterGenerateRequest,
    ChapterGenerateResponse,
    GenerateSummaryRequest,
    GenerateTextResponse,
    VolumeGenerateRequest,
    VolumeGenerateResponse,
)
from app.services.ai_service import (
    chat_with_ai,
    chat_with_ai_json,
)
from app.services.model_policy import TaskType


router = APIRouter(
    prefix="/api/projects/{project_id}/ai",
    tags=["AI 生成"]
)


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User,
    db: AsyncSession
) -> Project:
    """校验作品存在且归属当前用户，非本人作品统一返回 404。"""
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == current_user.id
        )
    )

    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(
            status_code=404,
            detail="作品不存在或无权访问"
        )

    return project


async def call_json_with_retry(
    system_prompt: str,
    user_prompt: str,
    response_model: type,
    task_type: str | None = TaskType.OUTLINE_PLAN.value,
):
    """调用 chat_with_ai_json；服务端校验失败自动重试一次，仍失败抛 502。

    阶段 1：默认按「规划类」任务分流（thinking=high 的 deepseek-v4-pro）。
    """
    try:
        return await chat_with_ai_json(
            system_prompt,
            user_prompt,
            response_model,
            task_type=task_type,
        )
    except RuntimeError:
        try:
            return await chat_with_ai_json(
                system_prompt,
                user_prompt,
                response_model,
                task_type=task_type,
            )
        except RuntimeError as error:
            raise HTTPException(
                status_code=502,
                detail=str(error),
            )


@router.post(
    "/generate-summary",
    response_model=GenerateTextResponse
)
async def generate_summary(
    project_id: uuid.UUID,
    data: GenerateSummaryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """生成故事总纲预览；AI 内容只返回不落库，由前端展示后走保存流程。"""
    project = await get_owned_project(
        project_id,
        current_user,
        db
    )

    character_result = await db.execute(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at.asc())
    )

    characters = list(character_result.scalars().all())

    world_result = await db.execute(
        select(WorldSetting)
        .where(WorldSetting.project_id == project_id)
        .order_by(WorldSetting.created_at.asc())
    )

    world_settings = list(world_result.scalars().all())

    character_text = "\n".join(
        (
            f"- {item.name}\n"
            f"  身份：{item.identity or '未设定'}\n"
            f"  性格：{item.personality or '未设定'}\n"
            f"  目标：{item.goal or '未设定'}\n"
            f"  冲突：{item.conflict or '未设定'}"
        )
        for item in characters
    )

    world_text = "\n".join(
        (
            f"- [{item.category}] {item.name}："
            f"{item.content}"
        )
        for item in world_settings
    )

    system_prompt = """
你是一名专业的长篇小说策划编辑。

请根据用户提供的作品设定，生成可执行的故事总纲。
要求：
1. 保持人物身份、目标和冲突的一致性。
2. 不要擅自改变用户已经确定的世界规则。
3. 总纲应包含起因、发展、转折、高潮和结局方向。
4. 重点说明主线冲突和人物成长。
5. 输出中文，使用清晰的小标题。
6. 不要输出与故事无关的解释。
"""

    user_prompt = f"""
作品名称：{project.title}
题材：{project.genre or '未设定'}
背景：{project.background or '未设定'}
世界设定：{project.world_setting or '未设定'}
主要冲突：{project.main_conflict or '未设定'}
结局方向：{project.ending_direction or '未设定'}
叙事视角：{project.narrative_perspective or '未设定'}
写作风格：{project.writing_style or '未设定'}
禁止设定：{project.forbidden_settings or '无'}

人物资料：
{character_text or '暂无人物资料'}

世界观条目：
{world_text or '暂无世界观条目'}

额外要求：
{data.additional_instruction or '无'}

请生成一份故事总纲。
"""

    try:
        # 阶段 1 最小补正：本接口输出面向用户的自然语言摘要（GenerateTextResponse，
        # 无 JSON 解析 / schema 校验重试），归入独立 summary 任务类型（flash 非思考）；
        # 真正的 outline_plan（结构化大纲 / 分卷 / 分章）调用点不受影响。
        content = await chat_with_ai(
            system_prompt,
            user_prompt,
            task_type=TaskType.SUMMARY.value
        )
    except RuntimeError as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        )

    return GenerateTextResponse(
        content=content
    )


@router.post(
    "/generate-volumes",
    response_model=VolumeGenerateResponse
)
async def generate_volumes(
    project_id: uuid.UUID,
    data: VolumeGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """基于已确认的故事总纲生成分卷预览；不落库，确认后走 outlines/batch 保存。"""
    project = await get_owned_project(
        project_id,
        current_user,
        db
    )

    # 定位故事总纲：取该项目最近创建的一条 summary 类型大纲作为父级
    summary_result = await db.execute(
        select(Outline)
        .where(
            Outline.project_id == project_id,
            Outline.outline_type == "summary"
        )
        .order_by(Outline.created_at.desc())
        .limit(1)
    )

    summary_outline = summary_result.scalar_one_or_none()

    if not summary_outline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先保存故事总纲后再生成分卷"
        )

    system_prompt = (
        "你是一名专业的长篇小说策划编辑。\n"
        "请根据用户提供的故事总纲，拆分生成指定数量的分卷。\n"
        "要求：\n"
        "1. 每卷都要有独立目标和阶段性高潮。\n"
        "2. 分卷之间逻辑连贯，承接总纲的主线冲突。\n"
        "3. 只返回 JSON，不要使用 Markdown 代码块，不要附加任何解释。\n"
        '4. JSON 必须符合以下结构：\n'
        '{"volumes": [{"title": "第一卷：xxx", '
        '"summary": "xxx", "main_conflict": "xxx", '
        '"turning_point": "xxx", "ending_hook": "xxx"}]}'
    )

    user_prompt = (
        f"作品名称：{project.title}\n"
        f"题材：{project.genre or '未设定'}\n"
        f"故事总纲：\n{data.summary}\n\n"
        f"需要拆分卷数：{data.volume_count}\n\n"
        f"额外要求：{data.additional_instruction or '无'}\n\n"
        "请按上述 JSON 结构输出。"
    )

    result = await call_json_with_retry(
        system_prompt,
        user_prompt,
        VolumeGenerateResponse,
    )

    # 若 AI 返回超过请求数量则截断，避免多写
    if len(result.volumes) > data.volume_count:
        result.volumes = result.volumes[: data.volume_count]

    return result


@router.post(
    "/generate-chapters",
    response_model=ChapterGenerateResponse
)
async def generate_chapters(
    project_id: uuid.UUID,
    data: ChapterGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """基于分卷标题与概要生成章节预览；不落库，确认后走 outlines/batch 保存。"""
    project = await get_owned_project(
        project_id,
        current_user,
        db
    )

    system_prompt = (
        "你是一名专业的长篇小说章节策划编辑。\n"
        "请根据用户提供的分卷信息，拆分生成指定数量的章节。\n"
        "要求：\n"
        "1. 每章都要有明确的情节推进目标。\n"
        "2. 章节之间逻辑连贯，承接分卷主线冲突。\n"
        "3. 只返回 JSON，不要使用 Markdown 代码块，不要附加任何解释。\n"
        '4. JSON 必须符合以下结构：\n'
        '{"chapters": [{"title": "第一章：xxx", '
        '"summary": "xxx", "purpose": "xxx", '
        '"conflict": "xxx", "ending_hook": "xxx"}]}'
    )

    user_prompt = (
        f"作品名称：{project.title}\n"
        f"分卷标题：{data.volume_title}\n"
        f"分卷概要：\n{data.volume_summary}\n\n"
        f"需要生成章节数：{data.chapter_count}\n\n"
        f"额外要求：{data.additional_instruction or '无'}\n\n"
        "请按上述 JSON 结构输出。"
    )

    result = await call_json_with_retry(
        system_prompt,
        user_prompt,
        ChapterGenerateResponse,
    )

    if len(result.chapters) > data.chapter_count:
        result.chapters = result.chapters[: data.chapter_count]

    return result
