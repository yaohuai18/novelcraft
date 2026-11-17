# 轻量数据库迁移（第一期「生成不丢失」/ 第三期「人物和时间不漂」/ 第四期「写错能发现」/
# 文26 P1「章节记忆补齐」）
# 说明：项目使用 create_all 建表，但 chapter_versions / characters / timeline_events /
# project_settings / chapter_memories 为既有表，新增列无法通过 create_all 追加，需在此以幂等方式补列并初始化。
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# 需要在既有表 chapter_versions 上补充的列定义（SQLite ALTER TABLE ADD COLUMN）
_CHAPTER_VERSION_COLUMNS = [
    ("word_count", "INTEGER DEFAULT 0 NOT NULL"),
    ("generation_record_id", "CHAR(32)"),
    ("parent_version_id", "CHAR(32)"),
    ("is_current", "BOOLEAN DEFAULT 0 NOT NULL"),
]

# 第三期：characters 表补充人物库字段（role/status 需带默认值以满足 NOT NULL）
_CHARACTER_PHASE3_COLUMNS = [
    ("aliases", "TEXT"),
    ("role", "VARCHAR(20) DEFAULT '配角' NOT NULL"),
    ("status", "VARCHAR(20) DEFAULT '在世' NOT NULL"),
    ("profile", "TEXT"),
    ("goals", "TEXT"),
    ("secrets", "TEXT"),
    ("abilities", "TEXT"),
    ("current_location", "VARCHAR(255)"),
    ("current_timeline_status", "TEXT"),
    ("first_chapter_id", "CHAR(32)"),
    ("last_chapter_id", "CHAR(32)"),
]

# 第三期：timeline_events 表补充结构化字段
_TIMELINE_PHASE3_COLUMNS = [
    ("chapter_id", "CHAR(32)"),
    ("event_date_label", "VARCHAR(255)"),
    ("sequence_order", "INTEGER"),
    ("location_id", "CHAR(32)"),
    ("participants", "TEXT"),
    ("source", "VARCHAR(20) DEFAULT 'manual' NOT NULL"),
    ("confirmed", "BOOLEAN DEFAULT 0 NOT NULL"),
]

# 第四期：project_settings 表补充「生成后自动一致性检查」开关（默认开）
_PROJECT_SETTING_PHASE4_COLUMNS = [
    ("auto_check", "BOOLEAN DEFAULT 1 NOT NULL"),
]

# 文26 P1：chapter_memories 表补充 generation_record_id（摘要/状态提取生成记录关联）
_CHAPTER_MEMORY_PHASE7_COLUMNS = [
    ("generation_record_id", "CHAR(32)"),
]

# 文26 P2：人物/关系/时间线/伏笔正式管理表补列（对历史库幂等补齐）
_CHARACTER_SNAPSHOT_P2_COLUMNS = [
    ("status", "VARCHAR(16) DEFAULT 'draft' NOT NULL"),
]
_CHARACTER_RELATIONSHIP_P2_COLUMNS = [
    ("status", "VARCHAR(16) DEFAULT 'draft' NOT NULL"),
]
_TIMELINE_P2_COLUMNS = [
    ("time_label", "VARCHAR(255)"),
    ("status", "VARCHAR(16) DEFAULT 'draft' NOT NULL"),
]
_FORESHADOW_P2_COLUMNS = [
    ("title", "VARCHAR(255)"),
    ("importance", "VARCHAR(8) DEFAULT 'medium' NOT NULL"),
    ("resolved_chapter_id", "CHAR(32)"),
]

# 章节计划（ChapterPlan）：outlines 表补结构化章节计划 JSON 列（幂等）。
# 为 NULL 时回退兼容旧数据（title/summary/content 即 legacy 计划）。
_OUTLINE_CHAPTER_PLAN_COLUMNS = [
    ("chapter_plan", "TEXT"),
]

# 「已确认大纲版本化」：generation_records 既有表补追溯字段（幂等）。
_GENERATION_RECORD_PHASE10_COLUMNS = [
    ("outline_plan_version_id", "CHAR(32)"),
    ("chapter_plan_id", "CHAR(64)"),
    ("chapter_plan_snapshot", "TEXT"),
    ("title_snapshot", "TEXT"),
]

# 「生成链路数据一致性/可追溯性」（第三批一致性修复）：generation_records 既有表
# 补原始响应与元数据列（幂等）。所有列带默认值/NULL，保证现有行不报错、历史页面可读。
_GENERATION_RECORD_PHASE11_COLUMNS = [
    ("raw_content", "TEXT DEFAULT '' NOT NULL"),
    ("processed_content", "TEXT DEFAULT '' NOT NULL"),
    ("final_content", "TEXT DEFAULT '' NOT NULL"),
    ("postprocess_actions", "TEXT"),
    ("finish_reason", "VARCHAR(30)"),
    ("prompt_tokens", "INTEGER"),
    ("completion_tokens", "INTEGER"),
    ("raw_content_length", "INTEGER"),
    ("final_content_length", "INTEGER"),
    ("was_truncated", "INTEGER DEFAULT 0"),
    ("truncation_reason", "TEXT"),
    ("review_flags", "TEXT"),
]

