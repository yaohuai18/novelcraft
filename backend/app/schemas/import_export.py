# 导入、导出、备份恢复相关 Pydantic 请求/响应模型（第十六批「数据导入、导出与备份恢复」规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ============ 导入 ============

class ImportCreate(BaseModel):
    """上传导入内容（文本接口：直接提交文件内容与类型）。"""

    file_name: str = Field(min_length=1, max_length=255, description="文件名")
    file_type: str = Field(description="markdown / text / html / json / csv")
    content: str = Field(min_length=1, description="导入内容文本")


class ImportPreviewRequest(BaseModel):
    """导入预览请求（规格五：正式导入前先预览；预览阶段可调整作品名/拆分方式等）。"""

    file_name: str = Field(min_length=1, max_length=255, description="文件名")
    file_type: str = Field(description="markdown / text / html / json / csv")
    content: str = Field(min_length=1, description="导入内容文本")
    split_mode: str = Field(
        default="chapter",
        description="拆分方式：file=按文件拆分章节 / volume=按一级标题 / chapter=按二级标题 / single=全部导入为单章",
    )


class ImportPreviewResponse(BaseModel):
    """导入预览（规格五：识别作品名/分卷数/章节数/预计字数/无法识别内容/重复内容/格式警告）。"""

    title: str
    file_type: str
    root_type: str
    split_mode: str = "chapter"
    volume_count: int = 0
    chapter_count: int = 0
    estimated_word_count: int = 0
    total_items: int = 0
    characters_count: int = 0
    locations_count: int = 0
    factions_count: int = 0
    world_rules_count: int = 0
    timeline_events_count: int = 0
    relationships_count: int = 0
    tags_count: int = 0
    comments_count: int = 0
    versions_count: int = 0
    unrecognized: list[dict] = Field(default_factory=list)
    duplicates: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    schema_version: str | None = None


class ImportExecuteRequest(BaseModel):
    """正式导入请求（规格四/五/六：预览确认后调整选项并执行）。"""

    project_id: uuid.UUID | None = Field(
        default=None,
        description="导入目标项目；为空时创建新项目（完整项目 JSON/CSV 表格导入）",
    )
    root_type: str = Field(
        default="outline",
        description="outline=导入到当前作品大纲；project=完整项目导入（创建新作品）",
    )
    title: str | None = Field(default=None, description="作品名称（覆盖预览识别结果）")
    split_mode: str = Field(default="chapter", description="章节拆分方式：file / volume / chapter / single")
    import_titles: bool = Field(
        default=True,
        description="是否导入标题（False 时章节命名为 第N章 等序号标题）",
    )
    import_tags: bool = Field(
        default=True,
        description="是否导入标签",
    )
    import_characters: bool = Field(default=True, description="是否导入人物")
    import_locations: bool = Field(default=True, description="是否导入地点")
    import_factions: bool = Field(default=True, description="是否导入势力")
    import_world_rules: bool = Field(default=True, description="是否导入世界观规则")
    import_timeline_events: bool = Field(default=True, description="是否导入时间线事件")
    import_relationships: bool = Field(default=True, description="是否导入人物关系")
    import_comments: bool = Field(
        default=True,
        description="是否导入评论（仅公开评论；私人批注不导入）",
    )
    import_versions: bool = Field(
        default=True,
        description="是否导入版本历史",
    )
    preserve_format: bool = Field(
        default=True,
        description="是否保留原始格式（引用/列表标记等）",
    )
    duplicate_strategy: str = Field(
        default="create_new",
        description="重复处理策略：skip / create_new / overwrite / merge / mark_duplicate（规格六）",
    )
    pause_on_duplicate: bool = Field(
        default=True,
        description="发现重复时暂停并返回确认要求（规格六默认建议）",
    )
    overwrite: bool = Field(
        default=False,
        description="是否覆盖已有章节（等价 duplicate_strategy=overwrite 的快捷开关）",
    )
    import_empty_chapters: bool = True


class ImportDuplicateConfirm(BaseModel):
    """重复内容暂停后的确认请求。"""

    strategy: str = Field(description="skip / create_new / overwrite / merge / mark_duplicate")


class ImportTaskCreateRequest(ImportExecuteRequest):
    """创建导入任务请求（规格二十一：POST /api/projects/import）。"""

    file_name: str = Field(min_length=1, max_length=255, description="文件名")
    file_type: str = Field(description="markdown / text / html / json / csv")
    content: str = Field(min_length=1, description="导入内容文本")


class ImportTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    file_name: str
    file_type: str
    source_type: str | None
    status: str
    progress: int
    total_items: int
    processed_items: int
    options: str | None
    error_message: str | None
    preview_data: str | None
    report_data: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ImportReportResponse(BaseModel):
    """导入完成报告（规格七：成功/警告/错误计数与错误列表；单条失败不中断）。"""

    status: str
    success_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    errors: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_outlines: int = 0
    created_chapters: int = 0
    created_knowledge_items: int = 0
    created_characters: int = 0
    created_locations: int = 0
    created_factions: int = 0
    created_world_rules: int = 0
    created_timeline_events: int = 0
    created_relationships: int = 0
    created_tags: int = 0
    created_comments: int = 0
    created_versions: int = 0
    skipped_items: int = 0
    overwritten_items: int = 0
    merged_items: int = 0
    marked_duplicates: int = 0
    created_project_id: str | None = None


# ============ 导出 ============

class ExportOptions(BaseModel):
    """导出选项（规格九）。"""

    include_drafts: bool = False
    include_private_notes: bool = False
    include_comments: bool = False
    include_archived: bool = False
    include_ai_records: bool = False
    include_version_history: bool = False
    # 旧兼容字段
    include_chapter_numbers: bool = False
    include_outline_summaries: bool = False
    include_tags: bool = False
    include_characters: bool = False
    include_toc: bool = False
    preserve_markdown: bool = True


class ExportCreate(BaseModel):
    """创建导出任务（规格八/九/十）。"""

    export_format: str = Field(default="markdown", description="text / markdown / html / json / csv")
    scope: str = Field(
        default="whole_project",
        description="whole_project / volume / chapter / characters / locations / world / timeline / tags / comments / version_history",
    )
    options: ExportOptions = Field(default_factory=ExportOptions)
    # 旧兼容字段（第十批）
    export_type: str | None = Field(default=None, description="旧字段：whole_project / outline / content / knowledge / timeline / relationship")
    outline_id: uuid.UUID | None = None
    chapter_id: uuid.UUID | None = None


class ExportTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    export_type: str | None
    scope: str
    export_format: str
    options: str | None
    status: str
    progress: int
    content: str | None
    content_size: int | None = None
    result_token: str | None
    expires_at: datetime | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class ExportResultResponse(BaseModel):
    """导出结果（规格十：通过 result_token 在有效期内获取；一次性取用）。"""

    task_id: uuid.UUID
    project_id: uuid.UUID
    format: str
    scope: str
    content: str
    content_size: int
    expires_at: datetime | None


# ============ 备份 / 恢复 ============

class BackupCreate(BaseModel):
    """创建备份（规格十三：manual / automatic / pre_publish / release_snapshot）。"""

    backup_type: str = Field(
        default="manual",
        description="manual=手动 / automatic=自动 / pre_publish=发布前 / release_snapshot=发布快照",
    )
    note: str | None = Field(default=None, max_length=500, description="备份备注")


class BackupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    backup_type: str
    schema_version: str
    status: str
    checksum: str
    size: int
    storage_key: str | None
    created_at: datetime
    expires_at: datetime | None


class BackupDetailResponse(BackupResponse):
    snapshot: str


class BackupValidateResponse(BaseModel):
    """备份校验结果（规格十八）。"""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    schema_version: str | None = None
    checksum_ok: bool = False


class BackupRestorePreview(BaseModel):
    """恢复前预览（规格十五）。"""

    backup_id: uuid.UUID
    created_at: datetime
    project_title: str
    chapter_count: int = 0
    knowledge_count: int = 0
    version: str | None = None
    backup_size: int = 0
    backup_type: str
    validation: BackupValidateResponse


class RestoreCreate(BaseModel):
    """恢复请求（规格十五/十六）。"""

    restore_mode: str = Field(
        default="new_project",
        description="new_project=恢复为新项目（默认）/ overwrite_current=覆盖当前项目 / missing_only=仅恢复缺失 / new_version=恢复到新版本",
    )
    confirm_title: str | None = Field(
        default=None,
        description="覆盖当前项目时需输入当前作品名称二次确认",
    )
    scope: str = Field(
        default="whole",
        description="whole / outline / content / knowledge（missing_only 与 overwrite_current 生效）",
    )


class RestoreResponse(BaseModel):
    """恢复报告。"""

    status: str
    restore_mode: str
    restored_project_id: uuid.UUID | None = None
    created_backup_id: uuid.UUID | None = None
    restored_outlines: int = 0
    restored_chapters: int = 0
    restored_knowledge_items: int = 0
    restored_locations: int = 0
    restored_factions: int = 0
    restored_world_rules: int = 0
    restored_timeline_events: int = 0
    restored_relationships: int = 0
    restored_tags: int = 0
    warnings: list[str] = Field(default_factory=list)
