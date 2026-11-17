// 文26 P4「一致性检查」：数据层静态检查面板 + P1 AI 深度检查（任务化）
// - 静态检查：全量/按维度触发，结构化问题、筛选、忽略、历史回看
// - AI 检查：创建 consistency_check 任务（统一 ai_tasks），轮询任务状态，成功后展示 AI 检查结果
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  type AITask,
  type CheckConsistencyResponse,
  type ConsistencyCheckRecord,
  type ConsistencyIssue,
  type DataConsistencyIssue,
  type DataConsistencyRunResponse,
  type OutlineNode,
} from "../api/client";

type DimKey =
  | "all"
  | "dangling_refs"
  | "character_state"
  | "relationships"
  | "foreshadows"
  | "appearance"
  | "memory";

const DIM_OPTIONS: { key: DimKey; label: string }[] = [
  { key: "all", label: "全量" },
  { key: "dangling_refs", label: "悬空引用" },
  { key: "character_state", label: "角色状态" },
  { key: "relationships", label: "关系矛盾" },
  { key: "foreshadows", label: "伏笔回收" },
  { key: "appearance", label: "出场/别名" },
  { key: "memory", label: "记忆/记录" },
];

const DIM_LABEL: Record<string, string> = {
  dangling_refs: "悬空引用",
  character_state: "角色状态",
  relationships: "关系矛盾",
  foreshadows: "伏笔回收",
  appearance: "出场/别名",
  memory: "记忆/记录",
};

const SEVERITY_LABEL: Record<string, string> = {
  error: "错误",
  warning: "警告",
  info: "提示",
};

const SEVERITY_COLOR: Record<string, string> = {
  error: "#e5484d",
  warning: "#f5a623",
  info: "#86909c",
};

// issue 维度 -> 修复跳转页面
const FIX_PAGE: Record<string, string> = {
  dangling_refs: "characters",
  character_state: "characters",
  relationships: "relationships",
  foreshadows: "foreshadows",
  appearance: "characters",
  memory: "chapters",
};

const AI_ISSUE_TYPE_LABEL: Record<string, string> = {
  character_state: "角色状态",
  relationship: "角色关系",
  timeline: "时间线",
  foreshadow: "伏笔",
  repetition: "重复表达",
  style: "文风",
  outline: "大纲覆盖",
};

