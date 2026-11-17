// P1 第二批「审核中心」：统一内容审核入口
// 数据源：GET /api/enterprise/organizations/{orgId}/review-tasks
// 操作：提交审核 / 通过 / 驳回（要求修改）/ 重新提交 / 撤回；全程对接 content_reviews 状态流转。
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ReviewTaskItem } from "../api/client";

const STATUS_OPTIONS = [
  { key: "", label: "全部状态" },
  { key: "reviewing", label: "审核中" },
  { key: "needs_changes", label: "要求修改" },
  { key: "re_reviewing", label: "复审中" },
  { key: "approved", label: "已通过" },
  { key: "rejected", label: "已驳回" },
  { key: "withdrawn", label: "已撤回" },
];

const STATUS_LABEL: Record<string, string> = {
  unsubmitted: "未提交",
  reviewing: "审核中",
  needs_changes: "要求修改",
  re_reviewing: "复审中",
  approved: "已通过",
  rejected: "已驳回",
  withdrawn: "已撤回",
};

const STATUS_COLOR: Record<string, string> = {
  unsubmitted: "#86909c",
  reviewing: "#1d39c4",
  needs_changes: "#f5a623",
  re_reviewing: "#722ed1",
  approved: "#389e0d",
  rejected: "#e5484d",
  withdrawn: "#86909c",
};

