import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AiConfigResponse,
  type LocaleInfo,
  type NotificationPref,
  type MeInfo,
  type UsageSummary,
} from "../api/client";
import { demoLocale, demoPrefs } from "../api/mock";

const MODEL_CFG_KEY = "novelcraft_ai_model_cfg"; // 模型偏好（不含密钥），与创建页共享

interface AiModelConfig {
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

function readModelCfg(): AiModelConfig {
  try {
    const raw = localStorage.getItem(MODEL_CFG_KEY);
    if (raw) return { ...DEFAULT_MODEL_CFG, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_MODEL_CFG };
}

function writeModelCfg(cfg: AiModelConfig): void {
  try {
    localStorage.setItem(MODEL_CFG_KEY, JSON.stringify(cfg));
  } catch {
    /* ignore */
  }
}

const LANGUAGE_OPTIONS = [
  "zh-CN",
  "zh-TW",
  "en-US",
  "ja-JP",
  "ko-KR",
  "fr-FR",
  "de-DE",
  "es-ES",
];

const TIMEZONES = [
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Seoul",
  "Asia/Singapore",
  "America/New_York",
  "Europe/London",
  "Europe/Paris",
  "UTC",
];

const REGIONS = ["CN", "HK", "TW", "US", "JP", "KR", "SG", "GB", "FR", "DE", "ES"];

const NOTIFICATION_TYPES = [
  { value: "comment", label: "评论与反馈" },
  { value: "collaboration", label: "协作邀请" },
  { value: "system", label: "系统通知" },
  { value: "ai_task", label: "AI 任务" },
];

function readLocalProfile(): { nickname: string; bio: string } {
  try {
    const raw = localStorage.getItem("novelcraft_local_profile");
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return { nickname: "", bio: "" };
}

export default function SettingsPage() {
  const [me, setMe] = useState<MeInfo | null>(null);
  const [nickname, setNickname] = useState("");
  const [bio, setBio] = useState("");
  const [profileSaved, setProfileSaved] = useState("");

  const [locale, setLocale] = useState<LocaleInfo>({});
  const [localeDemo, setLocaleDemo] = useState(false);
  const [localeSaved, setLocaleSaved] = useState("");

  const [prefs, setPrefs] = useState<NotificationPref[]>([]);
  const [prefsDemo, setPrefsDemo] = useState(false);
  const [prefsSaved, setPrefsSaved] = useState("");

  const [aiConfig, setAiConfig] = useState<AiConfigResponse | null>(null);
  const [aiConfigDemo, setAiConfigDemo] = useState(false);
  const [modelCfg, setModelCfg] = useState<AiModelConfig>(readModelCfg);
  const [modelSaved, setModelSaved] = useState("");

  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [usageBusy, setUsageBusy] = useState(false);

  async function loadUsage() {
    setUsageBusy(true);
    try {
      const data = await api.getUsageSummary(14);
      setUsage(data);
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        setError(err instanceof Error ? err.message : "用量加载失败");
      }
    } finally {
      setUsageBusy(false);
    }
  }

  useEffect(() => {
    void loadUsage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError("");
      // 个人资料
      try {
        const data = await api.me();
        setMe(data);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setError("登录已过期，请重新登录");
        }
      }
      const local = readLocalProfile();
      setNickname(local.nickname || "");
      setBio(local.bio || "");
      // 语言与时区
      try {
        const loc = await api.getLocale();
        setLocale(loc);
        setLocaleDemo(false);
      } catch {
        setLocale(demoLocale);
        setLocaleDemo(true);
      }
      // 通知偏好
      try {
        const list = await api.listNotificationPrefs();
        setPrefs(list);
        setPrefsDemo(false);
      } catch {
        setPrefs(demoPrefs);
        setPrefsDemo(true);
      }
      // 模型配置（第六阶段：模型切换入口）
      try {
        const cfg = await api.getAiConfig();
        setAiConfig(cfg);
        setAiConfigDemo(false);
        setModelCfg((prev) => {
          const providerExists = cfg.providers.some((p) => p.provider === prev.provider);
          const provider = providerExists ? prev.provider : (cfg.providers[0]?.provider || "");
          const prov = cfg.providers.find((p) => p.provider === provider);
          const model_name = prov?.models.includes(prev.model_name) ? prev.model_name : (prov?.models[0] || "");
          return { ...prev, provider, model_name };
        });
      } catch {
        setAiConfig(null);
        setAiConfigDemo(true);
      }
      setLoading(false);
    }
    void load();
  }, []);

