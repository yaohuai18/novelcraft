// NovelCraft 前端 API 封装（对接真实后端 /api，接口路径经后端 routers 核实）
const TOKEN_KEY = "novelcraft_token";
const USER_KEY = "novelcraft_user";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code = "") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function setUser(user: unknown): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function getUser(): { username?: string; email?: string; role?: string } | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export interface Project {
  id: string;
  title: string;
  deleted_at?: string | null;
  deleted_by?: string | null;
  genre?: string | null;
  background?: string | null;
  world_setting?: string | null;
  main_conflict?: string | null;
  ending_direction?: string | null;
  narrative_perspective?: string | null;
  writing_style?: string | null;
  forbidden_settings?: string | null;
  status: string;
  subtitle?: string | null;
  description?: string | null;
  cover_text?: string | null;
  project_slug?: string | null;
  publication_status: string;
  published_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface OutlineNode {
  id: string;
  project_id: string;
  parent_id?: string | null;
  title: string;
  outline_type: string;
  summary?: string | null;
  content?: string | null;
  sort_order: number;
  status: string;
  visibility: string;
  chapter_slug?: string | null;
  published_at?: string | null;
}

export interface ChapterContent {
  outline_id: string;
  content: string;
  word_count: number;
  version: number;
}

export interface WorldSetting {
  id: string;
  project_id: string;
  category: string;
  name: string;
  content: string;
  importance: string;
}

export interface PublishResult {
  project_id: string;
  status: string;
  project_slug?: string | null;
  published_at?: string | null;
  message: string;
}

export interface PreviewProject {
  project_id: string;
  title: string;
  subtitle?: string | null;
  description?: string | null;
  cover_text?: string | null;
  genre?: string | null;
  publication_status: string;
  slug?: string | null;
  preview: boolean;
}

export interface LocaleInfo {
  user_id?: string;
  ui_language?: string | null;
  content_language?: string | null;
  notification_language?: string | null;
  ai_output_language?: string | null;
  timezone?: string | null;
  locale?: string | null;
  date_format?: string | null;
  time_format?: string | null;
  region?: string | null;
}

export interface NotificationPref {
  user_id: string;
  notification_type: string;
  in_app_enabled: boolean;
  email_enabled: boolean;
  push_enabled: boolean;
}

export interface MeInfo {
  id: string;
  username: string;
  email: string;
  role: string;
  created_at?: string;
}

export interface AIResult {
  content: string;
  /** 第一期「生成不丢失」：AI 返回后服务端已写 generation_record，携带其 ID 供「接受为新版本」回传 */
  generation_record_id?: string | null;
}

// ---- 章节计划（ChapterPlan）与计划符合度检查 ----
export interface ChapterPlanData {
  title?: string | null;
  summary?: string | null;
  sceneGoals?: string[];
  requiredCharacters?: string[];
  requiredLocations?: string[];
  requiredEvents?: string[];
  expectedOutcome?: string | null;
  forbiddenReveals?: string[];
  status?: string;
}

export interface ChapterPlanResponse {
  projectId: string;
  chapterId: string;
  chapterNumber: number;
  title: string;
  summary?: string | null;
  plan: ChapterPlanData | null;
  planSource: "manual" | "legacy" | "none";
  titleSyncRequired: boolean;
}

export interface PlanComplianceIssue {
  severity: "error" | "warning" | "info";
  type: string;
  message: string;
  evidence?: string | null;
  suggestion?: string | null;
}

export interface PlanComplianceResponse {
  projectId: string;
  chapterId: string;
  chapterNumber: number;
  title: string;
  score: number;
  issues: PlanComplianceIssue[];
  planSummary?: Record<string, unknown> | null;
  checkedAt: string;
}

// ---- 第一期「生成不丢失」：生成记录 / 章节版本 / 导出 ----
export interface GenerationRecord {
  id: string;
  project_id: string | null;
  volume_id?: string | null;
  chapter_id?: string | null;
  scene_id?: string | null;
  action_type: string; // outline / draft / continue / rewrite / polish / summarize / extract_state / check
  model_provider: string | null;
  model_name: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  system_prompt: string;
  user_prompt: string;
  context_snapshot: string | null;
  response_content: string;
  input_tokens: number | null;
  output_tokens: number | null;
  status: string; // pending / succeeded / failed / cancelled
  error_message: string | null;
  created_at: string;
  context?: Record<string, unknown> | null;
  // 「已确认大纲版本化」追溯字段（历史记录可为 null，前端回退旧展示）
  outline_plan_version_id?: string | null;
  chapter_plan_id?: string | null;
  chapter_plan_snapshot?: string | null;
  title_snapshot?: string | null;
}

export interface GenerationRecordList {
  items: GenerationRecord[];
  total: number;
  page: number;
  page_size: number;
}

// ---- 「已确认大纲版本化」：OutlinePlanVersion 相关类型 ----
export interface OutlinePlanVersion {
  id: string;
  project_id: string;
  version: number;
  status: "draft" | "confirmed" | "superseded";
  confirmed_at: string | null;
  confirmed_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  content_summary: {
    projectTitle: string;
    volumeCount: number;
    chapterCount: number;
  };
}

export interface OutlinePlanVersionDetail extends OutlinePlanVersion {
  content_snapshot: Record<string, unknown> | null;
  chapter_plans_snapshot: Record<string, unknown> | null;
}

export interface OutlinePlanStatus {
  projectId: string;
  hasConfirmed: boolean;
  activeConfirmed: OutlinePlanVersion | null;
  latestDraft: OutlinePlanVersion | null;
  hasUnconfirmedChanges: boolean;
  versionCount: number;
}

export interface ConfirmPreviewItem {
  chapterId: string;
  chapterNumber: number;
  planTitle: string;
  outlineTitle: string;
  titleConflict: boolean;
  titleChanged: boolean;
  hasWrittenContent: boolean;
  followingPlan: boolean;
}

export interface ConfirmOutlinePlanResult {
  version: OutlinePlanVersionDetail;
  superseded_version: string | null;
  impact: ConfirmPreviewItem[];
  titleConflicts: ConfirmPreviewItem[];
  writtenChaptersAffected: ConfirmPreviewItem[];
  dryRun: boolean;
  unchanged?: boolean;
}

/** 请求级模型覆盖参数（随各 AI 生成请求发送，不包含密钥） */
export interface ModelOverrides {
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
}

export interface ChapterVersion {
  id: string;
  outline_id: string;
  version: number;
  content: string;
  word_count: number;
  source: string; // manual / ai_draft / ai_continue / ai_rewrite / ai_polish / restore
  generation_record_id?: string | null;
  parent_version_id?: string | null;
  is_current: boolean;
  change_summary?: string | null;
  created_at: string;
}

export interface ExportTask {
  id: string;
  user_id: string;
  project_id: string;
  export_type?: string | null;
  scope: string;
  export_format: string;
  options?: string | null;
  status: string; // pending / processing / completed / failed / cancelled
  progress: number;
  content?: string | null;
  content_size?: number | null;
  result_token?: string | null;
  expires_at?: string | null;
  error_message?: string | null;
  created_at: string;
  completed_at?: string | null;
}

export interface ExportResult {
  task_id: string;
  project_id: string;
  format: string;
  scope: string;
  content: string;
  content_size: number;
  expires_at?: string | null;
}

export interface ExportOptionsInput {
  include_drafts?: boolean;
  include_private_notes?: boolean;
  include_comments?: boolean;
  include_archived?: boolean;
  include_ai_records?: boolean;
  include_version_history?: boolean;
  include_chapter_numbers?: boolean;
  include_outline_summaries?: boolean;
  include_tags?: boolean;
  include_characters?: boolean;
  include_toc?: boolean;
  preserve_markdown?: boolean;
}

export interface ExportCreateInput {
  export_format: string; // text / markdown / html / json / csv
  scope: string; // whole_project / volume / chapter / characters / locations / world / timeline / tags / comments / version_history
  options?: ExportOptionsInput;
  outline_id?: string | null;
  chapter_id?: string | null;
}

// ---- AI 小说自动生成 ----
export interface NovelGenerateRequest {
  novel_type?: string;
  genre?: string;
  title: string;
  world_view?: string;
  era?: string;
  characters?: string;
  relationships?: string;
  main_plot?: string;
  chapter_count: number;
  words_per_chapter: number;
  perspective?: string;
  writing_style?: string;
  requirements?: string;
  outline?: string;
  continue_from?: string;
  locked?: string[];
  requested_mode?: "auto" | "mock" | "real";
  /** 请求级模型覆盖（第六阶段：模型切换入口） */
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
}

export interface NovelChapter {
  index: number;
  title: string;
  content: string;
}

export interface NovelGenerateResponse {
  mode: "mock" | "real";
  chapters: NovelChapter[];
  message: string;
}

export interface NovelStreamEvent {
  type: "start" | "status" | "progress" | "chapter" | "done" | "error" | "cancelled";
  mode?: "mock" | "real";
  total?: number;
  index?: number;
  title?: string;
  content?: string;
  message?: string;
  code?: string;
  request_id?: string;
  elapsed?: number;
  word_count?: number;
}

export interface AiStatus {
  configured: boolean;
  mode: "mock" | "real";
  base_url: string;
  model: string;
}

/** 第六阶段：模型切换入口 —— GET /api/ai/config 返回（绝不含任何 API Key） */
export interface AiProviderInfo {
  provider: string;
  label: string;
  base_url: string;
  models: string[];
  support_reasoning: boolean;
}

export interface AiConfigResponse {
  configured: boolean;
  mode: "mock" | "real";
  provider: string;
  model: string;
  temperature: number;
  max_tokens: number;
  base_url: string;
  providers: AiProviderInfo[];
  reasoning_models: string[];
}

export interface OutlineFirstResponse {
  mode: "mock" | "real";
  outline: string;
  chapter: NovelChapter;
  message: string;
}

export interface NextChapterRequest {
  setting: NovelGenerateRequest;
  outline?: string;
  previous_summary?: string;
  character_state?: string;
  chapter_index?: number;
  previous_chapter?: string;
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
  // 第二期「章节能被记住」：携带后走 Context Builder（已确认大纲版本）
  project_id?: string;
  chapter_id?: string;
}

export interface ChapterEditRequest {
  setting: NovelGenerateRequest;
  chapter_title?: string;
  chapter_content?: string;
  instruction?: string;
  style?: string;
  chapter_index?: number;
  outline?: string;
  previous_chapter_tail?: string;
  next_chapter_head?: string;
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
}

export interface ChapterEditResponse {
  mode: "mock" | "real";
  chapter: NovelChapter;
  message: string;
}

// ---- 批次 2：创作页轻量一致性校验（保存前 4 维规则检查，零 AI 成本）----
export interface QuickCheckIssue {
  severity: string;
  type: string;
  location: string;
  message: string;
  suggestion: string;
  data?: Record<string, unknown>;
}

export interface QuickCheckRequest {
  plan?: Record<string, unknown> | null;
  chapters?: { index?: number; title?: string; content?: string }[];
  rules?: string[];
}

export interface QuickCheckResponse {
  issues: QuickCheckIssue[];
  dimensions: number;
  checked_at: string;
  message: string;
}

// ---- 批次 4：追读力报告 + token 用量面板 ----
export interface ReadabilityReport {
  project_id: string;
  project_title: string;
  chapters_total: number;
  hook_coverage: {
    chapters_with_hook: number;
    rate: number;
    without_hook: string[];
  };
  thrill_density: {
    total_hits: number;
    per_1000_chars: number;
    top_chapters: { title: string; hits: number; chars: number }[];
  };
  foreshadow_debt: {
    open_count: number;
    items: { title: string; status: string; importance: string; planned_payoff: string }[];
  };
  checked_at: string;
}

export interface UsageSummary {
  days: number;
  since: string;
  total_input_tokens: number;
  total_output_tokens: number;
  records: number;
  by_day: { date: string; input: number; output: number; records: number }[];
  by_model: { model: string; input: number; output: number }[];
  by_project: { project_id: string; title: string; input: number; output: number }[];
}

// ---- 第二期「章节能被记住」：章节记忆 / 项目级设置 ----
export interface ChapterMemoryData {
  id: string;
  chapter_id: string;
  project_id: string;
  summary: string;
  memory: {
    timeline?: { relative_day?: string; location?: string[] };
    characters_present?: string[];
    character_updates?: { character?: string; changes?: string[] }[];
    foreshadows_added?: { content?: string; planned_payoff?: string }[];
    open_threads?: string[];
    important_items?: string[];
    next_chapter_hint?: string;
  };
  source: "ai" | "manual";
  status: "draft" | "confirmed";
  created_at: string;
  updated_at: string;
}

export interface ChapterMemoryConfirmResult {
  id: string;
  chapter_id: string;
  status: "draft" | "confirmed";
}

export interface ProjectSettingsData {
  project_id: string;
  auto_summarize: boolean;
  auto_check: boolean;
  character_confirm: "require" | "auto";
  foreshadow_confirm: "require" | "auto";
  timeline_confirm: "require" | "auto";
  updated_at: string;
}

// ---- 第三期「人物和时间不漂」：类型定义 ----
export interface CharacterData {
  id: string;
  project_id: string;
  name: string;
  aliases: string[];
  role: "主角" | "配角" | "反派" | "路人" | string;
  status: "在世" | "死亡" | "失踪" | "未知" | string;
  profile?: string | null;
  goals?: string | null;
  secrets?: string | null;
  abilities?: string | null;
  current_location?: string | null;
  current_timeline_status?: string | null;
  first_chapter_id?: string | null;
  last_chapter_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CharacterInput {
  name: string;
  aliases?: string[];
  role?: string;
  status?: string;
  profile?: string;
  goals?: string;
  secrets?: string;
  abilities?: string;
  current_location?: string;
  current_timeline_status?: string;
  first_chapter_id?: string | null;
  last_chapter_id?: string | null;
}

export interface CharacterSnapshotData {
  id: string;
  character_id: string;
  chapter_id?: string | null;
  state_json: Record<string, unknown>;
  source: "ai_extracted" | "user_confirmed" | "manual";
  status?: "draft" | "confirmed" | "rejected" | string;
  confirmed_at?: string | null;
  created_at: string;
}

export interface CharacterRelationshipData {
  id: string;
  project_id: string;
  source_character_id: string;
  target_character_id: string;
  relation_type: string;
  description?: string | null;
  start_chapter_id?: string | null;
  end_chapter_id?: string | null;
  status: "draft" | "confirmed" | "archived";
  created_at: string;
  updated_at: string;
}

export interface CharacterRelationshipInput {
  source_character_id: string;
  target_character_id: string;
  relation_type: string;
  description?: string;
  start_chapter_id?: string | null;
  end_chapter_id?: string | null;
  status?: "draft" | "confirmed" | "archived";
}

export interface TimelineEventData {
  id: string;
  project_id: string;
  chapter_id?: string | null;
  event_date_label?: string | null;
  time_label?: string | null;
  sequence_order?: number | null;
  location_id?: string | null;
  participants: string[];
  title: string;
  description?: string | null;
  source: "manual" | "ai_extracted";
  confirmed: boolean;
  status?: "draft" | "confirmed" | "archived" | string;
  created_at: string;
  updated_at: string;
}

export interface TimelineEventInput {
  chapter_id?: string | null;
  event_date_label?: string;
  time_label?: string;
  sequence_order?: number | null;
  location_id?: string | null;
  participants?: string[];
  title: string;
  description?: string;
  source?: "manual" | "ai_extracted";
  confirmed?: boolean;
  status?: "draft" | "confirmed" | "archived" | string;
}

export interface SuggestionData {
  id: string;
  project_id: string;
  chapter_id?: string | null;
  category: "character_state" | "relationship" | "timeline" | "foreshadow" | string;
  character_id?: string | null;
  target_character_id?: string | null;
  title: string;
  description?: string | null;
  payload: Record<string, unknown>;
  status: "pending" | "confirmed" | "modified" | "ignored";
  created_at: string;
  updated_at: string;
}

// ---- AI 规划（灵感 → 规划 → 锁定 → 逐章写作）----
export interface NovelChapterPlan {
  index: number;
  title: string;
  goal: string;
  conflict: string;
  key_events: string;
  cliffhanger: string;
}

export interface NovelVolumePlan {
  volume_index: number;
  title: string;
  summary: string;
  chapters: NovelChapterPlan[];
}

export interface CharacterProfile {
  name: string;
  role: string;
  description: string;
}

export interface ForeshadowPlan {
  item: string;
  planted_chapter: number;
  payoff_chapter: number;
  note: string;
}

export interface NovelPlan {
  title_candidates: string[];
  selling_point: string;
  synopsis: string;
  world_view: string;
  era: string;
  core_conflict: string;
  protagonist: string;
  supporting_characters: CharacterProfile[];
  relationships: string;
  main_plot: string;
  volumes: NovelVolumePlan[];
  foreshadowing: ForeshadowPlan[];
  ending_type: string;
}

export interface PlanNovelRequest {
  idea: string;
  genre?: string;
  writing_style?: string;
  perspective?: string;
  chapter_count?: number;
  words_per_chapter?: number;
  ending_type?: string;
  has_romance?: boolean;
  content_restrictions?: string;
  must_keep_settings?: string;
  existing_settings?: Record<string, unknown> | null;
  requested_mode?: "auto" | "mock" | "real";
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
}

export interface PlanNovelResponse {
  mode: "mock" | "real";
  plan: NovelPlan;
  message: string;
}

/** 整本规划后台作业：提交后立即返回 job_id，前端轮询取结果。 */
export interface PlanNovelJobSubmitResponse {
  job_id: string;
  status: string;
  mode: "mock" | "real";
  poll_url: string;
  /** 命中幂等键/同作用域活跃任务时复用已有作业 */
  reused?: boolean;
  idempotency_key?: string | null;
}

export type PlanNovelJobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancel_requested"
  | "cancelled"
  | "interrupted"
  | string;

/**
 * 晚到结果的独立状态轴（2026-11-12 修正）：
 * status 只表示任务如何结束（interrupted / cancelled ...），
 * 晚到的成功结果用 result_review_status 表达，绝不覆盖 status。
 */
export type PlanResultReviewStatus =
  | "none"
  | "pending_review"
  | "accepted"
  | "discarded";

export interface PlanNovelJobStatus {
  job_id: string;
  status: PlanNovelJobState;
  mode: "mock" | "real";
  progress: number;
  elapsed_seconds: number | null;
  error_code: string | null;
  error_message: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  /** 第二批新增：执行者/租约/重试信息（旧后端不返回时均可选） */
  finished_at?: string | null;
  heartbeat_at?: string | null;
  lease_expires_at?: string | null;
  attempt_count?: number;
  max_attempts?: number;
  can_retry?: boolean;
  can_cancel?: boolean;
  /** 晚到结果：独立状态轴，pending_review 表示有待人工处置的成功结果 */
  result_review_status?: PlanResultReviewStatus;
  late_result_reason?: string | null;
  late_result_at?: string | null;
  has_late_result?: boolean;
  result_source?: "late_result";
  /** 兼容镜像（老逻辑只认这个布尔值） */
  result_pending_review?: boolean;
  result_conflict_reason?: string | null;
  idempotency_key?: string | null;
  message?: string;
  plan?: NovelPlan;
}

export interface PlanUpdateRequest {
  instruction: string;
  current_plan: NovelPlan;
  locked?: string[];
  requested_mode?: "auto" | "mock" | "real";
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
}

export interface PlanUpdateResponse {
  mode: "mock" | "real";
  plan: NovelPlan;
  changed_chapter_ids: number[];
  message: string;
}

// ---- 第四期「写错能发现」：一致性检查 / 伏笔管理 类型定义 ----
export interface ConsistencyIssue {
  severity: "warning" | "error" | string;
  type: string;
  location: string;
  message: string;
  suggestion: string;
  data?: Record<string, unknown>;
}

export interface ConsistencyUpdate {
  category: "character_state" | "relationship" | "timeline" | "foreshadow" | string;
  title: string;
  description: string;
  payload: Record<string, unknown>;
}

export interface ConsistencyReminder {
  type: "foreshadow_due" | "foreshadow_open" | string;
  content: string;
  planned_payoff: string;
  status: string;
  message: string;
  suggestion: string;
  id?: string | null;
}

export interface OutlineCompletion {
  completed: string[];
  uncovered: string[];
  score: number;
}

export interface RepetitionReport {
  internal: ConsistencyIssue[];
  against_previous_summary: ConsistencyIssue[];
}

export interface StyleReport {
  score: number;
  evaluation: string;
}

export interface CheckConsistencyResponse {
  mode: "mock" | "real" | string;
  score: number;
  issues: ConsistencyIssue[];
  confirmed_updates: ConsistencyUpdate[];
  proposed_updates: ConsistencyUpdate[];
  reminders: ConsistencyReminder[];
  outline: OutlineCompletion;
  repetition: RepetitionReport;
  style: StyleReport;
  checked_at: string;
}

export interface ConsistencyCheckRecord {
  id: string;
  chapter_id: string;
  chapter_title: string;
  action_type: string;
  status: string;
  result: Record<string, unknown>;
  created_at: string;
}

// ---- 文26 P4「一致性检查」：静态数据层检查类型 ----
export interface DataConsistencyIssue {
  id: string;
  dimension:
    | "dangling_refs"
    | "character_state"
    | "relationships"
    | "foreshadows"
    | "appearance"
    | "memory"
    | string;
  type: string;
  severity: "error" | "warning" | "info" | string;
  location: string;
  message: string;
  suggestion: string;
  related_ids: string[];
}

export interface DataConsistencySummary {
  total: number;
  errors: number;
  warnings: number;
  info: number;
  by_type: Record<string, number>;
}

export interface DataConsistencyRunResponse {
  check_id: string;
  project_id: string;
  dimension: string;
  status: string;
  mode: string;
  summary: DataConsistencySummary;
  issues: DataConsistencyIssue[];
  created_at: string;
}

export interface ForeshadowData {
  id: string;
  project_id: string;
  title?: string | null;
  content: string;
  importance?: string | null;
  planted_chapter_id?: string | null;
  planned_payoff_chapter_id?: string | null;
  planned_payoff?: string | null;
  resolved_chapter_id?: string | null;
  status: "planned" | "planted" | "resolved" | "abandoned";
  source: string;
  note?: string | null;
  paid_off_chapter_id?: string | null;
  paid_off_note?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ForeshadowInput {
  title?: string | null;
  content: string;
  importance?: string | null;
  planted_chapter_id?: string | null;
  planned_payoff_chapter_id?: string | null;
  planned_payoff?: string | null;
  resolved_chapter_id?: string | null;
  status?: "planned" | "planted" | "resolved" | "abandoned";
  source?: string;
  note?: string | null;
}

export interface ForeshadowImportResult {
  imported: number;
  skipped_duplicates: number;
  items: ForeshadowData[];
}

// ---- P1 AI 任务中心：统一任务接入（POST /api/projects/{pid}/ai/tasks）----
export interface AITask {
  id: string;
  project_id: string;
  outline_id?: string | null;
  task_type: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "timeout" | string;
  prompt?: string | null;
  result?: string | null;
  error_message?: string | null;
  retry_count: number;
  model_name?: string | null;
  prompt_version?: string | null;
  idempotency_key?: string | null;
  request_snapshot?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  timeout_at?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface AITaskList {
  items: AITask[];
  total: number;
}

export interface AITaskCreateResult {
  task_id: string;
  status: string;
  duplicated?: boolean;
}

export const AI_TASK_TYPE_LABEL: Record<string, string> = {
  generate_summary: "生成摘要",
  generate_volumes: "生成分卷",
  generate_chapters: "生成章节",
  continue: "续写正文",
  rewrite: "重写正文",
  polish: "润色正文",
  consistency_check: "一致性检查",
  generate_suggestions: "生成变更建议",
};

async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) || {}),
  };
  const token = getToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  try {
    const res = await fetch(path, { ...options, headers, signal: controller.signal });
    if (res.status === 401) {
      clearToken();
      throw new ApiError("登录已过期，请重新登录", 401);
    }
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      let code = "";
      try {
        const data = await res.json();
        const detail =
        typeof data.detail === "string"
          ? data.detail
          : data.detail?.message || data.detail?.detail;
        // 保留后端错误码（如 generation_record_chapter_mismatch），前端据此分流提示
        code =
          (typeof data.detail === "object" &&
            data.detail &&
            typeof data.detail.code === "string" &&
            data.detail.code) ||
          data.error?.code ||
          "";
        msg = detail || data.error?.message || msg;
        // 422 validation_failed：透出具体字段名，避免「请求参数校验失败」无法定位
        if (data.error?.code === "validation_failed" && Array.isArray(data.error.details) && data.error.details.length > 0) {
          const d = data.error.details[0];
          const loc = (d.loc || []).filter((p: unknown) => p !== "body").join(".");
          msg = `请求参数校验失败：${loc || "请求体"} — ${d.msg || ""}`;
        }
        // FastAPI 原生 422：{ detail: [{ loc, msg, type }] }
        if (res.status === 422 && Array.isArray(data.detail) && data.detail.length > 0) {
          const d = data.detail[0];
          const loc = (d.loc || []).filter((p: unknown) => p !== "body").join(".");
          msg = `请求参数校验失败：${loc || "请求体"} — ${d.msg || ""}`;
        }
      } catch {
        /* ignore */
      }
      throw new ApiError(msg, res.status, code);
    }
    return res.status === 204 ? (undefined as unknown as T) : res.json();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError("请求超时，请检查服务是否可用", 0);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  // 认证
  register(data: { username: string; email: string; password: string }) {
    return request("/api/auth/register", { method: "POST", body: JSON.stringify(data) });
  },
  login(data: { username: string; password: string }) {
    return request<{ access_token: string; token_type: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  me() {
    return request<MeInfo>("/api/auth/me");
  },

  // 作品
  listProjects() {
    return request<Project[]>("/api/projects");
  },
  createProject(data: {
    title: string;
    genre?: string;
    background?: string;
    world_setting?: string;
    main_conflict?: string;
    ending_direction?: string;
    narrative_perspective?: string;
    writing_style?: string;
    forbidden_settings?: string;
  }, timeoutMs = 30000) {
    return request<Project>("/api/projects", { method: "POST", body: JSON.stringify(data) }, timeoutMs);
  },
  getProject(projectId: string) {
    return request<Project>(`/api/projects/${projectId}`);
  },
  updateProject(projectId: string, data: Record<string, unknown>) {
    return request<Project>(`/api/projects/${projectId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteProject(projectId: string) {
    return request<void>(`/api/projects/${projectId}`, { method: "DELETE" });
  },
  listTrashProjects() {
    return request<Project[]>("/api/projects/trash");
  },
  restoreProjectFromTrash(projectId: string) {
    return request<Project>(`/api/projects/${projectId}/restore`, { method: "POST" });
  },
  purgeProject(projectId: string) {
    return request<void>(`/api/projects/${projectId}/purge`, { method: "DELETE" });
  },

  // AI 小说生成
  generateNovel(data: NovelGenerateRequest, timeoutMs = 600000) {
    return request<NovelGenerateResponse>("/api/ai/generate-novel", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  getAiStatus(timeoutMs = 15000) {
    return request<AiStatus>("/api/ai/status", {}, timeoutMs);
  },
  getAiConfig(timeoutMs = 15000) {
    return request<AiConfigResponse>("/api/ai/config", {}, timeoutMs);
  },
  // 2026-11-16：思考模型单次生成可达 3~7 分钟，超时提到 15 分钟（实测 444 秒才返回 200）
  generateOutlineFirst(data: NovelGenerateRequest, timeoutMs = 900000) {
    return request<OutlineFirstResponse>("/api/ai/generate-outline-first", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  generateNextChapter(data: NextChapterRequest, timeoutMs = 600000) {
    return request<OutlineFirstResponse>("/api/ai/generate-next-chapter", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  regenerateChapter(data: ChapterEditRequest, timeoutMs = 600000) {
    return request<ChapterEditResponse>("/api/ai/regenerate-chapter", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  expandChapter(data: ChapterEditRequest, timeoutMs = 600000) {
    return request<ChapterEditResponse>("/api/ai/expand-chapter", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  polishChapter(data: ChapterEditRequest, timeoutMs = 600000) {
    return request<ChapterEditResponse>("/api/ai/polish-chapter", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  generateNovelStream(
    data: NovelGenerateRequest,
    onEvent: (ev: NovelStreamEvent) => void
  ): { cancel: () => void } {
    const controller = new AbortController();
    void (async () => {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const token = getToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
      try {
        const res = await fetch("/api/ai/generate-novel/stream", {
          method: "POST",
          headers,
          body: JSON.stringify(data),
          signal: controller.signal,
        });
        if (!res.ok) {
          let msg = `请求失败 (${res.status})`;
          try {
            const j = await res.json();
            msg = j.detail?.message || j.error?.message || msg;
          } catch { /* ignore */ }
          onEvent({ type: "error", code: "http_error", message: msg });
          return;
        }
        const reader = res.body?.getReader();
        if (!reader) {
          onEvent({ type: "error", code: "no_stream", message: "浏览器不支持流式读取" });
          return;
        }
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const part of parts) {
            const line = part
              .split("\n")
              .find((l) => l.startsWith("data: "));
            if (!line) continue;
            try {
              const ev = JSON.parse(line.slice(6)) as NovelStreamEvent;
              onEvent(ev);
            } catch { /* ignore */ }
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        onEvent({
          type: "error",
          code: "network_error",
          message: err instanceof Error ? err.message : "流式请求失败",
        });
      }
    })();
    return { cancel: () => controller.abort() };
  },
  planNovel(data: PlanNovelRequest, timeoutMs = 600000) {
    return request<PlanNovelResponse>("/api/ai/plan-novel", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },

  /** 提交整本规划后台作业：毫秒级返回，结果用 getPlanNovelJob 轮询 */
  submitPlanNovel(data: PlanNovelRequest, idempotencyKey?: string) {
    const headers: Record<string, string> = {};
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return request<PlanNovelJobSubmitResponse>("/api/ai/plan-novel/jobs", {
      method: "POST",
      body: JSON.stringify(data),
      headers,
    });
  },
  getPlanNovelJob(jobId: string) {
    return request<PlanNovelJobStatus>(`/api/ai/plan-novel/jobs/${jobId}`);
  },
  /** 取消规划作业：queued → cancelled，running → cancel_requested（幂等） */
  cancelPlanNovelJob(jobId: string) {
    return request<{
      job_id: string;
      status: PlanNovelJobState;
      changed: boolean;
      job: PlanNovelJobStatus;
    }>(`/api/ai/plan-novel/jobs/${jobId}/cancel`, { method: "POST" });
  },
  listPlanNovelJobs(limit = 10) {
    return request<{ items: PlanNovelJobStatus[]; total: number }>(
      `/api/ai/plan-novel/jobs?limit=${limit}`
    );
  },
  planUpdate(data: PlanUpdateRequest, timeoutMs = 600000) {
    return request<PlanUpdateResponse>("/api/ai/plan-update", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },

  // 大纲：分卷 / 章节
  listOutlines(projectId: string, outlineType?: string) {
    const qs = outlineType ? `?outline_type=${outlineType}` : "";
    return request<OutlineNode[]>(`/api/projects/${projectId}/outlines${qs}`);
  },
  createOutline(
    projectId: string,
    data: {
      title: string;
      outline_type: string;
      parent_id?: string | null;
      summary?: string;
      content?: string;
      sort_order?: number;
      status?: string;
    },
    timeoutMs = 30000
  ) {
    return request<OutlineNode>(`/api/projects/${projectId}/outlines`, {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },

  // 章节正文
  getChapterContent(projectId: string, outlineId: string) {
    return request<ChapterContent>(
      `/api/projects/${projectId}/chapters/${outlineId}/content`
    );
  },
  saveChapterContent(
    projectId: string,
    outlineId: string,
    content: string,
    opts?: {
      source?: "manual" | "ai_draft" | "ai_continue" | "ai_rewrite" | "ai_polish" | "restore";
      generation_record_id?: string | null;
      base_version?: number;
      change_summary?: string;
      timeoutMs?: number;
    }
  ) {
    return request<ChapterContent>(
      `/api/projects/${projectId}/chapters/${outlineId}/content`,
      {
        method: "PUT",
        body: JSON.stringify({
          content,
          source: opts?.source || "manual",
          generation_record_id: opts?.generation_record_id ?? null,
          base_version: opts?.base_version ?? null,
          change_summary: opts?.change_summary ?? null,
        }),
      },
      opts?.timeoutMs ?? 30000
    );
  },

  // 第一期「生成不丢失」：章节版本历史
  listChapterVersions(projectId: string, outlineId: string) {
    return request<ChapterVersion[]>(
      `/api/projects/${projectId}/chapters/${outlineId}/versions`
    );
  },
  restoreChapterVersion(projectId: string, outlineId: string, versionId: string) {
    return request<ChapterContent>(
      `/api/projects/${projectId}/chapters/${outlineId}/versions/${versionId}/restore`,
      { method: "POST" }
    );
  },

  // 第一期「生成不丢失」：生成记录
  listGenerations(
    projectId: string,
    opts?: { action_type?: string; page?: number; page_size?: number }
  ) {
    const qs = new URLSearchParams();
    if (opts?.action_type) qs.set("action_type", opts.action_type);
    qs.set("page", String(opts?.page || 1));
    qs.set("page_size", String(opts?.page_size || 20));
    return request<GenerationRecordList>(
      `/api/projects/${projectId}/generations?${qs.toString()}`
    );
  },
  getGeneration(projectId: string, recordId: string) {
    return request<GenerationRecord>(
      `/api/projects/${projectId}/generations/${recordId}`
    );
  },

  // 第二期「章节能被记住」：章节记忆
  getChapterMemory(projectId: string, outlineId: string) {
    return request<ChapterMemoryData>(
      `/api/projects/${projectId}/chapters/${outlineId}/memory`
    );
  },
  updateChapterMemory(
    projectId: string,
    outlineId: string,
    data: { summary: string; memory: ChapterMemoryData["memory"] }
  ) {
    return request<ChapterMemoryData>(
      `/api/projects/${projectId}/chapters/${outlineId}/memory`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },
  generateChapterMemory(projectId: string, outlineId: string) {
    return request<ChapterMemoryData>(
      `/api/projects/${projectId}/chapters/${outlineId}/memory/generate`,
      { method: "POST" }
    );
  },
  confirmChapterMemory(projectId: string, outlineId: string) {
    return request<ChapterMemoryConfirmResult>(
      `/api/projects/${projectId}/chapters/${outlineId}/memory/confirm`,
      { method: "POST" }
    );
  },

  // 第二期「章节能被记住」：项目级设置
  getProjectSettings(projectId: string) {
    return request<ProjectSettingsData>(
      `/api/projects/${projectId}/settings`
    );
  },
  updateProjectSettings(
    projectId: string,
    data: Partial<
      Pick<
        ProjectSettingsData,
        | "auto_summarize"
        | "auto_check"
        | "character_confirm"
        | "foreshadow_confirm"
        | "timeline_confirm"
      >
    >
  ) {
    return request<ProjectSettingsData>(
      `/api/projects/${projectId}/settings`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },

  // ---- 第三期「人物和时间不漂」：人物库 ----
  listCharacters(projectId: string) {
    return request<CharacterData[]>(`/api/projects/${projectId}/characters`);
  },
  getCharacter(projectId: string, characterId: string) {
    return request<CharacterData>(`/api/projects/${projectId}/characters/${characterId}`);
  },
  createCharacter(projectId: string, data: CharacterInput) {
    return request<CharacterData>(`/api/projects/${projectId}/characters`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateCharacter(projectId: string, characterId: string, data: Partial<CharacterInput>) {
    return request<CharacterData>(`/api/projects/${projectId}/characters/${characterId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteCharacter(projectId: string, characterId: string) {
    return request<void>(`/api/projects/${projectId}/characters/${characterId}`, {
      method: "DELETE",
    });
  },

  // ---- 第三期：人物状态快照 ----
  listCharacterSnapshots(projectId: string, characterId: string) {
    return request<CharacterSnapshotData[]>(
      `/api/projects/${projectId}/characters/${characterId}/snapshots`
    );
  },
  createCharacterSnapshot(
    projectId: string,
    characterId: string,
    data: { chapter_id?: string | null; state_json: Record<string, unknown>; source?: string }
  ) {
    return request<CharacterSnapshotData>(
      `/api/projects/${projectId}/characters/${characterId}/snapshots`,
      { method: "POST", body: JSON.stringify(data) }
    );
  },
  confirmCharacterSnapshot(projectId: string, characterId: string, snapshotId: string) {
    return request<CharacterSnapshotData>(
      `/api/projects/${projectId}/characters/${characterId}/snapshots/${snapshotId}/confirm`,
      { method: "POST" }
    );
  },

  // ---- 第三期：角色关系 ----
  listRelationships(projectId: string, statusFilter?: string) {
    const qs = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : "";
    return request<CharacterRelationshipData[]>(
      `/api/projects/${projectId}/relationships${qs}`
    );
  },
  createRelationship(projectId: string, data: CharacterRelationshipInput) {
    return request<CharacterRelationshipData>(`/api/projects/${projectId}/relationships`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateRelationship(
    projectId: string,
    relationId: string,
    data: Partial<CharacterRelationshipInput>
  ) {
    return request<CharacterRelationshipData>(
      `/api/projects/${projectId}/relationships/${relationId}`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },
  deleteRelationship(projectId: string, relationId: string) {
    return request<void>(`/api/projects/${projectId}/relationships/${relationId}`, {
      method: "DELETE",
    });
  },

  // ---- 第三期：时间线事件 ----
  listTimelineEvents(projectId: string, chapterId?: string) {
    const qs = chapterId ? `?chapter_id=${encodeURIComponent(chapterId)}` : "";
    return request<TimelineEventData[]>(
      `/api/projects/${projectId}/timeline-events${qs}`
    );
  },
  createTimelineEvent(projectId: string, data: TimelineEventInput) {
    return request<TimelineEventData>(`/api/projects/${projectId}/timeline-events`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateTimelineEvent(
    projectId: string,
    eventId: string,
    data: Partial<TimelineEventInput>
  ) {
    return request<TimelineEventData>(
      `/api/projects/${projectId}/timeline-events/${eventId}`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },
  deleteTimelineEvent(projectId: string, eventId: string) {
    return request<void>(`/api/projects/${projectId}/timeline-events/${eventId}`, {
      method: "DELETE",
    });
  },
  confirmTimelineEvent(projectId: string, eventId: string) {
    return request<TimelineEventData>(
      `/api/projects/${projectId}/timeline-events/${eventId}/confirm`,
      { method: "POST" }
    );
  },

  // ---- 第三期：变更建议（待确认 → 确认/修改/忽略） ----
  listSuggestions(projectId: string, statusFilter?: string, category?: string) {
    const qs = new URLSearchParams();
    if (statusFilter) qs.set("status", statusFilter);
    if (category) qs.set("category", category);
    return request<SuggestionData[]>(
      `/api/projects/${projectId}/suggestions?${qs.toString()}`
    );
  },
  getSuggestion(projectId: string, suggestionId: string) {
    return request<SuggestionData>(`/api/projects/${projectId}/suggestions/${suggestionId}`);
  },
  confirmSuggestion(
    projectId: string,
    suggestionId: string,
    data: { action: "confirm" | "modified" | "ignore"; payload?: Record<string, unknown> | null }
  ) {
    return request<SuggestionData>(
      `/api/projects/${projectId}/suggestions/${suggestionId}/confirm`,
      { method: "POST", body: JSON.stringify(data) }
    );
  },

  // 第一期「生成不丢失」：导出任务
  createExport(projectId: string, data: ExportCreateInput, timeoutMs = 30000) {
    return request<ExportTask>(
      `/api/projects/${projectId}/export`,
      { method: "POST", body: JSON.stringify(data) },
      timeoutMs
    );
  },
  getExportTask(taskId: string) {
    return request<ExportTask>(`/api/export-tasks/${taskId}`);
  },
  getExportResult(taskId: string) {
    return request<ExportResult>(`/api/export-tasks/${taskId}/result`);
  },

  // 世界观
  listWorldSettings(projectId: string) {
    return request<WorldSetting[]>(`/api/projects/${projectId}/world-settings`);
  },
  createWorldSetting(
    projectId: string,
    data: { category: string; name: string; content: string; importance?: string }
  ) {
    return request<WorldSetting>(`/api/projects/${projectId}/world-settings`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  deleteWorldSetting(projectId: string, settingId: string) {
    return request<void>(`/api/projects/${projectId}/world-settings/${settingId}`, {
      method: "DELETE",
    });
  },

  // AI 写作（预览不落库）
  // 注意：这三个接口后端是同步等模型返回，deepseek-v4-pro 写一章正文实测 40~70 秒，
  // 早期这里写死 12 秒会导致浏览器先超时、服务端仍在跑并照常写 generation_record，
  // 结果是用户看到「请求超时」但 token 已经花掉、正文没拿到。默认放宽到 180 秒。
  aiContinue(
    projectId: string,
    chapterId: string,
    content: string,
    maxLength = 800,
    instruction?: string,
    overrides?: ModelOverrides,
    timeoutMs = 180000
  ) {
    return request<AIResult>(`/api/projects/${projectId}/chapters/${chapterId}/ai/continue`, {
      method: "POST",
      body: JSON.stringify({
        content,
        max_length: maxLength,
        instruction: instruction || null,
        ...(overrides?.model_name ? { model_name: overrides.model_name } : {}),
        ...(overrides?.temperature !== undefined ? { temperature: overrides.temperature } : {}),
        ...(overrides?.max_tokens ? { max_tokens: overrides.max_tokens } : {}),
      }),
    }, timeoutMs);
  },
  aiRewrite(projectId: string, chapterId: string, content: string, instruction?: string, overrides?: ModelOverrides, timeoutMs = 180000) {
    return request<AIResult>(`/api/projects/${projectId}/chapters/${chapterId}/ai/rewrite`, {
      method: "POST",
      body: JSON.stringify({
        content,
        instruction: instruction || null,
        ...(overrides?.model_name ? { model_name: overrides.model_name } : {}),
        ...(overrides?.temperature !== undefined ? { temperature: overrides.temperature } : {}),
        ...(overrides?.max_tokens ? { max_tokens: overrides.max_tokens } : {}),
      }),
    }, timeoutMs);
  },
  aiPolish(projectId: string, chapterId: string, content: string, style?: string, overrides?: ModelOverrides, timeoutMs = 180000) {
    return request<AIResult>(`/api/projects/${projectId}/chapters/${chapterId}/ai/polish`, {
      method: "POST",
      body: JSON.stringify({
        content,
        style: style || null,
        ...(overrides?.model_name ? { model_name: overrides.model_name } : {}),
        ...(overrides?.temperature !== undefined ? { temperature: overrides.temperature } : {}),
        ...(overrides?.max_tokens ? { max_tokens: overrides.max_tokens } : {}),
      }),
    }, timeoutMs);
  },

  // 章节计划：按 chapterId 精确读取 / 保存（syncTitle 显式同步章节标题）/ 旧数据迁移
  getChapterPlan(projectId: string, chapterId: string) {
    return request<ChapterPlanResponse>(`/api/projects/${projectId}/chapter-plans/${chapterId}`, {
      method: "GET",
    });
  },
  rewriteSelection(projectId: string, chapterId: string, selectedText: string, instruction: string) {
    return request<{
      rewritten_text: string;
      original_length: number;
      rewritten_length: number;
    }>(
      `/api/projects/${projectId}/chapters/${chapterId}/ai/rewrite-selection`,
      {
        method: "POST",
        body: JSON.stringify({ selected_text: selectedText, instruction }),
      },
      120000
    );
  },
  getRewriteImpact(projectId: string, chapterId: string) {
    return request<{
      mode: string;
      count: number;
      items: Array<{
        chapter_id: string;
        title: string;
        sort_order: number;
        has_validation: boolean;
        needs_recheck: boolean;
      }>;
    }>(`/api/projects/${projectId}/chapters/${chapterId}/rewrite-impact`);
  },
  getValidationSummary(projectId: string) {
    return request<{
      total: number;
      validated: number;
      coverage: number;
      items: Array<{
        chapter_id: string;
        title: string;
        sort_order: number;
        validated_at: string | null;
        blocking: number;
        warnings: number;
        needs_recheck: boolean;
        lifecycle_status: string;
      }>;
    }>(`/api/projects/${projectId}/chapters/validation-summary`);
  },
  batchGenerateChapterPlans(projectId: string, chapterIds: string[]) {
    return request<{ results: Array<{ chapter_id: string; ok?: boolean; error?: string; plan?: ChapterPlanData }> }>(
      `/api/projects/${projectId}/chapter-plans/batch-generate`,
      {
        method: "POST",
        body: JSON.stringify({ chapter_ids: chapterIds }),
      },
      240000
    );
  },
  updateChapterPlan(
    projectId: string,
    chapterId: string,
    plan: ChapterPlanData,
    syncTitle = false
  ) {
    return request<ChapterPlanResponse>(`/api/projects/${projectId}/chapter-plans/${chapterId}`, {
      method: "PUT",
      body: JSON.stringify({ plan, sync_title: syncTitle }),
    });
  },
  migrateLegacyChapterPlan(projectId: string, chapterId: string) {
    return request<ChapterPlanResponse>(
      `/api/projects/${projectId}/chapter-plans/${chapterId}/migrate-legacy`,
      { method: "POST" }
    );
  },

  // 计划符合度检查：不修改正文，只返回结构化问题（服务端落 plan_check 生成记录）
  checkPlanCompliance(projectId: string, chapterId: string) {
    return request<PlanComplianceResponse>(
      `/api/projects/${projectId}/chapters/${chapterId}/ai/plan-compliance`,
      { method: "POST" }
    );
  },

  // 「已确认大纲版本化」：版本状态 / 列表 / 详情 / 草稿同步 / 确认
  getOutlinePlanStatus(projectId: string) {
    return request<OutlinePlanStatus>(
      `/api/projects/${projectId}/outline-plans/status`,
      { method: "GET" }
    );
  },
  listOutlinePlanVersions(projectId: string) {
    return request<OutlinePlanVersion[]>(
      `/api/projects/${projectId}/outline-plans`,
      { method: "GET" }
    );
  },
  getOutlinePlanVersion(projectId: string, versionId: string) {
    return request<OutlinePlanVersionDetail>(
      `/api/projects/${projectId}/outline-plans/${versionId}`,
      { method: "GET" }
    );
  },
  syncDraftOutlinePlan(projectId: string) {
    return request<OutlinePlanVersion>(
      `/api/projects/${projectId}/outline-plans/sync-draft`,
      { method: "POST" }
    );
  },
  confirmOutlinePlan(projectId: string, dryRun: boolean) {
    return request<ConfirmOutlinePlanResult>(
      `/api/projects/${projectId}/outline-plans/confirm`,
      { method: "POST", body: JSON.stringify({ dry_run: dryRun }) }
    );
  },

  // 发布
  publishProject(projectId: string, force = false) {
    return request<PublishResult>(`/api/projects/${projectId}/publish`, {
      method: "POST",
      body: JSON.stringify({ force }),
    });
  },
  updatePublication(
    projectId: string,
    data: {
      title?: string;
      subtitle?: string;
      description?: string;
      cover_text?: string;
      status?: string;
      project_slug?: string;
    }
  ) {
    return request<PublishResult>(`/api/projects/${projectId}/publication`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  restoreProject(projectId: string) {
    return request<PublishResult>(`/api/projects/${projectId}/restore`, {
      method: "POST",
    });
  },
  getPreview(projectId: string) {
    return request<PreviewProject>(`/api/projects/${projectId}/preview`);
  },

  // i18n 用户偏好
  getLocale() {
    return request<LocaleInfo>("/api/i18n/locale");
  },
  saveLocale(data: Partial<LocaleInfo>) {
    return request<LocaleInfo>("/api/i18n/locale", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  // 通知偏好
  listNotificationPrefs() {
    return request<NotificationPref[]>("/api/notifications/preferences");
  },
  updateNotificationPref(
    notificationType: string,
    data: { in_app_enabled?: boolean; email_enabled?: boolean; push_enabled?: boolean }
  ) {
    return request<NotificationPref>(
      `/api/notifications/preferences/${notificationType}`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },

  // ---- 第四期「写错能发现」：一致性检查 ----
  quickCheckConsistency(data: QuickCheckRequest, timeoutMs = 30000) {
    return request<QuickCheckResponse>("/api/consistency/quick-check", {
      method: "POST",
      body: JSON.stringify(data),
    }, timeoutMs);
  },
  // 批次 3：编辑类生成流式（续写/重生成/扩写/润色），返回 { cancel } 供中途取消
  streamChapterEdit(
    path: string,
    data: ChapterEditRequest | NextChapterRequest,
    onEvent: (ev: NovelStreamEvent) => void
  ): { cancel: () => void } {
    const controller = new AbortController();
    void (async () => {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const token = getToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
      try {
        const res = await fetch(path, {
          method: "POST",
          headers,
          body: JSON.stringify(data),
          signal: controller.signal,
        });
        if (!res.ok) {
          let msg = `请求失败 (${res.status})`;
          try {
            const j = await res.json();
            msg = j.detail?.message || j.error?.message || msg;
          } catch { /* ignore */ }
          onEvent({ type: "error", code: "http_error", message: msg });
          return;
        }
        const reader = res.body?.getReader();
        if (!reader) {
          onEvent({ type: "error", code: "no_stream", message: "浏览器不支持流式读取" });
          return;
        }
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const part of parts) {
            const line = part.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            try {
              onEvent(JSON.parse(line.slice(6)) as NovelStreamEvent);
            } catch { /* ignore */ }
          }
        }
      } catch {
        onEvent({ type: "error", code: "aborted", message: "请求已中断" });
      }
    })();
    return { cancel: () => controller.abort() };
  },
  cancelGeneration(requestId: string, timeoutMs = 15000) {
    return request<{ ok: boolean; message: string }>("/api/ai/cancel-generation", {
      method: "POST",
      body: JSON.stringify({ request_id: requestId }),
    }, timeoutMs);
  },
  getReadabilityReport(projectId: string, timeoutMs = 30000) {
    return request<ReadabilityReport>(`/api/projects/${projectId}/readability-report`, {}, timeoutMs);
  },
  getUsageSummary(days = 14, timeoutMs = 30000) {
    return request<UsageSummary>(`/api/ai/usage-summary?days=${days}`, {}, timeoutMs);
  },
  checkConsistency(
    projectId: string,
    chapterId: string,
    content: string,
    chapterTitle = "",
    outlineText?: string | null,
    timeoutMs = 60000
  ) {
    return request<CheckConsistencyResponse>(
      `/api/ai/check-consistency`,
      {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          chapter_id: chapterId,
          content,
          chapter_title: chapterTitle,
          outline_text: outlineText || null,
        }),
      },
      timeoutMs
    );
  },
  listConsistencyChecks(projectId: string, chapterId?: string, limit = 20) {
    const params = new URLSearchParams();
    if (chapterId) params.set("chapter_id", chapterId);
    params.set("limit", String(limit));
    return request<ConsistencyCheckRecord[]>(
      `/api/projects/${projectId}/consistency/checks?${params.toString()}`
    );
  },
  runDataConsistencyCheck(
    projectId: string,
    dimension: "all" | "dangling_refs" | "character_state" | "relationships" | "foreshadows" | "appearance" | "memory" = "all",
    timeoutMs = 60000
  ) {
    return request<DataConsistencyRunResponse>(
      `/api/projects/${projectId}/consistency/check`,
      {
        method: "POST",
        body: JSON.stringify({ dimension }),
      },
      timeoutMs
    );
  },

  // ---- 第四期「写错能发现」：伏笔管理 ----
  listForeshadows(projectId: string, status?: string) {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    return request<ForeshadowData[]>(
      `/api/projects/${projectId}/foreshadows?${params.toString()}`
    );
  },
  createForeshadow(projectId: string, data: ForeshadowInput) {
    return request<ForeshadowData>(`/api/projects/${projectId}/foreshadows`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateForeshadow(projectId: string, foreshadowId: string, data: Partial<ForeshadowInput>) {
    return request<ForeshadowData>(
      `/api/projects/${projectId}/foreshadows/${foreshadowId}`,
      { method: "PUT", body: JSON.stringify(data) }
    );
  },
  deleteForeshadow(projectId: string, foreshadowId: string) {
    return request<{ ok: boolean }>(
      `/api/projects/${projectId}/foreshadows/${foreshadowId}`,
      { method: "DELETE" }
    );
  },
  markForeshadowPaidOff(
    projectId: string,
    foreshadowId: string,
    data: { paid_off_chapter_id?: string | null; paid_off_note?: string | null; planned_payoff?: string | null }
  ) {
    return request<ForeshadowData>(
      `/api/projects/${projectId}/foreshadows/${foreshadowId}/paid-off`,
      { method: "POST", body: JSON.stringify(data) }
    );
  },
  importForeshadows(
    projectId: string,
    data: { chapter_ids?: string[] | null; mark_planted?: boolean; replace_duplicates?: boolean }
  ) {
    return request<ForeshadowImportResult>(`/api/projects/${projectId}/foreshadows/import`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  // ---- 第四期「写错能发现」：采纳建议（proposed_updates 转变更建议）----
  createSuggestion(
    projectId: string,
    data: {
      chapter_id: string;
      category: string;
      title: string;
      description?: string | null;
      payload: Record<string, unknown>;
    }
  ) {
    return request<SuggestionData>(`/api/projects/${projectId}/suggestions`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  // ---- P1 AI 任务中心：统一任务接入 ----
  createAITask(
    projectId: string,
    data: {
      task_type: string;
      prompt: string;
      outline_id?: string | null;
      chapter_id?: string | null;
      model_name?: string | null;
      prompt_version?: string | null;
      idempotency_key?: string | null;
      request_snapshot?: string | null;
      timeout_seconds?: number;
    }
  ) {
    const params = new URLSearchParams();
    params.set("task_type", data.task_type);
    params.set("prompt", data.prompt);
    if (data.outline_id) params.set("outline_id", data.outline_id);
    if (data.chapter_id) params.set("chapter_id", data.chapter_id);
    if (data.model_name) params.set("model_name", data.model_name);
    if (data.prompt_version) params.set("prompt_version", data.prompt_version);
    if (data.idempotency_key) params.set("idempotency_key", data.idempotency_key);
    if (data.request_snapshot) params.set("request_snapshot", data.request_snapshot);
    if (data.timeout_seconds) params.set("timeout_seconds", String(data.timeout_seconds));
    return request<AITaskCreateResult>(
      `/api/projects/${projectId}/ai/tasks?${params.toString()}`,
      { method: "POST" }
    );
  },
  listAITasks(projectId: string, opts?: { status?: string; limit?: number; offset?: number }) {
    const params = new URLSearchParams();
    if (opts?.status) params.set("status", opts.status);
    params.set("limit", String(opts?.limit ?? 20));
    params.set("offset", String(opts?.offset ?? 0));
    return request<AITaskList>(`/api/projects/${projectId}/ai/tasks?${params.toString()}`);
  },
  getAITask(projectId: string, taskId: string) {
    return request<AITask>(`/api/projects/${projectId}/ai/tasks/${taskId}`);
  },
  cancelAITask(projectId: string, taskId: string) {
    return request<AITask>(`/api/projects/${projectId}/ai/tasks/${taskId}/cancel`, { method: "POST" });
  },
  retryAITask(projectId: string, taskId: string) {
    return request<AITaskCreateResult>(
      `/api/projects/${projectId}/ai/tasks/${taskId}/retry`,
      { method: "POST" }
    );
  },

  // ---- P1 第二批：审核中心 / 协作权限 / 操作审计 ----
  listOrganizations() {
    return request<{ organizations: { id: string; name: string; role: string }[] }>(
      "/api/enterprise/organizations"
    );
  },
  createOrganization(data: { name: string; plan?: string | null }) {
    return request<{ id: string; name: string; owner_id: string; plan: string | null }>(
      "/api/enterprise/organizations",
      { method: "POST", body: JSON.stringify(data) }
    );
  },
  getOrganization(orgId: string) {
    return request<{ id: string; name: string; owner_id: string; plan: string | null; status: string }>(
      `/api/enterprise/organizations/${orgId}`
    );
  },
  listOrgProjects(orgId: string) {
    return request<{ projects: { id: string; title: string; status: string }[] }>(
      `/api/enterprise/organizations/${orgId}/projects`
    );
  },
  createOrgProject(orgId: string, data: { title: string; genre?: string | null }) {
    return request<{ id: string; title: string; organization_id: string | null; status: string }>(
      `/api/enterprise/organizations/${orgId}/projects`,
      { method: "POST", body: JSON.stringify(data) }
    );
  },

  // 审核中心
  listReviewTasks(
    orgId: string,
    opts?: { project_id?: string | null; status?: string | null }
  ) {
    const qs = new URLSearchParams();
    if (opts?.project_id) qs.set("project_id", opts.project_id);
    if (opts?.status) qs.set("status_filter", opts.status);
    return request<{ total: number; tasks: ReviewTaskItem[] }>(
      `/api/enterprise/organizations/${orgId}/review-tasks?${qs.toString()}`
    );
  },
  submitReview(
    orgId: string,
    projectId: string,
    data: { resource_type: string; resource_id: string; lock_after_submit?: boolean; change_summary?: string | null }
  ) {
    return request<{ id: string; status: string; stage: string; submitted_at: string | null }>(
      `/api/enterprise/organizations/${orgId}/projects/${projectId}/reviews`,
      { method: "POST", body: JSON.stringify(data) }
    );
  },
  rejectReview(orgId: string, taskId: string, reason: string) {
    return request<{ id: string; status: string; comment: string }>(
      `/api/enterprise/organizations/${orgId}/review-tasks/${taskId}/reject`,
      { method: "POST", body: JSON.stringify({ reason }) }
    );
  },
  approveReview(orgId: string, taskId: string) {
    return request<{ id: string; status: string; completed_at: string | null }>(
      `/api/enterprise/organizations/${orgId}/review-tasks/${taskId}/approve`,
      { method: "POST" }
    );
  },
  resubmitReview(orgId: string, taskId: string, changeSummary?: string | null) {
    return request<{ id: string; status: string; stage: string }>(
      `/api/enterprise/organizations/${orgId}/review-tasks/${taskId}/resubmit`,
      { method: "POST", body: JSON.stringify({ change_summary: changeSummary ?? null }) }
    );
  },
  withdrawReview(orgId: string, taskId: string) {
    return request<{ id: string; status: string }>(
      `/api/enterprise/organizations/${orgId}/review-tasks/${taskId}/withdraw`,
      { method: "POST" }
    );
  },

  // 协作权限
  listMembers(orgId: string) {
    return request<{ members: MemberItem[] }>(
      `/api/enterprise/organizations/${orgId}/members`
    );
  },
  listInvitations(orgId: string, status?: string) {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<{ invitations: InvitationItem[] }>(
      `/api/enterprise/organizations/${orgId}/invitations${qs}`
    );
  },
  inviteMembers(orgId: string, data: { emails: string[]; role: string; project_id?: string | null; expires_hours?: number }) {
    return request<{ created: { id: string; email: string; role: string; status: string }[] }>(
      `/api/enterprise/organizations/${orgId}/invitations`,
      { method: "POST", body: JSON.stringify({ ...data, expires_hours: data.expires_hours ?? 72 }) }
    );
  },
  revokeInvitation(orgId: string, invitationId: string) {
    return request<{ revoked: boolean; id: string }>(
      `/api/enterprise/organizations/${orgId}/invitations/${invitationId}/revoke`,
      { method: "POST" }
    );
  },
  updateMemberRole(orgId: string, memberId: string, role: string) {
    return request<{ id: string; user_id: string; role: string }>(
      `/api/enterprise/organizations/${orgId}/members/${memberId}/role`,
      { method: "PATCH", body: JSON.stringify({ new_role: role }) }
    );
  },
  offboardMember(orgId: string, userId: string) {
    return request<{ ok: boolean }>(
      `/api/enterprise/organizations/${orgId}/members/${userId}/offboard`,
      { method: "POST" }
    );
  },

  // 操作审计
  listAuditLogs(
    orgId: string,
    opts?: { page?: number; page_size?: number; action?: string | null; actor_id?: string | null; from_dt?: string | null; to_dt?: string | null }
  ) {
    const qs = new URLSearchParams();
    qs.set("page", String(opts?.page || 1));
    qs.set("page_size", String(opts?.page_size || 20));
    if (opts?.action) qs.set("action", opts.action);
    if (opts?.actor_id) qs.set("actor_id", opts.actor_id);
    if (opts?.from_dt) qs.set("from_dt", opts.from_dt);
    if (opts?.to_dt) qs.set("to_dt", opts.to_dt);
    return request<{ total: number; logs: AuditLogItem[] }>(
      `/api/enterprise/organizations/${orgId}/audit-logs?${qs.toString()}`
    );
  },
};

export interface ReviewTaskItem {
  id: string;
  project_id: string | null;
  project_title: string;
  resource_type: string;
  resource_id: string;
  status: string;
  stage: string;
  comment: string | null;
  submitter_id: string | null;
  submitter_name: string;
  reviewer_id: string | null;
  reviewer_name: string;
  submitted_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export interface MemberItem {
  id: string;
  user_id: string;
  username: string;
  email: string;
  role: string;
  status: string;
  joined_at: string | null;
}

export interface InvitationItem {
  id: string;
  email: string;
  role: string;
  status: string;
  expires_at: string | null;
  created_at: string | null;
  inviter_id: string | null;
}

export interface AuditLogItem {
  id: string;
  action: string;
  actor_id: string | null;
  actor_username: string;
  actor_email: string;
  target_type: string | null;
  target_id: string | null;
  before_value: string | null;
  after_value: string | null;
  metadata_json: string | null;
  created_at: string | null;
}
