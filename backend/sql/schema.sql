-- ============================================================
-- NovelCraft 第一版数据库建表脚本（PostgreSQL）
-- 数据库名称：novelcraft
--
-- 建库方式（psql 或 pgAdmin 执行）：
--   psql -U postgres -c "CREATE DATABASE novelcraft;"
--   psql -U postgres -d novelcraft -f schema.sql
--
-- 表清单（共 7 张）：
--   1. users               用户表（普通用户/管理员）
--   2. projects            作品表（含作品设定集）
--   3. characters          人物表
--   4. outlines            大纲表（支持树形层级）
--   5. chapters            章节表
--   6. chapter_versions    章节版本表
--   7. generation_records  AI 生成记录表
-- ============================================================

-- 1. 用户表
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'user',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. 作品表
-- 作品设定集字段：题材(genre)、故事背景(background)、世界观规则(world_setting)、
-- 主线冲突(main_conflict)、结局方向(ending_direction)、叙事视角(narrative_perspective)、
-- 语言风格(writing_style)、禁用设定(forbidden_settings)；人物关系在 characters 表中维护
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    genre VARCHAR(100),
    background TEXT,
    world_setting TEXT,
    main_conflict TEXT,
    ending_direction TEXT,
    narrative_perspective VARCHAR(100),
    writing_style TEXT,
    forbidden_settings TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 作品表索引：按用户查询作品
CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);

-- 3. 人物表
CREATE TABLE IF NOT EXISTS characters (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    identity VARCHAR(255),
    personality TEXT,
    appearance TEXT,
    background TEXT,
    goal TEXT,
    conflict TEXT,
    relationship TEXT,
    character_status TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 人物表索引：按作品查询人物
CREATE INDEX IF NOT EXISTS idx_characters_project_id ON characters(project_id);

-- 4. 大纲表（parent_id 自引用支持树形层级）
-- outline_type 取值：
--   summary  故事梗概
--   volume   分卷大纲
--   chapter  章节大纲
--   scene    场景卡片
CREATE TABLE IF NOT EXISTS outlines (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES outlines(id) ON DELETE CASCADE,
    outline_type VARCHAR(30) NOT NULL,
    title VARCHAR(255),
    summary TEXT,
    content TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 大纲表索引：按作品查询、按父子关系查询、按类型排序
CREATE INDEX IF NOT EXISTS idx_outlines_project_id ON outlines(project_id);
CREATE INDEX IF NOT EXISTS idx_outlines_parent_id ON outlines(parent_id);

-- 5. 章节表
CREATE TABLE IF NOT EXISTS chapters (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    outline_id UUID REFERENCES outlines(id) ON DELETE SET NULL,
    chapter_number INTEGER NOT NULL,
    title VARCHAR(255),
    content TEXT NOT NULL DEFAULT '',
    summary TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'draft',
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, chapter_number)
);

-- 章节表索引：按作品查询章节
CREATE INDEX IF NOT EXISTS idx_chapters_project_id ON chapters(project_id);

-- 6. 章节版本表（支持版本管理与 AI 初稿/人工修改稿/最终稿标记）
-- version_type 取值：
--   initial     初始版本
--   ai_draft    AI 初稿
--   user_edit   人工修改稿
--   diagnosed   诊断后版本
--   polished    润色版本
--   final       最终版本
CREATE TABLE IF NOT EXISTS chapter_versions (
    id UUID PRIMARY KEY,
    chapter_id UUID NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    version_type VARCHAR(30) NOT NULL,
    change_note TEXT,
    created_by VARCHAR(20) NOT NULL DEFAULT 'user',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 章节版本表索引：按章节查询版本
CREATE INDEX IF NOT EXISTS idx_chapter_versions_chapter_id ON chapter_versions(chapter_id);

-- 7. AI 生成记录表
CREATE TABLE IF NOT EXISTS generation_records (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    chapter_id UUID REFERENCES chapters(id) ON DELETE SET NULL,
    generation_type VARCHAR(50) NOT NULL,
    prompt TEXT NOT NULL,
    result TEXT,
    model_name VARCHAR(100),
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 生成记录表索引：按用户/作品/章节查询生成记录
CREATE INDEX IF NOT EXISTS idx_generation_records_user_id ON generation_records(user_id);
CREATE INDEX IF NOT EXISTS idx_generation_records_project_id ON generation_records(project_id);
CREATE INDEX IF NOT EXISTS idx_generation_records_chapter_id ON generation_records(chapter_id);