  function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    localStorage.setItem(
      "novelcraft_local_profile",
      JSON.stringify({ nickname: nickname.trim(), bio: bio.trim() })
    );
    setProfileSaved("已保存（本地设置：后端未提供个人资料更新接口）");
  }

  async function handleSaveLocale(e: React.FormEvent) {
    e.preventDefault();
    setLocaleSaved("");
    setError("");
    try {
      await api.saveLocale({
        ui_language: locale.ui_language || undefined,
        timezone: locale.timezone || undefined,
        region: locale.region || undefined,
        locale: locale.locale || undefined,
        date_format: locale.date_format || undefined,
        time_format: locale.time_format || undefined,
        content_language: locale.content_language || undefined,
        notification_language: locale.notification_language || undefined,
        ai_output_language: locale.ai_output_language || undefined,
      });
      setLocaleDemo(false);
      setLocaleSaved("语言与时区偏好已保存到后端");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setLocaleDemo(true);
        setLocaleSaved("");
        setError(`${err instanceof Error ? err.message : "保存失败"}，已保留本地编辑内容`);
      }
    }
  }

  async function togglePref(pref: NotificationPref, channel: "in_app_enabled" | "email_enabled" | "push_enabled") {
    setError("");
    const next = !pref[channel];
    const current = prefs.find((p) => p.notification_type === pref.notification_type);
    if (!current) return;
    try {
      await api.updateNotificationPref(pref.notification_type, { [channel]: next });
      setPrefs((prev) =>
        prev.map((p) =>
          p.notification_type === pref.notification_type ? { ...p, [channel]: next } : p
        )
      );
      setPrefsDemo(false);
      setPrefsSaved("通知偏好已更新");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setPrefsDemo(true);
        setPrefsSaved("");
        setError(`${err instanceof Error ? err.message : "更新失败"}，已使用演示偏好`);
      }
    }
  }

  function handleSaveModel(e: React.FormEvent) {
    e.preventDefault();
    writeModelCfg(modelCfg);
    setModelSaved("模型偏好已保存（本地设置，创建页生成时将随请求发送）");
  }

  return (
    <div style={{ maxWidth: 860 }}>
      <div className="topbar">
        <h1>
          设置
          {(localeDemo || prefsDemo || aiConfigDemo) && <span className="demo-badge">部分演示数据</span>}
        </h1>
      </div>

      {error && <div className="error-box">{error}</div>}

      {loading ? (
        <div className="empty">
          <span className="spin" /> 正在加载设置…
        </div>
      ) : (
        <div className="settings-grid">
          {/* 个人资料 */}
          <div className="card">
            <h3 className="panel-title">个人资料</h3>
            {me && (
              <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 12 }}>
                账号：{me.username} · {me.email} · 角色 {me.role}
              </div>
            )}
            <form onSubmit={handleSaveProfile}>
              <div className="field">
                <label>
                  昵称
                  <span style={{ fontSize: 11, color: "var(--text-dim)", marginLeft: 6 }}>
                    （本地设置）
                  </span>
                </label>
                <input value={nickname} onChange={(e) => setNickname(e.target.value)} placeholder="你的昵称" />
              </div>
              <div className="field">
                <label>
                  简介
                  <span style={{ fontSize: 11, color: "var(--text-dim)", marginLeft: 6 }}>
                    （本地设置）
                  </span>
                </label>
                <textarea value={bio} onChange={(e) => setBio(e.target.value)} style={{ minHeight: 70 }} placeholder="一句话介绍自己" />
              </div>
              <button type="submit">保存资料</button>
              {profileSaved && <div className="ok-box" style={{ marginTop: 10 }}>{profileSaved}</div>}
            </form>
          </div>

          {/* 语言与时区 */}
          <div className="card">
            <h3 className="panel-title">
              语言与时区
              {localeDemo && <span className="demo-badge">演示数据</span>}
            </h3>
            <form onSubmit={handleSaveLocale}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <div className="field">
                  <label>界面语言</label>
                  <select
                    value={locale.ui_language || "zh-CN"}
                    onChange={(e) => setLocale((prev) => ({ ...prev, ui_language: e.target.value }))}
                  >
                    {LANGUAGE_OPTIONS.map((l) => (
                      <option key={l} value={l}>{l}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>时区</label>
                  <select
                    value={locale.timezone || "Asia/Shanghai"}
                    onChange={(e) => setLocale((prev) => ({ ...prev, timezone: e.target.value }))}
                  >
                    {TIMEZONES.map((t) => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
                <div className="field">
                  <label>地区</label>
                  <select
                    value={locale.region || "CN"}
                    onChange={(e) => setLocale((prev) => ({ ...prev, region: e.target.value }))}
                  >
                    {REGIONS.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>日期格式</label>
                  <input
                    value={locale.date_format || ""}
                    onChange={(e) => setLocale((prev) => ({ ...prev, date_format: e.target.value }))}
                    placeholder="YYYY-MM-DD"
                  />
                </div>
                <div className="field">
                  <label>时间格式</label>
                  <input
                    value={locale.time_format || ""}
                    onChange={(e) => setLocale((prev) => ({ ...prev, time_format: e.target.value }))}
                    placeholder="HH:mm"
                  />
                </div>
              </div>
              <button type="submit">保存偏好</button>
              {localeSaved && <div className="ok-box" style={{ marginTop: 10 }}>{localeSaved}</div>}
            </form>
          </div>

          {/* 通知偏好 */}
          <div className="card">
            <h3 className="panel-title">
              通知偏好
              {prefsDemo && <span className="demo-badge">演示数据</span>}
            </h3>
            {prefs.length === 0 ? (
              <div className="empty">暂无通知偏好</div>
            ) : (
              prefs.map((p) => {
                const label = NOTIFICATION_TYPES.find((t) => t.value === p.notification_type)?.label || p.notification_type;
                return (
                  <div key={p.notification_type} className="pref-row">
                    <div style={{ flex: 1, fontWeight: 600, fontSize: 14 }}>{label}</div>
                    <div className="switch-group">
                      <label className="switch-item">
                        <input
                          type="checkbox"
                          checked={p.in_app_enabled}
                          onChange={() => void togglePref(p, "in_app_enabled")}
                        />
                        <span>站内</span>
                      </label>
                      <label className="switch-item">
                        <input
                          type="checkbox"
                          checked={p.email_enabled}
                          onChange={() => void togglePref(p, "email_enabled")}
                        />
                        <span>邮件</span>
                      </label>
                      <label className="switch-item">
                        <input
                          type="checkbox"
                          checked={p.push_enabled}
                          onChange={() => void togglePref(p, "push_enabled")}
                        />
                        <span>推送</span>
                      </label>
                    </div>
                  </div>
                );
              })
            )}
            {prefsSaved && <div className="ok-box" style={{ marginTop: 10 }}>{prefsSaved}</div>}
          </div>

          {/* 批次 4：token 用量面板（近 14 天，自费 API 成本可见） */}
          <div className="card">
            <h3 className="panel-title">
              Token 用量（近 {usage?.days ?? 14} 天，仅真实模型调用）
              <button
                className="secondary"
                style={{ marginLeft: 10, padding: "3px 10px" }}
                onClick={() => void loadUsage()}
                disabled={usageBusy}
              >
                {usageBusy ? "加载中…" : "刷新"}
              </button>
            </h3>
            {usage === null ? (
              <div className="field">尚未加载。点击刷新获取用量摘要。</div>
            ) : (
              <>
                <div className="field">
                  合计：输入 {usage.total_input_tokens.toLocaleString()} tokens ·
                  输出 {usage.total_output_tokens.toLocaleString()} tokens ·
                  共 {usage.records} 次调用
                </div>
                <div className="field">
                  <label>按模型</label>
                  {usage.by_model.length === 0 ? (
                    <div>暂无记录</div>
                  ) : (
                    <ul style={{ fontSize: 13 }}>
                      {usage.by_model.map((m) => (
                        <li key={m.model}>
                          {m.model}：入 {m.input.toLocaleString()} / 出 {m.output.toLocaleString()}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="field">
                  <label>近 14 天每日消耗（出/入）</label>
                  {usage.by_day.length === 0 ? (
                    <div>暂无记录</div>
                  ) : (
                    <ul style={{ fontSize: 13 }}>
                      {usage.by_day.slice(-7).map((d) => (
                        <li key={d.date}>
                          {d.date}：入 {d.input.toLocaleString()} / 出 {d.output.toLocaleString()}（{d.records} 次）
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                {usage.by_project.length > 0 && (
                  <div className="field">
                    <label>按作品 Top5</label>
                    <ul style={{ fontSize: 13 }}>
                      {usage.by_project.map((p) => (
                        <li key={p.project_id}>
                          {p.title || "（未命名）"}：{(p.input + p.output).toLocaleString()} tokens
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>

          {/* 模型配置（第六阶段：模型切换入口） */}
          <div className="card">
            <h3 className="panel-title">
              AI 模型偏好
              {aiConfigDemo && <span className="demo-badge">未连接后端</span>}
            </h3>
            <form onSubmit={handleSaveModel}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <div className="field">
                  <label>模型提供方</label>
                  <select
                    value={modelCfg.provider}
                    disabled={!aiConfig || aiConfig.providers.length === 0}
                    onChange={(e) => {
                      const provider = e.target.value;
                      const prov = aiConfig?.providers.find((p) => p.provider === provider);
                      setModelCfg((prev) => ({
                        ...prev,
                        provider,
                        model_name: prov?.models[0] || "",
                        reasoning: !!prov?.support_reasoning && !!aiConfig?.reasoning_models.includes(prov?.models[0] || ""),
                      }));
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
                    value={modelCfg.model_name}
                    disabled={!modelCfg.provider}
                    onChange={(e) => {
                      const model_name = e.target.value;
                      setModelCfg((prev) => ({
                        ...prev,
                        model_name,
                        reasoning: !!aiConfig?.reasoning_models.includes(model_name),
                      }));
                    }}
                  >
                    {(aiConfig?.providers.find((p) => p.provider === modelCfg.provider)?.models || []).map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>温度：{Number.isFinite(modelCfg.temperature) ? modelCfg.temperature.toFixed(1) : "-"}</label>
                  <input
                    type="range"
                    min={0}
                    max={2}
                    step={0.1}
                    value={Number.isFinite(modelCfg.temperature) ? modelCfg.temperature : 0.8}
                    onChange={(e) =>
                      setModelCfg((prev) => ({ ...prev, temperature: Number(e.target.value) }))
                    }
                  />
                </div>
                <div className="field">
                  <label>最大输出长度（token）</label>
                  <input
                    type="number"
                    min={256}
                    max={32000}
                    step={256}
                    value={modelCfg.max_tokens}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      if (Number.isFinite(v) && v > 0) {
                        setModelCfg((prev) => ({ ...prev, max_tokens: Math.min(32000, Math.max(256, v)) }));
                      }
                    }}
                  />
                </div>
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 10 }}>
                <button
                  type="button"
                  className={modelCfg.reasoning ? "chip chip-active" : "chip"}
                  disabled={!modelCfg.provider}
                  onClick={() => {
                    const prov = aiConfig?.providers.find((p) => p.provider === modelCfg.provider);
                    const reasoningModels = aiConfig?.reasoning_models || [];
                    const normalModels = (prov?.models || []).filter((m) => !reasoningModels.includes(m));
                    const nextReasoning = !modelCfg.reasoning;
                    setModelCfg((prev) => ({
                      ...prev,
                      reasoning: nextReasoning,
                      model_name: nextReasoning
                        ? reasoningModels.find((m) => prov?.models.includes(m)) || prev.model_name
                        : normalModels[0] || prev.model_name,
                    }));
                  }}
                  title="开启后自动切换到思维链模型（如 deepseek-reasoner）"
                >
                  {modelCfg.reasoning ? "🧠 思考模型：开" : "思考模型：关"}
                </button>
                <span style={{ opacity: 0.55, fontSize: 12 }}>
                  密钥仅存于服务端环境变量，前端不可见；所选配置在创作页生成时随请求发送并记入生成记录。
                </span>
              </div>
              <div style={{ marginTop: 12 }}>
                <button type="submit">保存模型偏好</button>
              </div>
              {modelSaved && <div className="ok-box" style={{ marginTop: 10 }}>{modelSaved}</div>}
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