# 第一批「事实数据库与规则校验」：三个既有表补结构化事实列（幂等）。
# 全部带默认值/NULL，历史数据不报错；新库由 create_all 创建（ORM 已含新列）。
_CHARACTER_SNAPSHOT_PHASE12_COLUMNS = [
    ("age", "INTEGER"),
    ("birth_year", "INTEGER"),
    ("is_alive", "BOOLEAN"),
    ("location_id", "CHAR(32)"),
    ("possessions", "TEXT"),
    ("known_secrets", "TEXT"),
    ("injuries", "TEXT"),
]
_TIMELINE_PHASE12_COLUMNS = [
    ("absolute_time", "VARCHAR(255)"),
    ("relative_time", "VARCHAR(255)"),
    ("participant_ids", "TEXT"),
    ("cause_event_id", "CHAR(32)"),
    ("result", "TEXT"),
    ("is_public", "BOOLEAN DEFAULT 1 NOT NULL"),
]
_LOCATION_PHASE12_COLUMNS = [
    ("parent_location_id", "CHAR(32)"),
    ("travel_hours_from", "INTEGER"),
]
_OUTLINE_PHASE12_COLUMNS = [
    ("chapter_time", "VARCHAR(255)"),
    ("location_id", "CHAR(32)"),
    ("present_characters", "TEXT"),
    ("chapter_goal", "TEXT"),
    ("chapter_outcome", "TEXT"),
    ("state_facts_updated_at", "DATETIME"),
]

# 第一批「事实数据库与规则校验」：三张核心事实新表 DDL。
# 新库由 create_all 创建；历史库在此以 CREATE TABLE IF NOT EXISTS 兜底建表。
_ITEM_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id CHAR(32) PRIMARY KEY,
    project_id CHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(20) DEFAULT 'item' NOT NULL,
    first_chapter_id CHAR(32),
    description TEXT,
    status VARCHAR(30) DEFAULT 'active' NOT NULL,
    created_at DATETIME NOT NULL
)
"""

_ITEM_POSSESSION_DDL = """
CREATE TABLE IF NOT EXISTS item_possessions (
    id CHAR(32) PRIMARY KEY,
    item_id CHAR(32) NOT NULL,
    owner_character_id CHAR(32) NOT NULL,
    since_chapter_id CHAR(32),
    until_chapter_id CHAR(32),
    status VARCHAR(20) DEFAULT 'active' NOT NULL,
    created_at DATETIME NOT NULL
)
"""

_SECRET_KNOWER_DDL = """
CREATE TABLE IF NOT EXISTS secret_knowers (
    id CHAR(32) PRIMARY KEY,
    secret_id CHAR(32) NOT NULL,
    character_id CHAR(32) NOT NULL,
    learned_chapter_id CHAR(32),
    source VARCHAR(20) DEFAULT 'ai_extracted' NOT NULL,
    reveal_chapter_id CHAR(32),
    forbidden_before_chapter_id CHAR(32),
    created_at DATETIME NOT NULL
)
"""

# outline_plan_versions 新表 DDL（新库由 create_all 创建；历史库在此幂等建表）。
_OUTLINE_PLAN_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS outline_plan_versions (
    id CHAR(32) PRIMARY KEY,
    project_id CHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    status VARCHAR(16) DEFAULT 'draft' NOT NULL,
    content_snapshot TEXT,
    chapter_plans_snapshot TEXT,
    confirmed_at DATETIME,
    confirmed_by CHAR(32),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_opv_project_version UNIQUE (project_id, version)
)
"""


async def _table_columns(conn: AsyncConnection, table: str) -> set[str]:
    """异步读取 SQLite 表列名（避免在 AsyncConnection 上使用同步 inspector）。"""
    try:
        rows = (await conn.execute(
            text("SELECT name FROM pragma_table_info(:table_name)"),
            {"table_name": table},
        )).all()
    except Exception:  # noqa: BLE001 表不存在或非 SQLite
        return set()
    return {str(row[0]) for row in rows}


async def _add_missing_columns(
    conn: AsyncConnection, table: str, columns: list[tuple[str, str]]
) -> None:
    """幂等补列：对既有表按列名检查后 ALTER TABLE ADD COLUMN。"""
    existing = await _table_columns(conn, table)
    for column, ddl in columns:
        if column not in existing:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            )
            logger.info("%s 新增列: %s", table, column)


async def ensure_phase1_schema(conn: AsyncConnection) -> None:
    """第一期「生成不丢失」表结构迁移（幂等）：
    1) chapter_versions 补列；
    2) 初始化 is_current：当前无 current 版本时，取版本号最大的行置为 current。
    generation_records 为新表，由 create_all 自动创建，无需处理。
    """
    await _add_missing_columns(conn, "chapter_versions", _CHAPTER_VERSION_COLUMNS)

    # 初始化 is_current：没有任何 current 行时，最新版本置为 current
    rows = (await conn.execute(
        text("SELECT COUNT(*) FROM chapter_versions WHERE is_current = 1")
    )).scalar()
    if not rows:
        await conn.execute(
            text(
                "UPDATE chapter_versions SET is_current = 1 "
                "WHERE id = ("
                "  SELECT id FROM chapter_versions "
                "  ORDER BY version DESC LIMIT 1"
                ")"
            )
        )
        logger.info("chapter_versions 初始化 is_current 完成")


async def ensure_phase3_schema(conn: AsyncConnection) -> None:
    """第三期「人物和时间不漂」表结构迁移（幂等）：
    1) characters 表补人物库字段；
    2) timeline_events 表补结构化字段；
    新表（character_state_snapshots / character_relationships / change_suggestions）
    由 create_all 自动创建，无需处理。
    """
    await _add_missing_columns(conn, "characters", _CHARACTER_PHASE3_COLUMNS)
    await _add_missing_columns(conn, "timeline_events", _TIMELINE_PHASE3_COLUMNS)


