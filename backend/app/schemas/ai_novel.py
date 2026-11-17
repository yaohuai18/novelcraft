# AI 小说生成（端到端自动创作）请求/响应模型
import uuid

from pydantic import BaseModel, ConfigDict, Field


class NovelGenerateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """AI 小说生成请求体：完整创作设定，按章节生成。"""

    novel_type: str = Field(default="", max_length=100)          # 小说类型（如 长篇/短篇/网文）
    genre: str = Field(default="", max_length=100)              # 题材（如 科幻/古风悬疑/奇幻日常）
    title: str = Field(min_length=1, max_length=200)  # 标题（必填）
    world_view: str = Field(default="", max_length=5000)        # 世界观
    era: str = Field(default="", max_length=1000)               # 时代背景
    characters: str = Field(default="", max_length=10000)       # 主要人物
    relationships: str = Field(default="", max_length=10000)    # 人物关系
    main_plot: str = Field(default="", max_length=20000)        # 故事主线
    chapter_count: int = Field(default=5, ge=1, le=1000)          # 章节数量（2026-11-16 上限 60→1000，支持长篇连载）
    words_per_chapter: int = Field(default=800, ge=100, le=8000)  # 每章字数
    perspective: str = Field(default="", max_length=200)        # 叙事视角
    writing_style: str = Field(default="", max_length=500)      # 文风
    requirements: str = Field(default="", max_length=5000)      # 其他创作要求
    outline: str = Field(default="", max_length=50000)          # 用户已提供的大纲（生成规划/章节时必须强制遵守）
    locked: list[str] = Field(default_factory=list, max_length=200)  # 已锁定设定（规划确认阶段锁定项），生成时必须遵守
    continue_from: str | None = Field(default=None, max_length=100000)  # 续写起点（已有文本），用于继续写作
    requested_mode: str = Field(default="auto", max_length=10)  # auto / mock / real：显式指定生成模式
    # 第六批遗留：请求级模型覆盖（前端模型配置区随请求下发；留空则用服务端默认配置）
    model_name: str = Field(default="", max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)


class NovelChapter(BaseModel):
    """AI 生成的一章小说：序号、标题、正文与字数统计（去空白后字符数）。"""

    index: int
    title: str
    content: str
    word_count: int = 0


class NovelGenerateResponse(BaseModel):
    """AI 小说生成响应：mode 标记 mock（动态模拟）/ real（真实模型）。"""

    mode: str
    chapters: list[NovelChapter]
    message: str = ""


class AiStatusResponse(BaseModel):
    """AI 服务状态：前端用于展示当前模式与配置说明。"""

    configured: bool
    mode: str  # real / mock
    base_url: str = ""
    model: str = ""


class AiConfigProvider(BaseModel):
    """模型提供方可选项：只含可展示信息，绝不含任何 API Key。"""

    provider: str  # deepseek / openai / custom
    label: str = ""
    base_url: str = ""
    models: list[str] = []
    support_reasoning: bool = False  # 是否提供思考类模型（实测模型与历史推理模型名）


class AiConfigResponse(BaseModel):
    """GET /api/ai/config 响应：当前生效配置 + 可选提供方/模型（密钥仅服务端环境变量，永不返回）。"""

    configured: bool
    mode: str  # real / mock
    provider: str = ""       # 当前生效提供方（deepseek / openai / custom / mock）
    model: str = ""          # 当前生效模型
    temperature: float = 0.7  # 当前默认温度
    max_tokens: int = 4000   # 当前默认最大输出长度
    base_url: str = ""       # 当前生效 base_url（不含密钥，可安全展示）
    providers: list[AiConfigProvider] = []  # 可选提供方与模型列表
    reasoning_models: list[str] = []        # 支持思考模式的模型名集合
    # ---- 阶段 1：模型策略可读诊断字段（只读展示；前端可不消费，不涉及 UI 重构） ----
    configured_model: str = ""              # 环境变量中配置的原始模型名（可能是旧别名）
    model_source: str = ""                  # 兜底模型来源：task_policy / config_fallback
    thinking: str = ""                      # 兜底策略思考开关：enabled / disabled
    default_task_model: str = ""            # 未声明任务类型时的兜底模型
    temperature_note: str = ""              # 温度由任务策略集中决定的说明
    task_policies: list[dict] = []          # 任务类型 → 模型 / thinking / effort / 温度
    legacy_model_aliases: dict[str, dict] = {}  # 旧模型名 → 新模型兼容映射


