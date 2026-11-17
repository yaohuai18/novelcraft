import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  type AiConfigResponse,
  type AiProviderInfo,
  type AiStatus,
  type CharacterProfile,
  type ForeshadowPlan,
  type NovelChapter,
  type NovelChapterPlan,
  type NovelGenerateRequest,
  type NovelPlan,
  type ConfirmOutlinePlanResult,
  type QuickCheckIssue,
} from "../api/client";

const PLAN_KEY = "novelcraft_ai_plan_v2"; // 三阶段状态（灵感/可选设置/规划/锁定/正文进度），不含任何密钥
const MODE_KEY = "novelcraft_ai_mode"; // 仅存模式选择（auto/mock/real），绝不存任何密钥
const MODEL_CFG_KEY = "novelcraft_ai_model_cfg"; // 模型偏好（provider/model_name/temperature/max_tokens/reasoning），不含密钥
// 整本规划后台作业 id（刷新后接着轮询，不含密钥）。
// 按作品/草稿维度分槽：编辑不同作品时各自的规划作业互不覆盖
const PLAN_JOB_KEY = (scope: string) => `novelcraft_ai_plan_job_${scope}`;
const WORK_KEY = (projectId: string) => `novelcraft_ai_works_${projectId}`;

/** 第六阶段：模型切换入口 —— 用户选择的模型偏好（纯配置，无密钥） */
export interface AiModelConfig {
  provider: string;
  model_name: string;
  temperature: number;
  max_tokens: number;
  reasoning: boolean;
}

const DEFAULT_MODEL_CFG: AiModelConfig = {
  provider: "",
  model_name: "",
  temperature: 0.8,
  max_tokens: 4096,
  reasoning: false,
};

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

function loadModelCfg(): AiModelConfig {
  try {
    const raw = localStorage.getItem(MODEL_CFG_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<AiModelConfig>;
      return { ...DEFAULT_MODEL_CFG, ...parsed };
    }
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_MODEL_CFG };
}

function saveModelCfg(cfg: AiModelConfig): void {
  try {
    localStorage.setItem(MODEL_CFG_KEY, JSON.stringify(cfg));
  } catch {
    /* ignore */
  }
}

export interface AiDraft extends Record<string, unknown> {
  novel_type: string;
  genre: string;
  title: string;
  world_view: string;
  era: string;
  characters: string;
  relationships: string;
  main_plot: string;
  chapter_count: number;
  words_per_chapter: number;
  perspective: string;
  writing_style: string;
  requirements: string;
}

export interface CreateOptions {
  genre: string;
  writing_style: string;
  perspective: string;
  chapter_count: number;
  words_per_chapter: number;
  ending_type: string;
  has_romance: boolean;
  content_restrictions: string;
  must_keep_settings: string;
  existing_settings: string; // 已填详细设定（自由文本，只补空白不擅改）
}

export interface CreateState {
  stage: "idea" | "plan" | "write";
  idea: string;
  options: CreateOptions;
  plan: NovelPlan | null;
  selectedTitle: string;
  lockedKeys: string[]; // 锁定项 key（如 protagonist / chapter:3 / volume:1）
  userEditedKeys: string[]; // 用户手动修改过的字段 key
  changedChapterIds: number[]; // plan-update 标记「已修改」的章节
  confirmed: boolean;
  chapters: NovelChapter[];
  outline: string;
  /** 第六阶段：模型偏好（与 localStorage MODEL_CFG_KEY 同步） */
  modelCfg: AiModelConfig;
}

export interface AiWorkSnapshot {
  projectId: string;
  title: string;
  settings: AiDraft;
  outline: string;
  chapters: NovelChapter[];
  savedAt: string;
  state?: CreateState | null;
}

const DEFAULT_OPTIONS: CreateOptions = {
  genre: "现代都市悬疑",
  writing_style: "冷峻写实，富有画面感",
  perspective: "第三人称",
  chapter_count: 6,
  words_per_chapter: 1200,
  ending_type: "开放式结局",
  has_romance: true,
  content_restrictions: "",
  must_keep_settings: "",
  existing_settings: "",
};

const DEFAULT_STATE: CreateState = {
  stage: "idea",
  idea: "",
  options: { ...DEFAULT_OPTIONS },
  plan: null,
  selectedTitle: "",
  lockedKeys: [],
  userEditedKeys: [],
  changedChapterIds: [],
  confirmed: false,
  chapters: [],
  outline: "",
  modelCfg: { ...DEFAULT_MODEL_CFG },
};

const DEFAULT_DRAFT: AiDraft = {
  novel_type: "长篇网络小说",
  genre: "科幻",
  title: "",
  world_view: "",
  era: "",
  characters: "",
  relationships: "",
  main_plot: "",
  chapter_count: 3,
  words_per_chapter: 800,
  perspective: "第三人称",
  writing_style: "冷峻写实，富有画面感",
  requirements: "",
};

function loadState(): CreateState {
  try {
    const raw = localStorage.getItem(PLAN_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<CreateState>;
      const opts = { ...DEFAULT_OPTIONS, ...(parsed.options || {}) };
      return {
        ...DEFAULT_STATE,
        ...parsed,
        options: opts,
        lockedKeys: parsed.lockedKeys || [],
        userEditedKeys: parsed.userEditedKeys || [],
        changedChapterIds: parsed.changedChapterIds || [],
        chapters: parsed.chapters || [],
        modelCfg: { ...DEFAULT_MODEL_CFG, ...(parsed.modelCfg || loadModelCfg()) },
      };
    }
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_STATE, options: { ...DEFAULT_OPTIONS } };
}

function loadMode(): "auto" | "mock" | "real" {
  try {
    const raw = localStorage.getItem(MODE_KEY);
    if (raw === "mock" || raw === "real") return raw;
  } catch {
    /* ignore */
  }
  return "auto";
}

function clonePlan(p: NovelPlan): NovelPlan {
  return JSON.parse(JSON.stringify(p)) as NovelPlan;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function planValue(p: NovelPlan, key: string): string {
  switch (key) {
    case "selling_point":
      return p.selling_point;
    case "synopsis":
      return p.synopsis;
    case "world_view":
      return p.world_view;
    case "era":
      return p.era;
    case "core_conflict":
      return p.core_conflict;
    case "protagonist":
      return p.protagonist;
    case "relationships":
      return p.relationships;
    case "main_plot":
      return p.main_plot;
    case "ending_type":
      return p.ending_type;
    case "supporting_characters":
      return p.supporting_characters
        .map((c) => `${c.name}（${c.role}）：${c.description}`)
        .join("；");
    case "foreshadowing":
      return p.foreshadowing
        .map((f) => `${f.item}（第${f.planted_chapter}章埋设→第${f.payoff_chapter}章回收）`)
        .join("；");
    case "outline":
      return planToOutline(p);
    default:
      if (key.startsWith("chapter:")) {
        const idx = Number(key.slice(8));
        for (const v of p.volumes) {
          for (const c of v.chapters) {
            if (c.index === idx) {
              return `第${idx}章 ${c.title}：目标=${c.goal}；冲突=${c.conflict}；关键事件=${c.key_events}；结尾悬念=${c.cliffhanger}`;
            }
          }
        }
      }
      if (key.startsWith("volume:")) {
        const vi = Number(key.slice(7));
        const v = p.volumes.find((x) => x.volume_index === vi);
        if (v) return `${v.title}：${v.summary}`;
      }
      return "";
  }
}

function lockedTexts(state: CreateState): string[] {
  if (!state.plan) return [];
  return state.lockedKeys.map((k) => {
    const v = planValue(state.plan as NovelPlan, k);
    return v ? `${k}：${truncate(v, 180)}` : k;
  });
}

function planToOutline(p: NovelPlan): string {
  const lines = [
    `书名候选：${p.title_candidates.join(" / ")}`,
    `一句话卖点：${p.selling_point}`,
    `故事简介：${p.synopsis}`,
    `世界观：${p.world_view}`,
    `时代背景：${p.era}`,
    `核心冲突：${p.core_conflict}`,
    `主角小传：${p.protagonist}`,
    `配角：${p.supporting_characters.map((c) => `${c.name}（${c.role}）：${c.description}`).join("；")}`,
    `人物关系：${p.relationships}`,
    `故事主线：${p.main_plot}`,
    `结局类型：${p.ending_type}`,
  ];
  for (const v of p.volumes) {
    lines.push(`【${v.title}】${v.summary}`);
    for (const c of v.chapters) {
      lines.push(
        `${c.index}. ${c.title}：目标=${c.goal}；冲突=${c.conflict}；关键事件=${c.key_events}；结尾悬念=${c.cliffhanger}`
      );
    }
  }
  lines.push(
    `伏笔与回收：${p.foreshadowing
      .map((f) => `${f.item}（第${f.planted_chapter}章埋设→第${f.payoff_chapter}章回收）`)
      .join("；")}`
  );
  return lines.join("\n\n");
}

// 与后端 schema 上限严格对齐，避免 UI 允许值触发 422「请求参数校验失败」
const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n || lo));
const cut = (s: string, n: number) => (s ? s.slice(0, n) : s);

