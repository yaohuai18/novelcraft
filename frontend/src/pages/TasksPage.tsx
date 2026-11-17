// P1「任务中心」：统一查看 / 筛选 / 取消 / 重试所有 AI 任务
// 数据源：GET /api/projects/{project_id}/ai/tasks
// 进行中任务每 3s 自动轮询；支持按状态筛选、分页、详情展开（含结果 JSON）。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import {
  api,
  AI_TASK_TYPE_LABEL,
  type AITask,
  type Project,
} from "../api/client";

const STATUS_OPTIONS: { key: string; label: string }[] = [
  { key: "", label: "全部状态" },
  { key: "pending", label: "排队中" },
  { key: "running", label: "执行中" },
  { key: "succeeded", label: "成功" },
  { key: "failed", label: "失败" },
  { key: "timeout", label: "超时" },
  { key: "cancelled", label: "已取消" },
];

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  running: "执行中",
  succeeded: "成功",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "#f5a623",
  running: "#1d39c4",
  succeeded: "#389e0d",
  failed: "#e5484d",
  timeout: "#e8792b",
  cancelled: "#86909c",
};

const ACTIVE_STATUSES = new Set(["pending", "running"]);

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function prettyJSON(text?: string | null): string {
  if (!text) return "";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function resultSummary(task: AITask): string {
  if (task.status === "succeeded" && task.result) {
    const t = task.result.slice(0, 160);
    return task.result.length > 160 ? `${t}…` : t;
  }
  if (task.error_message) return task.error_message.slice(0, 160);
  return "";
}

export default function TasksPage() {
  const { projectId = "" } = useParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState(projectId);
  const [tasks, setTasks] = useState<AITask[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const timerRef = useRef<number | null>(null);

  const PAGE_SIZE = 20;

  // 加载作品列表，默认选中路由携带的 projectId
  useEffect(() => {
    api
      .listProjects()
      .then((list) => {
        setProjects(list);
        if (!activeProject && list.length > 0) setActiveProject(list[0].id);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "加载作品列表失败"));
  }, []);

  const loadTasks = useCallback(
    async (project: string, status: string, off: number, silent = false) => {
      if (!project) return;
      if (!silent) setLoading(true);
      setError("");
      try {
        const resp = await api.listAITasks(project, {
          status: status || undefined,
          limit: PAGE_SIZE,
          offset: off,
        });
        setTasks(resp.items);
        setTotal(resp.total);
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载任务失败");
      } finally {
        setLoading(false);
        setBusy(false);
      }
    },
    []
  );

  const refresh = useCallback(() => {
    loadTasks(activeProject, statusFilter, offset, true);
  }, [activeProject, statusFilter, offset, loadTasks]);

  // 主加载：作品 / 筛选 / 分页变化时触发
  useEffect(() => {
    loadTasks(activeProject, statusFilter, offset);
  }, [activeProject, statusFilter, offset, loadTasks]);

  // 进行中任务轮询（有 pending/running 时每 3s 拉取一次）
  const hasActive = useMemo(
    () => tasks.some((t) => ACTIVE_STATUSES.has(t.status)),
    [tasks]
  );

  useEffect(() => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (hasActive) {
      timerRef.current = window.setInterval(refresh, 3000);
    }
    return () => {
      if (timerRef.current) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [hasActive, refresh]);

  async function handleCancel(taskId: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.cancelAITask(activeProject, taskId);
      setNotice("任务已取消");
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleRetry(taskId: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const resp = await api.retryAITask(activeProject, taskId);
      setNotice(`已重新创建任务（${resp.status}）`);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重试失败");
    } finally {
      setBusy(false);
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageIndex = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div>
      <div className="topbar">
        <h1>AI 任务中心</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <select
            value={activeProject}
            onChange={(e) => {
              setActiveProject(e.target.value);
              setOffset(0);
              setExpanded(null);
            }}
            style={{ minWidth: 220 }}
          >
            {projects.length === 0 && <option value="">加载作品中…</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
          <button className="secondary" onClick={refresh}>
            刷新
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}
      {notice && <div className="hint-box">{notice}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              className={statusFilter === opt.key ? "" : "secondary"}
              onClick={() => {
                setStatusFilter(opt.key);
                setOffset(0);
                setExpanded(null);
              }}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <div style={{ marginTop: 8, fontSize: 13, color: "#86909c" }}>
          共 {total} 个任务{hasActive ? " · 有任务执行中，自动刷新中…" : ""}
        </div>
      </div>

      {loading ? (
        <div className="empty">加载中…</div>
      ) : tasks.length === 0 ? (
        <div className="empty">
          {activeProject ? "暂无任务。前往创作页触发续写 / 重写 / 润色 / 一致性检查等操作后，任务会出现在这里。" : "请先选择作品。"}
        </div>
      ) : (
        <div className="list">
          {tasks.map((t) => {
            const expandedFlag = expanded === t.id;
            const color = STATUS_COLOR[t.status] || "#86909c";
            const label = STATUS_LABEL[t.status] || t.status;
            return (
              <div key={t.id} className="list-item" style={{ padding: "12px 14px" }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    flexWrap: "wrap",
                    cursor: "pointer",
                  }}
                  onClick={() => setExpanded(expandedFlag ? null : t.id)}
                >
                  <strong style={{ minWidth: 96 }}>
                    {AI_TASK_TYPE_LABEL[t.task_type] || t.task_type}
                  </strong>
                  <span
                    style={{
                      display: "inline-block",
                      borderRadius: 4,
                      padding: "1px 8px",
                      fontSize: 12,
                      color: "#fff",
                      background: color,
                    }}
                  >
                    {label}
                  </span>
                  {ACTIVE_STATUSES.has(t.status) && <span className="spin" style={{ fontSize: 12 }} />}
                  {t.retry_count > 0 && (
                    <span className="tag">重试 {t.retry_count}</span>
                  )}
                  <span style={{ color: "#86909c", fontSize: 12 }}>
                    {fmtTime(t.created_at)}
                  </span>
                  <span
                    style={{
                      marginLeft: "auto",
                      color: "#86909c",
                      fontSize: 12,
                    }}
                  >
                    {expandedFlag ? "收起 ▲" : "详情 ▼"}
                  </span>
                </div>

                {resultSummary(t) && (
                  <div
                    style={{
                      marginTop: 6,
                      fontSize: 12.5,
                      color: t.status === "succeeded" ? "#389e0d" : "#e5484d",
                      lineHeight: 1.5,
                    }}
                  >
                    {resultSummary(t)}
                  </div>
                )}

                <div
                  style={{
                    display: "flex",
                    gap: 8,
                    marginTop: 8,
                    flexWrap: "wrap",
                  }}
                >
                  {ACTIVE_STATUSES.has(t.status) && (
                    <button className="secondary" disabled={busy} onClick={() => handleCancel(t.id)}>
                      取消
                    </button>
                  )}
                  {(t.status === "failed" || t.status === "timeout") && (
                    <button className="secondary" disabled={busy} onClick={() => handleRetry(t.id)}>
                      重试
                    </button>
                  )}
                  {t.status === "succeeded" && t.task_type === "consistency_check" && (
                    <button
                      className="secondary"
                      onClick={() =>
                        window.open(`/projects/${activeProject}/consistency`, "_self")
                      }
                    >
                      查看一致性结果
                    </button>
                  )}
                  {t.status === "succeeded" && t.task_type === "generate_suggestions" && (
                    <button
                      className="secondary"
                      onClick={() =>
                        window.open(`/projects/${activeProject}/suggestions`, "_self")
                      }
                    >
                      查看变更建议
                    </button>
                  )}
                </div>

                {expandedFlag && (
                  <div
                    style={{
                      marginTop: 10,
                      borderTop: "1px dashed #e5e6eb",
                      paddingTop: 10,
                      display: "flex",
                      flexDirection: "column",
                      gap: 8,
                      fontSize: 12.5,
                    }}
                  >
                    <div>
                      <strong>任务 ID：</strong>
                      <code>{t.id}</code>
                    </div>
                    <div>
                      <strong>状态：</strong>
                      {label}
                      {t.completed_at ? ` · 完成于 ${fmtTime(t.completed_at)}` : ""}
                      {t.timeout_at ? ` · 超时阈值 ${fmtTime(t.timeout_at)}` : ""}
                    </div>
                    {t.model_name && (
                      <div>
                        <strong>模型：</strong>
                        {t.model_name}
                        {t.input_tokens != null || t.output_tokens != null
                          ? ` · tokens ${t.input_tokens ?? 0} / ${t.output_tokens ?? 0}`
                          : ""}
                      </div>
                    )}
                    {t.idempotency_key && (
                      <div>
                        <strong>幂等键：</strong>
                        <code>{t.idempotency_key}</code>
                      </div>
                    )}
                    {t.request_snapshot && (
                      <div>
                        <strong>请求快照：</strong>
                        <pre
                          style={{
                            margin: 0,
                            whiteSpace: "pre-wrap",
                            wordBreak: "break-word",
                            fontSize: 12,
                            color: "#4e5969",
                            background: "#f7f8fa",
                            borderRadius: 6,
                            padding: "8px 10px",
                          }}
                        >
                          {prettyJSON(t.request_snapshot)}
                        </pre>
                      </div>
                    )}
                    <div>
                      <strong>任务内容：</strong>
                      <pre
                        style={{
                          margin: 0,
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          fontSize: 12,
                          color: "#4e5969",
                          background: "#f7f8fa",
                          borderRadius: 6,
                          padding: "8px 10px",
                        }}
                      >
                        {t.prompt}
                      </pre>
                    </div>
                    {t.result && (
                      <div>
                        <strong>结果：</strong>
                        <pre
                          style={{
                            margin: 0,
                            whiteSpace: "pre-wrap",
                            wordBreak: "break-word",
                            fontSize: 12,
                            color: "#1d2129",
                            background: "#f0f5ff",
                            borderRadius: 6,
                            padding: "8px 10px",
                          }}
                        >
                          {prettyJSON(t.result)}
                        </pre>
                      </div>
                    )}
                    {t.error_message && (
                      <div style={{ color: "#e5484d" }}>
                        <strong>错误信息：</strong>
                        {t.error_message}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {total > PAGE_SIZE && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 10,
            marginTop: 14,
          }}
        >
          <button
            className="secondary"
            disabled={offset <= 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            上一页
          </button>
          <span style={{ fontSize: 13, color: "#86909c" }}>
            第 {pageIndex} / {pageCount} 页
          </span>
          <button
            className="secondary"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
