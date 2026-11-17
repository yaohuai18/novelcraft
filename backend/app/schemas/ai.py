# AI 生成相关 Pydantic 请求/响应模型（第六批规格实现）
from pydantic import BaseModel, Field


class GenerateSummaryRequest(BaseModel):
    """故事总纲生成请求体：附加指令可空，最长 3000 字。"""

    additional_instruction: str | None = Field(
        default=None,
        max_length=3000
    )


class GenerateTextResponse(BaseModel):
    """AI 文本生成响应：预览内容（不落库）。"""

    content: str


class VolumeDraft(BaseModel):
    """AI 返回的单卷草稿：字段必填，服务端 Pydantic 校验。"""

    title: str
    summary: str
    main_conflict: str
    turning_point: str
    ending_hook: str


class VolumeGenerateRequest(BaseModel):
    """分卷生成请求体：传入已确认的故事总纲文本与分卷数量。"""

    summary: str = Field(min_length=1, max_length=30000)
    volume_count: int = Field(default=5, ge=1, le=20)
    additional_instruction: str | None = Field(
        default=None,
        max_length=3000
    )


class VolumeGenerateResponse(BaseModel):
    """分卷生成响应：AI 结构化预览（不落库）。"""

    volumes: list[VolumeDraft]


class ChapterDraft(BaseModel):
    """AI 返回的单章草稿：字段必填，服务端 Pydantic 校验。"""

    title: str
    summary: str
    purpose: str
    conflict: str
    ending_hook: str


class ChapterGenerateRequest(BaseModel):
    """章节生成请求体：传入分卷标题与概要、章节数量。"""

    volume_title: str
    volume_summary: str
    chapter_count: int = Field(default=10, ge=1, le=100)
    additional_instruction: str | None = Field(
        default=None,
        max_length=3000
    )


class ChapterGenerateResponse(BaseModel):
    """章节生成响应：AI 结构化预览（不落库）。"""

    chapters: list[ChapterDraft]
