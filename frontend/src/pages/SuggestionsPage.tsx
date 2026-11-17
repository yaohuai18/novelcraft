// 第三期「人物和时间不漂」：变更确认面板
// 展示待确认建议（人物状态/关系/时间线/伏笔），支持确认 / 修改确认 / 忽略；含确认策略开关
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type AITask,
  type OutlineNode,
  type ProjectSettingsData,
  type SuggestionData,
} from "../api/client";

const CATEGORY_LABEL: Record<string, string> = {
  character_state: "人物状态",
  relationship: "角色关系",
  timeline: "时间线事件",
  foreshadow: "伏笔",
};

export default function SuggestionsPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [suggestions, setSuggestions] = useState<SuggestionData[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [settings, setSettings] = useState<ProjectSettingsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [modifiedPayload, setModifiedPayload] = useState<Record<string, unknown>>({});
  const [modifiedId, setModifiedId] = useState("");

  const chapterName = useCallback(
    (id?: string | null) => {
      if (!id) return "—";
      const c = chapters.find((x) => x.id === id);
      return c ? c.title : "—";
    },
    [chapters]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [sugs, chs, st] = await Promise.all([
        api.listSuggestions(projectId),
        api.listOutlines(projectId, "chapter"),
        api.getProjectSettings(projectId),
      ]);
      setSuggestions(sugs);
      setChapters(chs);
      setSettings(st);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载变更建议失败");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(sug: SuggestionData, action: "confirm" | "modified" | "ignore") {
    setBusy(sug.id);
    setError("");
    try {
      const payload =
        action === "modified" ? (modifiedId === sug.id ? modifiedPayload : null) : null;
      const updated = await api.confirmSuggestion(projectId, sug.id, { action, payload });
      setSuggestions((prev) => prev.filter((x) => x.id !== sug.id));
      if (updated.status !== "ignored") {
        // 记录已处理，刷新列表
        void load();
      }
      setModifiedId("");
      setModifiedPayload({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "处理建议失败");
    } finally {
      setBusy("");
    }
  }

  async function saveSetting(
    key: "character_confirm" | "foreshadow_confirm" | "timeline_confirm" | "auto_check",
    value: "require" | "auto" | boolean
  ) {
    setError("");
    try {
      const updated = await api.updateProjectSettings(projectId, { [key]: value } as Partial<ProjectSettingsData>);
      setSettings(updated);
      if (value === "auto") {
        void load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存确认策略失败");
    }
  }

  const showPending = (settings?.character_confirm === "require" || settings?.foreshadow_confirm === "require" || settings?.timeline_confirm === "require") || settings === null;

  // ---- P1 变更建议确认：AI 生成建议（任务化）----
  const [aiTask, setAiTask] = useState<AITask | null>(null);
  const [aiTaskId, setAiTaskId] = useState("");
  const [aiSuggestions, setAiSuggestions] = useState<
    { original: string; suggested: string; reason?: string; severity?: string }[]
  >([]);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiNotice, setAiNotice] = useState("");
  const [aiError, setAiError] = useState("");
  const [aiChapterId, setAiChapterId] = useState("");
  const [adopting, setAdopting] = useState("");

  useEffect(() => {
    if (chapters.length > 0 && !aiChapterId) setAiChapterId(chapters[0].id);
  }, [chapters, aiChapterId]);

  useEffect(() => {
    if (!aiTaskId) return;
    let stopped = false;
    let timer: number | null = null;
    const poll = async () => {
      try {
        const t = await api.getAITask(projectId, aiTaskId);
        if (stopped) return;
        setAiTask(t);
        if (t.status === "succeeded" && t.result) {
          try {
            const parsed = JSON.parse(t.result) as {
              suggestions?: { original: string; suggested: string; reason?: string; severity?: string }[];
              note?: string;
            };
            setAiSuggestions(parsed.suggestions || []);
            if (parsed.note) setAiNotice(parsed.note);
          } catch {
            setAiError("AI 建议结果解析失败");
          }
          setAiBusy(false);
          return;
        }
        if (t.status === "failed" || t.status === "timeout" || t.status === "cancelled") {
          setAiError(t.error_message || `任务${t.status}，可前往任务中心重试`);
          setAiBusy(false);
          return;
        }
        timer = window.setTimeout(poll, 3000);
      } catch (err) {
        if (!stopped) {
          setAiError(err instanceof Error ? err.message : "查询任务失败");
          setAiBusy(false);
        }
      }
    };
    poll();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [projectId, aiTaskId]);

  async function startAIGenerate() {
    if (!aiChapterId) {
      setAiError("请先选择要生成建议的章节");
      return;
    }
    setAiBusy(true);
    setAiError("");
    setAiNotice("");
    setAiSuggestions([]);
    setAiTask(null);
    try {
      const resp = await api.createAITask(projectId, {
        task_type: "generate_suggestions",
        prompt: `请阅读章节「${chapters.find((c) => c.id === aiChapterId)?.title || aiChapterId}」正文，生成可操作的修改建议`,
        outline_id: aiChapterId,
        request_snapshot: JSON.stringify({ chapter_title: chapters.find((c) => c.id === aiChapterId)?.title || "" }),
        timeout_seconds: 300,
      });
      setAiTaskId(resp.task_id);
      if (resp.duplicated) setAiNotice("检测到相同任务的重复提交，已返回原任务");
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "创建生成任务失败");
      setAiBusy(false);
    }
  }

  async function adoptSuggestion(idx: number) {
    const s = aiSuggestions[idx];
    setAdopting(String(idx));
    setAiError("");
    try {
      await api.createSuggestion(projectId, {
        chapter_id: aiChapterId,
        category: "ai_suggestion",
        title: (s.suggested || "AI 建议").slice(0, 60),
        description: s.reason || "由 AI 生成变更建议（任务）",
        payload: {
          original: s.original,
          suggested: s.suggested,
          reason: s.reason || "",
          severity: s.severity || "suggestion",
        },
      });
      setAiNotice("已采纳为待确认建议，可在下方列表确认/修改/忽略");
      void load();
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "采纳建议失败");
    } finally {
      setAdopting("");
    }
  }

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载变更建议…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>变更确认</h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/characters`)}>
            人物库
          </button>
          <button onClick={() => void load()}>刷新</button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {/* ---- P1 变更建议确认：AI 生成建议（任务化）---- */}
      <div className="card" style={{ marginBottom: 18 }}>
        <h3 className="panel-title">AI 生成变更建议（后台任务）</h3>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <select
            value={aiChapterId}
            onChange={(e) => setAiChapterId(e.target.value)}
            style={{ minWidth: 240 }}
            disabled={aiBusy}
          >
            {chapters.length === 0 && <option value="">暂无章节</option>}
            {chapters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title || c.id.slice(0, 8)}
              </option>
            ))}
          </select>
          <button
            className="secondary"
            disabled={aiBusy || !aiChapterId}
            onClick={() => void startAIGenerate()}
          >
            {aiBusy ? "创建中…" : "发起 AI 生成"}
          </button>
          {aiTaskId && (
            <button
              className="secondary"
              onClick={() => navigate(`/projects/${projectId}/tasks`)}
            >
              查看任务中心
            </button>
          )}
          {aiTask && (
            <span style={{ fontSize: 13, color: "#4e5969" }}>
              任务状态：{aiTask.status}
              {aiTask.status === "running" && <span className="spin" style={{ marginLeft: 6 }} />}
              {aiTask.status === "failed" || aiTask.status === "timeout" ? (
                <span style={{ color: "#e5484d" }}>（{aiTask.error_message || "执行失败"}）</span>
              ) : null}
            </span>
          )}
        </div>
        {aiError && <div className="error-box" style={{ marginTop: 10 }}>{aiError}</div>}
        {aiNotice && <div className="hint-box" style={{ marginTop: 10 }}>{aiNotice}</div>}
        {aiSuggestions.length > 0 && (
          <div style={{ marginTop: 12, borderTop: "1px dashed #e5e6eb", paddingTop: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>
              生成 {aiSuggestions.length} 条建议
            </div>
            <div className="list">
              {aiSuggestions.map((s, idx) => (
                <div key={idx} className="list-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="tag">
                      {s.severity === "critical" ? "严重" : s.severity === "warning" ? "警告" : "建议"}
                    </span>
                    <span style={{ fontWeight: 600 }}>建议 {idx + 1}</span>
                    <button
                      className="secondary"
                      style={{ marginLeft: "auto" }}
                      disabled={adopting === String(idx) || !aiChapterId}
                      onClick={() => void adoptSuggestion(idx)}
                    >
                      {adopting === String(idx) ? "采纳中…" : "采纳为待确认建议"}
                    </button>
                  </div>
                  {s.original && (
                    <div style={{ marginTop: 6 }}>
                      <span style={{ color: "#e5484d", fontSize: 13 }}>原文：</span>
                      <span style={{ fontSize: 13 }}>{s.original}</span>
                    </div>
                  )}
                  {s.suggested && (
                    <div style={{ marginTop: 4 }}>
                      <span style={{ color: "#389e0d", fontSize: 13 }}>改为：</span>
                      <span style={{ fontSize: 13 }}>{s.suggested}</span>
                    </div>
                  )}
                  {s.reason && (
                    <div style={{ marginTop: 4, fontSize: 12.5, color: "var(--text-dim)" }}>
                      理由：{s.reason}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {settings && (
        <div className="card" style={{ marginBottom: 18 }}>
          <h3 className="panel-title">确认策略</h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 10 }}>
            <div className="field">
              <label>人物状态变更</label>
              <select
                value={settings.character_confirm}
                onChange={(e) =>
                  void saveSetting(
                    "character_confirm",
                    e.target.value as "require" | "auto"
                  )
                }
              >
                <option value="require">需确认</option>
                <option value="auto">自动确认</option>
              </select>
            </div>
            <div className="field">
              <label>伏笔变更</label>
              <select
                value={settings.foreshadow_confirm}
                onChange={(e) =>
                  void saveSetting(
                    "foreshadow_confirm",
                    e.target.value as "require" | "auto"
                  )
                }
              >
                <option value="require">需确认</option>
                <option value="auto">自动确认</option>
              </select>
            </div>
            <div className="field">
              <label>时间线变更</label>
              <select
                value={settings.timeline_confirm}
                onChange={(e) =>
                  void saveSetting(
                    "timeline_confirm",
                    e.target.value as "require" | "auto"
                  )
                }
              >
                <option value="require">需确认</option>
                <option value="auto">自动确认</option>
              </select>
            </div>
            <div className="field" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label>生成后自动一致性检查</label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14 }}>
                <input
                  type="checkbox"
                  checked={settings.auto_check !== false}
                  onChange={(e) => void saveSetting("auto_check", e.target.checked)}
                />
                {settings.auto_check !== false ? "已开启：保存正文后自动执行检查" : "已关闭：仅手动检查"}
              </label>
            </div>
          </div>
          <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 6 }}>
            自动确认 = 提取后直接写入正式库并标记 ai_extracted，不进入待确认列表；需确认 = 生成 proposed
            建议，确认后才写入正式库。
          </div>
        </div>
      )}

      <div className="card">
        <h3 className="panel-title">
          待确认建议（{suggestions.length}）
          {!showPending && suggestions.length === 0 && (
            <span className="tag" style={{ marginLeft: 8 }}>
              全部策略为自动确认，无待确认项
            </span>
          )}
        </h3>
        <div className="list">
          {suggestions.length === 0 ? (
            <div className="empty">
              暂无待确认建议。保存章节正文后，系统会自动提取人物状态/关系/时间线变更建议。
            </div>
          ) : (
            suggestions.map((sug) => (
              <div key={sug.id} className="list-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ flex: 1, fontWeight: 600 }}>
                    {sug.title}
                    <span className="tag" style={{ marginLeft: 8 }}>
                      {CATEGORY_LABEL[sug.category] || sug.category}
                    </span>
                    <span className="tag" style={{ marginLeft: 4 }}>
                      来源章节：{chapterName(sug.chapter_id)}
                    </span>
                  </div>
                  <button
                    className="secondary"
                    disabled={busy === sug.id}
                    onClick={() => {
                      setModifiedId(sug.id);
                      setModifiedPayload(
                        typeof sug.payload === "object" && sug.payload
                          ? { ...sug.payload }
                          : {}
                      );
                    }}
                  >
                    修改确认
                  </button>
                  <button disabled={busy === sug.id} onClick={() => void act(sug, "confirm")}>
                    确认
                  </button>
                  <button
                    className="danger-btn"
                    disabled={busy === sug.id}
                    onClick={() => void act(sug, "ignore")}
                  >
                    忽略
                  </button>
                </div>
                {sug.description && (
                  <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 4 }}>
                    {sug.description}
                  </div>
                )}
                <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
                  数据：{JSON.stringify(sug.payload)}
                </div>
                {modifiedId === sug.id && (
                  <div style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                      修改确认（将覆盖建议数据后写入正式库）
                    </div>
                    <textarea
                      style={{ width: "100%", minHeight: 80, fontFamily: "monospace" }}
                      value={JSON.stringify(modifiedPayload, null, 2)}
                      onChange={(e) => {
                        try {
                          setModifiedPayload(JSON.parse(e.target.value));
                        } catch {
                          /* 输入未完成时忽略 */
                        }
                      }}
                    />
                    <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                      <button onClick={() => void act(sug, "modified")} disabled={busy === sug.id}>
                        按修改内容确认
                      </button>
                      <button
                        className="secondary"
                        onClick={() => {
                          setModifiedId("");
                          setModifiedPayload({});
                        }}
                      >
                        取消
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
