import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type ChapterContent,
  type ChapterMemoryData,
  type ChapterPlanData,
  type ChapterPlanResponse,
  type ChapterVersion,
  type CheckConsistencyResponse,
  type ConsistencyUpdate,
  type GenerationRecord,
  type OutlineNode,
  type OutlinePlanStatus,
  type OutlinePlanVersion,
  type OutlinePlanVersionDetail,
  type ConfirmPreviewItem,
  type ConfirmOutlinePlanResult,
  type PlanComplianceIssue,
  type PlanComplianceResponse,
  type ReadabilityReport,
} from "../api/client";
import { demoChapters, demoContent } from "../api/mock";
import { GenerationRecordsDrawer } from "./chapter-editor/GenerationRecordsDrawer";
import { ReadabilityDrawer } from "./chapter-editor/ReadabilityDrawer";
import { VersionHistoryDrawer } from "./chapter-editor/VersionHistoryDrawer";
import { ACTION_TYPE_LABEL, SOURCE_LABEL, formatTime } from "./chapter-editor/labels";

type AiMode = "continue" | "polish" | "rewrite";

const AI_SOURCE_MAP: Record<AiMode, "ai_continue" | "ai_rewrite" | "ai_polish"> = {
  continue: "ai_continue",
  rewrite: "ai_rewrite",
  polish: "ai_polish",
};

const AI_MODE_LABEL: Record<AiMode, string> = {
  continue: "续写",
  rewrite: "改写",
  polish: "润色",
};

const MODEL_CFG_KEY = "novelcraft_ai_model_cfg"; // 与创建页/设置页共享的模型偏好（不含密钥）

function readModelOverrides(): { model_name?: string; temperature?: number; max_tokens?: number } {
  try {
    const raw = localStorage.getItem(MODEL_CFG_KEY);
    if (raw) {
      const cfg = JSON.parse(raw);
      const out: { model_name?: string; temperature?: number; max_tokens?: number } = {};
      if (cfg.model_name) out.model_name = cfg.model_name;
      if (Number.isFinite(cfg.temperature)) out.temperature = Number(cfg.temperature);
      if (Number.isFinite(cfg.max_tokens) && cfg.max_tokens > 0) out.max_tokens = Number(cfg.max_tokens);
      return out;
    }
  } catch {
    /* ignore */
  }
  return {};
}

/** 简化的行级 diff：公共前缀/后缀之外的中段标记为差异 */
function simpleDiff(current: string, target: string): { kind: "same" | "removed" | "added" | "changed"; line: string }[] {
  const a = current.split("\n");
  const b = target.split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tailA = a.length;
  let tailB = b.length;
  while (tailA > head && tailB > head && a[tailA - 1] === b[tailB - 1]) {
    tailA--;
    tailB--;
  }
  const out: { kind: "same" | "removed" | "added" | "changed"; line: string }[] = [];
  for (let i = 0; i < head; i++) out.push({ kind: "same", line: a[i] });
  const middleA = a.slice(head, tailA);
  const middleB = b.slice(head, tailB);
  if (middleA.length === 0) {
    for (const l of middleB) out.push({ kind: "added", line: l });
  } else if (middleB.length === 0) {
    for (const l of middleA) out.push({ kind: "removed", line: l });
  } else {
    const max = Math.max(middleA.length, middleB.length);
    for (let i = 0; i < max; i++) {
      const la = i < middleA.length ? middleA[i] : "";
      const lb = i < middleB.length ? middleB[i] : "";
      if (la === lb) {
        out.push({ kind: "same", line: la });
      } else if (i < middleA.length && i < middleB.length) {
        out.push({ kind: "changed", line: `${la}  →  ${lb}` });
      } else if (i < middleA.length) {
        out.push({ kind: "removed", line: la });
      } else {
        out.push({ kind: "added", line: lb });
      }
    }
  }
  for (let i = tailB; i < b.length; i++) out.push({ kind: "same", line: b[i] });
  return out;
}

// ---- 本地草稿暂存（防丢稿）：切换章节/离开页面前自动把未保存正文写入 localStorage ----
function draftKeyOf(projectId: string, chapterId: string): string {
  return `nc-draft:${projectId}:${chapterId}`;
}

function saveDraft(projectId: string, chapterId: string, text: string) {
  try {
    if (text.trim()) localStorage.setItem(draftKeyOf(projectId, chapterId), text);
    else localStorage.removeItem(draftKeyOf(projectId, chapterId));
  } catch {
    /* localStorage 不可用时静默降级 */
  }
}

function loadDraft(projectId: string, chapterId: string): string | null {
  try {
    return localStorage.getItem(draftKeyOf(projectId, chapterId));
  } catch {
    return null;
  }
}

function clearDraft(projectId: string, chapterId: string) {
  try {
    localStorage.removeItem(draftKeyOf(projectId, chapterId));
  } catch {
    /* 忽略 */
  }
}

