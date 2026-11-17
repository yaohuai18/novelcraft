# 发布与阅读模式相关 Pydantic 请求/响应模型（第十三批规格实现）
import json
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------- 发布记录 ----------

class PublicationCreate(BaseModel):
    """章节发布请求体：可覆盖公开标题/简介/封面文字/可见性。"""

    title: str | None = Field(default=None, max_length=255, description="面向读者的章节标题，缺省取章节标题")
    description: str | None = Field(default=None, description="面向读者的章节简介")
    cover_text: str | None = Field(default=None, max_length=255, description="封面文字")
    visibility: str | None = Field(
        default=None,
        description="可见性：private / project_member / link_only / public，缺省取章节当前 visibility"
    )
    force: bool = Field(default=False, description="是否忽略低等级问题强制发布（高等级问题仍禁止）")


class PublicationUpdate(BaseModel):
    """作品发布信息更新（PUT /api/projects/{project_id}/publication）。"""

    title: str | None = Field(default=None, min_length=1, max_length=255, description="作品名称")
    subtitle: str | None = Field(default=None, max_length=255, description="副标题")
    description: str | None = Field(default=None, description="作品简介")
    cover_text: str | None = Field(default=None, max_length=255, description="封面文字")
    status: str | None = Field(default=None, description="发布状态：draft/review/published/unlisted/archived")
    project_slug: str | None = Field(default=None, max_length=255, description="公开链接 slug（仅小写字母/数字/中划线）")


class PublicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    description: str | None
    cover_text: str | None
    content: str
    content_text: str | None
    word_count: int
    status: str
    visibility: str
    baseline_meta: dict | None = Field(
        default=None,
        description="发布基线元数据（阶段C）：facts_version/setting_version/rules_version/prompt_version/model_name/validation_report_id",
    )
    baseline_snapshot: dict | None = Field(
        default=None,
        description="发布基线快照（阶段C）：事实库+设定引用式不可变快照（引用而非复制正文）",
    )
    published_at: datetime | None
    updated_at: datetime

    @field_validator("baseline_meta", "baseline_snapshot", mode="before")
    @classmethod
    def _parse_baseline_json(cls, value):
        """将数据库中的基线 JSON 字符串解析为 dict（NULL/空串 → None）。"""
        if value is None or value == "":
            return None
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None


# ---------- 发布前检查 ----------

class PublishCheckIssue(BaseModel):
    severity: str = Field(description="问题等级：high / medium / low")
    message: str = Field(description="问题描述")


class PublishCheckResponse(BaseModel):
    can_publish: bool = Field(description="是否可发布（存在高等级问题时为 False）")
    issues: list[PublishCheckIssue] = Field(default_factory=list)


# ---------- 作品发布 ----------

class ProjectPublishRequest(BaseModel):
    force: bool = Field(default=False, description="是否忽略低等级问题强制发布（高等级问题仍禁止）")


class ProjectPublishResponse(BaseModel):
    project_id: uuid.UUID
    status: str
    project_slug: str | None
    published_at: datetime | None
    message: str = ""


# ---------- 公开阅读 ----------

class PublicTagItem(BaseModel):
    name: str


class PublicVolumeItem(BaseModel):
    volume_id: uuid.UUID | None
    title: str | None
    chapters: list["PublicChapterSummary"] = Field(default_factory=list)


class PublicChapterSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chapter_id: uuid.UUID
    title: str
    chapter_number: int = Field(description="全书可见章节序号（1 起）")
    word_count: int
    published_at: datetime | None
    slug: str | None
    visibility: str


class PublicProjectResponse(BaseModel):
    project_id: uuid.UUID
    title: str
    subtitle: str | None
    description: str | None
    cover_text: str | None
    author_name: str
    genre: str | None
    tags: list[str] = Field(default_factory=list)
    status: str
    updated_at: datetime | None
    slug: str | None
    volumes: list[PublicVolumeItem] = Field(default_factory=list)
    chapter_count: int = 0
    word_count_total: int = 0


class PublicChapterResponse(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    chapter_number: int
    content: str = Field(description="转换后的阅读 HTML（已清理内部标记并转义危险 HTML）")
    word_count: int
    published_at: datetime | None
    updated_at: datetime | None
    slug: str | None
    visibility: str
    prev_chapter_id: uuid.UUID | None
    prev_chapter_slug: str | None
    prev_chapter_title: str | None
    next_chapter_id: uuid.UUID | None
    next_chapter_slug: str | None
    next_chapter_title: str | None


class NavigationItem(BaseModel):
    chapter_id: uuid.UUID | None
    slug: str | None
    title: str | None


class NavigationResponse(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    prev: NavigationItem
    next: NavigationItem


class PublicChapterNavigationResponse(BaseModel):
    """公开章节导航：当前章 + 上一章/下一章（用于翻页）。"""

    chapter_id: uuid.UUID
    title: str
    chapter_number: int
    slug: str | None
    prev_chapter_id: uuid.UUID | None
    prev_chapter_slug: str | None
    prev_chapter_title: str | None
    next_chapter_id: uuid.UUID | None
    next_chapter_slug: str | None
    next_chapter_title: str | None


# ---------- 作者预览 ----------

class PreviewProjectResponse(BaseModel):
    project_id: uuid.UUID
    title: str
    subtitle: str | None
    description: str | None
    cover_text: str | None
    genre: str | None
    publication_status: str
    slug: str | None
    volumes: list[PublicVolumeItem] = Field(default_factory=list)
    preview: bool = True


class PreviewChapterResponse(BaseModel):
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    content: str = Field(description="基于当前编辑稿转换的阅读 HTML（未改变发布状态）")
    content_text: str | None
    word_count: int
    status: str
    visibility: str
    preview: bool = True


# ---------- 阅读设置 ----------

class ReaderPreferenceUpdate(BaseModel):
    font_size: int | None = Field(default=None, ge=8, le=48, description="字号（px）")
    line_height: float | None = Field(default=None, ge=0.8, le=3.0, description="行距")
    content_width: int | None = Field(default=None, ge=320, le=1200, description="正文宽度（px）")
    theme: str | None = Field(default=None, description="主题：light / dark / sepia")
    font_family: str | None = Field(default=None, max_length=50, description="字体类型")


class ReaderPreferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    font_size: int
    line_height: float
    content_width: int
    theme: str
    font_family: str
    updated_at: datetime


# ---------- 阅读进度 ----------

class ReadingProgressUpdate(BaseModel):
    position: int | None = Field(default=None, ge=0, description="章节内滚动位置")
    completed: bool | None = Field(default=None, description="标记章节已读")


class ReadingProgressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID
    position: int
    last_read_at: datetime
    completed: bool


class ReadingProgressListResponse(BaseModel):
    project_id: uuid.UUID
    items: list[ReadingProgressResponse] = Field(default_factory=list)
    latest: ReadingProgressResponse | None = Field(default=None, description="继续阅读：最近一次阅读的章节进度")


PublicVolumeItem.model_rebuild()