async def ensure_phase4_schema(conn: AsyncConnection) -> None:
    """第四期「写错能发现」表结构迁移（幂等）：
    project_settings 表补 auto_check 开关列；
    foreshadows 表为全新表，由 create_all 自动创建，无需处理。
    """
    await _add_missing_columns(
        conn, "project_settings", _PROJECT_SETTING_PHASE4_COLUMNS
    )


async def ensure_phase7_schema(conn: AsyncConnection) -> None:
    """文26 P1「章节摘要和续写记忆闭环」表结构迁移（幂等）：
    chapter_memories 表补 generation_record_id 列；
    structuredMemory 结构存于既有 memory_json 列（JSON），无需新增表。
    """
    await _add_missing_columns(conn, "chapter_memories", _CHAPTER_MEMORY_PHASE7_COLUMNS)


async def ensure_phase8_schema(conn: AsyncConnection) -> None:
    """文26 P2「人物、关系、时间线和伏笔正式管理」表结构迁移（幂等）：
    1) character_state_snapshots 表补 status 列（draft|confirmed|rejected）；
    2) character_relationships 表补 status 列（draft|confirmed|archived，历史 proposed 行保留原值）；
    3) timeline_events 表补 time_label / status 列；
    4) foreshadows 表补 title / importance / resolved_chapter_id 列。
    各表其余新列为新表/由 create_all 或三期迁移创建，无需处理。
    """
    await _add_missing_columns(
        conn, "character_state_snapshots", _CHARACTER_SNAPSHOT_P2_COLUMNS
    )
    await _add_missing_columns(
        conn, "character_relationships", _CHARACTER_RELATIONSHIP_P2_COLUMNS
    )
    await _add_missing_columns(conn, "timeline_events", _TIMELINE_P2_COLUMNS)
    await _add_missing_columns(conn, "foreshadows", _FORESHADOW_P2_COLUMNS)


async def ensure_phase9_schema(conn: AsyncConnection) -> None:
    """章节计划（ChapterPlan）表结构迁移（幂等）：
    outlines 表补 chapter_plan JSON 列（仅 chapter 类型使用）。
    为 NULL 时回退兼容旧数据（title/summary/content 即 legacy 计划），
    因此历史项目无需迁移即可继续生成。
    """
    await _add_missing_columns(conn, "outlines", _OUTLINE_CHAPTER_PLAN_COLUMNS)


async def ensure_phase10_schema(conn: AsyncConnection) -> None:
    """「已确认大纲版本化」表结构迁移（幂等）：
    1) 新建 outline_plan_versions 表（新库由 create_all 创建，历史库在此兜底建表）；
    2) generation_records 补 outline_plan_version_id / chapter_plan_id /
       chapter_plan_snapshot / title_snapshot 追溯列。
    历史项目兼容：旧生成记录上述字段为 NULL；旧 outlines.chapter_plan 数据在
    首次「同步草稿/确认大纲」时由快照器归一化吸收，无需人工迁移。
    """
    await conn.execute(text(_OUTLINE_PLAN_VERSION_DDL))
    await _add_missing_columns(
        conn, "generation_records", _GENERATION_RECORD_PHASE10_COLUMNS
    )
    # 为既有建表路径补索引（IF NOT EXISTS 幂等）
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_generation_records_opv_id "
            "ON generation_records (outline_plan_version_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_outline_plan_versions_project "
            "ON outline_plan_versions (project_id)"
        )
    )
    logger.info("phase10 已确认大纲版本化迁移完成")


async def ensure_phase11_schema(conn: AsyncConnection) -> None:
    """「生成链路数据一致性/可追溯性」表结构迁移（幂等）：
    generation_records 补 raw_content / processed_content / final_content /
    postprocess_actions / finish_reason / prompt_tokens / completion_tokens /
    raw_content_length / final_content_length / was_truncated /
    truncation_reason / review_flags 一致性列。
    新库由 create_all 创建（ORM 已含全部新列）；历史库在此幂等补列，
    所有列带默认值，现有行不报错、历史页面可读。
    """
    await _add_missing_columns(
        conn, "generation_records", _GENERATION_RECORD_PHASE11_COLUMNS
    )
    logger.info("phase11 生成链路一致性迁移完成")


async def ensure_phase12_schema(conn: AsyncConnection) -> None:
    """第一批「事实数据库与规则校验」表结构迁移（幂等）：
    1) 新建三张核心事实表 items / item_possessions / secret_knowers
       （新库由 create_all 创建，历史库在此兜底建表）；
    2) character_state_snapshots / timeline_events / locations / outlines
       四个既有表补结构化事实列（age/birth_year/is_alive/location_id/
       possessions/known_secrets/injuries；absolute_time/relative_time/
       participant_ids/cause_event_id/result/is_public；parent_location_id/
       travel_hours_from；chapter_time/location_id/present_characters/
       chapter_goal/chapter_outcome/state_facts_updated_at）。
    全部带默认值/NULL，历史数据不报错；重复执行幂等。
    """
    await conn.execute(text(_ITEM_DDL))
    await conn.execute(text(_ITEM_POSSESSION_DDL))
    await conn.execute(text(_SECRET_KNOWER_DDL))
    await _add_missing_columns(
        conn, "character_state_snapshots", _CHARACTER_SNAPSHOT_PHASE12_COLUMNS
    )
    await _add_missing_columns(
        conn, "timeline_events", _TIMELINE_PHASE12_COLUMNS
    )
    await _add_missing_columns(conn, "locations", _LOCATION_PHASE12_COLUMNS)
    await _add_missing_columns(conn, "outlines", _OUTLINE_PHASE12_COLUMNS)
    # 为事实表补索引（IF NOT EXISTS 幂等）
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_items_project "
            "ON items (project_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_item_possessions_item "
            "ON item_possessions (item_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_item_possessions_owner "
            "ON item_possessions (owner_character_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_secret_knowers_secret "
            "ON secret_knowers (secret_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_secret_knowers_character "
            "ON secret_knowers (character_id)"
        )
    )
    logger.info("phase12 事实数据库与规则校验迁移完成")