function buildGenerateRequest(
  state: CreateState,
  modeSel: "auto" | "mock" | "real"
): NovelGenerateRequest {
  const plan = state.plan as NovelPlan;
  const title = state.selectedTitle || plan.title_candidates[0] || "未命名作品";
  const characters = [
    plan.protagonist,
    ...plan.supporting_characters.map((c) => `${c.name}（${c.role}）：${c.description}`),
  ].join("\n");
  const requirements = [
    `故事简介：${plan.synopsis}`,
    `一句话卖点：${plan.selling_point}`,
    `核心冲突：${plan.core_conflict}`,
    `结局类型：${plan.ending_type}`,
    `伏笔计划：${plan.foreshadowing
      .map((f) => `${f.item}（第${f.planted_chapter}章埋设→第${f.payoff_chapter}章回收）`)
      .join("；")}`,
  ].join("\n");
  return {
    novel_type: cut("长篇网络小说", 100),
    genre: cut(state.options.genre || "小说", 100),
    title: cut(title, 200),
    world_view: cut(plan.world_view, 5000),
    era: cut(plan.era, 1000),
    characters: cut(characters, 10000),
    relationships: cut(plan.relationships, 10000),
    main_plot: cut(plan.main_plot, 20000),
    chapter_count: clamp(state.options.chapter_count, 1, 1000),
    words_per_chapter: clamp(state.options.words_per_chapter, 100, 8000),
    perspective: cut(state.options.perspective, 200),
    writing_style: cut(state.options.writing_style, 500),
    requirements: cut(requirements, 5000),
    outline: cut(state.outline, 50000),
    locked: (lockedTexts(state) || []).slice(0, 200),
    ...(modeSel !== "auto" ? { requested_mode: modeSel } : {}),
    // 第六阶段：请求级模型覆盖（仅当用户显式选择模型时携带；temperature/max_tokens 随偏好设置）
    ...(state.modelCfg?.model_name ? { model_name: state.modelCfg.model_name } : {}),
    ...(state.modelCfg && Number.isFinite(state.modelCfg.temperature) ? { temperature: state.modelCfg.temperature } : {}),
    ...(state.modelCfg && state.modelCfg.max_tokens > 0 ? { max_tokens: state.modelCfg.max_tokens } : {}),
  };
}

function fullText(c: NovelChapter): string {
  return `${c.title}\n\n${c.content}`;
}

function modeLabel(mode: "mock" | "real" | ""): string {
  if (mode === "mock") return "演示模式（mode=mock）";
  if (mode === "real") return "AI 模式（mode=real）";
  return "";
}

function LockBadge({ locked, onToggle, edited }: { locked: boolean; onToggle: () => void; edited?: boolean }) {
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", marginLeft: 8 }}>
      {locked ? (
        <button className="chip" onClick={onToggle} title="点击解锁（解锁后该设定将不再随生成请求锁定）">
          🔒 已锁定
        </button>
      ) : (
        <button className="chip" onClick={onToggle} title="点击锁定（锁定后该设定不可编辑，后续正文生成将强制遵守）">
          🔓 锁定
        </button>
      )}
      {edited && <span className="chip" title="该内容由您或 AI 局部修改，非 AI 原始生成">已修改</span>}
    </span>
  );
}