const RESOURCE_LABEL: Record<string, string> = {
  chapter: "章节",
  comment: "评论",
  outline: "大纲",
  project: "发布内容",
  publish: "发布内容",
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function ReviewCenterPage() {
  const [orgs, setOrgs] = useState<{ id: string; name: string; role: string }[]>([]);
  const [orgId, setOrgId] = useState("");
  const [orgLoading, setOrgLoading] = useState(true);
  const [orgError, setOrgError] = useState("");

  const [tasks, setTasks] = useState<ReviewTaskItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const [projects, setProjects] = useState<{ id: string; title: string; status: string }[]>([]);
  const [showSubmit, setShowSubmit] = useState(false);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [submitForm, setSubmitForm] = useState({
    project_id: "",
    resource_type: "chapter",
    resource_id: "",
    change_summary: "",
  });

  // 驳回内联表单
  const [rejectFor, setRejectFor] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  // 初始化组织列表
  useEffect(() => {
    api
      .listOrganizations()
      .then((res) => {
        setOrgs(res.organizations || []);
        if (res.organizations && res.organizations.length > 0) {
          setOrgId(res.organizations[0].id);
        }
      })
      .catch((e: Error) => setOrgError(e.message))
      .finally(() => setOrgLoading(false));
  }, []);

  // 组织变化时拉取审核任务与作品列表
  useEffect(() => {
    if (!orgId) return;
    setLoading(true);
    setError("");
    api
      .listReviewTasks(orgId, { status: statusFilter || null })
      .then((res) => {
        setTasks(res.tasks || []);
        setTotal(res.total || 0);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
    api
      .listOrgProjects(orgId)
      .then((res) => {
        const list = res.projects || [];
        setProjects(list);
        if (list.length > 0 && !submitForm.project_id) {
          setSubmitForm((f) => ({ ...f, project_id: list[0].id }));
        }
      })
      .catch(() => {
        /* 组织内项目列表加载失败不影响审核任务展示 */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, statusFilter]);

  const refresh = useCallback(() => {
    if (!orgId) return;
    setLoading(true);
    setError("");
    api
      .listReviewTasks(orgId, { status: statusFilter || null })
      .then((res) => {
        setTasks(res.tasks || []);
        setTotal(res.total || 0);
        setNotice("");
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [orgId, statusFilter]);

  const myRole = useMemo(() => {
    const o = orgs.find((x) => x.id === orgId);
    return o ? o.role : "";
  }, [orgs, orgId]);

  async function handleApprove(taskId: string) {
    setActionBusy(true);
    setError("");
    setNotice("");
    try {
      const r = await api.approveReview(orgId, taskId);
      setNotice(`任务 ${taskId.slice(0, 8)}… 已通过（${r.status}）`);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleReject(taskId: string) {
    if (!rejectReason.trim()) {
      setError("驳回时必须填写审核意见（原因）");
      return;
    }
    setActionBusy(true);
    setError("");
    try {
      const r = await api.rejectReview(orgId, taskId, rejectReason.trim());
      setNotice(`任务 ${taskId.slice(0, 8)}… 已驳回：${r.status}`);
      setRejectFor(null);
      setRejectReason("");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleResubmit(task: ReviewTaskItem) {
    setActionBusy(true);
    setError("");
    try {
      await api.resubmitReview(orgId, task.id, submitForm.change_summary || `重新提交 ${task.resource_type}:${task.resource_id}`);
      setNotice(`任务 ${task.id.slice(0, 8)}… 已重新提交`);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleWithdraw(taskId: string) {
    setActionBusy(true);
    setError("");
    try {
      await api.withdrawReview(orgId, taskId);
      setNotice("已撤回审核任务");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleSubmitNew() {
    if (!submitForm.project_id || !submitForm.resource_id.trim()) {
      setError("请选择作品并填写资源 ID");
      return;
    }
    setSubmitBusy(true);
    setError("");
    try {
      const r = await api.submitReview(orgId, submitForm.project_id, {
        resource_type: submitForm.resource_type,
        resource_id: submitForm.resource_id.trim(),
        change_summary: submitForm.change_summary || null,
      });
      setNotice(`审核任务已提交（状态：${r.status}）`);
      setShowSubmit(false);
      setSubmitForm((f) => ({ ...f, resource_id: "", change_summary: "" }));
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitBusy(false);
    }
  }

  const style: Record<string, any> = {
    wrap: { padding: 24, maxWidth: 1180, margin: "0 auto" },
    row: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 16 },
    card: { background: "#fff", border: "1px solid #e8e8e8", borderRadius: 10, padding: 18, marginBottom: 12 },
    btn: { padding: "6px 14px", borderRadius: 6, border: "1px solid #d9d9d9", background: "#fff", cursor: "pointer", fontSize: 13 },
    btnPrimary: { padding: "6px 14px", borderRadius: 6, border: "none", background: "#1d39c4", color: "#fff", cursor: "pointer", fontSize: 13 },
    btnDanger: { padding: "6px 14px", borderRadius: 6, border: "1px solid #e5484d", background: "#fff", color: "#e5484d", cursor: "pointer", fontSize: 13 },
    input: { padding: "6px 10px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13, minWidth: 180 },
    select: { padding: "6px 8px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13 },
    table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
    th: { textAlign: "left", padding: "8px 10px", borderBottom: "2px solid #e8e8e8", color: "#555" },
    td: { padding: "8px 10px", borderBottom: "1px solid #f0f0f0", verticalAlign: "top" },
    badge: (color: string) => ({ display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 12, background: `${color}1a`, color }),
    empty: { color: "#999", padding: "40px 0", textAlign: "center" as const },
    err: { color: "#e5484d", background: "#fff1f0", border: "1px solid #ffccc7", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
    ok: { color: "#389e0d", background: "#f6ffed", border: "1px solid #b7eb8f", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
  };

  if (orgLoading) {
    return <div style={style.wrap}><div style={style.empty}>正在加载组织…</div></div>;
  }

  return (
    <div style={style.wrap}>
      <h2 style={{ marginTop: 0 }}>审核中心</h2>
      <div style={style.row}>
        <label style={{ fontSize: 13 }}>组织：</label>
        <select style={style.select} value={orgId} onChange={(e) => setOrgId(e.target.value)}>
          {orgs.length === 0 && <option value="">暂无组织</option>}
          {orgs.map((o) => (
            <option key={o.id} value={o.id}>{o.name}（{o.role}）</option>
          ))}
        </select>
        <select style={style.select} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_OPTIONS.map((s) => (
            <option key={s.key} value={s.key}>{s.label}</option>
          ))}
        </select>
        <button style={style.btn} onClick={refresh} disabled={loading || !orgId}>刷新</button>
        <button style={style.btnPrimary} onClick={() => setShowSubmit((v) => !v)} disabled={!orgId}>提交审核</button>
      </div>
      {orgs.length === 0 && (
        <div style={style.card}>
          <div style={style.empty}>当前账号尚未加入任何组织。请先在「协作权限」页创建组织，再回来使用审核中心。</div>
        </div>
      )}
      {orgError && <div style={style.err}>{orgError}</div>}
      {error && <div style={style.err}>{error}</div>}
      {notice && <div style={style.ok}>{notice}</div>}

      {showSubmit && (
        <div style={style.card}>
          <h4 style={{ marginTop: 0 }}>提交内容审核</h4>
          <div style={style.row}>
            <select
              style={style.select}
              value={submitForm.project_id}
              onChange={(e) => setSubmitForm((f) => ({ ...f, project_id: e.target.value }))}
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.title}</option>
              ))}
            </select>
            <select
              style={style.select}
              value={submitForm.resource_type}
              onChange={(e) => setSubmitForm((f) => ({ ...f, resource_type: e.target.value }))}
            >
              <option value="chapter">章节</option>
              <option value="comment">评论</option>
              <option value="outline">大纲</option>
              <option value="project">发布内容</option>
            </select>
            <input
              style={style.input}
              placeholder="资源 ID（章节/评论/大纲 ID）"
              value={submitForm.resource_id}
              onChange={(e) => setSubmitForm((f) => ({ ...f, resource_id: e.target.value }))}
            />
            <input
              style={{ ...style.input, minWidth: 240 }}
              placeholder="变更说明（可选）"
              value={submitForm.change_summary}
              onChange={(e) => setSubmitForm((f) => ({ ...f, change_summary: e.target.value }))}
            />
            <button style={style.btnPrimary} onClick={handleSubmitNew} disabled={submitBusy}>
              {submitBusy ? "提交中…" : "提交"}
            </button>
          </div>
        </div>
      )}

      <div style={style.card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <strong>审核任务</strong>
          <span style={{ color: "#999", fontSize: 12 }}>共 {total} 条{loading ? "（加载中…）" : ""}</span>
        </div>
        {loading ? (
          <div style={style.empty}>加载中…</div>
        ) : tasks.length === 0 ? (
          <div style={style.empty}>暂无审核任务{statusFilter ? "（当前筛选条件下）" : ""}，可点击「提交审核」创建。</div>
        ) : (
          <table style={style.table}>
            <thead>
              <tr>
                <th style={style.th}>作品</th>
                <th style={style.th}>内容类型</th>
                <th style={style.th}>资源 ID</th>
                <th style={style.th}>提交人</th>
                <th style={style.th}>提交时间</th>
                <th style={style.th}>状态</th>
                <th style={style.th}>审核意见</th>
                <th style={style.th}>操作</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.id}>
                  <td style={style.td}>{t.project_title || t.project_id || "—"}</td>
                  <td style={style.td}>{RESOURCE_LABEL[t.resource_type] || t.resource_type}</td>
                  <td style={style.td}><code>{t.resource_id}</code></td>
                  <td style={style.td}>{t.submitter_name || t.submitter_id || "—"}</td>
                  <td style={style.td}>{fmtTime(t.submitted_at)}</td>
                  <td style={style.td}><span style={style.badge(STATUS_COLOR[t.status] || "#86909c")}>{STATUS_LABEL[t.status] || t.status}</span></td>
                  <td style={style.td}>{t.comment ? <span style={{ color: "#888" }}>{t.comment}</span> : "—"}</td>
                  <td style={style.td}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                      {(t.status === "reviewing" || t.status === "re_reviewing") && (
                        <>
                          <button
                            style={style.btnPrimary}
                            onClick={() => handleApprove(t.id)}
                            disabled={actionBusy}
                          >
                            通过
                          </button>
                          <button
                            style={style.btnDanger}
                            onClick={() => {
                              setRejectFor(t.id);
                              setRejectReason("");
                              setError("");
                            }}
                            disabled={actionBusy}
                          >
                            驳回
                          </button>
                        </>
                      )}
                      {(t.status === "needs_changes" || t.status === "unsubmitted") && (
                        <button style={style.btn} onClick={() => handleResubmit(t)} disabled={actionBusy}>
                          重新提交
                        </button>
                      )}
                      {t.status === "reviewing" && (
                        <button style={style.btn} onClick={() => handleWithdraw(t.id)} disabled={actionBusy}>
                          撤回
                        </button>
                      )}
                      {rejectFor === t.id && (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                          <input
                            style={{ ...style.input, minWidth: 200 }}
                            placeholder="驳回原因（必填）"
                            value={rejectReason}
                            onChange={(e) => setRejectReason(e.target.value)}
                          />
                          <button style={style.btnDanger} onClick={() => handleReject(t.id)} disabled={actionBusy}>
                            确认驳回
                          </button>
                          <button style={style.btn} onClick={() => setRejectFor(null)}>取消</button>
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
