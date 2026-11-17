from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "NovelCraft"
    debug: bool = True

    # SQLAlchemy SQL echo 独立开关：与 debug 解耦，默认关闭，避免把 SQL 语句与绑定参数写进日志。
    # 需要排查 SQL 时显式设置 SQL_ECHO=true。
    sql_echo: bool = False

    # 实验接口（/api/v1/*、商业化、举报等）在开发模式下的额外开关：
    # 仅当 debug=True 且本开关=True 时，普通用户才可访问实验分组；
    # 管理员不受本开关限制。生产环境应保持 False。
    experimental_open_in_debug: bool = False

    database_url: str

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440

    ai_api_key: str | None = None
    ai_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"

    # ---- AI 小说生成（OpenAI 兼容服务通用，优先级：DEEPSEEK_* > AI_* > OPENAI_*） ----
    # 优先读取 DEEPSEEK_API_KEY（DeepSeek 官方密钥），其次 AI_API_KEY，最后 OPENAI_API_KEY。
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    deepseek_model: str | None = None

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None

    # ---- 阶段 1：模型策略兜底 ----
    # 仅在「未声明任务类型且无调用方 model_name」时使用的兜底模型；
    # 只允许实测通过的 deepseek-flash / deepseek-v4-pro，旧别名会被
    # app/services/model_policy.py 兼容映射，绝不作为真实请求模型下发。
    default_task_model: str = "deepseek-flash"

    ai_timeout_seconds: int = 120
    ai_max_output_tokens: int = 8192
    # mock 章节间延迟（秒），仅模拟模式生效，便于前端展示逐章进度
    ai_mock_delay_seconds: float = 0.6

    # ---- 第十八批「性能优化、可靠性与安全加固」配置 ----

    # API 安全：请求体大小限制（默认 1MB）
    request_body_max_bytes: int = 1_048_576

    # 缓存：默认 TTL（秒），主动失效由业务层驱动
    cache_default_ttl_seconds: int = 300

    # 分页：page_size 上限（规格六）
    page_size_max: int = 100

    # 限流：默认窗口（秒）与各作用域默认上限（规格十一）
    rate_limit_window_seconds: int = 60
    rate_limit_defaults: dict = {
        "login": 10,
        "register": 5,
        "password_reset": 5,
        "comment": 30,
        "ai": 20,
        "import_export": 10,
        "search": 60,
        "upload": 20,
        "email": 20,
        "default": 60,
    }

    # AI 成本保护（规格十二）
    ai_daily_default: int = 50
    ai_project_budget_default: int = 500
    ai_user_budget_default: int = 1000
    ai_concurrency_default: int = 3
    ai_max_input_chars: int = 100000

    # 认证安全（规格十八）
    refresh_token_expire_days: int = 30
    login_max_attempts: int = 5
    login_lock_minutes: int = 15

    # 二次确认（规格十九）
    confirmation_token_expire_minutes: int = 15

    # 文件存储安全（规格二十二）
    file_max_bytes: int = 20 * 1024 * 1024
    file_allowed_extensions: list = ["jpg", "jpeg", "png", "webp", "gif", "pdf", "txt", "md", "docx", "xlsx"]
    file_allowed_mime_types: list = [
        "image/jpeg", "image/png", "image/webp", "image/gif",
        "application/pdf", "text/plain", "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]
    file_signed_url_minutes: int = 15

    # 请求日志 / 安全头
    request_log_enabled: bool = True
    security_headers: bool = True
    # 第三批 a：安全响应头的 CSP 值。默认对纯 JSON API 安全；前端页面由 Vite/
    # 部署层单独下发 CSP，这里只保证"接口响应不带可执行内容且不可被嵌套"。
    security_csp_policy: str = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    # 第三批 b：是否沿用上游/前端的 X-Request-Id（仅接受 8-64 位安全字符集）。
    trust_request_id_header: bool = True

    # 第三批 e：整体下线未验证模块（逗号分隔，默认空=全部启用）。
    # 已登记：enterprise / billing / i18n / ops / analytics / security / openapi
    disabled_modules: str = ""

    # 第三批 f：匿名分享链接的口令失败限流（按文件+时间窗计数，不新增表）。
    share_access_max_failures: int = 10
    share_access_window_minutes: int = 15

    # ---- 第二十批「商业化、订阅与计费系统」配置 ----

    # 支付渠道 Webhook 签名密钥（本地 stub 渠道 HMAC-SHA256 验签）
    # 空值 = 未配置：验签一律拒绝（fail closed），生产必须显式配置强随机密钥
    billing_provider_secret: str = ""

    # 风控阈值（规格二十一）
    risk_payment_failure_threshold: int = 3
    risk_payment_failure_window_minutes: int = 30
    risk_trial_threshold: int = 2
    risk_quota_burn_threshold: int = 50000
    risk_quota_burn_window_minutes: int = 10

    # 阶段B「人工处置与误报闭环」：accepted_exception 豁免逻辑开关。
    # True 时命中豁免白名单的校验问题标记 exempted（不计入 blocking、不报错，
    # 仍记录为已豁免）；False 时豁免白名单不生效（仅标注处置状态）。
    fact_exemption_enabled: bool = True

    # 阶段F「场景合同」：contract 维度校验开关。
    # True 时 fact_validator 新增 contract.state_change / contract.forbidden_reveal
    # 两条 warning 级规则（校验正文是否满足计划中的预期状态变化、是否触碰
    # allowedReveals 之外的禁止揭示项）；False 时 contract 维度完全不生效，
    # 不影响既有 8 条规则与旧行为。
    fact_contract_enabled: bool = True

    # 阶段G「时间与认知系统」：认知类型细化开关。
    # True 时 fact_validator 启用认知类型细化：① secret.early_knowledge 对
    # belief/rumor/lie/uncertain 类快照不按客观知情判定（角色听说≠客观知晓，
    # 避免误报）；② 新增 timeline.date_order 规则解析结构化时间（time_type
    # 非 unknown 且可解析的事件，range 取区间交集判断先后顺序）。
    # False 时两类细化完全不生效：belief 快照仍按客观知情判定、date_order 不
    # 执行，既有 8 条规则与旧行为完全保留。
    fact_cognition_enabled: bool = True

    # 阶段I「独立 LLM 审稿 + 补丁闭环」：独立 LLM 连续性审稿开关。
    # True 时开放独立 LLM 连续性审稿（POST /api/ai/consistency-review）：异步、
    # 可取消，仅产出只读报告（落 consistency_review），问题只入 change_suggestions
    # 建议队列，人工确认后才可应用；绝不修改正文与事实库。
    # False 时接口直接拒绝（400/功能关闭），既有静态一致性检查不受影响。
    llm_consistency_review_enabled: bool = True

    # 阶段I：修复补丁候选生成开关。
    # True 时开放只读补丁候选生成（POST /api/ai/repair-candidate，异步任务
    # repair_candidate）：只返回候选补丁，不落正文、不改库；受阶段D
    # AUTO_REPAIR_MAX_PER_ISSUE（2 次/问题）限次守卫约束。
    # False 时接口拒绝（400/功能关闭）。
    repair_candidate_enabled: bool = True

    # 阶段J「审计补齐 + 备份调度」：内容级可追溯审计开关（补充 10 / R13）。
    # True 时 audit_service 记录 content_save / content_restore / ai_apply / backfill，
    # 含字段级 diff（正文只存哈希/长度/摘要，不复制全文）；审计只增不改。
    # False 时新增的内容级审计不写入（既有登录/权限/删除等审计不受影响）。
    content_audit_enabled: bool = True

    # 阶段J：备份自动调度开关（复用既有 backup_service 的 automatic 备份类型）。
    # 关闭后调度器一次也不会创建备份（策略可关，保证磁盘占用可控）。
    backup_auto_daily_enabled: bool = True
    # 每日自动备份的执行时刻（本地时间），默认 03:00
    backup_auto_hour: int = 3
    backup_auto_minute: int = 0
    # 距上次自动备份的最小间隔（小时）：同时决定「每日」语义与补偿追赶
    backup_auto_interval_hours: int = 24
    # 单个作品在 24 小时内最多自动创建几个备份（防止磁盘占用不可控）
    backup_auto_daily_limit: int = 1
    # 自动备份保留上限（个数），不超过 backup_service.MAX_BACKUPS_PER_PROJECT
    backup_auto_retention_limit: int = 7
    # 自动备份 TTL（天），与 backup_service.BACKUP_TTL_DAYS 一致
    backup_auto_retention_ttl_days: int = 90
    # 单轮调度最多处理的作品数（避免首次启用时一次性为全部作品生成备份）
    backup_auto_max_projects_per_run: int = 3
    # 后台调度检查间隔（秒）
    backup_auto_check_interval_seconds: int = 600

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )


settings = Settings()
