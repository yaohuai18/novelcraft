# NovelCraft 有状态生成改造 — 第一批设计（事实数据库 + 章节状态 + 规则校验）

日期：2026-10-14
范围：第一批。目标：把"不能随意变化的内容"从自然语言设定/章节正文中提取为结构化事实，每章结束写入状态快照，并用确定性规则拦截年龄/时间线/生死/地点/物品归属类冲突。
后续批次（场景合同、独立连续性审稿、质量闸门、全书回归审计）在本批预留接口与字段，不在本批实现。

## 一、设计原则

1. 复用现有模块，不推倒重建：`characters`、`character_state_snapshot`、`timeline_events`、`foreshadows`、`locations`、`world_rules`、`state_extraction_service`、`consistency_checker` 已存在，本批负责补全结构、串联管线、加确定性校验层。
2. 原始正文只读：本批不修改已有正文；只新增/扩展表与字段（迁移带默认值，幂等）。
3. 校验器分层：确定性规则（年龄/时间/生死/地点/归属）由代码直接判定，不交给 LLM"感觉"。
4. 结果可追溯：每次校验输出结构化问题清单（severity/category/quote/violated_fact_id/reason/repair_instruction），与章节、事实变更关联。

## 二、事实数据库（表结构）

### 2.1 人物（复用 characters，扩展 character_state_snapshots）
- `character_state_snapshots` 扩展字段（新增，均带默认值）：
  - `age` INT NULL、`birth_year` INT NULL
  - `is_alive` BOOLEAN DEFAULT TRUE
  - `location_id` UUID NULL（外键 locations.id）
  - `possessions` TEXT（JSON 数组，如 ["铜牌"]）
  - `known_secrets` TEXT（JSON 数组，secret 标识列表）
  - `injuries` TEXT（JSON 数组）
  - `snapshot_chapter_id` UUID NULL（该快照产生于哪一章）
- 每章结束后按章节写入一条快照（同一章内可有多条变更记录，取最新）。

### 2.2 事件（复用 timeline_events，扩展）
- 新增字段：
  - `absolute_time` TEXT NULL（如 "景和三年三月初二"）
  - `relative_time` TEXT NULL（如 "第1章后第三天"）
  - `participant_ids` TEXT（JSON 数组，参与人 character_id）
  - `cause_event_id` UUID NULL（因果前置）
  - `result` TEXT NULL（结果描述）
  - `is_public` BOOLEAN DEFAULT TRUE（是否已公开）

### 2.3 地点（复用 locations，扩展）
- 新增字段：
  - `parent_location_id` UUID NULL（地理父级）
  - `travel_hours_from` TEXT（JSON：{"<location_id>": 小时数}，通行时间）

### 2.4 物品（新增表）
- `items`：id、project_id、name、first_chapter_id、description、status（intact/damaged/consumed/lost）、created_at
- `item_possessions`：id、item_id、owner_character_id、since_chapter_id、until_chapter_id（NULL=至今）、status、created_at

### 2.5 秘密（复用 foreshadows，扩展知情表）
- 新增表 `secret_knowers`：id、secret_id（外键 foreshadows.id）、character_id、learned_chapter_id、created_at
- 预留字段 `reveal_chapter_id`、`forbidden_before_chapter_id`（本批只建字段，不做强制闸门）

### 2.6 章节元数据（扩展 chapters）
- 新增字段：
  - `chapter_time` TEXT NULL（绝对/相对时间表述）
  - `location_id` UUID NULL
  - `present_characters` TEXT（JSON 数组）
  - `chapter_goal` TEXT NULL
  - `chapter_outcome` TEXT NULL
  - `state_facts_updated_at` DATETIME NULL

## 三、章节状态写入管线

生成/保存章节完成后（沿用现有 `_run_post_save_ai_triggers` 后台任务模式）：
1. `state_extraction_service.extract_state_facts(chapter_id)`：用 LLM 按固定 JSON schema 从章节正文提取事实变更（人物状态、事件、地点移动、物品转移、秘密揭示），禁止改动正文。
2. 结构化落库：写 `character_state_snapshots`、`timeline_events`（补全扩展字段）、`item_possessions`、`secret_knowers`、`chapters` 元数据。幂等：同一章重复提取时先删该章已有快照再写，避免重复。
3. 失败兜底：提取失败不阻断正文保存，状态字段留空，记 audit_log。

## 四、规则校验器（确定性，新增 service: fact_validator）

输入：chapter_id（或项目当前状态栈）。输出：问题清单 `[{severity, category, quote, violated_fact_id, reason, repair_instruction}]`。

规则（第一批，代码实现，不调模型）：
1. timeline：出生年/年龄一致性（age == now_year - birth_year 或标注模糊）；事件时间先后（cause 早于 effect）；行程时间（同一人物在相邻事件中两地通行时间 >= travel_hours_from）。
2. state：死亡角色不得出现在后续章节 present_characters；同一时刻同一人物只能在一个地点（瞬移检测）。
3. possession：物品持有人一致性（until_chapter_id 后不得再持有；同一物品同一时间单一持有人）。
4. secret：角色不得在 learned_chapter_id 之前"知道"该秘密（正文关键词级粗检，标记 needs_review 不阻断）。
5. chapter_meta：章节时间/地点/登场人物与前后章衔接检查（时间倒退、地点瞬移、人物突然消失）。

分级：
- critical：年龄不可能、死亡重现、时间倒退、地点瞬移、物品归属冲突
- major：行程时间不足、事件因果倒置
- minor：秘密提前出现（需人工判断）、章节时间模糊

## 五、接口与数据流

- `POST /api/projects/{pid}/chapters/{oid}/validate`：对单章运行校验，返回问题清单（只读，不修改）。
- `GET /api/projects/{pid}/facts`：返回项目事实视图（人物当前状态、事件时间线、物品归属、秘密知情）。
- 生成/保存链路内：保存 → 状态提取 → 快照落库 → 规则校验 → 结果随响应附 `validation_summary`（不阻断保存，问题进 audit/前端提示）。
- 现有 `consistency_checker`（LLM 式）保留，作为后续"连续性审稿"批次基础，本批不改。

## 六、迁移与兼容

- 全部新列/新表带默认值；`_add_missing_columns` 模式幂等迁移；旧数据读取不报错。
- 不修改任何既有正文与既有表删除列。

## 七、测试（backend/temp/test_fact_validator.py）

- 年龄冲突（陈九 7 岁却十二年前认罪）→ critical timeline
- 死亡角色重现 → critical state
- 同一人物两地点瞬移 → critical location
- 行程时间不足 → major
- 物品双持有人 → critical possession
- 秘密提前知情 → minor needs_review
- 状态提取幂等：同一章重复提取不产生重复快照
- 旧数据无新字段时接口不报错

## 八、后续批次预留（本批不实现）

- 场景合同（scene contract）生成前锁定
- 独立连续性审稿模型（编辑职责分离）
- 质量闸门状态流（draft → … → published）
- 补丁式修改与全书回归审计（依赖标记字段：可在 facts 表预留 `depends_on_fact_id` 概念，本批不做表）
