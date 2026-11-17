// P1 第二批「操作审计」：audit_logs 只读展示
// 数据源：GET /api/enterprise/organizations/{orgId}/audit-logs
// 支持按操作类型 / 操作者 / 时间范围筛选 + 分页；只读，无任何写入。
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type AuditLogItem } from "../api/client";

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const PAGE_SIZE = 20;

export default function AuditLogPage() {
  const [orgs, setOrgs] = useState<{ id: string; name: string; role: string }[]>([]);
  const [orgId, setOrgId] = useState("");
  const [orgLoading, setOrgLoading] = useState(true);
  const [orgError, setOrgError] = useState("");

  const [logs, setLogs] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [filterAction, setFilterAction] = useState("");
  const [filterActor, setFilterActor] = useState("");
  const [filterFrom, setFilterFrom] = useState("");
  const [filterTo, setFilterTo] = useState("");

  useEffect(() => {
    api
      .listOrganizations()
      .then((res) => {
        setOrgs(res.organizations || []);
        if (res.organizations && res.organizations.length > 0) setOrgId(res.organizations[0].id);
      })
      .catch((e: Error) => setOrgError(e.message))
      .finally(() => setOrgLoading(false));
  }, []);

  const refresh = useCallback(() => {
    if (!orgId) return;
    setLoading(true);
    setError("");
    api
      .listAuditLogs(orgId, {
        page,
        page_size: PAGE_SIZE,
        action: filterAction || null,
        actor_id: filterActor || null,
        from_dt: filterFrom ? new Date(filterFrom).toISOString() : null,
        to_dt: filterTo ? new Date(filterTo).toISOString() : null,
      })
      .then((res) => {
        setLogs(res.logs || []);
        setTotal(res.total || 0);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [orgId, page, filterAction, filterActor, filterFrom, filterTo]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / PAGE_SIZE)), [total]);

  const style: Record<string, React.CSSProperties> = {
    wrap: { padding: 24, maxWidth: 1240, margin: "0 auto" },
    row: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 16 },
    card: { background: "#fff", border: "1px solid #e8e8e8", borderRadius: 10, padding: 18, marginBottom: 12 },
    btn: { padding: "6px 14px", borderRadius: 6, border: "1px solid #d9d9d9", background: "#fff", cursor: "pointer", fontSize: 13 },
    btnPrimary: { padding: "6px 14px", borderRadius: 6, border: "none", background: "#1d39c4", color: "#fff", cursor: "pointer", fontSize: 13 },
    input: { padding: "6px 10px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13, minWidth: 160 },
    select: { padding: "6px 8px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13 },
    table: { width: "100%", borderCollapse: "collapse", fontSize: 12.5 },
    th: { textAlign: "left", padding: "8px 10px", borderBottom: "2px solid #e8e8e8", color: "#555", whiteSpace: "nowrap" },
    td: { padding: "8px 10px", borderBottom: "1px solid #f0f0f0", verticalAlign: "top" },
    tag: { display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 12, background: "#f0f5ff", color: "#1d39c4" },
    empty: { color: "#999", padding: "40px 0", textAlign: "center" as const },
    err: { color: "#e5484d", background: "#fff1f0", border: "1px solid #ffccc7", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
    ok: { color: "#389e0d", background: "#f6ffed", border: "1px solid #b7eb8f", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
  };

  if (orgLoading) {
    return <div style={style.wrap}><div style={style.empty}>正在加载组织…</div></div>;
  }

  return (
    <div style={style.wrap}>
      <h2 style={{ marginTop: 0 }}>操作审计</h2>
      <div style={style.row}>
        <label style={{ fontSize: 13 }}>组织：</label>
        <select style={style.select} value={orgId} onChange={(e) => { setOrgId(e.target.value); setPage(1); }}>
          {orgs.length === 0 && <option value="">暂无组织</option>}
          {orgs.map((o) => (
            <option key={o.id} value={o.id}>{o.name}（{o.role}）</option>
          ))}
        </select>
        <button style={style.btn} onClick={() => { setPage(1); refresh(); }} disabled={loading || !orgId}>查询</button>
        <button style={style.btn} onClick={() => { setFilterAction(""); setFilterActor(""); setFilterFrom(""); setFilterTo(""); setPage(1); }}>清空筛选</button>
      </div>
      <div style={style.card}>
        <div style={style.row}>
          <input
            style={style.input}
            placeholder="操作类型，如 update_member_role"
            value={filterAction}
            onChange={(e) => setFilterAction(e.target.value)}
          />
          <input
            style={style.input}
            placeholder="操作者用户 ID"
            value={filterActor}
            onChange={(e) => setFilterActor(e.target.value)}
          />
          <label style={{ fontSize: 12, color: "#888" }}>从：</label>
          <input
            style={{ ...style.input, minWidth: 150 }}
            type="datetime-local"
            value={filterFrom}
            onChange={(e) => setFilterFrom(e.target.value)}
          />
          <label style={{ fontSize: 12, color: "#888" }}>至：</label>
          <input
            style={{ ...style.input, minWidth: 150 }}
            type="datetime-local"
            value={filterTo}
            onChange={(e) => setFilterTo(e.target.value)}
          />
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <strong>审计日志</strong>
          <span style={{ color: "#999", fontSize: 12 }}>共 {total} 条{loading ? "（加载中…）" : ""} · 只读展示</span>
        </div>
        {orgError && <div style={style.err}>{orgError}</div>}
        {error && <div style={style.err}>{error}</div>}
        {notice && <div style={style.ok}>{notice}</div>}
        {loading ? (
          <div style={style.empty}>加载中…</div>
        ) : logs.length === 0 ? (
          <div style={style.empty}>
            {orgs.length === 0 ? "当前账号尚未加入任何组织，请在「协作权限」页创建组织后查看审计日志。" : "暂无符合条件的审计日志"}
          </div>
        ) : (
          <table style={style.table}>
            <thead>
              <tr>
                <th style={style.th}>时间</th>
                <th style={style.th}>操作者</th>
                <th style={style.th}>操作类型</th>
                <th style={style.th}>目标</th>
                <th style={style.th}>变更前</th>
                <th style={style.th}>变更后</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td style={style.td}>{fmtTime(l.created_at)}</td>
                  <td style={style.td}>
                    {l.actor_username || l.actor_email || l.actor_id || "系统"}
                  </td>
                  <td style={style.td}><span style={style.tag}>{l.action}</span></td>
                  <td style={style.td}>
                    {l.target_type ? `${l.target_type}${l.target_id ? `:${l.target_id.slice(0, 8)}` : ""}` : "—"}
                  </td>
                  <td style={style.td}><code style={{ fontSize: 12 }}>{l.before_value || "—"}</code></td>
                  <td style={style.td}><code style={{ fontSize: 12 }}>{l.after_value || "—"}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {total > 0 && (
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 10, marginTop: 12 }}>
            <button style={style.btn} disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>上一页</button>
            <span style={{ fontSize: 13, color: "#555" }}>{page} / {totalPages}</span>
            <button style={style.btn} disabled={page >= totalPages || loading} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>下一页</button>
          </div>
        )}
      </div>
    </div>
  );
}
