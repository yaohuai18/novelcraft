// P1 第二批「协作权限」：组织成员 / 角色 / 邀请 / 移除
// 数据源：GET /api/enterprise/organizations/{orgId}/members | /invitations
// 操作：邀请成员、撤销邀请、变更角色、移除成员；owner/admin 可见管理操作。
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type InvitationItem,
  type MemberItem,
} from "../api/client";

const ROLE_OPTIONS = [
  { key: "owner", label: "owner（所有者）" },
  { key: "admin", label: "admin（管理员）" },
  { key: "editor", label: "editor（编辑）" },
  { key: "reviewer", label: "reviewer（审核）" },
  { key: "viewer", label: "viewer（只读）" },
];

const ROLE_COLOR: Record<string, string> = {
  owner: "#d4380d",
  admin: "#722ed1",
  editor: "#1d39c4",
  reviewer: "#08979c",
  viewer: "#8c8c8c",
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function CollaborationPage() {
  const [orgs, setOrgs] = useState<{ id: string; name: string; role: string }[]>([]);
  const [orgId, setOrgId] = useState("");
  const [orgLoading, setOrgLoading] = useState(true);
  const [orgError, setOrgError] = useState("");

  const [members, setMembers] = useState<MemberItem[]>([]);
  const [invitations, setInvitations] = useState<InvitationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [showCreate, setShowCreate] = useState(false);
  const [newOrgName, setNewOrgName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);

  const [inviteEmails, setInviteEmails] = useState("");
  const [inviteRole, setInviteRole] = useState("editor");
  const [inviteBusy, setInviteBusy] = useState(false);

  const [busyKey, setBusyKey] = useState("");

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
    Promise.all([api.listMembers(orgId), api.listInvitations(orgId, "")])
      .then(([m, i]) => {
        setMembers(m.members || []);
        setInvitations(i.invitations || []);
        setNotice("");
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [orgId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const myRole = useMemo(() => {
    const o = orgs.find((x) => x.id === orgId);
    return o ? o.role : "";
  }, [orgs, orgId]);

  const canManage = myRole === "owner" || myRole === "admin";

  async function handleCreateOrg() {
    if (!newOrgName.trim()) {
      setError("组织名称不能为空");
      return;
    }
    setCreateBusy(true);
    setError("");
    try {
      const r = await api.createOrganization({ name: newOrgName.trim() });
      setNotice(`组织「${r.name}」创建成功`);
      setShowCreate(false);
      setNewOrgName("");
      const res = await api.listOrganizations();
      setOrgs(res.organizations || []);
      setOrgId(r.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreateBusy(false);
    }
  }

  async function handleInvite() {
    const emails = inviteEmails
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (emails.length === 0) {
      setError("请至少填写一个邮箱");
      return;
    }
    setInviteBusy(true);
    setError("");
    try {
      const r = await api.inviteMembers(orgId, { emails, role: inviteRole });
      setNotice(`已发出 ${r.created.length} 个邀请`);
      setInviteEmails("");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "邀请失败");
    } finally {
      setInviteBusy(false);
    }
  }

  async function handleRevoke(invId: string) {
    setBusyKey(`revoke-${invId}`);
    setError("");
    try {
      await api.revokeInvitation(orgId, invId);
      setNotice("邀请已撤销");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "撤销失败");
    } finally {
      setBusyKey("");
    }
  }

  async function handleRoleChange(member: MemberItem, role: string) {
    setBusyKey(`role-${member.id}`);
    setError("");
    try {
      await api.updateMemberRole(orgId, member.id, role);
      setNotice(`${member.username || member.user_id} 角色已更新为 ${role}`);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "角色更新失败");
    } finally {
      setBusyKey("");
    }
  }

  async function handleOffboard(member: MemberItem) {
    if (!window.confirm(`确定将成员 ${member.username || member.email || member.user_id} 移出组织？`)) return;
    setBusyKey(`off-${member.id}`);
    setError("");
    try {
      await api.offboardMember(orgId, member.user_id);
      setNotice("成员已移除");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "移除失败");
    } finally {
      setBusyKey("");
    }
  }

  const style: Record<string, any> = {
    wrap: { padding: 24, maxWidth: 1180, margin: "0 auto" },
    row: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 16 },
    card: { background: "#fff", border: "1px solid #e8e8e8", borderRadius: 10, padding: 18, marginBottom: 12 },
    btn: { padding: "6px 14px", borderRadius: 6, border: "1px solid #d9d9d9", background: "#fff", cursor: "pointer", fontSize: 13 },
    btnPrimary: { padding: "6px 14px", borderRadius: 6, border: "none", background: "#1d39c4", color: "#fff", cursor: "pointer", fontSize: 13 },
    btnDanger: { padding: "6px 14px", borderRadius: 6, border: "1px solid #e5484d", background: "#fff", color: "#e5484d", cursor: "pointer", fontSize: 13 },
    input: { padding: "6px 10px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13, minWidth: 220 },
    select: { padding: "6px 8px", borderRadius: 6, border: "1px solid #d9d9d9", fontSize: 13 },
    table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
    th: { textAlign: "left", padding: "8px 10px", borderBottom: "2px solid #e8e8e8", color: "#555" },
    td: { padding: "8px 10px", borderBottom: "1px solid #f0f0f0", verticalAlign: "top" },
    badge: (color: string) => ({ display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 12, background: `${color}1a`, color }),
    empty: { color: "#999", padding: "30px 0", textAlign: "center" as const },
    err: { color: "#e5484d", background: "#fff1f0", border: "1px solid #ffccc7", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
    ok: { color: "#389e0d", background: "#f6ffed", border: "1px solid #b7eb8f", padding: "8px 12px", borderRadius: 6, marginBottom: 12 },
  };

  if (orgLoading) {
    return <div style={style.wrap}><div style={style.empty}>正在加载组织…</div></div>;
  }

  return (
    <div style={style.wrap}>
      <h2 style={{ marginTop: 0 }}>协作权限</h2>
      <div style={style.row}>
        <label style={{ fontSize: 13 }}>组织：</label>
        <select style={style.select} value={orgId} onChange={(e) => setOrgId(e.target.value)}>
          {orgs.length === 0 && <option value="">暂无组织</option>}
          {orgs.map((o) => (
            <option key={o.id} value={o.id}>{o.name}（{o.role}）</option>
          ))}
        </select>
        <button style={style.btn} onClick={refresh} disabled={loading || !orgId}>刷新</button>
        <button style={style.btn} onClick={() => setShowCreate((v) => !v)}>创建组织</button>
      </div>

      {showCreate && (
        <div style={style.card}>
          <div style={style.row}>
            <input
              style={style.input}
              placeholder="组织名称"
              value={newOrgName}
              onChange={(e) => setNewOrgName(e.target.value)}
            />
            <button style={style.btnPrimary} onClick={handleCreateOrg} disabled={createBusy}>
              {createBusy ? "创建中…" : "创建"}
            </button>
            <button style={style.btn} onClick={() => setShowCreate(false)}>取消</button>
          </div>
        </div>
      )}

      {orgError && <div style={style.err}>{orgError}</div>}
      {error && <div style={style.err}>{error}</div>}
      {notice && <div style={style.ok}>{notice}</div>}

      {orgs.length === 0 ? (
        <div style={style.card}>
          <div style={style.empty}>
            当前账号尚未加入任何组织。点击「创建组织」后，您将自动成为 owner，可邀请成员并分配角色。
          </div>
        </div>
      ) : (
        <>
          <div style={style.card}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <strong>成员列表</strong>
              <span style={{ color: "#999", fontSize: 12 }}>
                您的角色：<span style={{ ...style.badge(ROLE_COLOR[myRole] || "#8c8c8c") }}>{myRole}</span>
              </span>
            </div>
            {loading ? (
              <div style={style.empty}>加载中…</div>
            ) : members.length === 0 ? (
              <div style={style.empty}>暂无成员</div>
            ) : (
              <table style={style.table}>
                <thead>
                  <tr>
                    <th style={style.th}>用户名</th>
                    <th style={style.th}>邮箱</th>
                    <th style={style.th}>角色</th>
                    <th style={style.th}>加入时间</th>
                    {canManage && <th style={style.th}>操作</th>}
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.id}>
                      <td style={style.td}>{m.username || "—"}</td>
                      <td style={style.td}>{m.email || "—"}</td>
                      <td style={style.td}>
                        {canManage && m.role !== "owner" ? (
                          <select
                            style={style.select}
                            value={m.role}
                            disabled={busyKey === `role-${m.id}`}
                            onChange={(e) => handleRoleChange(m, e.target.value)}
                          >
                            {ROLE_OPTIONS.filter((r) => r.key !== "owner").map((r) => (
                              <option key={r.key} value={r.key}>{r.label}</option>
                            ))}
                          </select>
                        ) : (
                          <span style={style.badge(ROLE_COLOR[m.role] || "#8c8c8c")}>{m.role}</span>
                        )}
                      </td>
                      <td style={style.td}>{fmtTime(m.joined_at)}</td>
                      {canManage && (
                        <td style={style.td}>
                          {m.role !== "owner" && (
                            <button
                              style={style.btnDanger}
                              disabled={busyKey === `off-${m.id}`}
                              onClick={() => handleOffboard(m)}
                            >
                              移除
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {canManage && (
            <div style={style.card}>
              <strong>邀请成员</strong>
              <div style={style.row}>
                <input
                  style={style.input}
                  placeholder="邮箱（多个用逗号分隔）"
                  value={inviteEmails}
                  onChange={(e) => setInviteEmails(e.target.value)}
                />
                <select style={style.select} value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
                  {ROLE_OPTIONS.filter((r) => r.key !== "owner").map((r) => (
                    <option key={r.key} value={r.key}>{r.label}</option>
                  ))}
                </select>
                <button style={style.btnPrimary} onClick={handleInvite} disabled={inviteBusy}>
                  {inviteBusy ? "发送中…" : "发送邀请"}
                </button>
              </div>
            </div>
          )}

          {canManage && (
            <div style={style.card}>
              <strong>待处理邀请</strong>
              {invitations.filter((i) => i.status === "pending").length === 0 ? (
                <div style={style.empty}>暂无待处理邀请</div>
              ) : (
                <table style={style.table}>
                  <thead>
                    <tr>
                      <th style={style.th}>邮箱</th>
                      <th style={style.th}>角色</th>
                      <th style={style.th}>过期时间</th>
                      <th style={style.th}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invitations
                      .filter((i) => i.status === "pending")
                      .map((i) => (
                        <tr key={i.id}>
                          <td style={style.td}>{i.email}</td>
                          <td style={style.td}><span style={style.badge(ROLE_COLOR[i.role] || "#8c8c8c")}>{i.role}</span></td>
                          <td style={style.td}>{fmtTime(i.expires_at)}</td>
                          <td style={style.td}>
                            <button
                              style={style.btnDanger}
                              disabled={busyKey === `revoke-${i.id}`}
                              onClick={() => handleRevoke(i.id)}
                            >
                              撤销
                            </button>
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          <div style={{ ...style.card, color: "#555", fontSize: 12 }}>
            <strong style={{ color: "#333" }}>角色权限说明</strong>
            <ul style={{ margin: "8px 0 0", paddingLeft: 18, lineHeight: 1.9 }}>
              <li>owner：组织所有者，全部管理权限（成员管理、审计查看、删除组织等）。</li>
              <li>admin：管理员，可管理成员/邀请、查看审计日志，不可转让所有权。</li>
              <li>editor：编辑，可创作与修改作品内容。</li>
              <li>reviewer：审核，可对提交内容进行审核（通过/驳回）。</li>
              <li>viewer：只读，仅可查看组织内可见内容。</li>
            </ul>
          </div>
        </>
      )}
    </div>
  );
}