export default function ChapterEditorPage() {
  const { projectId = "", chapterId = "" } = useParams();
  const navigate = useNavigate();

  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [content, setContent] = useState("");
  const [version, setVersion] = useState(1);
  const [wordCount, setWordCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [contentLoading, setContentLoading] = useState(false);
  const [isDemo, setIsDemo] = useState(false);
  const [error, setError] = useState("");
  const [savedHint, setSavedHint] = useState("");
  const [draftHint, setDraftHint] = useState("");
  const [impactHint, setImpactHint] = useState("");
  const [readability, setReadability] = useState<ReadabilityReport | null>(null);
  const [readabilityBusy, setReadabilityBusy] = useState(false);
  // 选段改写（第三档升级）
  const editorTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [showSelRewrite, setShowSelRewrite] = useState(false);
  const [selRewriteInstruction, setSelRewriteInstruction] = useState("");
  const [selRewriteBusy, setSelRewriteBusy] = useState(false);
  const [selRewriteResult, setSelRewriteResult] = useState<{
    original: string;
    rewritten: string;
  } | null>(null);
  const [selRewriteError, setSelRewriteError] = useState("");
  const [autosaveHint, setAutosaveHint] = useState("");

  // 防丢稿：请求序号（切章竞态守卫）+ 最近已保存内容（dirty 判断）
  const loadSeqRef = useRef(0);
  const savedContentRef = useRef("");
  // 服务端自动保存：停止输入 2 秒触发；同一内容最多每 30 秒强制一次
  const autosaveTimerRef = useRef<number | null>(null);
  const autosavingRef = useRef(false);
  const lastAutosaveAtRef = useRef(0);

  // AI 面板状态
  const [aiMode, setAiMode] = useState<AiMode | null>(null);
  const [aiInstruction, setAiInstruction] = useState("");
  const [aiStyle, setAiStyle] = useState("");
  const [aiMaxLength, setAiMaxLength] = useState(800);
  const [aiResult, setAiResult] = useState<{
    content: string;
    recordId?: string | null;
    chapterId?: string;
    planSummary?: string;
    mode?: AiMode;
  } | null>(null);
  // 接受动作自身要有可见状态：失败原因必须显示在弹窗里（页面主体被遮罩挡住，看不到）
  const [aiAccepting, setAiAccepting] = useState(false);
  const [aiAcceptError, setAiAcceptError] = useState("");
  const [aiConflict, setAiConflict] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  // 同步等待模型返回时给用户一个可见的进度，避免几十秒内看不出服务在工作
  const [aiElapsed, setAiElapsed] = useState(0);
  useEffect(() => {
    if (!aiBusy) {
      setAiElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const timer = setInterval(() => {
      setAiElapsed(Math.round((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [aiBusy]);
  const [aiNotice, setAiNotice] = useState("");

  // 章节计划（ChapterPlan）状态
  const [chapterPlan, setChapterPlan] = useState<ChapterPlanData | null>(null);
  const [planChapterNumber, setPlanChapterNumber] = useState(0);
  const [planSource, setPlanSource] = useState<ChapterPlanResponse["planSource"]>("none");
  const [titleSyncRequired, setTitleSyncRequired] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const [planEdit, setPlanEdit] = useState(false);
  const [planDraft, setPlanDraft] = useState<ChapterPlanData | null>(null);
  const [planSaving, setPlanSaving] = useState(false);
  const [planError, setPlanError] = useState("");
  const [genNextBusy, setGenNextBusy] = useState(false);
  const [planBatchBusy, setPlanBatchBusy] = useState(false);
  const [planBatchProgress, setPlanBatchProgress] = useState("");
  const [planBatchError, setPlanBatchError] = useState("");
  const [validationSummary, setValidationSummary] = useState<{
    items: Array<{ chapter_id: string; blocking: number; warnings: number; validated_at: string | null; needs_recheck: boolean }>;
  } | null>(null);
  const [planHint, setPlanHint] = useState("");
  const [freeCreateConfirm, setFreeCreateConfirm] = useState(false);

  // 计划符合度检查（第 5 项需求）
  const [planCompliance, setPlanCompliance] = useState<PlanComplianceResponse | null>(null);
  const [planCheckBusy, setPlanCheckBusy] = useState(false);
  const [planCheckError, setPlanCheckError] = useState("");

  // 历史版本抽屉
  const [showVersions, setShowVersions] = useState(false);
  const [versions, setVersions] = useState<ChapterVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<ChapterVersion | null>(null);
  const [versionsBusy, setVersionsBusy] = useState(false);
  const [versionsError, setVersionsError] = useState("");

  // 生成记录抽屉
  const [showGenerations, setShowGenerations] = useState(false);
  const [generations, setGenerations] = useState<GenerationRecord[]>([]);
  const [generationsLoading, setGenerationsLoading] = useState(false);
  const [expandedGen, setExpandedGen] = useState<string | null>(null);
  const [generationsError, setGenerationsError] = useState("");

  // 「已确认大纲版本化」：版本状态 / 确认新版本 / 历史版本抽屉 / 标题冲突保护
  const [outlineStatus, setOutlineStatus] = useState<OutlinePlanStatus | null>(null);
  const [outlineStatusLoading, setOutlineStatusLoading] = useState(false);
  const [outlineHint, setOutlineHint] = useState("");
  const [confirmPreview, setConfirmPreview] = useState<ConfirmOutlinePlanResult | null>(null);
  const [confirmPlanBusy, setConfirmPlanBusy] = useState(false);
  const [showOutlineVersions, setShowOutlineVersions] = useState(false);
  const [outlineVersions, setOutlineVersions] = useState<OutlinePlanVersion[]>([]);
  const [outlineVersionsLoading, setOutlineVersionsLoading] = useState(false);
  const [outlineVersionsError, setOutlineVersionsError] = useState("");
  const [selectedOutlineVersion, setSelectedOutlineVersion] = useState<OutlinePlanVersionDetail | null>(null);
  const [selectedOutlineVersionId, setSelectedOutlineVersionId] = useState<string>("");
  const [outlineVersionMap, setOutlineVersionMap] = useState<Record<string, number>>({});
  const [titleConflict, setTitleConflict] = useState<{ message: string } | null>(null);
  const [titleConflictBusy, setTitleConflictBusy] = useState(false);

  // 第二期「章节能被记住」：章节记忆抽屉
  const [showMemory, setShowMemory] = useState(false);
  const [memory, setMemory] = useState<ChapterMemoryData | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [memoryEdit, setMemoryEdit] = useState(false);
  const [memorySummary, setMemorySummary] = useState("");
  const [memoryJson, setMemoryJson] = useState("");
  const [memoryHint, setMemoryHint] = useState("");

  // 第四期「写错能发现」：检查结果抽屉
  const [showCheck, setShowCheck] = useState(false);
  const [checkResult, setCheckResult] = useState<CheckConsistencyResponse | null>(null);
  const [checkLoading, setCheckLoading] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [checkAccepting, setCheckAccepting] = useState(false);
  const [checkHint, setCheckHint] = useState("");
  const [acceptedKeys, setAcceptedKeys] = useState<string[]>([]);

  async function loadChapters() {
    setLoading(true);
    setError("");
    try {
      const list = await api.listOutlines(projectId, "chapter");
      setChapters(list);
      setIsDemo(false);
      if (list.length > 0) {
        const initial = chapterId && list.some((c) => c.id === chapterId) ? chapterId : list[0].id;
        setActiveId(initial);
      } else {
        setActiveId("");
      }
      void loadOutlineStatus();
      // 校验状态摘要（不影响主流程）：失败静默，徽标只是增强
      api
        .getValidationSummary(projectId)
        .then((s) => setValidationSummary(s))
        .catch(() => setValidationSummary(null));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setChapters(demoChapters);
        setActiveId(chapterId && demoChapters.some((c) => c.id === chapterId) ? chapterId : demoChapters[0].id);
        setIsDemo(true);
      }
    } finally {
      setLoading(false);
    }
  }

  const loadContent = useCallback(
    async (outlineId: string) => {
      const seq = ++loadSeqRef.current;
      setContentLoading(true);
      setError("");
      setAiResult(null);
      setAiNotice("");
      try {
        const data: ChapterContent = await api.getChapterContent(projectId, outlineId);
        if (seq !== loadSeqRef.current) return; // 已切换到其他章节：丢弃迟到响应，防止正文/版本号写错章
        const draft = loadDraft(projectId, outlineId);
        if (draft !== null && draft !== data.content) {
          setContent(draft);
          setDraftHint("已恢复上次未保存的草稿（离开前自动暂存）。保存成功后自动清除。");
        } else {
          setContent(data.content || "");
          setDraftHint("");
        }
        savedContentRef.current =
          draft !== null && draft !== data.content ? draft : data.content || "";
        setVersion(data.version);
        setWordCount(data.word_count);
        setIsDemo(false);
      } catch (err) {
        if (seq !== loadSeqRef.current) return;
        if (err instanceof ApiError && err.status === 401) {
          setError("登录已过期，请重新登录");
        } else {
          setContent(demoContent.content);
          setVersion(demoContent.version);
          setWordCount(demoContent.word_count);
          setIsDemo(true);
        }
      } finally {
        if (seq === loadSeqRef.current) setContentLoading(false);
      }
    },
    [projectId]
  );

  /** 有未保存改动时先把正文暂存到本地草稿（不弹窗打扰，切回来自动恢复） */
  const stashDraftIfDirty = useCallback(() => {
    if (!activeId) return;
    if (content.trim() && content !== savedContentRef.current) {
      saveDraft(projectId, activeId, content);
    }
  }, [activeId, content, projectId]);

  // 关闭/刷新页面且存在未保存改动时：先暂存草稿，再交浏览器原生确认
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (!activeId || !content.trim() || content === savedContentRef.current) return;
      saveDraft(projectId, activeId, content);
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [activeId, content, projectId]);

  useEffect(() => {
    void loadChapters();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, chapterId]);

  useEffect(() => {
    if (activeId) {
      void loadContent(activeId);
      void loadChapterPlan(activeId);
    }
  }, [activeId, loadContent]);

  // 批次 3：快捷键最小集（Ctrl+S 保存 / Ctrl+Enter 生成 / Ctrl+←→ 翻章）
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      const target = e.target as HTMLElement | null;
      const inEditor = Boolean(
        target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.isContentEditable)
      );
      if (e.key.toLowerCase() === "s") {
        e.preventDefault();
        void handleSave();
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        void handleAiRun();
        return;
      }
      if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !inEditor) {
        const ordered = [...chapters].sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));
        const idx = ordered.findIndex((c) => c.id === activeId);
        if (idx < 0) return;
        const next = e.key === "ArrowRight" ? idx + 1 : idx - 1;
        if (next >= 0 && next < ordered.length) {
          e.preventDefault();
          setActiveId(ordered[next].id);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapters, activeId, content]);

  async function loadChapterPlan(outlineId: string) {
    setPlanLoading(true);
    setPlanError("");
    setPlanHint("");
    try {
      const data = await api.getChapterPlan(projectId, outlineId);
      setChapterPlan(data.plan);
      setPlanChapterNumber(data.chapterNumber);
      setPlanSource(data.planSource);
      setTitleSyncRequired(data.titleSyncRequired);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setChapterPlan(null);
        setPlanSource("none");
        setPlanError(err instanceof Error ? err.message : "章节计划加载失败");
      }
    } finally {
      setPlanLoading(false);
    }
  }

  /** 本次生成所依据的章节计划摘要（用于生成前展示与生成后预览） */
  function buildPlanSummaryText(): string {
    const lines = [`第 ${planChapterNumber || "?"} 章`];
    const title = activeChapter?.title || chapterPlan?.title || "";
    if (title) lines.push(`标题：《${title}》`);
    if (chapterPlan?.summary) lines.push(`目标/摘要：${chapterPlan.summary}`);
    if (chapterPlan?.sceneGoals?.length) lines.push(`场景目标：${chapterPlan.sceneGoals.join("；")}`);
    if (chapterPlan?.requiredCharacters?.length) lines.push(`必须出场人物：${chapterPlan.requiredCharacters.join("；")}`);
    if (chapterPlan?.requiredLocations?.length) lines.push(`必须出现地点：${chapterPlan.requiredLocations.join("；")}`);
    if (chapterPlan?.requiredEvents?.length) lines.push(`必须发生事件：${chapterPlan.requiredEvents.join("；")}`);
    if (chapterPlan?.expectedOutcome) lines.push(`本章结束状态：${chapterPlan.expectedOutcome}`);
    if (chapterPlan?.forbiddenReveals?.length) lines.push(`禁止提前揭露：${chapterPlan.forbiddenReveals.join("；")}`);
    if (planSource === "none") lines.push("（无章节计划：自由创作）");
    else if (planSource === "legacy") lines.push("（依据旧章节细纲回退的计划）");
    return lines.join("\n");
  }

  /** 生成前的前置校验：无标题 / 无计划 / 计划编辑中 一律阻止直接生成 */
  function canGenerateNow(): string {
    if (!activeId) return "未选择章节";
    if (planEdit) return "章节计划正在编辑中，请先保存计划后再生成";
    const title = activeChapter?.title || chapterPlan?.title || "";
    if (!title.trim()) return "该章节无标题，无法作为生成依据。请先补全章节标题（或编辑章节计划并同步标题）后再生成";
    if (planSource === "none" && !freeCreateConfirm) return "当前章节没有章节计划。请先编辑并保存章节计划，或勾选“无计划自由创作”后继续";
    return "";
  }

  const liveCount = content.replace(/\s/g, "").length;

  /** 选段改写：读取 textarea 选区 → 弹窗 → AI 改写 → 接受时替换选区文本 */
  function openSelectionRewrite() {
    const ta = editorTextareaRef.current;
    if (!ta || !activeId) return;
    const start = ta.selectionStart ?? 0;
    const end = ta.selectionEnd ?? 0;
    const selected = content.slice(start, end);
    if (!selected.trim()) {
      setSelRewriteError("请先在正文里选中要改写的一段文字");
      setShowSelRewrite(true);
      return;
    }
    setSelRewriteResult({ original: selected, rewritten: "" });
    setSelRewriteInstruction("");
    setSelRewriteError("");
    setShowSelRewrite(true);
  }

  async function runSelectionRewrite() {
    const ta = editorTextareaRef.current;
    if (!ta || !activeId || !selRewriteResult) return;
    const start = ta.selectionStart ?? 0;
    const end = ta.selectionEnd ?? 0;
    if (end <= start) return;
    setSelRewriteBusy(true);
    setSelRewriteError("");
    try {
      const res = await api.rewriteSelection(
        projectId,
        activeId,
        selRewriteResult.original,
        selRewriteInstruction
      );
      setSelRewriteResult({ original: selRewriteResult.original, rewritten: res.rewritten_text });
    } catch (err) {
      setSelRewriteError(err instanceof Error ? err.message : "选段改写失败");
    } finally {
      setSelRewriteBusy(false);
    }
  }

  function acceptSelectionRewrite() {
    const ta = editorTextareaRef.current;
    if (!ta || !selRewriteResult || !selRewriteResult.rewritten) return;
    const start = ta.selectionStart ?? 0;
    const end = ta.selectionEnd ?? 0;
    const next = content.slice(0, start) + selRewriteResult.rewritten + content.slice(end);
    setContent(next);
    setSelRewriteResult(null);
    setShowSelRewrite(false);
  }

  /** 保存后查询重写影响面：有受影响的后文章节时给出提示 */
  async function checkRewriteImpact(outlineId: string) {
    try {
      const impact = await api.getRewriteImpact(projectId, outlineId);
      if (impact.count > 0) {
        const names = impact.items.slice(0, 3).map((i) => i.title).join("、");
        setImpactHint(
          `重写影响面：${impact.count} 个后续章节可能受影响（${names}${impact.count > 3 ? "…" : ""}），建议按顺序复核。`
        );
      } else {
        setImpactHint("");
      }
    } catch {
      /* 影响面查询失败不阻断保存 */
    }
  }

  async function handleSave() {
    if (!activeId) return;
    setError("");
    setSavedHint("");
    try {
      const data = await api.saveChapterContent(projectId, activeId, content);
      savedContentRef.current = content;
      clearDraft(projectId, activeId);
      setDraftHint("");
      lastAutosaveAtRef.current = Date.now();
      setAutosaveHint("");
      setVersion(data.version);
      setWordCount(data.word_count);
      setSavedHint(`已保存 v${data.version}，后端字数 ${data.word_count}`);
      setIsDemo(false);
      if (showMemory) void loadMemory();
      void checkRewriteImpact(activeId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setSavedHint("");
        setError(err instanceof Error ? err.message : "保存失败");
      }
    }
  }

  // 批次 4：追读力分析（钩子覆盖率 / 爽点密度 / 伏笔债务）
  async function handleReadabilityReport() {
    setReadabilityBusy(true);
    setError("");
    try {
      const report = await api.getReadabilityReport(projectId);
      setReadability(report);
    } catch (err) {
      setError(err instanceof Error ? err.message : "追读力分析失败");
    } finally {
      setReadabilityBusy(false);
    }
  }

  /** 服务端自动保存：静默保存当前正文（版本冲突时保留本地草稿并提示，绝不覆盖） */
  async function autoSave() {
    if (autosavingRef.current || !activeId) return;
    const current = content;
    if (!current.trim() || current === savedContentRef.current) return;
    autosavingRef.current = true;
    try {
      const data = await api.saveChapterContent(projectId, activeId, current);
      savedContentRef.current = current;
      clearDraft(projectId, activeId);
      setDraftHint("");
      lastAutosaveAtRef.current = Date.now();
      setVersion(data.version);
      setWordCount(data.word_count);
      setAutosaveHint(`已自动保存 ${new Date().toLocaleTimeString()}`);
      setSavedHint(`已保存 v${data.version}，后端字数 ${data.word_count}`);
      setIsDemo(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setAutosaveHint("自动保存发现版本冲突（可能有其他页面在编辑本章），已保留本地草稿，请手动保存确认");
      } else if (!(err instanceof ApiError && err.status === 401)) {
        setAutosaveHint("自动保存失败，草稿已保留在本地，请检查网络后手动保存");
      }
    } finally {
      autosavingRef.current = false;
    }
  }

  // 自动保存触发：正文变化且未保存时，停止输入 2 秒触发；长时间连续输入时每 30 秒强制一次
  useEffect(() => {
    if (!activeId || contentLoading || aiBusy) return;
    if (!content.trim() || content === savedContentRef.current) return;
    const sinceLast = Date.now() - lastAutosaveAtRef.current;
    const delay = sinceLast >= 30000 ? 0 : 2000;
    if (autosaveTimerRef.current !== null) window.clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = window.setTimeout(() => {
      autosaveTimerRef.current = null;
      void autoSave();
    }, delay);
    return () => {
      if (autosaveTimerRef.current !== null) window.clearTimeout(autosaveTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [content, activeId, contentLoading, aiBusy]);

  async function handleAiRun() {
    if (!activeId || !content.trim()) return;
    if (!aiMode) return;
    const blockReason = canGenerateNow();
    if (blockReason) {
      setAiNotice(blockReason);
      return;
    }
    const requestedId = activeId; // 绑定发起时的章节，防止生成结果被写进其他章节
    setAiBusy(true);
    setAiNotice("");
    setError("");
    try {
      let result: { content: string; generation_record_id?: string | null };
      const overrides = readModelOverrides();
      if (aiMode === "continue") {
        result = await api.aiContinue(projectId, requestedId, content, aiMaxLength, aiInstruction || undefined, overrides);
      } else if (aiMode === "rewrite") {
        result = await api.aiRewrite(projectId, requestedId, content, aiInstruction || undefined, overrides);
      } else {
        result = await api.aiPolish(projectId, requestedId, content, aiStyle || undefined, overrides);
      }
      if (activeId !== requestedId) {
        // 生成期间切换了章节：结果属于原章节，丢弃并明确提示（不接受、不落库）
        setAiResult(null);
        setAiNotice("生成期间已切换章节，本次结果已丢弃（结果属于其他章节，避免写错章）。");
        return;
      }
      setAiResult({
        content: result.content || "",
        recordId: result.generation_record_id || null,
        chapterId: requestedId,
        planSummary: buildPlanSummaryText(),
        mode: aiMode || undefined,
      });
      setAiAcceptError("");
      setAiConflict(false);
      setAiNotice("");
      setIsDemo(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setAiResult(null);
        setAiNotice(`AI 服务未配置或调用失败：${err instanceof Error ? err.message : "未知错误"}`);
      }
    } finally {
      setAiBusy(false);
    }
  }

  /** 保存章节计划（syncTitle=true 时显式同步章节标题，不静默覆盖） */
  /** 生成下一章：AI 续写 → 创建章节节点 → 保存正文 → 跳转到新章 */
  async function handleGenerateNextChapter() {
    if (!activeId || !chapters.length) return;
    setGenNextBusy(true);
    setAiNotice("");
    setError("");
    try {
      const maxSort = Math.max(...chapters.map((c) => c.sort_order || 0), 0);
      const nextIndex = maxSort + 1;
      const current = chapters.find((c) => c.id === activeId);
      const res = await api.generateNextChapter(
        {
          setting: {
            title: current?.title || "未命名作品",
            chapter_count: 1,
            words_per_chapter: 3500,
            requested_mode: "auto",
          },
          project_id: projectId,
          chapter_id: activeId,
          chapter_index: nextIndex,
        },
        300000
      );
      // 创建章节节点（chapter 必须挂在卷节点下：取项目第一个 volume 作父级）
      let parentId: string | null = null;
      try {
        const allOutlines = await api.listOutlines(projectId);
        const volume = allOutlines.find((o) => o.outline_type === "volume");
        parentId = volume?.id || null;
      } catch {
        /* 父级获取失败则不带（后端会提示） */
      }
      const outline = await api.createOutline(projectId, {
        title: res.chapter.title || `第${nextIndex}章`,
        outline_type: "chapter",
        parent_id: parentId,
        sort_order: nextIndex,
        status: "draft",
      });
      // 保存生成正文（AI 端点不落库，需手动保存；生成记录已在后端留档）
      await api.saveChapterContent(projectId, outline.id, res.chapter.content, {
        source: "manual",
        change_summary: "AI 生成下一章",
      });
      savedContentRef.current = res.chapter.content;
      setAiNotice(`已生成并保存第 ${nextIndex} 章《${res.chapter.title}》。`);
      await loadChapters();
      setActiveId(outline.id);
      navigate(`/projects/${projectId}/chapters/${outline.id}`, { replace: true });
    } catch (err) {
      setAiNotice(
        `生成下一章失败：${err instanceof Error ? err.message : "未知错误"}`
      );
    } finally {
      setGenNextBusy(false);
    }
  }

  async function handleBatchGeneratePlans() {
    if (!chapters.length) return;
    const targets = chapters
      .filter((c) => c.id)
      .map((c) => c.id);
    if (!targets.length) return;
    setPlanBatchBusy(true);
    setPlanBatchProgress(`0/${targets.length}`);
    setPlanBatchError("");
    let done = 0;
    let failed = 0;
    try {
      for (let i = 0; i < targets.length; i += 5) {
        const batch = targets.slice(i, i + 5);
        const res = await api.batchGenerateChapterPlans(projectId, batch);
        for (const r of res.results) {
          if (r.ok) done += 1;
          else failed += 1;
        }
        setPlanBatchProgress(`${done + failed}/${targets.length}`);
      }
      setPlanBatchBusy(false);
      if (activeId) void loadChapterPlan(activeId);
      setPlanBatchError(
        failed > 0
          ? `已生成 ${done} 章计划，${failed} 章失败（可能 AI 未配置或超时，可重试）`
          : `全书 ${done} 章章节计划已生成。逐章审阅确认后，生成将严格遵循计划。`
      );
    } catch (err) {
      setPlanBatchBusy(false);
      setPlanBatchError(
        `批量生成中断（已完成 ${done + failed}/${targets.length}）：${err instanceof Error ? err.message : "未知错误"}。可再次点击继续。`
      );
    }
  }

  async function handlePlanSave(syncTitle = false) {
    if (!activeId || !planDraft) return;
    setPlanSaving(true);
    setPlanError("");
    try {
      const saved = await api.updateChapterPlan(projectId, activeId, planDraft, syncTitle);
      setChapterPlan(saved.plan);
      setPlanChapterNumber(saved.chapterNumber);
      setPlanSource(saved.planSource);
      setTitleSyncRequired(saved.titleSyncRequired);
      setPlanEdit(false);
      setPlanDraft(null);
      setPlanHint(syncTitle ? "章节计划已保存，并已将标题同步到章节标题。" : "章节计划已保存，生成将严格遵循该计划。");
      if (syncTitle) {
        await loadChapters();
        await loadContent(activeId);
      }
      void loadOutlineStatus();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 标题冲突保护：不静默覆盖，弹窗让用户明确选择
        setPlanError("");
        setTitleConflict({ message: err.message || "计划标题与已确认大纲不一致" });
      } else {
        setPlanError(err instanceof Error ? err.message : "章节计划保存失败");
      }
    } finally {
      setPlanSaving(false);
    }
  }

  /** 计划符合度检查：不修改正文，只展示结构化问题 */
  async function runPlanCompliance() {
    if (!activeId) return;
    setPlanCheckBusy(true);
    setPlanCheckError("");
    try {
      const res = await api.checkPlanCompliance(projectId, activeId);
      setPlanCompliance(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setPlanCheckError(err instanceof Error ? err.message : "计划符合度检查失败");
      }
    } finally {
      setPlanCheckBusy(false);
    }
  }

  /** 按计划修订：把符合度检查问题作为改写指令发起 rewrite */
  function reviseByPlan() {
    if (!planCompliance || !activeId) return;
    const lines = planCompliance.issues.map(
      (i) => `[${i.severity}] ${i.message}${i.suggestion ? ` 建议：${i.suggestion}` : ""}`
    );
    setAiMode("rewrite");
    setAiInstruction("请严格按当前章节计划修订以下问题：" + lines.join("；"));
    setAiResult(null);
    setAiNotice("");
  }

  /** 计划编辑表单：保存后才可生成；标题不同步时显式提示并允许同步 */
  function renderPlanEditor() {
    const draft = planDraft || {};
    const setDraft = (patch: Partial<ChapterPlanData>) => setPlanDraft({ ...draft, ...patch });
    const listKeys = ["sceneGoals", "requiredCharacters", "requiredLocations", "requiredEvents", "forbiddenReveals"] as const;
    const setList = (key: (typeof listKeys)[number], value: string) =>
      setDraft({ [key]: value.split("\n").map((s) => s.trim()).filter(Boolean) } as Partial<ChapterPlanData>);
    const listToText = (arr?: string[]) => (arr || []).join("\n");
    const titleDiffers =
      Boolean(draft.title && activeChapter?.title && draft.title.trim() !== activeChapter.title.trim());
    return (
      <div className="plan-editor">
        <div className="field">
          <label>章节标题（章节表为单一事实来源；不同步保存不会覆盖章节标题）</label>
          <input value={draft.title || ""} onChange={(e) => setDraft({ title: e.target.value })} />
        </div>
        <div className="field">
          <label>本章目标 / 摘要</label>
          <textarea rows={3} value={draft.summary || ""} onChange={(e) => setDraft({ summary: e.target.value })} />
        </div>
        <div className="field">
          <label>必须发生事件（每行一个）</label>
          <textarea rows={3} value={listToText(draft.requiredEvents)} onChange={(e) => setList("requiredEvents", e.target.value)} />
        </div>
        <div className="field">
          <label>本章结束状态</label>
          <textarea rows={2} value={draft.expectedOutcome || ""} onChange={(e) => setDraft({ expectedOutcome: e.target.value })} />
        </div>
        <div className="field">
          <label>禁止提前揭露（每行一个）</label>
          <textarea rows={3} value={listToText(draft.forbiddenReveals)} onChange={(e) => setList("forbiddenReveals", e.target.value)} />
        </div>
        <div className="field">
          <label>场景目标（每行一个，可选）</label>
          <textarea rows={2} value={listToText(draft.sceneGoals)} onChange={(e) => setList("sceneGoals", e.target.value)} />
        </div>
        <div className="field">
          <label>必须出场人物（每行一个，可选）</label>
          <textarea rows={2} value={listToText(draft.requiredCharacters)} onChange={(e) => setList("requiredCharacters", e.target.value)} />
        </div>
        <div className="field">
          <label>必须出现地点（每行一个，可选）</label>
          <textarea rows={2} value={listToText(draft.requiredLocations)} onChange={(e) => setList("requiredLocations", e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => void handlePlanSave(false)} disabled={planSaving}>
            {planSaving ? "保存中…" : "保存计划"}
          </button>
          {titleDiffers && (
            <button onClick={() => void handlePlanSave(true)} disabled={planSaving} className="secondary">
              保存并同步标题到章节（{activeChapter?.title} → {draft.title}）
            </button>
          )}
          <button
            className="secondary"
            onClick={() => {
              setPlanEdit(false);
              setPlanDraft(null);
              setPlanError("");
            }}
          >
            取消
          </button>
        </div>
        <div className="plan-save-tip">保存后生成按钮才会启用；生成的正文将严格遵循该计划，不会静默覆盖章节标题。</div>
      </div>
    );
  }

  /** 接受为新版本：携带 generation_record_id 创建 chapter_version（ai_* 来源），后端更新正文 */
  async function persistAiResult(ignoreVersion: boolean) {
    if (!activeId || !aiResult) return;
    if (aiResult.chapterId && aiResult.chapterId !== activeId) {
      // 结果属于其他章节：拒绝保存，防止写错章
      setAiAcceptError("该结果属于其他章节（生成期间切换过章节）。请丢弃后回到原章节重新生成。");
      return;
    }
    // 生成方式优先取结果里记录的那一份：aiMode 可能已被别处清空，导致旧代码静默 return（表现为「点了没反应」）
    const mode = aiResult.mode || aiMode;
    if (!mode) {
      setAiAcceptError("没有记录本次生成的方式，无法确定版本来源。请点「重新生成」再接受一次。");
      return;
    }
    setAiAccepting(true);
    setAiAcceptError("");
    setAiConflict(false);
    setSavedHint("");
    try {
      const recordId = aiResult.recordId || null;
      const data = await api.saveChapterContent(projectId, activeId, aiResult.content, {
        // 没有生成记录时不能标 ai_*（后端 400「AI 来源版本必须携带 generation_record_id」），
        // 退回 manual 至少把正文保住，并在版本说明里写清楚
        source: recordId ? AI_SOURCE_MAP[mode] : "manual",
        generation_record_id: recordId,
        // undefined 会被 JSON.stringify 丢掉，后端收到缺省值即跳过乐观锁校验
        base_version: ignoreVersion ? undefined : version,
        change_summary: `接受${AI_MODE_LABEL[mode]}结果${recordId ? "" : "（无生成记录，按手动保存）"}`,
      });
      setContent(data.content || "");
      savedContentRef.current = data.content || "";
      clearDraft(projectId, activeId);
      setDraftHint("");
      setVersion(data.version);
      setWordCount(data.word_count);
      setSavedHint(`已接受为新版本 v${data.version}`);
      setIsDemo(false);
      setError("");
      setAiResult(null);
      setAiMode(null);
      setAiAcceptError("");
      setAiConflict(false);
      if (showMemory) void loadMemory();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAiAcceptError("登录已过期，请重新登录后再试。");
      } else if (err instanceof ApiError && err.status === 409) {
        if (err.code === "generation_record_chapter_mismatch") {
          setAiConflict(false);
          setAiAcceptError("该生成结果不属于当前章节（生成期间切换过章节或结果已过期）。请丢弃后回到原章节重新生成。");
        } else if (err.code === "generation_record_already_consumed") {
          setAiConflict(false);
          setAiAcceptError("该生成结果已被接受过（可能在其他页面/标签页已处理）。请丢弃并重新生成。");
        } else {
          setAiConflict(true);
          setAiAcceptError(
            `正文在别处已被更新：你基于 v${version}，服务器版本已经变了。可以点「仍然覆盖」用这份结果覆盖，或先「丢弃」再重新读取最新正文。`
          );
        }
      } else if (err instanceof ApiError && err.status === 400) {
        setAiAcceptError(`后端拒绝保存：${err.message}`);
      } else if (err instanceof ApiError && err.status === 404) {
        setAiAcceptError(`保存失败：${err.message}`);
      } else {
        setAiAcceptError(err instanceof Error ? err.message : "接受失败");
      }
    } finally {
      setAiAccepting(false);
    }
  }

  async function acceptAiResult() {
    await persistAiResult(false);
  }

  /** 版本冲突后强制覆盖：不提交 base_version，后端跳过乐观锁 */
  async function acceptAiResultForce() {
    await persistAiResult(true);
  }

  function discardAiResult() {
    setAiResult(null);
    setAiMode(null);
    setAiAcceptError("");
    setAiConflict(false);
  }

  // ---- 章节记忆抽屉（第二期）----
  async function loadMemory() {
    if (!activeId) return;
    setMemoryLoading(true);
    setMemoryError("");
    try {
      const data = await api.getChapterMemory(projectId, activeId);
      setMemory(data);
      setMemoryHint("");
    } catch {
      setMemory(null);
    } finally {
      setMemoryLoading(false);
    }
  }

  function openMemory() {
    setShowMemory(true);
    setMemoryEdit(false);
    setMemoryHint("");
    void loadMemory();
  }

  async function saveMemory() {
    if (!activeId || !memory) return;
    let memObj: ChapterMemoryData["memory"];
    try {
      memObj = memoryJson.trim() ? JSON.parse(memoryJson) : {};
    } catch {
      setMemoryError("结构化记忆 JSON 格式错误，请检查后重试");
      return;
    }
    setMemoryBusy(true);
    setMemoryError("");
    setMemoryHint("");
    try {
      const data = await api.updateChapterMemory(projectId, activeId, {
        summary: memorySummary,
        memory: memObj,
      });
      setMemory(data);
      setMemoryEdit(false);
      setMemoryHint(`已保存并确认（${data.status} / ${data.source === "ai" ? "AI 生成" : "人工编辑"}）`);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function regenerateMemory() {
    if (!activeId) return;
    setMemoryBusy(true);
    setMemoryError("");
    setMemoryHint("");
    try {
      const data = await api.generateChapterMemory(projectId, activeId);
      setMemory(data);
      setMemoryEdit(false);
      setMemoryHint(`已重新生成（${data.status} / AI 生成）`);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "重新生成失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function confirmMemory() {
    if (!activeId || !memory) return;
    setMemoryBusy(true);
    setMemoryError("");
    setMemoryHint("");
    try {
      await api.confirmChapterMemory(projectId, activeId);
      await loadMemory();
      setMemoryHint("已确认，续写上下文将优先引用本章记忆");
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "确认失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  // ---- 第四期「写错能发现」：一致性检查抽屉 ----
  async function runCheck() {
    if (!activeId || !content) return;
    const title = chapters.find((c) => c.id === activeId)?.title || "";
    setCheckLoading(true);
    setCheckError("");
    setCheckHint("");
    try {
      const data = await api.checkConsistency(projectId, activeId, content, title);
      setCheckResult(data);
      setAcceptedKeys([]);
      if (data.reminders?.length > 0) {
        setCheckHint(`发现 ${data.reminders.length} 条未回收伏笔提醒，可在伏笔管理中处理`);
      }
    } catch (err) {
      setCheckResult(null);
      setCheckError(err instanceof Error ? err.message : "检查失败");
    } finally {
      setCheckLoading(false);
    }
  }

  function openCheck() {
    setShowCheck(true);
    setCheckHint("");
    void runCheck();
  }

  async function acceptProposal(proposal: ConsistencyUpdate, idx: number) {
    if (!activeId || !checkResult) return;
    const key = `${proposal.category}-${idx}`;
    if (acceptedKeys.includes(key)) return;
    setCheckAccepting(true);
    setCheckError("");
    setCheckHint("");
    try {
      await api.createSuggestion(projectId, {
        chapter_id: activeId,
        category: proposal.category,
        title: proposal.title || "一致性检查建议",
        description: proposal.description || "",
        payload: proposal.payload || {},
      });
      setAcceptedKeys((prev) => [...prev, key]);
      setCheckHint("已转入变更确认流程，请在「变更确认」页面审核采纳");
    } catch (err) {
      setCheckError(err instanceof Error ? err.message : "采纳失败");
    } finally {
      setCheckAccepting(false);
    }
  }

  // ---- 历史版本抽屉 ----
  async function openVersions() {
    setShowVersions(true);
    if (!activeId) return;
    setVersionsLoading(true);
    setVersionsError("");
    try {
      const list = await api.listChapterVersions(projectId, activeId);
      setVersions(list);
      setSelectedVersion(list.find((v) => v.is_current) || list[0] || null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setVersionsError("版本历史加载失败（演示模式不支持）");
        setVersions([]);
        setSelectedVersion(null);
      }
    } finally {
      setVersionsLoading(false);
    }
  }

  async function handleRestore(vid: string) {
    if (!activeId) return;
    setVersionsBusy(true);
    setVersionsError("");
    try {
      const data = await api.restoreChapterVersion(projectId, activeId, vid);
      setContent(data.content || "");
      setVersion(data.version);
      setWordCount(data.word_count);
      setSavedHint(`已恢复为新版本 v${data.version}`);
      setIsDemo(false);
      const list = await api.listChapterVersions(projectId, activeId);
      setVersions(list);
      setSelectedVersion(list.find((v) => v.is_current) || list[0] || null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setVersionsError(err instanceof Error ? err.message : "恢复失败");
      }
    } finally {
      setVersionsBusy(false);
    }
  }

  // ---- 生成记录抽屉 ----
  async function openGenerations() {
    setShowGenerations(true);
    if (!activeId) return;
    setGenerationsLoading(true);
    setGenerationsError("");
    try {
      const data = await api.listGenerations(projectId, { page_size: 50 });
      setGenerations(data.items);
      // 拉取版本映射（id -> version），用于生成记录追溯行展示「已确认大纲 vN」
      try {
        const vs = await api.listOutlinePlanVersions(projectId);
        const map: Record<string, number> = {};
        for (const v of vs) map[v.id] = v.version;
        setOutlineVersionMap(map);
      } catch {
        setOutlineVersionMap({});
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setGenerationsError("生成记录加载失败（演示模式不支持）");
        setGenerations([]);
      }
    } finally {
      setGenerationsLoading(false);
    }
  }

  // ---- 「已确认大纲版本化」：版本状态 / 确认新版本 / 历史版本 / 标题冲突保护 ----
  async function loadOutlineStatus() {
    setOutlineStatusLoading(true);
    try {
      const st = await api.getOutlinePlanStatus(projectId);
      setOutlineStatus(st);
    } catch {
      // 后端不可用/未迁移时静默降级，不影响主流程
      setOutlineStatus(null);
    } finally {
      setOutlineStatusLoading(false);
    }
  }

  /** 未确认修改提示：章节计划被保存或标题同步后刷新状态 */
  function refreshOutlineStatus() {
    void loadOutlineStatus();
  }

  /** 需求一：先 dry_run 拿影响面预览，再展示确认卡片（不静默替换） */
  async function handleConfirmNewVersion() {
    setConfirmPlanBusy(true);
    setOutlineHint("");
    setError("");
    try {
      const res = await api.confirmOutlinePlan(projectId, true);
      setConfirmPreview(res);
      if (res.unchanged) {
        setOutlineHint("当前目录与章节计划与已确认版本一致，无需创建新版本。");
        setConfirmPreview(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认预览失败");
      setConfirmPreview(null);
    } finally {
      setConfirmPlanBusy(false);
    }
  }

  /** 用户确认影响面后持久化新版本 */
  async function doConfirmNewVersion() {
    setConfirmPlanBusy(true);
    setOutlineHint("");
    setError("");
    try {
      const res = await api.confirmOutlinePlan(projectId, false);
      setConfirmPreview(null);
      setOutlineHint(`已确认新版本 v${res.version.version}，旧 confirmed 已标记为 superseded。`);
      await loadOutlineStatus();
      await loadChapters();
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认新版本失败");
    } finally {
      setConfirmPlanBusy(false);
    }
  }

  function cancelConfirmNewVersion() {
    setConfirmPreview(null);
  }

  /** 需求五：大纲历史版本抽屉 */
  async function openOutlineVersions() {
    setShowOutlineVersions(true);
    setOutlineVersionsLoading(true);
    setOutlineVersionsError("");
    try {
      const vs = await api.listOutlinePlanVersions(projectId);
      setOutlineVersions(vs);
      const map: Record<string, number> = {};
      for (const v of vs) map[v.id] = v.version;
      setOutlineVersionMap(map);
      const active = vs.find((v) => v.status === "confirmed") || vs[0];
      if (active) {
        setSelectedOutlineVersionId(active.id);
        const detail = await api.getOutlinePlanVersion(projectId, active.id);
        setSelectedOutlineVersion(detail);
      } else {
        setSelectedOutlineVersionId("");
        setSelectedOutlineVersion(null);
      }
    } catch (err) {
      setOutlineVersionsError("大纲版本加载失败（后端未启用版本化或演示模式不支持）");
      setOutlineVersions([]);
      setSelectedOutlineVersion(null);
    } finally {
      setOutlineVersionsLoading(false);
    }
  }

  async function selectOutlineVersion(versionId: string) {
    setSelectedOutlineVersionId(versionId);
    setSelectedOutlineVersion(null);
    setOutlineVersionsLoading(true);
    setOutlineVersionsError("");
    try {
      const detail = await api.getOutlinePlanVersion(projectId, versionId);
      setSelectedOutlineVersion(detail);
    } catch (err) {
      setOutlineVersionsError(err instanceof Error ? err.message : "版本详情加载失败");
    } finally {
      setOutlineVersionsLoading(false);
    }
  }

  /** 解析 chapter_plans_snapshot：稳定按 chapterId 映射的章节计划快照列表 */
  function chapterPlansFromSnapshot(detail: OutlinePlanVersionDetail | null): { chapterNumber: number; title: string; conflict: boolean }[] {
    if (!detail?.chapter_plans_snapshot) return [];
    const raw = detail.chapter_plans_snapshot;
    const out: { chapterNumber: number; title: string; conflict: boolean }[] = [];
    if (Array.isArray(raw)) {
      for (const item of raw as Record<string, unknown>[]) {
        out.push({
          chapterNumber: Number(item?.chapterNumber || 0),
          title: String(item?.title || ""),
          conflict: false,
        });
      }
      return out;
    }
    const obj = raw as Record<string, unknown>;
    for (const key of Object.keys(obj)) {
      const item = obj[key] as Record<string, unknown> | undefined;
      if (!item || typeof item !== "object") continue;
      out.push({
        chapterNumber: Number((item as Record<string, unknown>).chapterNumber || 0),
        title: String((item as Record<string, unknown>).title || ""),
        conflict: Boolean((item as Record<string, unknown>).titleConflict),
      });
    }
    return out.sort((a, b) => a.chapterNumber - b.chapterNumber);
  }

  /** 从 generation_record 追溯字段解析「第 N 章」（chapter_plan_snapshot 内含 chapterNumber） */
  function traceChapterNumber(g: GenerationRecord): number {
    if (!g.chapter_plan_snapshot) return 0;
    try {
      const parsed = JSON.parse(g.chapter_plan_snapshot) as Record<string, unknown>;
      return Number(parsed?.chapterNumber || 0);
    } catch {
      return 0;
    }
  }

  /** 需求四：标题冲突弹窗——用户明确选择「采用新标题并同步」后才允许同步 */
  async function adoptNewTitleAndSync() {
    if (!activeId || !planDraft) return;
    setTitleConflictBusy(true);
    setError("");
    try {
      // 先确认新大纲版本：把当前计划标题登记为新的 confirmed 计划标题
      const res = await api.confirmOutlinePlan(projectId, false);
      if (!res.version) throw new Error("确认新大纲版本失败");
      setTitleConflict(null);
      setOutlineHint(`已确认新版本 v${res.version.version}（新计划标题成为已确认标题），正在同步章节标题…`);
      await loadOutlineStatus();
      // 再同步标题：此时 confirmed 计划标题与 plan.title 一致，不再触发 409
      await handlePlanSave(true);
      setTitleConflict(null);
      setOutlineHint(`已确认新版本 v${res.version.version}，并已将章节标题同步为新标题。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "采用新标题并同步失败");
    } finally {
      setTitleConflictBusy(false);
    }
  }

  const activeChapter = chapters.find((c) => c.id === activeId);

  const aiModeLabel =
    aiMode === "continue" ? "AI 续写" : aiMode === "polish" ? "AI 润色" : "AI 改写";

  const diffLines = selectedVersion ? simpleDiff(content, selectedVersion.content) : [];

  return (
    <div>
      <div className="topbar">
        <h1>
          章节编辑
          {isDemo && <span className="demo-badge">演示数据</span>}
        </h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="secondary" onClick={() => void handleReadabilityReport()} disabled={readabilityBusy}>
            {readabilityBusy ? "分析中…" : "追读力分析"}
          </button>
          <button
            className="secondary"
            onClick={() => {
              stashDraftIfDirty();
              navigate(`/projects/${projectId}`);
            }}
          >
            ← 返回作品详情
          </button>
          <button
            className="secondary"
            onClick={() => {
              stashDraftIfDirty();
              navigate("/projects");
            }}
          >
            作品库
          </button>
        </div>
      </div>

      <ReadabilityDrawer report={readability} onClose={() => setReadability(null)} />

      {error && <div className="error-box">{error}</div>}

      {draftHint && (
        <div
          style={{
            background: "#fff8e1",
            color: "#7a5c00",
            border: "1px solid #f0d98c",
            padding: "8px 12px",
            borderRadius: 6,
            marginBottom: 10,
            fontSize: 13,
          }}
        >
          {draftHint}
        </div>
      )}

      {impactHint && (
        <div
          style={{
            background: "#e8f0fe",
            color: "#1a4f8b",
            border: "1px solid #b8d0f0",
            padding: "8px 12px",
            borderRadius: 6,
            marginBottom: 10,
            fontSize: 13,
          }}
        >
          {impactHint}
        </div>
      )}

      {loading ? (
        <div className="empty">
          <span className="spin" /> 正在加载章节…
        </div>
      ) : (
        <div className="chapter-layout">
          <div className="chapter-list">
            <div className="chapter-list-title">章节大纲（{chapters.length}）</div>
            <div style={{ marginBottom: 8 }}>
              <button
                className="secondary"
                disabled={planBatchBusy}
                onClick={() => void handleBatchGeneratePlans()}
                title="逐章 AI 生成场景合同（每章目标/事件/禁写内容），每批 5 章"
              >
                {planBatchBusy ? `生成计划中 ${planBatchProgress}…` : "✦ 生成全书章节计划"}
              </button>
              {planBatchError && (
                <div style={{ fontSize: 12, marginTop: 6, color: "var(--text-dim)" }}>
                  {planBatchError}
                </div>
              )}
            </div>
            {chapters.length === 0 ? (
              <div className="empty">暂无章节</div>
            ) : (
              chapters.map((c) => (
                <div
                  key={c.id}
                  className={`chapter-item ${c.id === activeId ? "active" : ""}`}
                  onClick={() => {
                    if (c.id === activeId) return;
                    stashDraftIfDirty();
                    setActiveId(c.id);
                    navigate(`/projects/${projectId}/chapters/${c.id}`, { replace: true });
                  }}
                >
                  {c.title}
                  {(() => {
                    const v = validationSummary?.items.find((x) => x.chapter_id === c.id);
                    if (!v || !v.validated_at) {
                      return <span title="未校验"> ⚪</span>;
                    }
                    if (v.blocking > 0) return <span title={`阻断问题 ${v.blocking} 个`}> 🔴</span>;
                    if (v.warnings > 0) return <span title={`警告 ${v.warnings} 个`}> 🟡</span>;
                    return <span title="校验通过"> 🟢</span>;
                  })()}
                  <small>
                    {c.status === "confirmed" ? "已确认" : c.status === "archived" ? "已归档" : "草稿"}
                    {c.summary ? ` · ${c.summary}` : ""}
                  </small>
                </div>
              ))
            )}
          </div>

          <div className="editor">
            <div className="editor-toolbar">
              <div>
                <div className="editor-title">{activeChapter?.title || "选择章节"}</div>
                {version > 0 && (
                  <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                    {autosaveHint ? (
                      <span style={{ color: "var(--ok, #2e7d32)" }}>{autosaveHint} · </span>
                    ) : null}
                    版本 v{version} · 实时字数 {liveCount}
                    {wordCount > 0 ? ` · 后端字数 ${wordCount}` : ""}
                  </div>
                )}
              </div>
              {(outlineStatus || outlineHint) && (
                <div className="hint-box" style={{ marginBottom: 10 }}>
                  {outlineStatus?.hasConfirmed && outlineStatus.activeConfirmed && (
                    <span>
                      已确认大纲 <strong>v{outlineStatus.activeConfirmed.version}</strong>
                      {outlineStatus.hasUnconfirmedChanges ? " · " : ""}
                    </span>
                  )}
                  {outlineStatus?.hasUnconfirmedChanges && (
                    <span className="warn-text">存在未确认修改（目录/章节计划与已确认版本不一致）</span>
                  )}
                  {outlineHint && <div style={{ marginTop: 4 }}>{outlineHint}</div>}
                </div>
              )}
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                {savedHint && <span className="save-hint">{savedHint}</span>}
                <button className="secondary" onClick={() => { setAiMode("continue"); }} disabled={!activeId}>
                  生成
                </button>
                <button className="secondary" onClick={() => void openVersions()} disabled={!activeId}>
                  历史版本
                </button>
                <button className="secondary" onClick={() => void openGenerations()} disabled={!activeId}>
                  生成记录
                </button>
                <button className="secondary" onClick={() => void handleConfirmNewVersion()} disabled={outlineStatusLoading || !activeId}>
                  {outlineStatus?.hasUnconfirmedChanges ? "存在未确认修改 · 确认新版本" : "确认新版本"}
                </button>
                <button className="secondary" onClick={() => void openOutlineVersions()} disabled={outlineStatusLoading}>
                  大纲版本
                </button>
                <button className="secondary" onClick={openMemory} disabled={!activeId}>
                  章节记忆
                </button>
                <button className="secondary" onClick={openCheck} disabled={!activeId || !content}>
                  检查结果
                </button>
                <button className="secondary" onClick={openSelectionRewrite} disabled={!activeId || contentLoading} title="先选中一段正文，再点此改写该段（不整章重写）">
                  ✂ 改写选区
                </button>
                <button onClick={() => void handleGenerateNextChapter()} disabled={!activeId || genNextBusy || aiBusy} title="基于已确认大纲生成下一章，自动创建章节并保存正文">
                  {genNextBusy ? "生成中…" : "✦ 生成下一章"}
                </button>
                <button onClick={handleSave} disabled={!activeId || contentLoading}>
                  保存正文
                </button>
              </div>
            </div>
            {contentLoading ? (
              <div className="empty">
                <span className="spin" /> 正在加载正文…
              </div>
            ) : activeId ? (
              <textarea
                ref={editorTextareaRef}
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="在这里开始创作你的章节正文…"
              />
            ) : (
              <div className="empty">请先在左侧选择章节</div>
            )}

            {/* AI 写作面板 */}
            {activeId && (
              <div className="ai-panel">
                <div className="ai-toolbar">
                  <span className="ai-title">AI 写作助手</span>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      className={`ai-mode-btn ${aiMode === "continue" ? "active" : ""}`}
                      onClick={() => {
                        setAiMode(aiMode === "continue" ? null : "continue");
                        setAiResult(null);
                      }}
                    >
                      续写
                    </button>
                    <button
                      className={`ai-mode-btn ${aiMode === "polish" ? "active" : ""}`}
                      onClick={() => {
                        setAiMode(aiMode === "polish" ? null : "polish");
                        setAiResult(null);
                      }}
                    >
                      润色
                    </button>
                    <button
                      className={`ai-mode-btn ${aiMode === "rewrite" ? "active" : ""}`}
                      onClick={() => {
                        setAiMode(aiMode === "rewrite" ? null : "rewrite");
                        setAiResult(null);
                      }}
                    >
                      改写
                    </button>
                  </div>
                </div>
                {/* 当前章节计划（第 4 项需求：显示第N章/标题/摘要；无计划阻止直接生成；编辑保存后才可生成） */}
                <div className="plan-panel">
                  <div className="plan-head">
                    <span className="plan-title">当前章节计划</span>
                    {planLoading && <span className="spin" style={{ marginLeft: 8 }} />}
                    {planSource === "manual" && <span className="plan-badge plan-manual">结构化计划</span>}
                    {planSource === "legacy" && <span className="plan-badge plan-legacy">旧细纲回退</span>}
                    {planSource === "none" && <span className="plan-badge plan-none">无计划</span>}
                    {!planEdit && (
                      <button
                        className="secondary"
                        style={{ marginLeft: "auto" }}
                        onClick={() => {
                          setPlanEdit(true);
                          setPlanDraft({ ...(chapterPlan || {}) });
                          setPlanError("");
                        }}
                        disabled={planLoading}
                      >
                        编辑计划
                      </button>
                    )}
                  </div>
                  {planError && <div className="error-box" style={{ marginBottom: 0, marginTop: 8 }}>{planError}</div>}
                  {planHint && <div className="hint-box" style={{ marginBottom: 0, marginTop: 8 }}>{planHint}</div>}
                  {planEdit && renderPlanEditor()}
                  {!planEdit && (
                    <div className="plan-summary">
                      <div className="plan-summary-line">
                        <strong>第 {planChapterNumber || "?"} 章</strong>
                        {activeChapter?.title || chapterPlan?.title
                          ? ` · 《${activeChapter?.title || chapterPlan?.title}》`
                          : " · （无标题）"}
                      </div>
                      {chapterPlan?.summary && (
                        <div className="plan-summary-line">目标/摘要：{chapterPlan.summary}</div>
                      )}
                      {chapterPlan?.sceneGoals?.length ? (
                        <div className="plan-summary-line">场景目标：{chapterPlan.sceneGoals.join("；")}</div>
                      ) : null}
                      {chapterPlan?.requiredCharacters?.length ? (
                        <div className="plan-summary-line">必须出场人物：{chapterPlan.requiredCharacters.join("；")}</div>
                      ) : null}
                      {chapterPlan?.requiredLocations?.length ? (
                        <div className="plan-summary-line">必须出现地点：{chapterPlan.requiredLocations.join("；")}</div>
                      ) : null}
                      {chapterPlan?.requiredEvents?.length ? (
                        <div className="plan-summary-line">必须发生事件：{chapterPlan.requiredEvents.join("；")}</div>
                      ) : null}
                      {chapterPlan?.expectedOutcome && (
                        <div className="plan-summary-line">本章结束状态：{chapterPlan.expectedOutcome}</div>
                      )}
                      {chapterPlan?.forbiddenReveals?.length ? (
                        <div className="plan-summary-line plan-forbidden">禁止提前揭露：{chapterPlan.forbiddenReveals.join("；")}</div>
                      ) : null}
                      {planSource === "none" && (
                        <label className="plan-free-label">
                          <input
                            type="checkbox"
                            checked={freeCreateConfirm}
                            onChange={(e) => setFreeCreateConfirm(e.target.checked)}
                          />
                          无计划自由创作（本次生成不依据章节计划，需明确确认）
                        </label>
                      )}
                      {titleSyncRequired && (
                        <div className="plan-sync-tip">计划标题与章节标题不一致，可编辑计划后“保存并同步标题到章节”。</div>
                      )}
                    </div>
                  )}
                  {/* 计划符合度检查（第 5 项需求：不修改正文，只提示并按计划修订/重新生成） */}
                  <div className="plan-check-row">
                    <button
                      className="secondary"
                      onClick={() => void runPlanCompliance()}
                      disabled={planCheckBusy || !activeChapter?.id}
                    >
                      {planCheckBusy ? "检查中…" : "计划符合度检查"}
                    </button>
                  </div>
                  {planCheckError && <div className="error-box" style={{ marginBottom: 0, marginTop: 8 }}>{planCheckError}</div>}
                  {planCompliance && (
                    <div className="plan-compliance">
                      <div className="plan-compliance-head">
                        <strong>计划符合度：{planCompliance.score} 分</strong>
                        <span className="plan-compliance-time">检查于 {planCompliance.checkedAt.replace("T", " ").slice(0, 19)}</span>
                      </div>
                      {planCompliance.issues.length === 0 ? (
                        <div className="hint-box" style={{ marginBottom: 0 }}>未发现问题，正文符合当前章节计划。</div>
                      ) : (
                        <>
                          <ul className="plan-issue-list">
                            {planCompliance.issues.map((issue, idx) => (
                              <li key={idx} className={`plan-issue plan-issue-${issue.severity}`}>
                                <span className="plan-issue-tag">{issue.severity}</span>
                                <span>{issue.message}</span>
                                {issue.evidence && <div className="plan-issue-evidence">证据：{issue.evidence}</div>}
                                {issue.suggestion && <div className="plan-issue-suggestion">建议：{issue.suggestion}</div>}
                              </li>
                            ))}
                          </ul>
                          <div style={{ display: "flex", gap: 8 }}>
                            <button onClick={reviseByPlan}>按计划修订</button>
                            <button
                              className="secondary"
                              onClick={() => {
                                setAiMode("rewrite");
                                setAiInstruction("请严格按当前章节计划重新生成本章正文，完整覆盖计划要求。");
                              }}
                            >
                              重新生成
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
                {aiMode && (
                  <div className="ai-form">
                    {aiMode === "continue" && (
                      <div className="ai-row">
                        <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                          <label>续写要求（可选）</label>
                          <input
                            value={aiInstruction}
                            onChange={(e) => setAiInstruction(e.target.value)}
                            placeholder="例如：补充一段人物心理描写"
                          />
                        </div>
                        <div className="field" style={{ width: 160, marginBottom: 0 }}>
                          <label>期望长度</label>
                          <input
                            type="number"
                            value={aiMaxLength}
                            onChange={(e) => setAiMaxLength(Number(e.target.value))}
                            min={100}
                            max={5000}
                          />
                        </div>
                      </div>
                    )}
                    {aiMode === "polish" && (
                      <div className="ai-row">
                        <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                          <label>目标风格（可选）</label>
                          <input
                            value={aiStyle}
                            onChange={(e) => setAiStyle(e.target.value)}
                            placeholder="例如：语言克制、节奏紧凑"
                          />
                        </div>
                      </div>
                    )}
                    {aiMode === "rewrite" && (
                      <div className="ai-row">
                        <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                          <label>改写要求（可选）</label>
                          <input
                            value={aiInstruction}
                            onChange={(e) => setAiInstruction(e.target.value)}
                            placeholder="例如：增强冲突感、突出人物动机"
                          />
                        </div>
                      </div>
                    )}
                    <div style={{ display: "flex", gap: 10 }}>
                      <button onClick={() => void handleAiRun()} disabled={aiBusy || !content.trim()}>
                        {aiBusy ? `生成中…已 ${aiElapsed} 秒` : `开始${aiModeLabel}`}
                      </button>
                      <button className="secondary" onClick={() => setAiMode(null)}>
                        取消
                      </button>
                    </div>
                    {aiBusy && (
                      <div className="hint" style={{ marginTop: 6 }}>
                        模型正在逐字生成，写完整章通常需要 40~70 秒，请勿关闭页面。
                      </div>
                    )}
                  </div>
                )}
                {aiNotice && (
                  <div className="error-box" style={{ marginBottom: 0, marginTop: 10 }}>
                    {aiNotice}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 生成预览弹窗：丢弃 / 重新生成 / 接受为新版本 */}
      {aiResult && (
        <div className="modal-overlay" onClick={() => setAiResult(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              {(aiResult.mode ? AI_MODE_LABEL[aiResult.mode] : aiModeLabel)}结果预览
              {aiResult.recordId && (
                <span className="gen-record-tag">生成记录 {aiResult.recordId.slice(0, 8)}…</span>
              )}
            </div>
            {aiResult.planSummary && (
              <div className="modal-plan-summary">
                <div className="modal-plan-summary-title">本次生成依据的章节计划</div>
                <pre className="modal-plan-summary-pre">{aiResult.planSummary}</pre>
              </div>
            )}
            <div className="modal-body">
              <pre className="modal-pre">{aiResult.content}</pre>
            </div>
            {aiAcceptError && <div className="error-box modal-accept-error">{aiAcceptError}</div>}
            <div className="modal-actions">
              <button className="secondary" onClick={discardAiResult}>
                丢弃
              </button>
              <button className="secondary" onClick={() => void handleAiRun()} disabled={aiBusy}>
                {aiBusy ? `生成中…已 ${aiElapsed} 秒` : "重新生成"}
              </button>
              {aiConflict && (
                <button onClick={() => void acceptAiResultForce()} disabled={aiAccepting}>
                  {aiAccepting ? "保存中…" : "仍然覆盖"}
                </button>
              )}
              <button onClick={() => void acceptAiResult()} disabled={!aiResult.content || aiAccepting}>
                {aiAccepting ? "保存中…" : "接受为新版本"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 历史版本抽屉（批次 6 拆分） */}
      <VersionHistoryDrawer
        open={showVersions}
        title={activeChapter?.title || ""}
        versions={versions}
        loading={versionsLoading}
        error={versionsError}
        busy={versionsBusy}
        selected={selectedVersion}
        currentVersion={version}
        diffLines={diffLines}
        onSelect={setSelectedVersion}
        onRestore={(id) => void handleRestore(id)}
        onClose={() => setShowVersions(false)}
      />

      {/* 生成记录抽屉（批次 6 拆分） */}
      <GenerationRecordsDrawer
        open={showGenerations}
        records={generations}
        loading={generationsLoading}
        error={generationsError}
        expandedId={expandedGen}
        versionMap={outlineVersionMap}
        traceChapter={traceChapterNumber}
        onToggle={setExpandedGen}
        onClose={() => setShowGenerations(false)}
      />

      {/* 「已确认大纲版本化」：确认新版本影响面卡片（先预览后持久化，不静默替换） */}
      {confirmPreview && (
        <div className="drawer-overlay" onClick={() => cancelConfirmNewVersion()}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="drawer-title">确认新大纲版本</span>
              <button className="secondary" onClick={() => cancelConfirmNewVersion()}>取消</button>
            </div>
            <div className="drawer-body">
              <div className="drawer-item-meta">
                将创建 <strong>v{confirmPreview.version.version}</strong>（status: {confirmPreview.version.status}）
                {confirmPreview.version.confirmed_at ? ` · 确认时间 ${formatTime(confirmPreview.version.confirmed_at)}` : ""}
                {confirmPreview.version.confirmed_by ? ` · 确认人 ${confirmPreview.version.confirmed_by}` : ""}
              </div>
              {confirmPreview.superseded_version && (
                <div className="drawer-item-meta">
                  旧版本（{confirmPreview.superseded_version.slice(0, 8)}…）将标记为 superseded，不删除。
                </div>
              )}
              {confirmPreview.titleConflicts && confirmPreview.titleConflicts.length > 0 && (
                <div className="error-box">
                  <strong>标题冲突（{confirmPreview.titleConflicts.length}）</strong>
                  <ul>
                    {confirmPreview.titleConflicts.map((c) => (
                      <li key={c.chapterId}>
                        第 {c.chapterNumber} 章：计划标题《{c.planTitle}》与大纲标题《{c.outlineTitle}》不一致
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {confirmPreview.writtenChaptersAffected && confirmPreview.writtenChaptersAffected.length > 0 && (
                <div className="drawer-item-meta warn-text">
                  已有正文的章节受影响：{confirmPreview.writtenChaptersAffected.length} 章（确认后其正文将基于新版本计划，需重新生成或按计划修订）。
                </div>
              )}
              <div className="drawer-item-title">受影响章节（{confirmPreview.impact?.length ?? 0}）</div>
              {confirmPreview.impact && confirmPreview.impact.length > 0 ? (
                <div className="plan-list" style={{ maxHeight: 280, overflow: "auto" }}>
                  {confirmPreview.impact.map((item) => (
                    <div key={item.chapterId} className="plan-item">
                      <div className="drawer-item-title">
                        第 {item.chapterNumber} 章《{item.planTitle}》
                        {item.titleConflict ? <span className="gen-status gen-status-failed">标题冲突</span> : null}
                        {item.titleChanged ? <span className="gen-status gen-status-succeeded">标题变更</span> : null}
                      </div>
                      <div className="drawer-item-meta">
                        {item.hasWrittenContent ? "已有正文 · 确认后需修订/重新生成" : "尚无正文"}
                        {item.followingPlan ? " · 后续计划沿用" : ""}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty">无受影响章节（内容与已确认版本一致）</div>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
                <button className="primary" onClick={() => void doConfirmNewVersion()} disabled={confirmPlanBusy}>
                  {confirmPlanBusy ? "确认中…" : "确认并生成新版本"}
                </button>
                <button className="secondary" onClick={() => cancelConfirmNewVersion()} disabled={confirmPlanBusy}>取消</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 「已确认大纲版本化」：标题冲突确认（不静默覆盖） */}
      {titleConflict && (
        <div className="drawer-overlay" onClick={() => setTitleConflict(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="drawer-title">标题冲突：不能静默覆盖已确认大纲</span>
              <button className="secondary" onClick={() => setTitleConflict(null)}>关闭</button>
            </div>
            <div className="drawer-body">
              <div className="error-box">{titleConflict.message}</div>
              <div className="drawer-item-meta">
                章节计划新标题与当前「已确认大纲」中的标题不一致。直接同步会覆盖已确认版本的计划标题，需先确认新大纲版本（将新标题登记为已确认标题）再同步章节标题。
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
                <button className="primary" onClick={() => void adoptNewTitleAndSync()} disabled={titleConflictBusy}>
                  {titleConflictBusy ? "处理中…" : "采用新标题并同步（先确认新版本）"}
                </button>
                <button className="secondary" onClick={() => setTitleConflict(null)} disabled={titleConflictBusy}>暂不同步（保留编辑）</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 「已确认大纲版本化」：大纲历史版本抽屉（只读） */}
      {showOutlineVersions && (
        <div className="drawer-overlay" onClick={() => setShowOutlineVersions(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="drawer-title">大纲版本历史</span>
              <button className="secondary" onClick={() => setShowOutlineVersions(false)}>关闭</button>
            </div>
            <div className="drawer-body">
              {outlineVersionsError && <div className="error-box">{outlineVersionsError}</div>}
              {outlineVersionsLoading && selectedOutlineVersion == null ? (
                <div className="empty"><span className="spin" /> 加载中…</div>
              ) : (
                <div className="drawer-split">
                  <div className="drawer-col">
                    {outlineVersions.length === 0 ? (
                      <div className="empty">暂无版本（尚未确认大纲）</div>
                    ) : (
                      outlineVersions.map((v) => (
                        <div
                          key={v.id}
                          className="plan-item"
                          style={{ cursor: "pointer" }}
                          onClick={() => void selectOutlineVersion(v.id)}
                        >
                          <div className="drawer-item-title">
                            v{v.version}
                            {selectedOutlineVersionId === v.id ? " ✓" : ""}
                          </div>
                          <div className="drawer-item-meta">
                            <span className={`gen-status ${v.status === "confirmed" ? "gen-status-succeeded" : ""}`}>
                              {v.status === "confirmed" ? "当前已确认" : v.status === "superseded" ? "已取代" : "草稿"}
                            </span>
                            {v.confirmed_at ? ` · 确认于 ${formatTime(v.confirmed_at)}` : ` · 创建于 ${formatTime(v.created_at)}`}
                            {v.confirmed_by ? ` · ${v.confirmed_by}` : ""}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                  <div className="drawer-col drawer-preview">
                    {selectedOutlineVersion ? (
                      <>
                        <div className="drawer-preview-head">
                          <span>v{selectedOutlineVersion.version} · {selectedOutlineVersion.status}</span>
                        </div>
                        {(() => {
                          const plans = chapterPlansFromSnapshot(selectedOutlineVersion);
                          const snap = selectedOutlineVersion.content_snapshot as Record<string, unknown> | null;
                          return (
                            <>
                              {snap && (
                                <div className="gen-detail-row">
                                  <span className="gen-detail-label">大纲快照</span>
                                  <pre className="gen-detail-pre">
                                    {String(snap?.outlineText || snap?.outline || "（无大纲文本）").slice(0, 400)}
                                  </pre>
                                </div>
                              )}
                              <div className="gen-detail-row">
                                <span className="gen-detail-label">章节计划快照（{plans.length} 章）</span>
                                <div className="plan-list" style={{ maxHeight: 260, overflow: "auto" }}>
                                  {plans.length === 0 ? (
                                    <div className="empty">无章节计划</div>
                                  ) : (
                                    plans.map((p, i) => (
                                      <div key={i} className="plan-item">
                                        <span className="drawer-item-title">第 {p.chapterNumber} 章《{p.title}》</span>
                                        {p.conflict ? <span className="gen-status gen-status-failed">标题冲突</span> : null}
                                      </div>
                                    ))
                                  )}
                                </div>
                              </div>
                            </>
                          );
                        })()}
                      </>
                    ) : (
                      <div className="empty">{outlineVersionsLoading ? <span className="spin" /> : "选择左侧版本查看详情"}</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 章节记忆抽屉（第二期：章节能被记住） */}
      {showMemory && (
        <div className="drawer-overlay" onClick={() => setShowMemory(false)}>
          <div className="drawer drawer-memory" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="drawer-title">章节记忆（{activeChapter?.title || ""}）</span>
              <button className="drawer-close" onClick={() => setShowMemory(false)}>×</button>
            </div>
            <div className="drawer-body">
              {memoryLoading ? (
                <div className="empty">
                  <span className="spin" /> 正在加载章节记忆…
                </div>
              ) : !memory ? (
                <div className="empty">
                  暂无章节记忆。保存正文后会自动生成（自动摘要），也可点击「重新生成」手动提取。
                </div>
              ) : (
                <div className="memory-view">
                  <div className="memory-meta">
                    <span className={`memory-tag memory-tag-${memory.source}`}>
                      {memory.source === "ai" ? "AI 生成" : "人工编辑"}
                    </span>
                    <span className={`memory-tag memory-tag-${memory.status}`}>
                      {memory.status === "confirmed" ? "已确认" : "草稿"}
                    </span>
                    <span className="memory-meta-time">
                      更新于 {new Date(memory.updated_at).toLocaleString()}
                    </span>
                  </div>
                  {memoryError && <div className="error-text">{memoryError}</div>}
                  {memoryHint && <div className="save-hint">{memoryHint}</div>}
                  {!memoryEdit ? (
                    <div className="memory-read">
                      <div className="memory-block">
                        <div className="memory-block-title">短摘要</div>
                        <div className="memory-block-body">{memory.summary || "（无）"}</div>
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">时间 / 地点</div>
                        <div className="memory-block-body">
                          {memory.memory.timeline?.relative_day || "（未标注）"}
                          {memory.memory.timeline?.location?.length
                            ? ` · ${memory.memory.timeline.location.join("、")}`
                            : ""}
                        </div>
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">出场人物</div>
                        <div className="memory-block-body">
                          {memory.memory.characters_present?.join("、") || "（无）"}
                        </div>
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">人物变化</div>
                        {memory.memory.character_updates?.length ? (
                          memory.memory.character_updates.map((u, i) => (
                            <div className="memory-block-body" key={i}>
                              · {u.character}：{u.changes?.join("；") || "（无描述）"}
                            </div>
                          ))
                        ) : (
                          <div className="memory-block-body">（无）</div>
                        )}
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">新增伏笔</div>
                        {memory.memory.foreshadows_added?.length ? (
                          memory.memory.foreshadows_added.map((f, i) => (
                            <div className="memory-block-body" key={i}>
                              · {f.content}
                              {f.planned_payoff ? `（计划回收：${f.planned_payoff}）` : ""}
                            </div>
                          ))
                        ) : (
                          <div className="memory-block-body">（无）</div>
                        )}
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">未解决冲突 / 悬线</div>
                        <div className="memory-block-body">
                          {memory.memory.open_threads?.join("；") || "（无）"}
                        </div>
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">关键物品</div>
                        <div className="memory-block-body">
                          {memory.memory.important_items?.join("、") || "（无）"}
                        </div>
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">下章承接提示</div>
                        <div className="memory-block-body">
                          {memory.memory.next_chapter_hint || "（无）"}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="memory-edit">
                      <div className="memory-block">
                        <div className="memory-block-title">短摘要（100-250 字）</div>
                        <textarea
                          className="memory-textarea"
                          value={memorySummary}
                          onChange={(e) => setMemorySummary(e.target.value)}
                          rows={4}
                        />
                      </div>
                      <div className="memory-block">
                        <div className="memory-block-title">结构化记忆（JSON，可编辑）</div>
                        <textarea
                          className="memory-textarea memory-textarea-json"
                          value={memoryJson}
                          onChange={(e) => setMemoryJson(e.target.value)}
                          rows={12}
                          spellCheck={false}
                        />
                      </div>
                    </div>
                  )}
                  <div className="memory-actions">
                    {!memoryEdit ? (
                      <>
                        <button
                          className="secondary"
                          onClick={() => {
                            setMemorySummary(memory.summary || "");
                            setMemoryJson(JSON.stringify(memory.memory || {}, null, 2));
                            setMemoryEdit(true);
                          }}
                        >
                          编辑并确认
                        </button>
                        {memory.status !== "confirmed" && (
                          <button
                            className="secondary"
                            onClick={() => void confirmMemory()}
                            disabled={memoryBusy}
                          >
                            确认
                          </button>
                        )}
                        <button
                          className="secondary"
                          onClick={() => void regenerateMemory()}
                          disabled={memoryBusy}
                        >
                          重新生成
                        </button>
                      </>
                    ) : (
                      <>
                        <button onClick={() => void saveMemory()} disabled={memoryBusy}>
                          保存（确认）
                        </button>
                        <button className="secondary" onClick={() => setMemoryEdit(false)}>
                          取消
                        </button>
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {showCheck && (
        <div className="drawer-overlay" onClick={() => setShowCheck(false)}>
          <div className="check-drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-header">
              <span className="drawer-title">检查结果</span>
              <span className="drawer-subtitle">
                {activeChapter?.title || "当前章节"}（{checkResult ? (checkResult.mode === "real" ? "AI 检查" : "演示检查") : ""}）
              </span>
              <button className="drawer-close" onClick={() => setShowCheck(false)}>×</button>
            </div>
            <div className="drawer-body">
              {checkError && <div className="form-error">{checkError}</div>}
              {checkHint && <div className="form-hint">{checkHint}</div>}
              {checkLoading && <div className="memory-loading">正在检查…</div>}
              {!checkLoading && !checkResult && !checkError && (
                <div className="memory-loading">暂无内容或尚未检查</div>
              )}
              {!checkLoading && checkResult && (
                <div className="check-content">
                  <div className="check-score-row">
                    <div
                      className={`check-score ${checkResult.score >= 80 ? "score-good" : checkResult.score >= 60 ? "score-warn" : "score-bad"}`}
                    >
                      {checkResult.score}
                    </div>
                    <div className="check-score-meta">
                      <span>一致性评分（0-100）</span>
                      <span className="check-mode-tag">
                        {checkResult.mode === "real" ? "AI 检查" : "演示模式"}
                      </span>
                      {checkResult.checked_at && (
                        <span className="check-time">{checkResult.checked_at}</span>
                      )}
                    </div>
                    <button
                      className="secondary"
                      onClick={() => void runCheck()}
                      disabled={checkLoading || checkAccepting}
                    >
                      重新检查
                    </button>
                  </div>

                  <div className="check-section">
                    <div className="check-section-title">问题列表（{checkResult.issues?.length || 0}）</div>
                    {!checkResult.issues?.length && <div className="check-empty">未发现问题</div>}
                    {checkResult.issues?.map((issue, i) => (
                      <div key={`issue-${i}`} className={`check-issue severity-${issue.severity}`}>
                        <div className="check-issue-head">
                          <span className={`severity-badge ${issue.severity}`}>
                            {issue.severity === "error" ? "错误" : "警告"}
                          </span>
                          <span className="issue-type">{issue.type}</span>
                          {issue.location && <span className="issue-location">{issue.location}</span>}
                        </div>
                        <div className="issue-message">{issue.message}</div>
                        {issue.suggestion && (
                          <div className="issue-suggestion">建议：{issue.suggestion}</div>
                        )}
                      </div>
                    ))}
                  </div>

                  {checkResult.reminders && checkResult.reminders.length > 0 && (
                    <div className="check-section">
                      <div className="check-section-title">
                        未回收伏笔提醒（{checkResult.reminders.length}）
                      </div>
                      {checkResult.reminders.map((r, i) => (
                        <div key={`reminder-${i}`} className="check-reminder">
                          <span className={`severity-badge ${r.type === "foreshadow_due" ? "error" : "warning"}`}>
                            {r.type === "foreshadow_due" ? "已到期" : "未回收"}
                          </span>
                          <span className="issue-message">{r.message || r.content}</span>
                          {r.planned_payoff && (
                            <div className="issue-suggestion">计划回收：{r.planned_payoff}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {checkResult.outline && (checkResult.outline.uncovered.length > 0 || checkResult.outline.score > 0) && (
                    <div className="check-section">
                      <div className="check-section-title">
                        细纲完成度（{checkResult.outline.score} / 100）
                      </div>
                      {checkResult.outline.completed && checkResult.outline.completed.length > 0 && (
                        <div className="check-sub-list">
                          <span className="check-sub-label">已覆盖：</span>
                          {checkResult.outline.completed.join("；")}
                        </div>
                      )}
                      {checkResult.outline.uncovered.length > 0 && (
                        <div className="check-sub-list check-sub-uncovered">
                          <span className="check-sub-label">未覆盖项：</span>
                          {checkResult.outline.uncovered.join("；")}
                        </div>
                      )}
                    </div>
                  )}

                  {checkResult.repetition && (checkResult.repetition.internal.length > 0 || checkResult.repetition.against_previous_summary.length > 0) && (
                    <div className="check-section">
                      <div className="check-section-title">重复度检查</div>
                      {checkResult.repetition.internal.map((r, i) => (
                        <div key={`rep-in-${i}`} className="check-issue severity-warning">
                          <div className="check-issue-head">
                            <span className="severity-badge warning">警告</span>
                            <span className="issue-type">正文重复</span>
                            {r.location && <span className="issue-location">{r.location}</span>}
                          </div>
                          <div className="issue-message">{r.message}</div>
                        </div>
                      ))}
                      {checkResult.repetition.against_previous_summary.map((r, i) => (
                        <div key={`rep-prev-${i}`} className="check-issue severity-warning">
                          <div className="check-issue-head">
                            <span className="severity-badge warning">警告</span>
                            <span className="issue-type">与前文重复</span>
                            {r.location && <span className="issue-location">{r.location}</span>}
                          </div>
                          <div className="issue-message">{r.message}</div>
                        </div>
                      ))}
                    </div>
                  )}

                  {checkResult.style && (
                    <div className="check-section">
                      <div className="check-section-title">文风一致性（{checkResult.style.score} / 100）</div>
                      {checkResult.style.evaluation && (
                        <div className="issue-message">{checkResult.style.evaluation}</div>
                      )}
                    </div>
                  )}

                  {checkResult.proposed_updates && checkResult.proposed_updates.length > 0 && (
                    <div className="check-section">
                      <div className="check-section-title">
                        建议更新（采纳后将转入变更确认流程，不直接改文）
                      </div>
                      {checkResult.proposed_updates.map((p, i) => {
                        const key = `${p.category}-${i}`;
                        const done = acceptedKeys.includes(key);
                        return (
                          <div key={key} className="check-proposal">
                            <div className="check-proposal-title">{p.title}</div>
                            {p.description && <div className="issue-message">{p.description}</div>}
                            <button
                              className={done ? "secondary" : ""}
                              onClick={() => void acceptProposal(p, i)}
                              disabled={done || checkAccepting}
                            >
                              {done ? "已转入确认" : "采纳建议"}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 选段改写弹窗（第三档升级） */}
      {showSelRewrite && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.45)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 60,
          }}
          onClick={() => {
            if (!selRewriteBusy) {
              setShowSelRewrite(false);
              setSelRewriteResult(null);
              setSelRewriteError("");
            }
          }}
        >
          <div
            className="card"
            style={{ width: 640, maxWidth: "92vw", maxHeight: "80vh", overflow: "auto" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ fontWeight: 600, marginBottom: 10 }}>✂ 选段改写（只改选中段落，不整章重写）</div>
            {selRewriteError && <div className="error-box">{selRewriteError}</div>}
            {selRewriteResult && (
              <>
                <div className="field">
                  <label>改写指令（可选）</label>
                  <input
                    value={selRewriteInstruction}
                    onChange={(e) => setSelRewriteInstruction(e.target.value)}
                    placeholder="例如：更口语化 / 增强紧张感 / 压缩到一半长度"
                  />
                </div>
                <div style={{ display: "flex", gap: 10 }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, color: "var(--text-dim)" }}>原文（{selRewriteResult.original.length} 字）</div>
                    <div
                      style={{
                        whiteSpace: "pre-wrap",
                        background: "var(--bg-dim, #f5f5f5)",
                        padding: 8,
                        borderRadius: 6,
                        maxHeight: 200,
                        overflow: "auto",
                        fontSize: 13,
                      }}
                    >
                      {selRewriteResult.original}
                    </div>
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                      改写结果{selRewriteResult.rewritten ? `（${selRewriteResult.rewritten.length} 字）` : ""}
                    </div>
                    <div
                      style={{
                        whiteSpace: "pre-wrap",
                        background: "#eef7ee",
                        padding: 8,
                        borderRadius: 6,
                        maxHeight: 200,
                        overflow: "auto",
                        fontSize: 13,
                      }}
                    >
                      {selRewriteResult.rewritten || (selRewriteBusy ? "改写中…" : "等待改写")}
                    </div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button onClick={() => void runSelectionRewrite()} disabled={selRewriteBusy}>
                    {selRewriteBusy ? "改写中…" : "开始改写"}
                  </button>
                  <button
                    className="secondary"
                    onClick={acceptSelectionRewrite}
                    disabled={!selRewriteResult.rewritten}
                  >
                    替换选区（不落库，可再保存）
                  </button>
                  <button
                    className="secondary"
                    onClick={() => {
                      setShowSelRewrite(false);
                      setSelRewriteResult(null);
                      setSelRewriteError("");
                    }}
                  >
                    关闭
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