class OutlineFirstResponse(BaseModel):
    """「全书大纲 + 第一章」响应。"""

    mode: str
    outline: str
    chapter: NovelChapter
    message: str = ""


class NextChapterRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """生成下一章请求：携带全书大纲、前文摘要、人物状态与上一章结尾。

    第二期「章节能被记住」：新增 project_id / chapter_id 后可携带，后端将用
    Context Builder 按优先级组装续写上下文（优先引用已确认章节记忆），否则回退旧字段拼接。
    """

    setting: NovelGenerateRequest
    outline: str = Field(default="", max_length=50000)
    previous_summary: str = Field(default="", max_length=30000)
    character_state: str = Field(default="", max_length=30000)
    chapter_index: int = Field(default=1, ge=1, le=1000)
    previous_chapter: str = Field(default="", max_length=100000)
    project_id: uuid.UUID | None = None   # 第二期：作品 ID（携带时走 Context Builder）
    chapter_id: uuid.UUID | None = None   # 第二期：当前章节 ID（携带时走 Context Builder）
    # 第六批遗留：请求级模型覆盖
    model_name: str = Field(default="", max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)


class ChapterEditRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """重新生成 / 扩写 / 润色本章请求。"""

    setting: NovelGenerateRequest
    chapter_title: str = Field(default="", max_length=500)
    chapter_content: str = Field(default="", max_length=100000)
    instruction: str = Field(default="", max_length=5000)  # 扩写/重生成额外要求
    style: str = Field(default="", max_length=1000)  # 润色目标风格
    # 2026-11-17：默认改为 None——客户端未传时后端从 chapter_title「第X章」提取，
    # 提不到才回退 1。旧实现 default=1 会把「第2章：传检签」重写为「第1章：传检签」。
    chapter_index: int | None = Field(default=None, ge=1, le=1000)  # 当前章节序号（标题编号修复：不再硬编码第 1 章）
    # 2026-11-17 前后章衔接修复：重生成时携带上下文，避免与前后章脱节。
    outline: str = Field(default="", max_length=30000)  # 本章大纲/章节计划
    previous_chapter_tail: str = Field(default="", max_length=8000)  # 上一章结尾原文
    next_chapter_head: str = Field(default="", max_length=8000)  # 下一章开头原文
    project_id: uuid.UUID | None = None   # 第二期：作品 ID（携带时走 Context Builder）
    chapter_id: uuid.UUID | None = None   # 第二期：当前章节 ID（携带时走 Context Builder）
    # 第六批遗留：请求级模型覆盖
    model_name: str = Field(default="", max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)


class ChapterEditResponse(BaseModel):
    """重新生成 / 扩写 / 润色本章响应。"""

    mode: str
    chapter: NovelChapter
    message: str = ""


# ---------------------------------------------------------------------------
# 灵感 → AI 规划 → 确认锁定 → 逐章生成正文（规划阶段）
# ---------------------------------------------------------------------------


class NovelChapterPlan(BaseModel):
    """章节大纲：每章目标 / 冲突 / 关键事件 / 结尾悬念。"""

    index: int
    title: str = ""
    goal: str = ""          # 本章目标
    conflict: str = ""      # 冲突
    key_events: str = ""    # 关键事件
    cliffhanger: str = ""   # 结尾悬念