# ---------- 第一阶段「章节生命周期与可追溯元数据」（phase13） ----------
# 章节实体存放于 outlines 表（outline_type='chapter'），生命周期列落在 outlines；
# chapter_contents 为正文表，同步补同列并修复 R4（validation_summary 真实落库）。
# 全部列带默认值/NULL，历史 169 章存量数据不报错；重复执行幂等。
_OUTLINE_PHASE13_COLUMNS = [
    ("lifecycle_status", "VARCHAR(30) DEFAULT 'draft' NOT NULL"),
    ("generation_meta", "TEXT"),
    ("validation_report", "TEXT"),
    ("repair_records", "TEXT"),
    ("outline_version_id", "CHAR(32)"),
    ("facts_version", "INTEGER DEFAULT 0 NOT NULL"),
]

_CHAPTER_CONTENT_PHASE13_COLUMNS = [
    ("lifecycle_status", "VARCHAR(30) DEFAULT 'draft' NOT NULL"),
    ("generation_meta", "TEXT"),
    ("validation_report", "TEXT"),
    ("repair_records", "TEXT"),
    ("outline_version_id", "CHAR(32)"),
    ("facts_version", "INTEGER DEFAULT 0 NOT NULL"),
    ("validation_summary", "TEXT"),
    ("validation_updated_at", "DATETIME"),
]


async def ensure_phase13_schema(conn: AsyncConnection) -> None:
    """第一阶段「章节生命周期与可追溯元数据」表结构迁移（幂等）：
    1) outlines 既有表补 lifecycle_status / generation_meta / validation_report /
       repair_records / outline_version_id / facts_version 列；
    2) chapter_contents 既有表补同组列 + validation_summary / validation_updated_at
       （修复审查 R4：校验结果原本只挂在 Python 动态属性上不落库）。
    新库由 create_all 创建（ORM 已含全部新列）；历史库在此幂等补列，
    全部带默认值，现有行不报错、旧数据读取新字段返回默认值。
    """
    await _add_missing_columns(conn, "outlines", _OUTLINE_PHASE13_COLUMNS)
    await _add_missing_columns(
        conn, "chapter_contents", _CHAPTER_CONTENT_PHASE13_COLUMNS
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_outlines_lifecycle "
            "ON outlines (lifecycle_status)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_chapter_contents_lifecycle "
            "ON chapter_contents (lifecycle_status)"
        )
    )
    logger.info("phase13 章节生命周期与可追溯元数据迁移完成")


# ---------- 阶段A「事实权威与锁定」（phase14） ----------
# 五张事实表补权威等级/置信度/人工锁定/来源记录列（幂等，带默认值）。
# 存量行 authority_level 默认 normal、locked 默认 0（不锁定），可正常读取；
# 重复执行幂等（_add_missing_columns 按列名去重）。
_FACT_AUTHORITY_COLUMNS = [
    ("authority_level", "VARCHAR(20) DEFAULT 'normal' NOT NULL"),
    ("confidence", "REAL"),
    ("locked", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("source_record_id", "VARCHAR(64)"),
]

_FACT_AUTHORITY_TABLES = (
    "character_state_snapshots",
    "timeline_events",
    "items",
    "secret_knowers",
    "item_possessions",
)


async def ensure_phase14_schema(conn: AsyncConnection) -> None:
    """阶段A「事实权威与锁定」表结构迁移（幂等）：
    character_state_snapshots / timeline_events / items / secret_knowers /
    item_possessions 五张事实表补 authority_level（low|normal|high）/
    confidence（0-1）/ locked（人工锁定）/ source_record_id（来源记录）列。
    新库由 create_all 创建（ORM 已含全部新列）；历史库在此幂等补列，
    全部带默认值/NULL，现有行不报错、旧数据读取新字段返回默认值。
    另为五表补 locked 索引，支撑过滤接口按锁定状态检索（IF NOT EXISTS 幂等）。
    """
    for table in _FACT_AUTHORITY_TABLES:
        await _add_missing_columns(conn, table, _FACT_AUTHORITY_COLUMNS)
        await conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_locked "
                f"ON {table} (locked)"
            )
        )
    logger.info("phase14 事实权威与锁定迁移完成")


# ---------- 阶段B「人工处置与误报闭环」（phase15） ----------
# fact_issue_dispositions 新表：人工审稿问题处置状态机（confirmed /
# false_positive / accepted_exception / fixed / deferred / wont_fix），
# accepted_exception 命中后进入豁免白名单，后续回归校验不报错仍记录为已豁免。
# 回滚方式：DROP TABLE fact_issue_dispositions 即可整体删除（新表无存量依赖）；
# 豁免逻辑另由 settings.fact_exemption_enabled 开关控制（可关）。
# quality 指标扩展（generation_truncate_rate/validation_blocking_rate/
# false_positive_rate/repair_success_rate）不涉及表结构变更，
# 复用 data_quality_metrics 由 quality_service 写入新 metric_type。
_PHASE15_DDL = """
CREATE TABLE IF NOT EXISTS fact_issue_dispositions (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(36) NOT NULL,
    issue_key VARCHAR(64) NOT NULL,
    disposition VARCHAR(20) NOT NULL,
    note TEXT,
    entity_type VARCHAR(30),
    entity_id VARCHAR(36),
    rule VARCHAR(50),
    set_by VARCHAR(36),
    set_at DATETIME NOT NULL,
    UNIQUE (project_id, issue_key)
)
"""


