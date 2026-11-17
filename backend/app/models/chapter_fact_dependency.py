# -*- coding: utf-8 -*-
"""章节事实依赖模型：chapter_fact_dependencies 表（阶段H「依赖图增量回归」）。

每个章节在保存并成功提取结构化事实后，记录本章引用的事实键：
  chapter_id   ：所属章节（outlines.id）
  entity_type  ：依赖实体类型 character/location/item/secret/chapter_meta
  entity_id    ：实体 UUID（名字级兜底依赖时为 NULL）
  fact_key     ：规范化事实键，如 character:{id}:is_alive / item:name:{name}:possessed
  dep_snapshot ：该事实键对应的值摘要 JSON（供未来 diff，本阶段仅记录）

依赖图用途：变更章节 X 后，仅将「后续章节中与 X 依赖键有交集」的章节标记为
needs_recheck（增量回归）；若变更章无任何依赖记录（依赖未知），回退为标记全部
后续章节（兼容旧行为）。本表只读改写自事实提取结果，不触碰正文。
回滚方式：新表可清理（删除表或清空行），依赖未知自动回退旧行为。

建表口径（I-3a-fix-P19）
  本表的唯一建表来源是 app/core/db_migrations.py 的 PHASE19_MIGRATION_STATEMENTS
  （_CHAPTER_FACT_DEPENDENCIES_DDL + 三个索引）。为消除 create_all 与迁移 DDL 的
  签名漂移，本表不属于 Base.metadata：下方用独立 MetaData 显式 __table__ 声明，
  任何入口顺序下 create_all 都不会建它，只有 ensure_phase19_schema 会建。
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, MetaData, String, Table, Text, Uuid

from app.core.database import Base

# 依赖实体类型（与依赖键规范化一致）
DEP_ENTITY_TYPES = (
    "character",
    "location",
    "item",
    "secret",
    "timeline_event",
    "chapter_meta",
)

# phase19 独立元数据：仅承载 chapter_fact_dependencies，不注册进 Base.metadata。
phase19_metadata = MetaData()

# 列名/列序/类型/nullable 逐项对齐冻结的 phase19 DDL：
#   id/chapter_id/project_id -> CHAR(32)（Uuid(as_uuid=True) 在 SQLite 渲染 CHAR(32)）
#   entity_type VARCHAR(30)、entity_id CHAR(32) 可空、fact_key VARCHAR(255)、
#   dep_snapshot TEXT、created_at DATETIME 可空（DDL 未写 NOT NULL）。
# PK 显式 nullable=True 以匹配 SQLite PRAGMA notnull=0（DDL 为 "id CHAR(32) PRIMARY KEY"）。
chapter_fact_dependencies_table = Table(
    "chapter_fact_dependencies",
    phase19_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, nullable=True, default=uuid.uuid4),
    Column("chapter_id", Uuid(as_uuid=True), nullable=False),
    Column("project_id", Uuid(as_uuid=True), nullable=False),
    Column("entity_type", String(30), nullable=False),
    Column("entity_id", String(32), nullable=True),
    Column("fact_key", String(255), nullable=False),
    Column("dep_snapshot", Text, nullable=True),
    Column("created_at", DateTime, default=datetime.utcnow, nullable=True),
)

# 索引严格三定义，名称/列序与正式库一致；无单列 entity_type / entity_id / project_id 索引。
Index(
    "ix_chapter_fact_dependencies_chapter_id",
    chapter_fact_dependencies_table.c.chapter_id,
)
Index(
    "ix_chapter_fact_dependencies_lookup",
    chapter_fact_dependencies_table.c.entity_type,
    chapter_fact_dependencies_table.c.entity_id,
)
Index(
    "ix_chapter_fact_dependencies_fact_key",
    chapter_fact_dependencies_table.c.fact_key,
)


class ChapterFactDependency(Base):
    """章节事实依赖（表由 phase19 迁移创建，不参与 Base.metadata.create_all）。"""

    __table__ = chapter_fact_dependencies_table