function ignoreStorageKey(projectId: string) {
  return `novelcraft:consistency:ignored:${projectId}`;
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function taskStatusText(status: string): string {
  const map: Record<string, string> = {
    pending: "排队中",
    running: "执行中",
    succeeded: "完成",
    failed: "失败",
    timeout: "超时",
    cancelled: "已取消",
  };
  return map[status] || status;
}

export default function ConsistencyPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [run, setRun] = useState<DataConsistencyRunResponse | null>(null);
  const [history, setHistory] = useState<ConsistencyCheckRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [hint, setHint] = useState("");

  const [severityFilter, setSeverityFilter] = useState("all");
  const [dimFilter, setDimFilter] = useState("all");
  const [showIgnored, setShowIgnored] = useState(false);
  const [ignored, setIgnored] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(ignoreStorageKey(projectId)) || "[]");
    } catch {
      return [];
    }
  });

  // ---- P1 AI 深度检查（任务化）----
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [aiChapterId, setAiChapterId] = useState("");
  const [aiTask, setAiTask] = useState<AITask | null>(null);
  const [aiTaskId, setAiTaskId] = useState("");
  const [aiResult, setAiResult] = useState<CheckConsistencyResponse | null>(null);
  const [aiBusy, setAiBusy] = useState(false);

  const persistIgnored = useCallback(
    (ids: string[]) => {
      localStorage.setItem(ignoreStorageKey(projectId), JSON.stringify(ids));
      setIgnored(ids);
    },
    [projectId]
  );

  const runCheck = useCallback(
    async (dimension: DimKey) => {
      setBusy(true);
      setError("");
      setHint("");
      try {
        const resp = await api.runDataConsistencyCheck(projectId, dimension);
        setRun(resp);
        if (resp.summary.total === 0) {
          setHint("本次检查未发现问题");
        }
        // 同步最新历史
        const records = await api.listConsistencyChecks(projectId, undefined, 20);
        setHistory(records.filter((r) => r.action_type === "consistency"));
      } catch (err) {
        setError(err instanceof Error ? err.message : "检查失败");
      } finally {
        setBusy(false);
        setLoading(false);
      }
    },
    [projectId]
  );

  const loadHistory = useCallback(async () => {
    try {
      const records = await api.listConsistencyChecks(projectId, undefined, 20);
      const staticRecs = records.filter((r) => r.action_type === "consistency");
      setHistory(staticRecs);
      return staticRecs;
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载历史失败");
      return [];
    }
  }, [projectId]);

  useEffect(() => {
    (async () => {
      setLoading(true);
      const staticRecs = await loadHistory();
      // 章节列表：AI 深度检查选章
      try {
        const chs = await api.listOutlines(projectId, "chapter");
        setChapters(chs);
        if (chs.length > 0) setAiChapterId(chs[0].id);
      } catch {
        /* 章节列表加载失败不影响静态检查 */
      }
      // 打开页面默认展示最近一次静态检查结果；没有历史则自动执行一次全量检查
      if (staticRecs.length > 0) {
        const latest = staticRecs[0];
        const result = latest.result as Record<string, unknown>;
        if (result && Array.isArray(result.issues) && result.issues.length > 0) {
          setRun({
            check_id: latest.id,
            project_id: projectId,
            dimension: String(result.dimension || "all"),
            status: latest.status,
            mode: "static",
            summary: (result.summary as DataConsistencyRunResponse["summary"]) || {
              total: 0,
              errors: 0,
              warnings: 0,
              info: 0,
              by_type: {},
            },
            issues: result.issues as DataConsistencyIssue[],
            created_at: latest.created_at,
          });
          setLoading(false);
          return;
        }
      }
      await runCheck("all");
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // AI 深度检查：轮询任务状态
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
            setAiResult(JSON.parse(t.result) as CheckConsistencyResponse);
          } catch {
            setAiResult(null);
            setError("AI 检查结果解析失败");
          }
          setAiBusy(false);
          return;
        }
        if (t.status === "failed" || t.status === "timeout" || t.status === "cancelled") {
          setAiBusy(false);
          return;
        }
        timer = window.setTimeout(poll, 3000);
      } catch (err) {
        if (!stopped) {
          setError(err instanceof Error ? err.message : "查询任务失败");
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

  async function startAICheck() {
    if (!aiChapterId) {
      setError("请先选择要检查的章节");
      return;
    }
    setAiBusy(true);
    setError("");
    setHint("");
    setAiResult(null);
    setAiTask(null);
    try {
      const resp = await api.createAITask(projectId, {
        task_type: "consistency_check",
        prompt: `对章节「${chapters.find((c) => c.id === aiChapterId)?.title || aiChapterId}」执行 AI 一致性深度检查`,
        outline_id: aiChapterId,
        request_snapshot: JSON.stringify({ chapter_title: chapters.find((c) => c.id === aiChapterId)?.title || "" }),
        timeout_seconds: 300,
      });
      setAiTaskId(resp.task_id);
      if (resp.duplicated) setHint("检测到相同任务的重复提交，已返回原任务");
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建检查任务失败");
      setAiBusy(false);
    }
  }

  function viewRecord(rec: ConsistencyCheckRecord) {
    const result = rec.result as Record<string, unknown>;
    if (!result || !Array.isArray(result.issues)) {
      setHint("该历史记录没有可展示的问题明细");
      return;
    }
    setRun({
      check_id: rec.id,
      project_id: projectId,
      dimension: String(result.dimension || "all"),
      status: rec.status,
      mode: "static",
      summary: (result.summary as DataConsistencyRunResponse["summary"]) || {
        total: 0,
        errors: 0,
        warnings: 0,
        info: 0,
        by_type: {},
      },
      issues: result.issues as DataConsistencyIssue[],
      created_at: rec.created_at,
    });
    setHint(`已载入历史检查 ${rec.created_at}`);
  }

  function toggleIgnore(issueId: string) {
    if (ignored.includes(issueId)) {
      persistIgnored(ignored.filter((x) => x !== issueId));
    } else {
      persistIgnored([...ignored, issueId]);
    }
  }

  const visibleIssues = useMemo(() => {
    if (!run) return [];
    let list = run.issues;
    if (severityFilter !== "all") {
      list = list.filter((i) => i.severity === severityFilter);
    }
    if (dimFilter !== "all") {
      list = list.filter((i) => i.dimension === dimFilter);
    }
    if (!showIgnored) {
      list = list.filter((i) => !ignored.includes(i.id));
    }
    return list;
  }, [run, severityFilter, dimFilter, showIgnored, ignored]);

  function fixTarget(issue: DataConsistencyIssue) {
    const page = FIX_PAGE[issue.dimension] || "characters";
    return `/projects/${projectId}/${page}`;
  }

  const summary = run?.summary;

  return (
    <div>
      <div className="topbar">
        <h1>一致性检查</h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/tasks`)}>
            AI 任务中心
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button disabled={busy} onClick={() => runCheck("all")}>
            {busy ? "检查中…" : "全量检查"}
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}
      {hint && <div className="hint-box">{hint}</div>}

      {/* ---- P1 AI 深度检查（任务化）---- */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0, marginBottom: 10 }}>AI 深度检查（后台任务）</h3>
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
          <button className="secondary" disabled={aiBusy || !aiChapterId} onClick={startAICheck}>
            {aiBusy ? "创建中…" : "发起 AI 检查"}
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
              任务状态：{taskStatusText(aiTask.status)}
              {aiTask.status === "running" && <span className="spin" style={{ marginLeft: 6 }} />}
              {aiTask.status === "failed" || aiTask.status === "timeout" ? (
                <span style={{ color: "#e5484d" }}>（{aiTask.error_message || "执行失败"}）</span>
              ) : null}
            </span>
          )}
        </div>

        {aiResult && (
          <div style={{ marginTop: 12, borderTop: "1px dashed #e5e6eb", paddingTop: 12 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                flexWrap: "wrap",
                marginBottom: 10,
              }}
            >
              <span className="check-mode-tag">AI 深度检查结果</span>
              <span>
                综合评分 <b>{aiResult.score}</b>
              </span>
              {aiResult.style && (
                <span style={{ color: "#86909c", fontSize: 13 }}>
                  文风 {aiResult.style.score} · {aiResult.style.evaluation}
                </span>
              )}
              {aiResult.outline && (
                <span style={{ color: "#86909c", fontSize: 13 }}>
                  大纲覆盖 {aiResult.outline.completed.length}/{aiResult.outline.completed.length + aiResult.outline.uncovered.length}
                </span>
              )}
              <span style={{ color: "#86909c", fontSize: 12 }}>
                检查时间 {fmtTime(aiResult.checked_at)}
              </span>
            </div>

            {(aiResult.issues?.length || 0) > 0 && (
              <div className="check-section" style={{ marginBottom: 8 }}>
                <div className="check-section-title">发现的问题</div>
                {aiResult.issues.map((issue: ConsistencyIssue, idx: number) => (
                  <div key={idx} className={`check-issue severity-${issue.severity || "info"}`}>
                    <div className="check-issue-head">
                      <span
                        className="severity-badge"
                        style={{ background: SEVERITY_COLOR[issue.severity] || "#86909c" }}
                      >
                        {SEVERITY_LABEL[issue.severity] || issue.severity}
                      </span>
                      <span className="tag">{AI_ISSUE_TYPE_LABEL[issue.type] || issue.type}</span>
                      <span className="issue-location">{issue.location}</span>
                    </div>
                    <div className="issue-message">{issue.message}</div>
                    {issue.suggestion && (
                      <div className="issue-suggestion">建议：{issue.suggestion}</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {(aiResult.proposed_updates?.length || 0) > 0 && (
              <div className="check-section" style={{ marginBottom: 8 }}>
                <div className="check-section-title">建议变更</div>
                {aiResult.proposed_updates.map((u, idx) => (
                  <div key={idx} className="check-proposal">
                    <div className="check-proposal-title">
                      {AI_ISSUE_TYPE_LABEL[u.category] || u.category} · {u.title}
                    </div>
                    <div style={{ fontSize: 12.5 }}>{u.description}</div>
                    <button
                      className="secondary"
                      onClick={() =>
                        navigate(`/projects/${projectId}/suggestions`)
                      }
                    >
                      前往确认
                    </button>
                  </div>
                ))}
              </div>
            )}

            {(aiResult.reminders?.length || 0) > 0 && (
              <div className="check-section">
                <div className="check-section-title">伏笔提醒</div>
                {aiResult.reminders.map((r, idx) => (
                  <div key={idx} className="check-reminder">
                    <span className="tag">{r.type}</span>
                    <span>{r.content}</span>
                    <span style={{ color: "#86909c", fontSize: 12 }}>{r.message}</span>
                  </div>
                ))}
              </div>
            )}

            {aiResult.confirmed_updates?.length ? (
              <div className="check-section" style={{ marginTop: 8 }}>
                <div className="check-section-title">已确认变更</div>
                {aiResult.confirmed_updates.map((u, idx) => (
                  <div key={idx} style={{ fontSize: 13, marginBottom: 4 }}>
                    {u.title}：{u.description}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {DIM_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              className={run?.dimension === opt.key ? "primary" : "secondary"}
              disabled={busy}
              onClick={() => runCheck(opt.key)}
            >
              {opt.label}
            </button>
          ))}
        </div>
        {summary && (
          <div style={{ display: "flex", gap: 18, marginTop: 12, flexWrap: "wrap" }}>
            <span>
              共 <b>{summary.total}</b> 项
            </span>
            <span style={{ color: SEVERITY_COLOR.error }}>
              错误 <b>{summary.errors}</b>
            </span>
            <span style={{ color: SEVERITY_COLOR.warning }}>
              警告 <b>{summary.warnings}</b>
            </span>
            <span style={{ color: SEVERITY_COLOR.info }}>
              提示 <b>{summary.info}</b>
            </span>
            {run && (
              <span style={{ color: "#86909c" }}>
                检查维度：{run.dimension === "all" ? "全量" : DIM_LABEL[run.dimension] || run.dimension}
              </span>
            )}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label>
            严重级别{" "}
            <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
              <option value="all">全部</option>
              <option value="error">错误</option>
              <option value="warning">警告</option>
              <option value="info">提示</option>
            </select>
          </label>
          <label>
            维度{" "}
            <select value={dimFilter} onChange={(e) => setDimFilter(e.target.value)}>
              <option value="all">全部</option>
              {DIM_OPTIONS.filter((x) => x.key !== "all").map((x) => (
                <option key={x.key} value={x.key}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <input
              type="checkbox"
              checked={showIgnored}
              onChange={(e) => setShowIgnored(e.target.checked)}
            />
            显示已忽略
          </label>
        </div>
      </div>

      {loading && <div className="card">正在加载检查结果…</div>}

      {!loading && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3 style={{ marginTop: 0 }}>
            问题列表{run ? `（${visibleIssues.length}/${run.issues.length}）` : ""}
          </h3>
          {visibleIssues.length === 0 ? (
            <p className="empty-tip">
              {run && run.issues.length > 0
                ? "当前筛选条件下没有可见问题（可能已被忽略）"
                : "暂无问题，运行检查后可在此查看结果"}
            </p>
          ) : (
            visibleIssues.map((issue) => {
              const isIgnored = ignored.includes(issue.id);
              return (
                <div
                  key={issue.id}
                  className="consistency-issue"
                  style={{ opacity: isIgnored ? 0.55 : 1 }}
                >
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span
                      className="tag"
                      style={{ color: "#fff", background: SEVERITY_COLOR[issue.severity] || "#86909c" }}
                    >
                      {SEVERITY_LABEL[issue.severity] || issue.severity}
                    </span>
                    <span className="tag">{DIM_LABEL[issue.dimension] || issue.dimension}</span>
                    <b>{issue.location}</b>
                    {isIgnored && <span className="tag">已忽略</span>}
                  </div>
                  <p style={{ margin: "6px 0 2px" }}>{issue.message}</p>
                  {issue.suggestion && (
                    <p style={{ margin: 0, color: "#4e5969", fontSize: 13 }}>
                      建议：{issue.suggestion}
                    </p>
                  )}
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button
                      className="secondary"
                      onClick={() => navigate(fixTarget(issue))}
                      title={issue.related_ids.join(", ")}
                    >
                      去修复
                    </button>
                    <button className="secondary" onClick={() => toggleIgnore(issue.id)}>
                      {isIgnored ? "取消忽略" : "忽略"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>检查历史</h3>
        {history.length === 0 ? (
          <p className="empty-tip">暂无静态检查历史</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>时间</th>
                <th style={{ textAlign: "left" }}>维度</th>
                <th style={{ textAlign: "left" }}>状态</th>
                <th style={{ textAlign: "left" }}>汇总</th>
                <th style={{ textAlign: "left" }}></th>
              </tr>
            </thead>
            <tbody>
              {history.map((rec) => {
                const result = rec.result as Record<string, unknown>;
                const s = (result?.summary as DataConsistencyRunResponse["summary"]) || undefined;
                return (
                  <tr key={rec.id} style={{ borderTop: "1px solid #eee" }}>
                    <td style={{ padding: "6px 8px", fontSize: 13 }}>{rec.created_at}</td>
                    <td style={{ padding: "6px 8px", fontSize: 13 }}>
                      {DIM_LABEL[String(result?.dimension || "all")] || "全量"}
                    </td>
                    <td style={{ padding: "6px 8px", fontSize: 13 }}>{rec.status}</td>
                    <td style={{ padding: "6px 8px", fontSize: 13 }}>
                      {s ? `共 ${s.total}（错 ${s.errors} / 警 ${s.warnings} / 提 ${s.info}）` : "—"}
                    </td>
                    <td style={{ padding: "6px 8px", fontSize: 13 }}>
                      <button className="secondary" onClick={() => viewRecord(rec)}>
                        查看
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