async def ensure_phase15_schema(conn: AsyncConnection) -> None:
    """阶段B「人工处置与误报闭环」迁移（幂等，可重复执行）：
    - 新增 fact_issue_dispositions 表（人工处置状态机 + 豁免白名单）；
    - 为 disposition / issue_key 建索引（IF NOT EXISTS 幂等）。
    新库由 create_all 创建（ORM 已含该模型）；历史库在此幂等建表。
    """
    await conn.execute(text(_PHASE15_DDL))
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_fact_issue_dispositions_disposition "
            "ON fact_issue_dispositions (disposition)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_fact_issue_dispositions_issue_key "
            "ON fact_issue_dispositions (issue_key)"
        )
    )
    logger.info("phase15 人工处置与误报闭环迁移完成")


# ---------- 阶段C「发布基线绑定」（phase16） ----------
# publications 表补基线元数据与引用式快照列：
#   baseline_meta（JSON）：facts_version / setting_version / rules_version /
#     prompt_version / model_name / validation_report_id；
#   baseline_snapshot（JSON）：事实库 + 设定不可变快照（引用式，仅实体 id 列表，
#     不复制正文）。
# 字段可空：旧发布记录 baseline_meta=NULL 不追溯；收集失败降级 NULL 不阻断发布。
# 回滚方式：字段可空，旧发布记录不受影响；保留列停用字段即可回滚。
_PHASE16_PUBLICATION_COLUMNS = [
    ("baseline_meta", "TEXT"),
    ("baseline_snapshot", "TEXT"),
]


async def ensure_phase16_schema(conn: AsyncConnection) -> None:
    """阶段C「发布基线绑定」迁移（幂等，可重复执行）：
    为 publications 表幂等补 baseline_meta / baseline_snapshot 列（Text 可空）。
    新库由 create_all 创建（ORM 已含新列）；历史库在此幂等补列。
    """
    await _add_missing_columns(conn, "publications", _PHASE16_PUBLICATION_COLUMNS)
    logger.info("phase16 发布基线绑定迁移完成")


# ---------- 阶段D「异步任务增强」（phase17） ----------
# ai_tasks 表补自动重试 / 成本预算 / 取消恢复字段：
#   max_retries（INTEGER DEFAULT 0 NOT NULL）：自动重试上限，0 = 不自动重试（默认关闭）；
#   cost_budget（INTEGER NULL）：成本预算上限（累计 input+output token 数），NULL = 不检查（默认关闭）；
#   resume_token（TEXT NULL）：取消后恢复的进度快照 JSON，NULL = 不可恢复（默认关闭）。
# retry_count 已存在（P0 字段），无需重复加列。
# 全部带默认值/NULL，存量行可读；行为仅在显式设置新字段后生效。
# 回滚方式：字段可空，保留列停用字段即可回滚。
_PHASE17_AI_TASK_COLUMNS = [
    ("max_retries", "INTEGER DEFAULT 0 NOT NULL"),
    ("cost_budget", "INTEGER"),
    ("resume_token", "TEXT"),
]


async def ensure_phase17_schema(conn: AsyncConnection) -> None:
    """阶段D「异步任务增强」迁移（幂等，可重复执行）：
    为 ai_tasks 表幂等补 max_retries / cost_budget / resume_token 列。
    新库由 create_all 创建（ORM 已含新列）；历史库在此幂等补列。
    """
    await _add_missing_columns(conn, "ai_tasks", _PHASE17_AI_TASK_COLUMNS)
    logger.info("phase17 异步任务增强迁移完成")


# ---------- 阶段G「时间与认知系统」（phase18） ----------
# timeline_events 补时间类型与三通道：
#   time_type（exact|range|relative|approximate|unknown，存量默认 unknown）、
#   time_start / time_end（结构化区间）、narrated_at（叙述时间）、
#   known_at（获知时间）；
# timeline_events / character_state_snapshots 补事实认知类型与认知主体：
#   fact_kind（fact|belief|rumor|lie|uncertain，存量默认 fact）、
#   holder_character_id（认知主体，believe 类必填，可空）。
# 全部带默认值/NULL：存量行 time_type=unknown、fact_kind=fact 兜底，
# 不回溯改写；规则细化仅在启用认知类型时生效，旧校验行为保留。
# 回滚方式：新增字段默认值兜底，保留列停用字段即可回滚。
_TIMELINE_PHASE18_COLUMNS = [
    ("time_type", "VARCHAR(20) DEFAULT 'unknown' NOT NULL"),
    ("time_start", "VARCHAR(255)"),
    ("time_end", "VARCHAR(255)"),
    ("narrated_at", "VARCHAR(255)"),
    ("known_at", "VARCHAR(255)"),
    ("fact_kind", "VARCHAR(20) DEFAULT 'fact' NOT NULL"),
    ("holder_character_id", "CHAR(32)"),
]

_CHARACTER_SNAPSHOT_PHASE18_COLUMNS = [
    ("fact_kind", "VARCHAR(20) DEFAULT 'fact' NOT NULL"),
    ("holder_character_id", "CHAR(32)"),
]