export default function CreatePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const editProjectId = searchParams.get("edit") || "";

  const [state, setState] = useState<CreateState>(loadState);
  const [modeSel, setModeSel] = useState<"auto" | "mock" | "real">(loadMode);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [aiConfig, setAiConfig] = useState<AiConfigResponse | null>(null);
  const [generating, setGenerating] = useState(false);
  const [planUpdating, setPlanUpdating] = useState(false);
  const [genMode, setGenMode] = useState<"mock" | "real" | "">("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [quickIssues, setQuickIssues] = useState<QuickCheckIssue[] | null>(null);
  const [quickBusy, setQuickBusy] = useState(false);
  const [cancellable, setCancellable] = useState(false);
  const requestIdRef = useRef<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [copied, setCopied] = useState(false);
  const [planInstruction, setPlanInstruction] = useState("");
  const [confirmPreview, setConfirmPreview] = useState<ConfirmOutlinePlanResult | null>(null);
  const [confirmPlanBusy, setConfirmPlanBusy] = useState(false);
  const cancelRef = useRef<{ cancel: () => void } | null>(null);
  const planJobIdRef = useRef<string | null>(null);
  const aliveRef = useRef(true);
  const [planJobElapsed, setPlanJobElapsed] = useState(0);
  const [planJobId, setPlanJobId] = useState<string | null>(null);
  const [planJobStatus, setPlanJobStatus] = useState("");
  const planIdemKeyRef = useRef<string | null>(null);

  const aiReady = aiStatus?.configured === true;
  const stage = state.stage;

  // 实时持久化三阶段状态（不含密钥）
  useEffect(() => {
    try {
      localStorage.setItem(PLAN_KEY, JSON.stringify(state));
    } catch {
      /* ignore */
    }
  }, [state]);

  useEffect(() => {
    try {
      localStorage.setItem(MODE_KEY, modeSel);
    } catch {
      /* ignore */
    }
  }, [modeSel]);

  useEffect(() => {
    api
      .getAiStatus()
      .then((st) => setAiStatus(st))
      .catch(() => setAiStatus(null));
  }, []);

  // 第六阶段：拉取模型配置（provider/model 选项，不含密钥），并用服务端可选项校正本地偏好
  useEffect(() => {
    api
      .getAiConfig()
      .then((cfg) => {
        setAiConfig(cfg);
        setState((prev) => {
          const cur = prev.modelCfg;
          const providerExists = cfg.providers.some((p) => p.provider === cur.provider);
          const provider = providerExists ? cur.provider : (cfg.providers[0]?.provider || "");
          const prov = cfg.providers.find((p) => p.provider === provider);
          const modelExists = prov ? prov.models.includes(cur.model_name) : false;
          const model_name = modelExists ? cur.model_name : (prov?.models[0] || "");
          const cfgNext: AiModelConfig = { ...cur, provider, model_name };
          saveModelCfg(cfgNext);
          return { ...prev, modelCfg: cfgNext };
        });
      })
      .catch(() => setAiConfig(null));
  }, []);

  // 从本地快照恢复（作品库「编辑」入口）
  useEffect(() => {
    const st = location.state as { importSnapshot?: AiDraft } | null;
    if (st?.importSnapshot) {
      setState((prev) => ({
        ...prev,
        options: {
          ...prev.options,
          existing_settings: [
            ...(prev.options.existing_settings ? [prev.options.existing_settings] : []),
            `旧版设定导入：题材=${st.importSnapshot?.genre || "-"}；世界观=${st.importSnapshot?.world_view || "-"}；人物=${st.importSnapshot?.characters || "-"}；主线=${st.importSnapshot?.main_plot || "-"}`,
          ].join("\n"),
        },
      }));
      setInfo("已载入旧版创作设定到「已填详细设定」，可作为规划参考（AI 只补空白不擅改）。");
      return;
    }
    if (!editProjectId) return;
    try {
      const raw = localStorage.getItem(WORK_KEY(editProjectId));
      if (raw) {
        const snap = JSON.parse(raw) as AiWorkSnapshot;
        if (snap.state) {
          setState({ ...DEFAULT_STATE, ...snap.state, options: { ...DEFAULT_OPTIONS, ...snap.state.options } });
          setInfo(`已载入作品「${snap.title}」的三阶段进度（规划${snap.state.confirmed ? "已确认" : "未确认"}，已生成 ${snap.state.chapters.length} 章），刷新页面仍可恢复。`);
        } else {
          setState((prev) => ({
            ...prev,
            idea: snap.settings?.main_plot || "",
            options: {
              ...prev.options,
              genre: snap.settings?.genre || prev.options.genre,
              existing_settings: [
                `作品设定导入：世界观=${snap.settings?.world_view || "-"}；人物=${snap.settings?.characters || "-"}；关系=${snap.settings?.relationships || "-"}；主线=${snap.settings?.main_plot || "-"}`,
              ].join("\n"),
            },
            chapters: snap.chapters || [],
            outline: snap.outline || "",
          }));
          setInfo(`已载入作品「${snap.title}」的旧版设定、大纲与生成文本（刷新页面仍可恢复）。`);
        }
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editProjectId]);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      planJobIdRef.current = null;
      cancelRef.current?.cancel();
    };
  }, []);

  // 刷新/切回页面时接上仍在后台跑的规划作业（后台任务不随页面关闭而消失）
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(PLAN_JOB_KEY(editProjectId || "new"));
    } catch {
      saved = null;
    }
    if (!saved) return;
    let cancelled = false;
    void (async () => {
      try {
        const job = await api.getPlanNovelJob(saved);
        if (cancelled) return;
        if (
          job.status === "queued" ||
          job.status === "running" ||
          job.status === "cancel_requested"
        ) {
          setGenerating(true);
          setPlanJobId(saved);
          setPlanJobStatus(job.status);
          setPlanJobElapsed(job.elapsed_seconds ?? 0);
          setInfo(
            job.status === "cancel_requested"
              ? "上一次的整本规划正在取消，已自动接上。"
              : `上一次的整本规划还在后台生成（已 ${job.elapsed_seconds ?? 0} 秒），已自动接上，完成后会自动填入。`
          );
          void pollPlanJob(saved);
          return;
        }
        if (job.status === "succeeded" && job.plan) {
          applyPlan(job.plan, job.mode, job.elapsed_seconds);
          try {
            localStorage.removeItem(PLAN_JOB_KEY(editProjectId || "new"));
          } catch {
            /* ignore */
          }
          return;
        }
        if (job.result_review_status === "pending_review" && job.plan) {
          applyPlan(job.plan, job.mode, job.elapsed_seconds);
          setInfo(
            `上一次的规划结果晚到，尚未应用${
              job.result_conflict_reason ? `（${job.result_conflict_reason}）` : ""
            }。已填入下方草稿供你核对。`
          );
          try {
            localStorage.removeItem(PLAN_JOB_KEY(editProjectId || "new"));
          } catch {
            /* ignore */
          }
          return;
        }
        try {
          localStorage.removeItem(PLAN_JOB_KEY(editProjectId || "new"));
        } catch {
          /* ignore */
        }
      } catch {
        /* 任务不存在或网络失败：静默放弃，不影响手动提交 */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const update = useCallback((k: keyof CreateOptions, v: string | number | boolean) => {
    setState((prev) => ({ ...prev, options: { ...prev.options, [k]: v } }));
  }, []);

  function updatePlan(mutator: (p: NovelPlan) => NovelPlan) {
    setState((prev) => {
      if (!prev.plan) return prev;
      return { ...prev, plan: mutator(clonePlan(prev.plan)) };
    });
  }

  function markEdited(key: string) {
    setState((prev) =>
      prev.userEditedKeys.includes(key)
        ? prev
        : { ...prev, userEditedKeys: [...prev.userEditedKeys, key] }
    );
  }

  function toggleLock(key: string) {
    setState((prev) => ({
      ...prev,
      lockedKeys: prev.lockedKeys.includes(key)
        ? prev.lockedKeys.filter((k) => k !== key)
        : [...prev.lockedKeys, key],
    }));
  }

  function editString(key: string, value: string) {
    markEdited(key);
    updatePlan((p) => ({ ...p, [key]: value }));
  }

  function editChapter(globalIndex: number, field: keyof NovelChapterPlan, value: string) {
    markEdited(`chapter:${globalIndex}`);
    updatePlan((p) => {
      for (const v of p.volumes) {
        for (const c of v.chapters) {
          if (c.index === globalIndex) {
            (c as unknown as Record<string, string>)[field] = value;
          }
        }
      }
      return p;
    });
  }

  function editCharacter(i: number, field: keyof CharacterProfile, value: string) {
    markEdited("supporting_characters");
    updatePlan((p) => {
      p.supporting_characters = p.supporting_characters.map((c, idx) =>
        idx === i ? { ...c, [field]: value } : c
      );
      return p;
    });
  }

  function addCharacter() {
    markEdited("supporting_characters");
    updatePlan((p) => {
      p.supporting_characters = [
        ...p.supporting_characters,
        { name: "新角色", role: "配角", description: "" },
      ];
      return p;
    });
  }

  function removeCharacter(i: number) {
    markEdited("supporting_characters");
    updatePlan((p) => {
      p.supporting_characters = p.supporting_characters.filter((_, idx) => idx !== i);
      return p;
    });
  }

  function editForeshadow(i: number, field: keyof ForeshadowPlan, value: string | number) {
    markEdited("foreshadowing");
    updatePlan((p) => {
      p.foreshadowing = p.foreshadowing.map((f, idx) =>
        idx === i ? { ...f, [field]: value } : f
      );
      return p;
    });
  }

  // 阶段一：灵感 → AI 生成规划
  // 阶段一：灵感 → 后台生成完整规划（提交后轮询，不再受浏览器 240 秒超时限制）
  async function handlePlanNovel() {
    setError("");
    setInfo("");
    if (!state.idea.trim()) {
      setError("先写点想法吧，一句话也行，AI 会为你补全完整规划。");
      return;
    }
    setGenerating(true);
    setPlanJobElapsed(0);
    try {
      const existing: Record<string, unknown> = {};
      if (state.options.existing_settings.trim()) {
        existing.details = state.options.existing_settings.trim();
      }
      const idem =
        planIdemKeyRef.current ||
        (typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `plan-${Date.now()}-${Math.random().toString(16).slice(2)}`);
      planIdemKeyRef.current = idem;
      const sub = await api.submitPlanNovel({
        idea: state.idea.trim(),
        genre: state.options.genre,
        writing_style: state.options.writing_style,
        perspective: state.options.perspective,
        chapter_count: Math.min(1000, Math.max(1, state.options.chapter_count || 10)),
        words_per_chapter: Math.min(8000, Math.max(100, state.options.words_per_chapter || 1200)),
        ending_type: state.options.ending_type,
        has_romance: state.options.has_romance,
        content_restrictions: cut(state.options.content_restrictions, 2000),
        must_keep_settings: cut(state.options.must_keep_settings, 5000),
        existing_settings: Object.keys(existing).length ? existing : null,
        ...(modeSel !== "auto" ? { requested_mode: modeSel } : {}),
        ...(state.modelCfg?.model_name ? { model_name: state.modelCfg.model_name } : {}),
        ...(state.modelCfg && Number.isFinite(state.modelCfg.temperature) ? { temperature: state.modelCfg.temperature } : {}),
        ...(state.modelCfg && state.modelCfg.max_tokens > 0 ? { max_tokens: Math.min(32000, state.modelCfg.max_tokens) } : {}),
      }, idem);
      planIdemKeyRef.current = null; // 已受理，下次提交用新键
      planJobIdRef.current = sub.job_id;
      setPlanJobId(sub.job_id);
      setPlanJobStatus(sub.status);
      try {
        localStorage.setItem(PLAN_JOB_KEY(editProjectId || "new"), sub.job_id);
      } catch {
        /* ignore */
      }
      setInfo("已提交后台生成（预计 3~6 分钟）。可以关掉页面或切到别的作品，回来会自动接上并填入结果。");
      void pollPlanJob(sub.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "规划提交失败");
      setGenerating(false);
    }
  }

  // 后台作业结果落到阶段二（结构与同步接口一致，便于随时回退）
  function applyPlan(plan: NovelPlan, mode: string, elapsed: number | null) {
    setState((prev) => ({
      ...prev,
      plan,
      selectedTitle: plan.title_candidates[0] || "",
      stage: "plan",
      changedChapterIds: [],
    }));
    setGenMode(mode === "real" ? "real" : "mock");
    const chapters = plan.volumes.reduce((s, v) => s + v.chapters.length, 0);
    setInfo(
      `${modeLabel(mode === "real" ? "real" : "mock")}：已生成完整创作规划（${chapters} 章大纲 / ${plan.foreshadowing.length} 条伏笔${elapsed ? `，用时 ${elapsed} 秒` : ""}）。请确认、可编辑，满意后继续写正文。`
    );
  }

  // 轮询后台作业：成功 → 落规划；失败/中断 → 显示原因并清掉本地记录
  async function pollPlanJob(jobId: string) {
    planJobIdRef.current = jobId;
    const clearSaved = () => {
      try {
        localStorage.removeItem(PLAN_JOB_KEY(editProjectId || "new"));
      } catch {
        /* ignore */
      }
    };
    try {
      for (;;) {
        if (!aliveRef.current || planJobIdRef.current !== jobId) return;
        let job: Awaited<ReturnType<typeof api.getPlanNovelJob>>;
        try {
          job = await api.getPlanNovelJob(jobId);
        } catch (err) {
          const status = (err as { status?: number }).status;
          if (typeof status === "number" && status >= 400 && status < 500) {
            setError(err instanceof Error ? err.message : "规划任务不存在或无权访问");
            clearSaved();
            return;
          }
          await sleep(3000);
          continue;
        }
        if (!aliveRef.current || planJobIdRef.current !== jobId) return;
        setPlanJobElapsed(job.elapsed_seconds ?? 0);
        setPlanJobStatus(job.status);
        if (job.status === "succeeded" && job.plan) {
          applyPlan(job.plan, job.mode, job.elapsed_seconds);
          clearSaved();
          return;
        }
        if (job.result_review_status === "pending_review" && job.plan) {
          // 作业已被判中断/取消，但模型结果晚到：保留为待审阅草稿，不自动落库
          applyPlan(job.plan, job.mode, job.elapsed_seconds);
          setInfo(
            `结果晚到，尚未应用：作业已结束或已取消${
              job.result_conflict_reason ? `（${job.result_conflict_reason}）` : ""
            }。已把结果填入下方草稿供你核对，确认无误后再继续写正文。`
          );
          clearSaved();
          return;
        }
        if (job.status === "cancelled") {
          setInfo("规划任务已取消，未产生可用结果。可重新点击「✦ AI 生成规划」。");
          clearSaved();
          return;
        }
        if (job.status === "cancel_requested") {
          setInfo("已请求取消，正在停止模型调用，请稍候…");
        }
        if (job.status === "failed" || job.status === "interrupted") {
          setError(
            job.error_message ||
              (job.error_code ? `规划失败（${job.error_code}）` : "规划生成失败")
          );
          clearSaved();
          return;
        }
        await sleep(3000);
      }
    } finally {
      // 仅当自己仍是当前作业时才清理 UI 状态：
      // 防止被新作业取代的旧轮询循环，在退出时误清新作业的“生成中”状态。
      // 注意：终态分支不得提前置空 planJobIdRef（否则本守卫永不成立，generating 卡死）
      if (aliveRef.current && planJobIdRef.current === jobId) {
        planJobIdRef.current = null;
        setGenerating(false);
        setPlanJobId(null);
        setPlanJobStatus("");
      }
    }
  }

  // 取消仍在跑的规划作业：queued 直接取消，running 先发停止请求
  async function handleCancelPlanJob() {
    const id = planJobIdRef.current;
    if (!id) return;
    try {
      const res = await api.cancelPlanNovelJob(id);
      setPlanJobStatus(res.status);
      setInfo(
        res.status === "cancelled"
          ? "规划任务已取消。"
          : "已请求取消，正在停止模型调用，请稍候…"
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    }
  }

  // AI 局部修改规划（plan-update，只刷新受影响章节）
  async function handlePlanUpdate() {
    if (!planInstruction.trim() || !state.plan) return;
    setError("");
    setInfo("");
    setPlanUpdating(true);
    try {
      const res = await api.planUpdate({
        instruction: planInstruction.trim(),
        current_plan: state.plan,
        locked: lockedTexts(state),
        ...(modeSel !== "auto" ? { requested_mode: modeSel } : {}),
        ...(state.modelCfg?.model_name ? { model_name: state.modelCfg.model_name } : {}),
        ...(state.modelCfg && Number.isFinite(state.modelCfg.temperature) ? { temperature: state.modelCfg.temperature } : {}),
        ...(state.modelCfg && state.modelCfg.max_tokens > 0 ? { max_tokens: state.modelCfg.max_tokens } : {}),
      });
      setState((prev) => ({
        ...prev,
        plan: res.plan,
        changedChapterIds: Array.from(
          new Set([...(prev.changedChapterIds || []), ...res.changed_chapter_ids])
        ),
      }));
      setInfo(
        `${modeLabel(res.mode)}：局部修改完成，仅刷新受影响内容${
          res.changed_chapter_ids.length
            ? `，章节：${res.changed_chapter_ids.map((i) => `第${i}章`).join("、")}`
            : "（未涉及具体章节，已更新概要字段）"
        }。`
      );
      setPlanInstruction("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "局部修改失败");
    } finally {
      setPlanUpdating(false);
    }
  }

  // 确认规划 → 进入写作（版本化：已有项目先 dry_run 预览影响面，用户确认后持久化；新建项目暂存本地，保存到作品库后到章节编辑页正式确认）
  async function handleConfirmPlan() {
    if (!state.plan) return;
    setError("");
    if (!editProjectId) {
      setState((prev) => ({ ...prev, confirmed: true, stage: "write" }));
      setInfo("规划已确认并锁定。现在可开始逐章生成正文：先「生成第一章」。（新建项目尚未持久化大纲版本，保存到作品库后可在章节编辑页「确认新版本」正式确认）");
      return;
    }
    setConfirmPlanBusy(true);
    try {
      const res = await api.confirmOutlinePlan(editProjectId, true);
      setConfirmPreview(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认失败：无法获取大纲版本影响面预览");
      setConfirmPreview(null);
    } finally {
      setConfirmPlanBusy(false);
    }
  }

  async function doConfirmPlan() {
    if (!editProjectId || !state.plan) return;
    setConfirmPlanBusy(true);
    try {
      const res = await api.confirmOutlinePlan(editProjectId, false);
      setState((prev) => ({ ...prev, confirmed: true, stage: "write" }));
      setInfo(`规划已确认并锁定（已确认大纲 v${res.version.version}）。现在可开始逐章生成正文：先「生成第一章」。`);
      setConfirmPreview(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认失败：大纲版本持久化未成功");
      setConfirmPreview(null);
    } finally {
      setConfirmPlanBusy(false);
    }
  }

  function cancelConfirmPlan() {
    setConfirmPreview(null);
    setInfo("");
  }

  function handleBackToPlan() {
    setError("");
    setInfo("");
    setState((prev) => ({ ...prev, stage: "plan" }));
  }

  // 生成第一章（遵守 locked）
  async function handleGenerateFirstChapter() {
    setError("");
    setInfo("");
    if (!state.plan) {
      setError("请先生成规划");
      return;
    }
    setGenerating(true);
    setChaptersSafe([]);
    try {
      const req = buildGenerateRequest(state, modeSel);
      const res = await api.generateOutlineFirst(req, 240000);
      setState((prev) => ({
        ...prev,
        outline: res.outline || planToOutline(prev.plan as NovelPlan),
        chapters: res.chapter ? [res.chapter] : [],
        stage: "write",
      }));
      setActiveIndex(0);
      setGenMode(res.mode);
      setInfo(
        `${modeLabel(res.mode)}：已生成全书大纲与第一章（${(res.chapter?.content || "").length} 字），正文已遵守锁定设定。可继续「生成下一章」或对本章重生成 / 扩写 / 润色。`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setGenerating(false);
    }
  }

  function setChaptersSafe(chs: NovelChapter[]) {
    setState((prev) => ({ ...prev, chapters: chs }));
  }

  // 批次 3：编辑类生成统一走 SSE 流式（进度心跳 + 可取消）
  function runEditStream(
    path: string,
    req: unknown,
    applyChapter: (title: string, content: string, wordCount: number) => void,
    actionLabel: string
  ): Promise<void> {
    return new Promise((resolve) => {
      setCancellable(true);
      const stream = api.streamChapterEdit(path, req as never, (ev) => {
        switch (ev.type) {
          case "start":
            requestIdRef.current = ev.request_id || null;
            if (ev.mode) setGenMode(ev.mode);
            break;
          case "status":
            setInfo(ev.message || "");
            break;
          case "progress":
            setInfo(`${actionLabel}… 已进行 ${ev.elapsed ?? 0} 秒，可随时取消`);
            break;
          case "chapter":
            applyChapter(ev.title || "", ev.content || "", ev.word_count || 0);
            break;
          case "done":
            setInfo(ev.message || `已完成${actionLabel}`);
            break;
          case "cancelled":
            setInfo(ev.message || "生成已取消");
            break;
          case "error":
            setError(ev.message || "生成失败");
            break;
        }
        if (ev.type === "done" || ev.type === "cancelled" || ev.type === "error") {
          setCancellable(false);
          cancelRef.current = null;
          requestIdRef.current = null;
          resolve();
        }
      });
      cancelRef.current = stream;
    });
  }

  async function handleCancelGeneration() {
    if (requestIdRef.current) {
      try {
        await api.cancelGeneration(requestIdRef.current);
      } catch {
        /* 忽略：断连检测同样会取消 */
      }
    }
    cancelRef.current?.cancel();
    setInfo("已请求取消生成…");
  }

  // 生成下一章
  async function handleNextChapter() {
    const cur = state.chapters[state.chapters.length - 1];
    if (!cur) {
      setError("请先「生成第一章」再续写");
      return;
    }
    setError("");
    setInfo("");
    setGenerating(true);
    const nextIndex = state.chapters.length + 1;
    try {
      await runEditStream(
        "/api/ai/generate-next-chapter/stream",
        {
          setting: buildGenerateRequest(state, modeSel),
          outline: state.outline,
          previous_summary: (cur.content || "").slice(-600),
          character_state: `人物：${state.plan?.protagonist || "-"}；关系：${state.plan?.relationships || "-"}`,
          chapter_index: nextIndex,
          previous_chapter: (cur.content || "").slice(-800),
        },
        (title, content) => {
          setState((prev) => ({
            ...prev,
            chapters: [
              ...prev.chapters,
              { index: nextIndex, title: title || `第 ${nextIndex} 章`, content },
            ],
          }));
          setActiveIndex(nextIndex - 1);
        },
        "生成下一章"
      );
    } finally {
      setGenerating(false);
    }
  }

  // 从规划中取第 idx 章的章节计划文本（2026-11-17 前后章衔接修复）
  function planChapterText(plan: NovelPlan | null, idx: number): string {
    if (!plan) return "";
    for (const vol of plan.volumes || []) {
      const ch = (vol.chapters || []).find((c) => c.index === idx);
      if (ch) {
        return [
          `标题：${ch.title || "-"}`,
          `目标：${ch.goal || "-"}`,
          `冲突：${ch.conflict || "-"}`,
          `关键事件：${ch.key_events || "-"}`,
          `章末钩子：${ch.cliffhanger || "-"}`,
        ].join("\n");
      }
    }
    return "";
  }

  // 重新生成本章（批次 3：流式 + 可取消）
  async function handleRegenerateChapter() {
    const cur = state.chapters[activeIndex];
    if (!cur) return;
    setError("");
    setInfo("");
    setGenerating(true);
    try {
      await runEditStream(
        "/api/ai/regenerate-chapter/stream",
        {
          setting: buildGenerateRequest(state, modeSel),
          chapter_title: cur.title,
          chapter_content: (cur.content || "").slice(0, 3000),
          instruction:
            "重新生成本章：保持同一章节定位与剧情推进方向，但换一种写法，内容不得与上版雷同；" +
            "第一段必须从上一章结尾无缝承接，结尾钩子必须为下一章开头做铺垫",
          chapter_index: activeIndex + 1,
          outline: planChapterText(state.plan, activeIndex + 1),
          previous_chapter_tail: (state.chapters[activeIndex - 1]?.content || "").slice(-800),
          next_chapter_head: (state.chapters[activeIndex + 1]?.content || "").slice(0, 600),
        },
        (title, content) => {
          setState((prev) => ({
            ...prev,
            chapters: prev.chapters.map((c, i) =>
              i === activeIndex ? { index: activeIndex + 1, title, content } : c
            ),
          }));
        },
        "重新生成本章"
      );
    } finally {
      setGenerating(false);
    }
  }

  // 扩写本章（批次 3：流式 + 可取消）
  async function handleExpandChapter() {
    const cur = state.chapters[activeIndex];
    if (!cur) return;
    setError("");
    setInfo("");
    setGenerating(true);
    try {
      await runEditStream(
        "/api/ai/expand-chapter/stream",
        {
          setting: buildGenerateRequest(state, modeSel),
          chapter_title: cur.title,
          chapter_content: (cur.content || "").slice(0, 3000),
          instruction: "扩写本章：补充环境细节、人物心理与对话，丰富情节层次，保持剧情一致",
          chapter_index: activeIndex + 1,
          outline: planChapterText(state.plan, activeIndex + 1),
          previous_chapter_tail: (state.chapters[activeIndex - 1]?.content || "").slice(-800),
          next_chapter_head: (state.chapters[activeIndex + 1]?.content || "").slice(0, 600),
        },
        (title, content) => {
          setState((prev) => ({
            ...prev,
            chapters: prev.chapters.map((c, i) =>
              i === activeIndex ? { index: activeIndex + 1, title, content } : c
            ),
          }));
        },
        "扩写本章"
      );
    } finally {
      setGenerating(false);
    }
  }

  // 润色本章（批次 3：流式 + 可取消）
  async function handlePolishChapter() {
    const cur = state.chapters[activeIndex];
    if (!cur) return;
    setError("");
    setInfo("");
    setGenerating(true);
    try {
      await runEditStream(
        "/api/ai/polish-chapter/stream",
        {
          setting: buildGenerateRequest(state, modeSel),
          chapter_title: cur.title,
          chapter_content: (cur.content || "").slice(0, 3000),
          style: "润色本章：语言更凝练，增强画面感、节奏与情绪张力，保留全部情节",
          chapter_index: activeIndex + 1,
        },
        (title, content) => {
          setState((prev) => ({
            ...prev,
            chapters: prev.chapters.map((c, i) =>
              i === activeIndex ? { index: activeIndex + 1, title, content } : c
            ),
          }));
        },
        "润色本章"
      );
    } finally {
      setGenerating(false);
    }
  }

  async function handleCopyAll() {
    const head = state.outline ? `【全书大纲】\n${state.outline}\n\n` : "";
    const text = head + state.chapters.map(fullText).join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  function updateChapterContent(value: string) {
    setState((prev) => ({
      ...prev,
      chapters: prev.chapters.map((c, i) => (i === activeIndex ? { ...c, content: value } : c)),
    }));
  }

  // 批次 2：保存前 4 维轻校验（伏笔顺序/时间线/人物状态/规则违反），零 AI 成本，不阻断保存
  async function runQuickCheck(): Promise<QuickCheckIssue[] | null> {
    if (!state.plan || state.chapters.length === 0) return null;
    setQuickBusy(true);
    try {
      const res = await api.quickCheckConsistency({
        plan: state.plan as unknown as Record<string, unknown>,
        chapters: state.chapters.map((c, i) => ({ index: i + 1, title: c.title, content: c.content })),
        rules: lockedTexts(state),
      });
      setQuickIssues(res.issues);
      setInfo(res.message);
      return res.issues;
    } catch (err) {
      setQuickIssues(null);
      setError(err instanceof Error ? `一致性检查失败：${err.message}` : "一致性检查失败");
      return null;
    } finally {
      setQuickBusy(false);
    }
  }

  async function handleSaveToLibrary() {
    setError("");
    setInfo("");
    if (state.chapters.length === 0) {
      setError("还没有生成正文，请先在写作阶段生成章节");
      return;
    }
    if (!state.plan) return;
    // 保存前自动跑一致性检查（黄条提示，不阻断保存）
    const issues = await runQuickCheck();
    setSaving(true);
    try {
      const plan = state.plan;
      const title = state.selectedTitle || plan.title_candidates[0] || "未命名作品";
      const characters = [
        plan.protagonist,
        ...plan.supporting_characters.map((c) => `${c.name}（${c.role}）：${c.description}`),
      ].join("\n");
      const draft: AiDraft = {
        novel_type: "长篇网络小说",
        genre: state.options.genre || "小说",
        title,
        world_view: plan.world_view,
        era: plan.era,
        characters,
        relationships: plan.relationships,
        main_plot: plan.main_plot,
        chapter_count: state.options.chapter_count,
        words_per_chapter: state.options.words_per_chapter,
        perspective: state.options.perspective,
        writing_style: state.options.writing_style,
        requirements: [
          `简介：${plan.synopsis}`,
          `卖点：${plan.selling_point}`,
          `核心冲突：${plan.core_conflict}`,
          `结局：${plan.ending_type}`,
        ].join("\n"),
      };

      const created = await api.createProject({
        title: title.trim(),
        genre: state.options.genre.trim() || undefined,
        background: `AI 自动创作（灵感→规划→锁定→逐章生成）\n简介：${plan.synopsis || "-"}\n卖点：${plan.selling_point || "-"}\n时代背景：${plan.era || "-"}\n主要人物：${characters || "-"}\n人物关系：${plan.relationships || "-"}\n故事主线：${plan.main_plot || "-"}\n结局类型：${plan.ending_type || "-"}`,
        world_setting: plan.world_view.trim() || undefined,
        main_conflict: plan.core_conflict.trim() || undefined,
        narrative_perspective: state.options.perspective.trim() || undefined,
        writing_style: state.options.writing_style.trim() || undefined,
      });

      let summaryId = "";
      const sums = await api.listOutlines(created.id, "summary");
      if (sums.length > 0) summaryId = sums[0].id;
      if (!summaryId) {
        const sum = await api.createOutline(created.id, {
          title: "作品概要",
          outline_type: "summary",
          summary: plan.main_plot.trim() || undefined,
          content: state.outline ? `【全书大纲】\n${state.outline}` : undefined,
          status: "active",
        });
        summaryId = sum.id;
      }
      const vol = await api.createOutline(created.id, {
        title: `第一卷 起航`,
        outline_type: "volume",
        parent_id: summaryId,
        summary: plan.main_plot.trim() || undefined,
        sort_order: 1,
        status: "active",
      });
      // 章节间无依赖，分批并行保存（每批 5 章），避免串行逐章等待拖慢整体保存
      const chapters = state.chapters;
      for (let i = 0; i < chapters.length; i += 5) {
        await Promise.all(
          chapters.slice(i, i + 5).map(async (c, j) => {
            const idx = i + j;
            const ch = await api.createOutline(created.id, {
              title: c.title || `第${idx + 1}章`,
              outline_type: "chapter",
              parent_id: vol.id,
              sort_order: idx + 1,
              status: "confirmed",
            });
            await api.saveChapterContent(created.id, ch.id, c.content, { timeoutMs: 60000 });
          })
        );
      }

      const snap: AiWorkSnapshot = {
        projectId: created.id,
        title: title.trim(),
        settings: { ...draft },
        outline: state.outline,
        chapters: state.chapters,
        savedAt: new Date().toISOString(),
        state,
      };
      try {
        localStorage.setItem(WORK_KEY(created.id), JSON.stringify(snap));
      } catch {
        /* ignore */
      }

      setInfo(`已保存到作品库，共 ${state.chapters.length} 章。正在跳转…`);
      // 有一致性提示时多停留几秒，让作者看清黄条再跳转
      setTimeout(() => navigate(`/projects/${created.id}/chapters`), issues && issues.length > 0 ? 5000 : 600);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else if (err instanceof ApiError && err.status === 403) {
        setError("没有保存权限，请检查当前账号角色");
      } else if (err instanceof ApiError && err.status === 422) {
        setError(err.message || "请求参数校验失败，请检查保存内容后重试");
      } else if (err instanceof ApiError && err.status >= 500) {
        setError(`服务端错误，请稍后重试（${err.message}）`);
      } else if (err instanceof ApiError && err.status === 0) {
        setError("请求超时，服务暂时不可用，请稍后重试");
      } else if (err instanceof TypeError) {
        setError("无法连接后端服务，请确认服务已启动后重试");
      } else {
        setError(`${err instanceof Error ? err.message : "保存失败"}，请稍后重试`);
      }
    } finally {
      setSaving(false);
    }
  }

  const totalChars = state.chapters.reduce((s, c) => s + c.content.length, 0);
  const plan = state.plan;

  return (
    <div style={{ maxWidth: 1040 }}>
      <div className="topbar">
        <h1>AI 小说创作（灵感 → 规划 → 锁定 → 写作）</h1>
        <div style={{ display: "flex", gap: 10 }}>
          {editProjectId && (
            <button className="secondary" onClick={() => navigate(`/projects/${editProjectId}`)}>
              ← 返回作品
            </button>
          )}
          <button className="secondary" onClick={() => navigate("/projects")}>
            ← 作品库
          </button>
        </div>
      </div>

      {/* 步骤指示 */}
      <div className="card" style={{ padding: "10px 14px", display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontWeight: 700 }}>
          {stage === "idea" && "① 灵感输入"}
          {stage === "plan" && "② 规划确认"}
          {stage === "write" && "③ 逐章写作"}
        </span>
        <span className={stage === "idea" ? "chip chip-active" : "chip"}>灵感输入</span>
        <span>→</span>
        <span className={stage === "plan" ? "chip chip-active" : "chip"}>规划确认（可编辑 / 可锁定 / 可局部修改）</span>
        <span>→</span>
        <span className={stage === "write" ? "chip chip-active" : "chip"}>逐章写作</span>
        {stage === "write" && (
          <button className="chip" onClick={handleBackToPlan}>
            ← 返回修改规划（不会重置正文进度）
          </button>
        )}
      </div>

      {info && <div className="success-box">{info}</div>}
      {error && <div className="error-box">{error}</div>}

      {/* 模式切换开关 */}
      <div className="card">
        <h3 className="panel-title">模式切换</h3>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {(
            [
              ["auto", "自动（默认）", "后端根据是否配置有效密钥自动选择：已配置走真实模型，未配置走演示模式"],
              ["mock", "演示模式", "强制使用本地动态模拟，结果标注「演示生成」"],
              ["real", "AI 模式", "强制使用真实 DeepSeek 模型；未配置密钥时返回「尚未配置 AI 服务」错误，绝不伪装"],
            ] as const
          ).map(([val, label, desc]) => (
            <button
              key={val}
              className={modeSel === val ? "chip chip-active" : "chip"}
              onClick={() => setModeSel(val)}
              title={desc}
            >
              {label}
            </button>
          ))}
          <span style={{ opacity: 0.7, fontSize: 13 }}>
            当前后端状态：{aiStatus === null ? "加载中…" : aiReady
              ? `AI 模式已就绪（${aiStatus.model} @ ${aiStatus.base_url}）`
              : "尚未配置 AI 服务（后端未检测到有效密钥）"}
          </span>
        </div>
        {!aiReady && (
          <div className="demo-badge" style={{ marginTop: 8, padding: "8px 12px" }}>
            配置说明：在 backend/.env 中填写 DEEPSEEK_API_KEY、AI_BASE_URL、AI_MODEL 并重启后端后，此处将显示「AI 模式已就绪」。
          </div>
        )}
        {modeSel === "real" && !aiReady && (
          <div style={{ marginTop: 8, color: "#b54708" }}>
            当前未配置有效密钥：AI 模式按钮仍可点击，但后端会返回「尚未配置 AI 服务」结构化错误，不会伪装成已生成。
          </div>
        )}
      </div>

      {/* ============ 第六阶段：模型配置区（provider / model / 温度 / max_tokens / 思考模型） ============ */}
      <div className="card">
        <h3 className="panel-title">模型配置</h3>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div className="field">
            <label>模型提供方</label>
            <select
              value={state.modelCfg.provider}
              disabled={!aiConfig || aiConfig.providers.length === 0}
              onChange={(e) => {
                const provider = e.target.value;
                const prov = aiConfig?.providers.find((p) => p.provider === provider);
                const model_name = prov?.models[0] || "";
                const reasoning = !!prov?.support_reasoning && (aiConfig?.reasoning_models.includes(model_name) ?? false);
                setState((prev) => {
                  const next: AiModelConfig = { ...prev.modelCfg, provider, model_name, reasoning };
                  saveModelCfg(next);
                  return { ...prev, modelCfg: next };
                });
              }}
            >
              {aiConfig === null && <option value="">加载中…</option>}
              {aiConfig?.providers.map((p) => (
                <option key={p.provider} value={p.provider}>
                  {p.label}（{p.provider}）
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>模型名称</label>
            <select
              value={state.modelCfg.model_name}
              disabled={!state.modelCfg.provider}
              onChange={(e) => {
                const model_name = e.target.value;
                const reasoning = !!aiConfig?.reasoning_models.includes(model_name);
                setState((prev) => {
                  const next: AiModelConfig = { ...prev.modelCfg, model_name, reasoning };
                  saveModelCfg(next);
                  return { ...prev, modelCfg: next };
                });
              }}
            >
              {(aiConfig?.providers.find((p) => p.provider === state.modelCfg.provider)?.models || []).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>
              温度：{Number.isFinite(state.modelCfg.temperature) ? state.modelCfg.temperature.toFixed(1) : "-"}
            </label>
            <input
              type="range"
              min={0}
              max={2}
              step={0.1}
              value={Number.isFinite(state.modelCfg.temperature) ? state.modelCfg.temperature : 0.8}
              onChange={(e) => {
                const temperature = Number(e.target.value);
                setState((prev) => {
                  const next: AiModelConfig = { ...prev.modelCfg, temperature };
                  saveModelCfg(next);
                  return { ...prev, modelCfg: next };
                });
              }}
            />
          </div>
          <div className="field">
            <label>最大输出长度（token）</label>
            <input
              type="number"
              min={256}
              max={32000}
              step={256}
              value={state.modelCfg.max_tokens}
              onChange={(e) => {
                const max_tokens = Number(e.target.value);
                if (!Number.isFinite(max_tokens) || max_tokens <= 0) return;
                setState((prev) => {
                  const next: AiModelConfig = { ...prev.modelCfg, max_tokens: Math.min(32000, Math.max(256, max_tokens)) };
                  saveModelCfg(next);
                  return { ...prev, modelCfg: next };
                });
              }}
            />
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 10 }}>
          <button
            className={state.modelCfg.reasoning ? "chip chip-active" : "chip"}
            disabled={!state.modelCfg.provider}
            onClick={() => {
              const prov = aiConfig?.providers.find((p) => p.provider === state.modelCfg.provider);
              const reasoningModels = aiConfig?.reasoning_models || [];
              const normalModels = (prov?.models || []).filter((m) => !reasoningModels.includes(m));
              const nextReasoning = !state.modelCfg.reasoning;
              const model_name = nextReasoning
                ? reasoningModels.find((m) => prov?.models.includes(m)) || state.modelCfg.model_name
                : normalModels[0] || state.modelCfg.model_name;
              setState((prev) => {
                const next: AiModelConfig = { ...prev.modelCfg, reasoning: nextReasoning, model_name };
                saveModelCfg(next);
                return { ...prev, modelCfg: next };
              });
            }}
            title="开启后自动切换到思维链模型（如 deepseek-reasoner），适合复杂创作推理；关闭则使用常规模型"
          >
            {state.modelCfg.reasoning ? "🧠 思考模型：开" : "思考模型：关"}
          </button>
          <span style={{ opacity: 0.7, fontSize: 13 }}>
            {aiConfig === null
              ? "正在加载后端模型配置…"
              : aiConfig.providers.length === 0
                ? "后端未返回可选项（请配置 AI 服务后刷新）"
                : `当前生效：${state.modelCfg.provider || "-"} / ${state.modelCfg.model_name || "-"} · 温度 ${Number.isFinite(state.modelCfg.temperature) ? state.modelCfg.temperature.toFixed(1) : "-"} · 最大 ${state.modelCfg.max_tokens} token${state.modelCfg.reasoning ? " · 思考模型" : ""}`}
          </span>
          <span style={{ opacity: 0.55, fontSize: 12 }}>
            密钥仅存于服务端环境变量，前端不可见；所选配置随每次生成请求发送并记入生成记录。
          </span>
        </div>
      </div>

      {genMode === "mock" && (stage !== "idea") && (
        <div className="demo-badge" style={{ marginBottom: 10, padding: "8px 12px", display: "inline-block" }}>
          当前为演示模式（mode=mock）：结果由本地模拟器按灵感与设置动态构造，并标注「演示生成」。
        </div>
      )}

      {/* ============ 阶段一：灵感输入 ============ */}
      {stage === "idea" && (
        <div className="card">
          <h3 className="panel-title">一、灵感输入（只填灵感即可，AI 自动补全完整规划）</h3>
          <div className="field">
            <label>灵感（必填）</label>
            <textarea
              value={state.idea}
              onChange={(e) => setState((prev) => ({ ...prev, idea: e.target.value }))}
              placeholder="示例：现代都市悬疑，心理医生女主林晚，一名病人声称她曾被抹去了一段关键记忆，而那段记忆与十年前的一起失踪案有关……"
              style={{ minHeight: 120, fontFamily: "inherit", lineHeight: 1.7 }}
            />
          </div>

          <h3 className="panel-title" style={{ marginTop: 18 }}>可选设置（不填则交由 AI 按灵感判断）</h3>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <div className="field">
              <label>题材</label>
              <input
                value={state.options.genre}
                onChange={(e) => update("genre", e.target.value)}
                placeholder="现代都市悬疑 / 科幻 / 古风…"
              />
            </div>
            <div className="field">
              <label>文风</label>
              <input
                value={state.options.writing_style}
                onChange={(e) => update("writing_style", e.target.value)}
                placeholder="冷峻写实 / 轻松幽默 / 细腻古雅…"
              />
            </div>
            <div className="field">
              <label>叙事视角</label>
              <input
                value={state.options.perspective}
                onChange={(e) => update("perspective", e.target.value)}
                placeholder="第一人称 / 第三人称 / 有限视角…"
              />
            </div>
            <div className="field">
              <label>结局类型</label>
              <input
                value={state.options.ending_type}
                onChange={(e) => update("ending_type", e.target.value)}
                placeholder="圆满 / 开放式 / 悲剧 / 反转…"
              />
            </div>
            <div className="field">
              <label>目标章节数</label>
              <input
                type="number"
                min={1}
                max={1000}
                value={state.options.chapter_count}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  update("chapter_count", Number.isFinite(v) ? Math.min(1000, Math.max(1, v)) : 1);
                }}
              />
            </div>
            <div className="field">
              <label>每章字数（约）</label>
              <input
                type="number"
                min={100}
                max={8000}
                step={100}
                value={state.options.words_per_chapter}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  update("words_per_chapter", Number.isFinite(v) ? Math.min(8000, Math.max(100, v)) : 500);
                }}
              />
            </div>
          </div>
          <div style={{ display: "flex", gap: 20, alignItems: "center", marginTop: 8 }}>
            <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={state.options.has_romance}
                onChange={(e) => update("has_romance", e.target.checked)}
              />
              需要感情线
            </label>
          </div>
          <div className="field">
            <label>内容限制</label>
            <input
              value={state.options.content_restrictions}
              onChange={(e) => update("content_restrictions", e.target.value)}
              placeholder="如：不出现血腥描写、主角不能黑化…"
            />
          </div>
          <div className="field">
            <label>必须保留设定</label>
            <textarea
              value={state.options.must_keep_settings}
              onChange={(e) => update("must_keep_settings", e.target.value)}
              placeholder="如：主角必须是一名心理医生；故事必须发生在现代都市…"
              style={{ minHeight: 50 }}
            />
          </div>
          <div className="field">
            <label>已填详细设定（若有，AI 只补空白，不擅改已确认内容）</label>
            <textarea
              value={state.options.existing_settings}
              onChange={(e) => update("existing_settings", e.target.value)}
              placeholder="已有的人物名、世界观规则、情节节点等，可自由书写多行…"
              style={{ minHeight: 60 }}
            />
          </div>
          <div style={{ marginTop: 16, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <button onClick={() => void handlePlanNovel()} disabled={generating}>
              {generating
                ? `AI 规划生成中…已 ${planJobElapsed} 秒${
                    planJobStatus === "queued"
                      ? "（排队中）"
                      : planJobStatus === "cancel_requested"
                        ? "（正在取消）"
                        : "（可离开本页）"
                  }`
                : "✦ AI 生成规划"}
            </button>
            {generating && planJobId && planJobStatus !== "cancel_requested" && (
              <button className="secondary" onClick={() => void handleCancelPlanJob()}>
                取消生成
              </button>
            )}
          </div>
        </div>
      )}

      {/* ============ 阶段二：规划确认 ============ */}
      {stage === "plan" && plan && (
        <div className="card">
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <h3 className="panel-title" style={{ margin: 0 }}>二、AI 规划确认（可直接编辑；🔓 点击锁定，锁定后该项禁编且后续正文强制遵守）</h3>
            <span style={{ fontSize: 13, opacity: 0.7 }}>
              锁定 {state.lockedKeys.length} 项 · 手动修改 {state.userEditedKeys.length} 项 · AI 局部修改章节 {state.changedChapterIds.length} 章
            </span>
          </div>

          {/* 书名候选 */}
          <div className="field">
            <label>书名候选（点击选择，用于后续生成）</label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {plan.title_candidates.map((t) => (
                <button
                  key={t}
                  className={state.selectedTitle === t ? "chip chip-active" : "chip"}
                  onClick={() => setState((prev) => ({ ...prev, selectedTitle: t }))}
                >
                  {state.selectedTitle === t ? "✓ " : ""}{t}
                </button>
              ))}
            </div>
          </div>

          {/* 概要类字段 */}
          {(
            [
              ["selling_point", "一句话卖点", plan.selling_point],
              ["synopsis", "故事简介", plan.synopsis],
              ["world_view", "世界观", plan.world_view],
              ["era", "时代背景", plan.era],
              ["core_conflict", "核心冲突", plan.core_conflict],
              ["protagonist", "主角小传", plan.protagonist],
              ["relationships", "人物关系", plan.relationships],
              ["main_plot", "故事主线", plan.main_plot],
              ["ending_type", "结局类型", plan.ending_type],
            ] as const
          ).map(([key, label, value]) => (
            <div className="field" key={key}>
              <label>
                {label}
                <LockBadge
                  locked={state.lockedKeys.includes(key)}
                  onToggle={() => toggleLock(key)}
                  edited={state.userEditedKeys.includes(key)}
                />
              </label>
              <textarea
                value={value}
                disabled={state.lockedKeys.includes(key)}
                onChange={(e) => editString(key, e.target.value)}
                style={{ minHeight: 44, fontFamily: "inherit", lineHeight: 1.6 }}
              />
            </div>
          ))}

          {/* 配角 */}
          <div className="field">
            <label>
              配角小传
              <LockBadge
                locked={state.lockedKeys.includes("supporting_characters")}
                onToggle={() => toggleLock("supporting_characters")}
                edited={state.userEditedKeys.includes("supporting_characters")}
              />
            </label>
            {plan.supporting_characters.map((c, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 3fr auto", gap: 8, marginBottom: 8 }}>
                <input
                  value={c.name}
                  disabled={state.lockedKeys.includes("supporting_characters")}
                  onChange={(e) => editCharacter(i, "name", e.target.value)}
                  placeholder="姓名"
                />
                <input
                  value={c.role}
                  disabled={state.lockedKeys.includes("supporting_characters")}
                  onChange={(e) => editCharacter(i, "role", e.target.value)}
                  placeholder="身份"
                />
                <textarea
                  value={c.description}
                  disabled={state.lockedKeys.includes("supporting_characters")}
                  onChange={(e) => editCharacter(i, "description", e.target.value)}
                  placeholder="性格与经历"
                  style={{ minHeight: 30, fontFamily: "inherit", lineHeight: 1.5 }}
                />
                <button
                  className="chip"
                  onClick={() => removeCharacter(i)}
                  disabled={state.lockedKeys.includes("supporting_characters")}
                >
                  删除
                </button>
              </div>
            ))}
            <button className="secondary" onClick={addCharacter} disabled={state.lockedKeys.includes("supporting_characters")}>
              + 添加配角
            </button>
          </div>

          {/* 分卷与章节大纲 */}
          <h3 className="panel-title" style={{ marginTop: 16 }}>分卷与章节大纲（每章：目标 / 冲突 / 关键事件 / 结尾悬念）</h3>
          {plan.volumes.map((v) => (
            <div key={v.volume_index} style={{ border: "1px solid var(--border,#e5e7eb)", borderRadius: 8, padding: 12, marginBottom: 12 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong>第 {v.volume_index} 卷</strong>
                <input
                  value={v.title}
                  disabled={state.lockedKeys.includes(`volume:${v.volume_index}`)}
                  onChange={(e) => {
                    markEdited(`volume:${v.volume_index}`);
                    updatePlan((p) => {
                      const tv = p.volumes.find((x) => x.volume_index === v.volume_index);
                      if (tv) tv.title = e.target.value;
                      return p;
                    });
                  }}
                  style={{ flex: 1 }}
                />
                <LockBadge
                  locked={state.lockedKeys.includes(`volume:${v.volume_index}`)}
                  onToggle={() => toggleLock(`volume:${v.volume_index}`)}
                />
              </div>
              <div className="field" style={{ marginTop: 6 }}>
                <textarea
                  value={v.summary}
                  disabled={state.lockedKeys.includes(`volume:${v.volume_index}`)}
                  onChange={(e) => {
                    markEdited(`volume:${v.volume_index}`);
                    updatePlan((p) => {
                      const tv = p.volumes.find((x) => x.volume_index === v.volume_index);
                      if (tv) tv.summary = e.target.value;
                      return p;
                    });
                  }}
                  placeholder="本卷核心"
                  style={{ minHeight: 40, fontFamily: "inherit", lineHeight: 1.5 }}
                />
              </div>
              {v.chapters.map((c) => (
                <div key={c.index} style={{ border: "1px dashed #d1d5db", borderRadius: 6, padding: 8, marginBottom: 8 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <strong>第 {c.index} 章</strong>
                    <input
                      value={c.title}
                      disabled={state.lockedKeys.includes(`chapter:${c.index}`)}
                      onChange={(e) => editChapter(c.index, "title", e.target.value)}
                      style={{ flex: 1 }}
                    />
                    <LockBadge
                      locked={state.lockedKeys.includes(`chapter:${c.index}`)}
                      onToggle={() => toggleLock(`chapter:${c.index}`)}
                      edited={state.changedChapterIds.includes(c.index)}
                    />
                  </div>
                  {(
                    [
                      ["goal", "本章目标", c.goal],
                      ["conflict", "冲突", c.conflict],
                      ["key_events", "关键事件", c.key_events],
                      ["cliffhanger", "结尾悬念", c.cliffhanger],
                    ] as const
                  ).map(([field, label, value]) => (
                    <div className="field" key={field}>
                      <label style={{ fontSize: 12, opacity: 0.75 }}>{label}</label>
                      <textarea
                        value={value}
                        disabled={state.lockedKeys.includes(`chapter:${c.index}`)}
                        onChange={(e) => editChapter(c.index, field, e.target.value)}
                        style={{ minHeight: 30, fontFamily: "inherit", lineHeight: 1.5, fontSize: 13 }}
                      />
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ))}

          {/* 伏笔与回收 */}
          <div className="field">
            <label>
              伏笔与回收计划
              <LockBadge
                locked={state.lockedKeys.includes("foreshadowing")}
                onToggle={() => toggleLock("foreshadowing")}
                edited={state.userEditedKeys.includes("foreshadowing")}
              />
            </label>
            {plan.foreshadowing.map((f, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 3fr", gap: 8, marginBottom: 8 }}>
                <input
                  value={f.item}
                  disabled={state.lockedKeys.includes("foreshadowing")}
                  onChange={(e) => editForeshadow(i, "item", e.target.value)}
                  placeholder="伏笔内容"
                />
                <input
                  type="number"
                  value={f.planted_chapter}
                  disabled={state.lockedKeys.includes("foreshadowing")}
                  onChange={(e) => editForeshadow(i, "planted_chapter", Number(e.target.value) || 0)}
                  placeholder="埋设章"
                />
                <input
                  type="number"
                  value={f.payoff_chapter}
                  disabled={state.lockedKeys.includes("foreshadowing")}
                  onChange={(e) => editForeshadow(i, "payoff_chapter", Number(e.target.value) || 0)}
                  placeholder="回收章"
                />
                <input
                  value={f.note}
                  disabled={state.lockedKeys.includes("foreshadowing")}
                  onChange={(e) => editForeshadow(i, "note", e.target.value)}
                  placeholder="说明"
                />
              </div>
            ))}
          </div>

          {/* AI 局部修改 */}
          <div className="card" style={{ marginTop: 14, background: "rgba(255,255,255,0.5)" }}>
            <h3 className="panel-title">AI 修改规划（局部修改，不重置整本）</h3>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                value={planInstruction}
                onChange={(e) => setPlanInstruction(e.target.value)}
                placeholder="如：把第三章改成主角第一次发现反派身份 / 删除感情线 / 结局改开放式 / 增加五章调查过程"
                style={{ flex: 1 }}
              />
              <button onClick={() => void handlePlanUpdate()} disabled={planUpdating || !planInstruction.trim()}>
                {planUpdating ? "修改中…" : "AI 局部修改"}
              </button>
            </div>
            {state.changedChapterIds.length > 0 && (
              <div style={{ marginTop: 8, fontSize: 13, opacity: 0.8 }}>
                已标记 AI 局部修改的章节：{state.changedChapterIds.map((i) => `第${i}章`).join("、")}
              </div>
            )}
          </div>

          <div style={{ marginTop: 18, display: "flex", gap: 10, flexWrap: "wrap" }}>
            <button onClick={handleConfirmPlan}>✓ 确认规划并开始写作</button>
            <button
              className="secondary"
              onClick={() => setState((prev) => ({ ...prev, stage: "idea" }))}
            >
              ← 返回修改灵感（重新生成规划）
            </button>
            {generating && (
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className="spin" /> 生成中…
              </span>
            )}
          </div>
        </div>
      )}

      {/* ============ 阶段三：逐章写作 ============ */}
      {stage === "write" && plan && (
        <>
          <div className="card">
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <h3 className="panel-title" style={{ margin: 0 }}>三、逐章写作（规划已确认，正文将强制遵守锁定设定）</h3>
              <span style={{ fontSize: 13, opacity: 0.7 }}>
                已确认规划 · 锁定 {state.lockedKeys.length} 项 · 已生成 {state.chapters.length} 章
              </span>
            </div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 12 }}>
              {state.chapters.length === 0 && (
                <button onClick={() => void handleGenerateFirstChapter()} disabled={generating}>
                  {generating ? "AI 生成中…" : "✦ 生成第一章"}
                </button>
              )}
              {state.chapters.length > 0 && (
                <>
                  <button onClick={() => void handleGenerateFirstChapter()} disabled={generating}>
                    {generating ? "AI 生成中…" : "重新生成大纲 + 第一章"}
                  </button>
                  <button className="secondary" onClick={() => void handleNextChapter()} disabled={generating}>
                    生成下一章
                  </button>
                  <button className="secondary" onClick={() => void handleRegenerateChapter()} disabled={generating}>
                    重新生成本章
                  </button>
                  <button className="secondary" onClick={() => void handleExpandChapter()} disabled={generating}>
                    扩写本章
                  </button>
                  <button className="secondary" onClick={() => void handlePolishChapter()} disabled={generating}>
                    润色本章
                  </button>
                  <button className="secondary" onClick={() => void handleCopyAll()}>
                    {copied ? "已复制 ✓" : "复制大纲 + 全部章节"}
                  </button>
                  <button className="secondary" onClick={() => void runQuickCheck()} disabled={quickBusy || generating}>
                    {quickBusy ? "检查中…" : "一致性检查"}
                  </button>
                  <button onClick={() => void handleSaveToLibrary()} disabled={saving || generating}>
                    {saving ? "保存中…" : "保存到作品库"}
                  </button>
                </>
              )}
            </div>
            {generating && (
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10 }}>
                <span className="spin" />
                <span>正在调用后端 AI 生成…（演示模式秒回，AI 模式视内容长度约 10~120 秒）</span>
                {cancellable && (
                  <button className="secondary" onClick={() => void handleCancelGeneration()}>
                    取消生成
                  </button>
                )}
              </div>
            )}
          </div>

          {state.chapters.length > 0 && (
            <div className="card">
              <h3 className="panel-title">生成结果（{state.chapters.length} 章 · 约 {totalChars} 字）</h3>
              {quickIssues !== null && (
                quickIssues.length === 0 ? (
                  <div className="info-box">✓ 一致性检查通过（4 维：伏笔顺序 / 时间线 / 人物状态 / 设定限制）：未发现矛盾</div>
                ) : (
                  <div className="error-box" style={{ borderColor: "#d9a514", background: "#fff8e1", color: "#5c4300" }}>
                    <strong>⚠ 一致性检查发现 {quickIssues.length} 处提示（不阻断保存，请自行判断；回忆/闪回场景可忽略）</strong>
                    <ul>
                      {quickIssues.map((q, i) => (
                        <li key={i} style={{ marginTop: 4 }}>
                          [{q.location || "-"} · {q.type}] {q.message}
                          {q.suggestion ? ` 建议：${q.suggestion}` : ""}
                        </li>
                      ))}
                    </ul>
                  </div>
                )
              )}
              <div className="chapter-tabs">
                {state.chapters.map((c, i) => (
                  <button
                    key={i}
                    className={`chip${i === activeIndex ? " chip-active" : ""}`}
                    onClick={() => setActiveIndex(i)}
                  >
                    第 {i + 1} 章
                  </button>
                ))}
              </div>
              {state.chapters[activeIndex] && (
                <div>
                  <div className="field">
                    <label>章节标题</label>
                    <input
                      value={state.chapters[activeIndex].title}
                      onChange={(e) =>
                        setState((prev) => ({
                          ...prev,
                          chapters: prev.chapters.map((c, i) =>
                            i === activeIndex ? { ...c, title: e.target.value } : c
                          ),
                        }))
                      }
                    />
                  </div>
                  <div className="field">
                    <label>
                      正文（{state.chapters[activeIndex].content.length} 字，可直接编辑）
                    </label>
                    <textarea
                      value={state.chapters[activeIndex].content}
                      onChange={(e) => updateChapterContent(e.target.value)}
                      style={{ minHeight: 320, fontFamily: "inherit", lineHeight: 1.7 }}
                    />
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* 「已确认大纲版本化」：确认新大纲版本影响面卡片（先预览后持久化，不静默替换） */}
      {confirmPreview && (
        <div className="drawer-overlay" onClick={cancelConfirmPlan}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="drawer-title">确认新大纲版本</span>
              <button className="secondary" onClick={cancelConfirmPlan}>取消</button>
            </div>
            <div className="drawer-body">
              <div className="drawer-item-meta">
                将创建 <strong>v{confirmPreview.version.version}</strong>（status: {confirmPreview.version.status}）
                {confirmPreview.version.confirmed_at ? ` · 确认时间 ${confirmPreview.version.confirmed_at}` : ""}
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
                <button className="primary" onClick={() => void doConfirmPlan()} disabled={confirmPlanBusy}>
                  {confirmPlanBusy ? "确认中…" : "确认并持久化版本"}
                </button>
                <button className="secondary" onClick={cancelConfirmPlan} disabled={confirmPlanBusy}>取消</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