class NovelVolumePlan(BaseModel):
    """分卷大纲：卷名与卷内章节大纲。"""

    volume_index: int
    title: str = ""
    summary: str = ""       # 本卷核心
    chapters: list[NovelChapterPlan] = []


class CharacterProfile(BaseModel):
    """人物小传：姓名 / 身份定位 / 性格与经历描述。"""

    name: str = ""
    role: str = ""          # 主角 / 配角
    description: str = ""


class ForeshadowPlan(BaseModel):
    """伏笔与回收计划：埋设章节 / 回收章节 / 说明。"""

    item: str = ""
    planted_chapter: int = 0
    payoff_chapter: int = 0
    note: str = ""


class NovelPlan(BaseModel):
    """完整创作规划：书名候选 / 卖点 / 简介 / 世界观 / 人物 / 关系 / 主线 / 分卷 / 章节大纲 / 伏笔回收。"""

    title_candidates: list[str] = []
    selling_point: str = ""                 # 一句话卖点
    synopsis: str = ""                      # 故事简介
    world_view: str = ""                    # 世界观
    era: str = ""                           # 时代背景
    core_conflict: str = ""                 # 核心冲突
    protagonist: str = ""                   # 主角小传
    supporting_characters: list[CharacterProfile] = []   # 配角小传
    relationships: str = ""                 # 人物关系
    main_plot: str = ""                     # 故事主线
    volumes: list[NovelVolumePlan] = []     # 分卷大纲（内含章节大纲）
    foreshadowing: list[ForeshadowPlan] = []  # 伏笔与回收计划
    ending_type: str = ""                   # 结局类型


class PlanNovelRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """AI 规划请求：灵感（必填）+ 可选设置 + 已填详细设定（只补空白不擅改）。"""

    idea: str = Field(min_length=1, max_length=20000)   # 灵感（自然语言，必填）
    genre: str = Field(default="", max_length=100)      # 题材
    writing_style: str = Field(default="", max_length=500)  # 文风
    perspective: str = Field(default="", max_length=200)    # 叙事视角
    chapter_count: int = Field(default=10, ge=1, le=1000)    # 目标章节数（2026-11-16 上限 60→1000）
    words_per_chapter: int = Field(default=1200, ge=100, le=8000)  # 每章字数
    ending_type: str = Field(default="", max_length=200)  # 结局类型（圆满/开放/悲剧/反转…）
    has_romance: bool = Field(default=False)              # 是否感情线
    content_restrictions: str = Field(default="", max_length=2000)  # 内容限制
    must_keep_settings: str = Field(default="", max_length=5000)    # 必须保留设定
    existing_settings: dict | None = Field(default=None)  # 已填详细设定（只补空白不擅改）
    requested_mode: str = Field(default="auto", max_length=10)  # auto / mock / real
    # ---- 第六批遗留：请求级模型覆盖（可选，缺省用服务端生效配置） ----
    model_name: str = Field(default="", max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)


class PlanNovelResponse(BaseModel):
    """AI 规划响应：完整规划 + mode 标记。"""

    mode: str
    plan: NovelPlan
    message: str = ""


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """局部修改请求：修改指令 + 当前规划 + 锁定项。"""

    instruction: str = Field(min_length=1, max_length=5000)  # 用户修改指令
    current_plan: NovelPlan
    locked: list[str] = Field(default_factory=list, max_length=200)  # 锁定项
    requested_mode: str = Field(default="auto", max_length=10)
    # ---- 第六批遗留：请求级模型覆盖（可选，缺省用服务端生效配置） ----
    model_name: str = Field(default="", max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)


class PlanUpdateResponse(BaseModel):
    """局部修改响应：更新后的规划 + 受影响章节列表。"""

    mode: str
    plan: NovelPlan
    changed_chapter_ids: list[int] = []
    message: str = ""


class PlanUpdateAIResult(BaseModel):
    """AI 局部修改的内部结构化返回：更新后的规划 + 受影响章节。"""

    plan: NovelPlan
    changed_chapter_ids: list[int] = []