async def ensure_phase18_schema(conn: AsyncConnection) -> None:
    """阶段G「时间与认知系统」表结构迁移（幂等）：
    timeline_events 补时间类型（exact/range/relative/approximate/unknown）与
    time_start/time_end/narrated_at/known_at（发生/叙述/获知三通道）；
    timeline_events / character_state_snapshots 补事实认知类型
    （fact/belief/rumor/lie/uncertain）与 holder_character_id 认知主体列。
    新库由 create_all 创建（ORM 已含全部新列）；历史库在此幂等补列，
    全部带默认值/NULL：存量行 time_type=unknown、fact_kind=fact 兜底可读，
    不回溯改写存量数据。另为两表 holder_character_id 补索引（IF NOT EXISTS 幂等）。
    """
    await _add_missing_columns(conn, "timeline_events", _TIMELINE_PHASE18_COLUMNS)
    await _add_missing_columns(
        conn, "character_state_snapshots", _CHARACTER_SNAPSHOT_PHASE18_COLUMNS
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_timeline_events_holder_character_id "
            "ON timeline_events (holder_character_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_character_state_snapshots_holder_character_id "
            "ON character_state_snapshots (holder_character_id)"
        )
    )
    logger.info("phase18 时间与认知系统迁移完成")


# ---------- 阶段H「依赖图增量回归」（phase19） ----------
# 新建 chapter_fact_dependencies 表：保存章节事实后记录本章引用的事实键
# （chapter_id / entity_type / entity_id / fact_key / dep_snapshot）。
# 依赖图用途：变更章节 X 后，仅将「后续章节中与 X 依赖键有交集」的章节标记为
# needs_recheck；变更章无依赖记录（依赖未知）时回退为标记全部后续章节，
# 兼容旧行为（mark_later_chapters_needs_recheck 全量标记）。
# 本表只读改写自事实提取结果，不触碰正文；仅新增表、无存量数据影响。
# 回滚方式：新表可清理（DROP TABLE / 清空行），依赖未知自动回退旧行为。
_CHAPTER_FACT_DEPENDENCIES_DDL = """
CREATE TABLE IF NOT EXISTS chapter_fact_dependencies (
    id CHAR(32) PRIMARY KEY,
    chapter_id CHAR(32) NOT NULL,
    project_id CHAR(32) NOT NULL,
    entity_type VARCHAR(30) NOT NULL,
    entity_id CHAR(32),
    fact_key VARCHAR(255) NOT NULL,
    dep_snapshot TEXT,
    created_at DATETIME
)
"""


# 建表 + 建索引语句的唯一来源（顺序即执行顺序，改动必须与 ORM __table_args__ 同步）。
PHASE19_MIGRATION_STATEMENTS: list[str] = [
    _CHAPTER_FACT_DEPENDENCIES_DDL,
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_chapter_fact_dependencies_chapter_id "
        "ON chapter_fact_dependencies (chapter_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_chapter_fact_dependencies_lookup "
        "ON chapter_fact_dependencies (entity_type, entity_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_chapter_fact_dependencies_fact_key "
        "ON chapter_fact_dependencies (fact_key)"
    ),
]


async def ensure_phase19_schema(conn: AsyncConnection) -> None:
    """阶段H「依赖图增量回归」表结构迁移（幂等，可重复执行）：
    新建 chapter_fact_dependencies 表（chapter_id / entity_type / entity_id /
    fact_key / dep_snapshot），并为 chapter_id / 依赖键查找建立索引。
    新库由 create_all 创建（ORM 已注册模型）；历史库在此幂等建表。
    表内容仅在章节保存并成功提取事实后由依赖记录函数写入，不触碰正文。
    """
    for statement in PHASE19_MIGRATION_STATEMENTS:
        await conn.execute(text(statement))
    logger.info("phase19 依赖图增量回归迁移完成")

# ---------- 阶段I「独立 LLM 审稿 + 补丁闭环」（phase20） ----------
# 独立 LLM 连续性审稿报告表：只读报告，不修改正文与事实库。
# 新表可整体删除（DROP TABLE consistency_review）即回滚；功能开关关闭时审稿接口拒绝。
_CONSISTENCY_REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS consistency_review (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(36) NOT NULL,
    chapter_id VARCHAR(36),
    chapter_title VARCHAR(255),
    scope VARCHAR(20) NOT NULL DEFAULT 'chapter',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
    model_name VARCHAR(100),
    prompt_version VARCHAR(50),
    score FLOAT,
    issue_count INTEGER NOT NULL DEFAULT 0,
    severity_counts TEXT,
    issues TEXT,
    suggestion_count INTEGER NOT NULL DEFAULT 0,
    task_id VARCHAR(32),
    raw_response TEXT,
    error_message TEXT,
    note TEXT,
    created_at DATETIME,
    completed_at DATETIME
)
"""


# 建表 + 建索引语句的唯一来源（顺序即执行顺序，改动必须与 ORM __table_args__ 同步）。
PHASE20_MIGRATION_STATEMENTS: list[str] = [
    _CONSISTENCY_REVIEW_DDL,
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_consistency_review_project_id "
        "ON consistency_review (project_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_consistency_review_chapter_id "
        "ON consistency_review (chapter_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS "
        "ix_consistency_review_task_id "
        "ON consistency_review (task_id)"
    ),
]


async def ensure_phase20_schema(conn: AsyncConnection) -> None:
    """阶段I「独立 LLM 审稿 + 补丁闭环」表结构迁移（幂等，可重复执行）。

    新建 consistency_review 表（审稿只读报告）并为 project_id / chapter_id /
    task_id 建立索引。新库由 create_all 创建（ORM 已注册模型）；历史库在此幂等建表。
    表内容仅由审稿执行器写入，不触碰正文与事实库；表可整体删除回滚。
    """
    for statement in PHASE20_MIGRATION_STATEMENTS:
        await conn.execute(text(statement))
    logger.info("phase20 独立 LLM 审稿报告表迁移完成")


# ---------- 阶段J「审计补齐 + 备份调度」（phase21） ----------
# audit_logs 扩展：内容级可追溯审计字段（补充 10 / R13）。
# action 由 String(60) 承载新取值 content_save / content_restore / ai_apply /
# backfill，无需改列类型；此处仅幂等补列（默认 NULL，存量行可读）。
# 审计只增不改：本迁移不修改、不删除任何既有审计行。
_AUDIT_LOG_PHASE21_COLUMNS = [
    ("project_id", "VARCHAR(36)"),
    ("diff_json", "TEXT"),
    ("content_hash_before", "VARCHAR(64)"),
    ("content_hash_after", "VARCHAR(64)"),
    ("content_version", "INTEGER"),
    ("change_source", "VARCHAR(30)"),
]


async def ensure_phase21_schema(conn: AsyncConnection) -> None:
    """阶段J「审计补齐 + 备份调度」表结构迁移（幂等，可重复执行）：

    1) audit_logs 补 project_id / diff_json / content_hash_before /
       content_hash_after / content_version / change_source 六列；
    2) 为 project_id 与 (target_type, target_id) 建索引，支撑按作品/章节追溯。

    新库由 create_all 创建（ORM 模型已含这些列）；历史库在此幂等补列。
    备份自动调度复用既有 backup_service 与 project_backups 表，无需新表。
    回滚方式：保留列、关闭 content_audit_enabled / backup_auto_daily_enabled
    即可停止写入，不改动任何既有审计数据与备份数据。
    """
    await _add_missing_columns(conn, "audit_logs", _AUDIT_LOG_PHASE21_COLUMNS)
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_project_id "
            "ON audit_logs (project_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_content_target "
            "ON audit_logs (target_type, target_id)"
        )
    )
    logger.info("phase21 内容级审计扩展迁移完成")


# ---------- 阶段 B-2「逐次 token 账目表」（phase22） ----------
# 新增 ai_task_token_usage 表：按 (task_id, attempt_no, call_no) 逐次记录每次 LLM
# 调用的 token 账目，供成本预算（cost_budget）闸门与崩溃遗留审计使用。
#   - ai_tasks 表零变更（不加列、不改默认值、不动任何既有行）；
#   - 严格只增不改：本迁移不含任何 DML 回填，不修改、不删除既有数据。
# 双通道建表，两处共用下方同一份 DDL 常量（禁止在 migrate.py 内重复手写，避免漂移）：
#   ① 首启通道（本函数）：schema_migrations 为空时由 main._init_schema 调用；
#   ② 增量通道：migrate.MIGRATION_STEPS[3]（引用 PHASE22_MIGRATION_STATEMENTS）。
# 幂等：CREATE TABLE / CREATE INDEX 均 IF NOT EXISTS；另按 pragma_table_info
# 校验 19 列是否齐全，缺列才 ALTER TABLE ADD COLUMN（本轮一次建齐，正常不触发）。
# 回滚：DROP TABLE ai_task_token_usage（两个索引随表删除；表内无存量依赖数据）。
_PHASE22_USAGE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ai_task_token_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             CHAR(32)    NOT NULL,
    attempt_no          INTEGER     NOT NULL DEFAULT 1,
    call_no             INTEGER     NOT NULL DEFAULT 1,
    usage_status        VARCHAR(24) NOT NULL DEFAULT 'pending_dispatch',
    usage_source        VARCHAR(32) NOT NULL DEFAULT 'not_dispatched',
    provider            VARCHAR(32),
    requested_model     VARCHAR(64),
    normalized_model    VARCHAR(64),
    response_model      VARCHAR(64),
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    provider_request_id VARCHAR(128),
    finish_reason       VARCHAR(32),
    dispatched_at       DATETIME,
    settled_at          DATETIME,
    created_at          DATETIME    NOT NULL,
    updated_at          DATETIME,
    CONSTRAINT uq_usage_task_attempt_call UNIQUE (task_id, attempt_no, call_no)
)
"""

# 索引（B-5 裁定：采纳 idx_usage_task_dispatched）
_PHASE22_USAGE_INDEX_DDL = [
    (
        "CREATE INDEX IF NOT EXISTS idx_usage_status_settled "
        "ON ai_task_token_usage (usage_status, settled_at)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_usage_task_dispatched "
        "ON ai_task_token_usage (task_id, dispatched_at)"
    ),
]

# migrate.MIGRATION_STEPS[3] 与本模块 ensure_phase22_schema 共用（同源 DDL）
PHASE22_MIGRATION_STATEMENTS: list[str] = [
    _PHASE22_USAGE_TABLE_DDL,
    *_PHASE22_USAGE_INDEX_DDL,
]

# 补列分支用列定义：**由主 DDL 现场解析派生**（单一来源），禁止再手写第二份。
# 修复 B-2 漂移：旧手写版 task_id 多 DEFAULT ''、created_at 缺 NOT NULL，
# 与 _PHASE22_USAGE_TABLE_DDL 不一致（v3 已记账不会重放，故只能从实现层消除漂移）。
# 不含 id：SQLite 不支持 ALTER TABLE 追加主键列；id 缺失属残留异常表场景，
# 仅报告不重建，见 ensure_phase22_schema。
def _column_definitions_from_ddl(ddl: str) -> list[tuple[str, str]]:
    """从 CREATE TABLE 主 DDL 文本解析出顶层 (列名, 列定义) 列表。

    仅取顶层列定义行：跳过 CONSTRAINT 行与空行；列定义内多余空白归一为单空格。
    """
    body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
    columns: list[tuple[str, str]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.upper().startswith("CONSTRAINT"):
            continue
        name, _, definition = line.partition(" ")
        normalized = " ".join(definition.split())
        if not name or not normalized:
            continue
        columns.append((name, normalized))
    return columns


_PHASE22_USAGE_COLUMNS = [
    (name, column_ddl)
    for name, column_ddl in _column_definitions_from_ddl(_PHASE22_USAGE_TABLE_DDL)
    if name != "id"
]

# 枚举允许值（B-3 裁定：usage_source 收敛为 5 个值，不新增第六值）
PHASE22_ALLOWED_USAGE_STATUS = (
    "pending_dispatch",
    "request_uncertain",
    "complete",
    "partial",
    "unverifiable",
)
PHASE22_ALLOWED_USAGE_SOURCE = (
    "not_dispatched",
    "provider_prompt_completion",
    "provider_total",
    "missing",
    "request_uncertain",
)


async def _report_phase22_enum_anomalies(conn: AsyncConnection) -> dict:
    """只读扫描 ai_task_token_usage 的 usage_status / usage_source 取值分布。

    B-3 裁定：迁移与校验必须「识别、报告并安全处理」历史异常值，不得静默丢失数据。
    本函数只做 SELECT + WARNING 日志：异常行保留原值，不做任何 UPDATE / DELETE /
    归一化改写（是否需要人工处置由后续裁定），因此不会造成数据丢失。
    扫描失败（如表结构异常）不阻断迁移，仅记录 error 并继续。
    """
    report: dict = {
        "rows": None,
        "usage_status": {},
        "usage_source": {},
        "unexpected_usage_status": [],
        "unexpected_usage_source": [],
    }
    try:
        report["rows"] = int(
            (await conn.execute(text("SELECT COUNT(*) FROM ai_task_token_usage"))).scalar() or 0
        )
        for column, allowed in (
            ("usage_status", PHASE22_ALLOWED_USAGE_STATUS),
            ("usage_source", PHASE22_ALLOWED_USAGE_SOURCE),
        ):
            grouped = (
                await conn.execute(
                    text(
                        f"SELECT {column}, COUNT(*) FROM ai_task_token_usage "
                        f"GROUP BY {column}"
                    )
                )
            ).all()
            distribution = {
                ("" if row[0] is None else str(row[0])): int(row[1]) for row in grouped
            }
            unexpected = sorted(value for value in distribution if value not in allowed)
            report[column] = distribution
            report[f"unexpected_{column}"] = unexpected
            if unexpected:
                logger.warning(
                    "phase22 %s 存在 %d 个非允许值（仅报告，不改写不删除）：%s；"
                    "允许值=%s",
                    column,
                    len(unexpected),
                    unexpected,
                    list(allowed),
                )
    except Exception as exc:  # noqa: BLE001 扫描失败不阻断迁移
        report["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("phase22 枚举异常值扫描未完成（不阻断迁移）：%s", report["error"])
    return report


async def ensure_phase22_schema(conn: AsyncConnection, *, repair: bool = True) -> dict:
    """阶段 B-2「逐次 token 账目表」（phase22）表结构迁移（幂等，可重复执行）：

    1) 新建 ai_task_token_usage 表（19 列 + 唯一约束 uq_usage_task_attempt_call）；
    2) 建两个索引 idx_usage_status_settled / idx_usage_task_dispatched；
    3) 按 pragma_table_info 校验列齐全度，缺列才补列（本轮一次建齐）；
    4) 只读扫描 usage_status / usage_source 异常取值并 WARNING 报告（不改写数据）。

    repair 语义（A-4 裁定，生产入口接入时使用）：
      - repair=True（默认）：只增补缺失对象（建表 / 补列 / 建索引），不改既有数据；
        用于空库首启与迁移已执行（initialized / upgraded）的路径。
      - repair=False：只做只读兼容性检查，**不执行任何 DDL**；用于正式库已是最新
        版本（migrate 结果为 noop）的启动路径——异常枚举值仍会 WARNING 报告，
        不被静默吞掉，但绝不改写既有结构与数据。

    与 migrate.MIGRATION_STEPS[3] 共用同一份 DDL 常量（同源）；只增不改，
    ai_tasks 表零变更，不含任何 DML 回填。回滚：DROP TABLE ai_task_token_usage。

    返回：枚举异常值扫描报告（rows / unexpected_* / 可选 error）。
    """
    if repair:
        await conn.execute(text(_PHASE22_USAGE_TABLE_DDL))
        if "id" not in await _table_columns(conn, "ai_task_token_usage"):
            logger.warning(
                "phase22 ai_task_token_usage 缺少 id 主键列（疑似残留异常表），"
                "仅报告不重建、不清空"
            )
        await _add_missing_columns(conn, "ai_task_token_usage", _PHASE22_USAGE_COLUMNS)
        for statement in _PHASE22_USAGE_INDEX_DDL:
            await conn.execute(text(statement))
    report = await _report_phase22_enum_anomalies(conn)
    logger.info(
        "phase22 逐次 token 账目表迁移完成（repair=%s，rows=%s，异常 usage_source=%s，"
        "异常 usage_status=%s）",
        repair,
        report.get("rows"),
        report.get("unexpected_usage_source"),
        report.get("unexpected_usage_status"),
    )
    return report
