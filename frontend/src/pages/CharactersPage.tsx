// 文26 P2「人物、关系、时间线和伏笔正式管理」：人物库管理页（人物卡 + 关系 + 状态快照）
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type CharacterData,
  type CharacterInput,
  type CharacterRelationshipData,
  type CharacterSnapshotData,
  type OutlineNode,
} from "../api/client";

const ROLE_OPTIONS = ["主角", "配角", "反派", "路人"];
const STATUS_OPTIONS = ["在世", "死亡", "失踪", "未知"];
const RELATION_OPTIONS = ["师徒", "敌对", "同盟", "亲属", "怀疑", "爱慕", "同僚", "主从", "其他"];
const REL_STATUS_OPTIONS: Array<"draft" | "confirmed" | "archived"> = ["draft", "confirmed", "archived"];
const REL_STATUS_LABEL: Record<string, string> = {
  draft: "待确认",
  confirmed: "已确认",
  archived: "已归档",
};
const SNAP_STATUS_LABEL: Record<string, string> = {
  draft: "待确认",
  confirmed: "已确认",
  rejected: "已驳回",
};
const SNAP_SOURCE_LABEL: Record<string, string> = {
  user_confirmed: "已确认",
  ai_extracted: "AI提取",
  manual: "手工",
};

const emptyForm: CharacterInput = {
  name: "",
  aliases: [],
  role: "配角",
  status: "在世",
  profile: "",
  goals: "",
  secrets: "",
  abilities: "",
  current_location: "",
  current_timeline_status: "",
  first_chapter_id: null,
  last_chapter_id: null,
};

export default function CharactersPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [chars, setChars] = useState<CharacterData[]>([]);
  const [relations, setRelations] = useState<CharacterRelationshipData[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<CharacterData | null>(null);
  const [form, setForm] = useState<CharacterInput>(emptyForm);
  const [formBusy, setFormBusy] = useState(false);
  const [aliasesText, setAliasesText] = useState("");
  // 视图切换：人物 / 关系（文26 P2 独立可访问视图）
  const [view, setView] = useState<"characters" | "relations">("characters");
  // 关系筛选
  const [relFilter, setRelFilter] = useState("");

  // 关系新建 / 编辑
  const [relOpen, setRelOpen] = useState(false);
  const [relEditing, setRelEditing] = useState<CharacterRelationshipData | null>(null);
  const [relForm, setRelForm] = useState({
    source_character_id: "",
    target_character_id: "",
    relation_type: "同盟",
    description: "",
    start_chapter_id: "",
    end_chapter_id: "",
    status: "confirmed",
  });
  const [relBusy, setRelBusy] = useState(false);

  // 快照查看
  const [snapCharId, setSnapCharId] = useState("");
  const [snapshots, setSnapshots] = useState<CharacterSnapshotData[]>([]);
  const [snapBusy, setSnapBusy] = useState(false);

  const nameMap = useMemo(() => {
    const m: Record<string, string> = {};
    chars.forEach((c) => (m[c.id] = c.name));
    return m;
  }, [chars]);
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
      const [cs, rs, chs] = await Promise.all([
        api.listCharacters(projectId),
        api.listRelationships(projectId),
        api.listOutlines(projectId, "chapter"),
      ]);
      setChars(cs);
      setRelations(rs);
      setChapters(chs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载人物库失败");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  function startEdit(c: CharacterData) {
    setEditing(c);
    setForm({
      name: c.name,
      aliases: c.aliases || [],
      role: c.role,
      status: c.status,
      profile: c.profile || "",
      goals: c.goals || "",
      secrets: c.secrets || "",
      abilities: c.abilities || "",
      current_location: c.current_location || "",
      current_timeline_status: c.current_timeline_status || "",
      first_chapter_id: c.first_chapter_id || null,
      last_chapter_id: c.last_chapter_id || null,
    });
    setAliasesText((c.aliases || []).join("、"));
  }

  function startCreate() {
    setEditing(null);
    setForm(emptyForm);
    setAliasesText("");
  }

  async function submitForm(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("人物名称必填");
      return;
    }
    setFormBusy(true);
    setError("");
    const aliases = aliasesText
      .split(/[、,，;；]/)
      .map((s) => s.trim())
      .filter(Boolean);
    const payload: CharacterInput = { ...form, name: form.name.trim(), aliases };
    try {
      if (editing) {
        const updated = await api.updateCharacter(projectId, editing.id, payload);
        setChars((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
      } else {
        const created = await api.createCharacter(projectId, payload);
        setChars((prev) => [...prev, created]);
      }
      setForm(emptyForm);
      setAliasesText("");
      setEditing(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存人物失败");
    } finally {
      setFormBusy(false);
    }
  }

  async function removeCharacter(id: string) {
    if (!window.confirm("确认删除该人物？关联的关系与快照将保留在数据库中。")) return;
    setError("");
    try {
      await api.deleteCharacter(projectId, id);
      setChars((prev) => prev.filter((c) => c.id !== id));
      setRelations((prev) =>
        prev.filter((r) => r.source_character_id !== id && r.target_character_id !== id)
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除人物失败");
    }
  }

  async function submitRelation(e: React.FormEvent) {
    e.preventDefault();
    if (!relForm.source_character_id || !relForm.target_character_id) {
      setError("关系双方必选");
      return;
    }
    setRelBusy(true);
    setError("");
    const payload = {
      source_character_id: relForm.source_character_id,
      target_character_id: relForm.target_character_id,
      relation_type: relForm.relation_type,
      description: relForm.description || undefined,
      start_chapter_id: relForm.start_chapter_id || null,
      end_chapter_id: relForm.end_chapter_id || null,
      status: relForm.status as "draft" | "confirmed" | "archived",
    };
    try {
      if (relEditing) {
        const updated = await api.updateRelationship(projectId, relEditing.id, payload);
        setRelations((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      } else {
        const created = await api.createRelationship(projectId, payload);
        setRelations((prev) => [...prev, created]);
      }
      setRelOpen(false);
      setRelEditing(null);
      setRelForm({
        source_character_id: "",
        target_character_id: "",
        relation_type: "同盟",
        description: "",
        start_chapter_id: "",
        end_chapter_id: "",
        status: "confirmed",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存关系失败");
    } finally {
      setRelBusy(false);
    }
  }

  function startRelEdit(r: CharacterRelationshipData) {
    setRelEditing(r);
    setRelOpen(true);
    setRelForm({
      source_character_id: r.source_character_id,
      target_character_id: r.target_character_id,
      relation_type: r.relation_type,
      description: r.description || "",
      start_chapter_id: r.start_chapter_id || "",
      end_chapter_id: r.end_chapter_id || "",
      status: r.status,
    });
  }

  function startRelCreate() {
    setRelEditing(null);
    setRelForm({
      source_character_id: "",
      target_character_id: "",
      relation_type: "同盟",
      description: "",
      start_chapter_id: "",
      end_chapter_id: "",
      status: "confirmed",
    });
    setRelOpen(true);
  }

  async function removeRelation(id: string) {
    if (!window.confirm("确认删除该关系？")) return;
    setError("");
    try {
      await api.deleteRelationship(projectId, id);
      setRelations((prev) => prev.filter((r) => r.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除关系失败");
    }
  }

  async function confirmRelation(r: CharacterRelationshipData) {
    if (r.status === "confirmed") return;
    setError("");
    try {
      const updated = await api.updateRelationship(projectId, r.id, { status: "confirmed" });
      setRelations((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认关系失败");
    }
  }

  async function toggleSnapshots(characterId: string) {
    if (snapCharId === characterId) {
      setSnapCharId("");
      setSnapshots([]);
      return;
    }
    setSnapCharId(characterId);
    setSnapBusy(true);
    setError("");
    try {
      const list = await api.listCharacterSnapshots(projectId, characterId);
      setSnapshots(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载快照失败");
    } finally {
      setSnapBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载人物库…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>人物库</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ display: "flex", gap: 4 }}>
            <button
              className={view === "characters" ? "btn-primary" : "secondary"}
              onClick={() => setView("characters")}
            >
              人物
            </button>
            <button
              className={view === "relations" ? "btn-primary" : "secondary"}
              onClick={() => setView("relations")}
            >
              关系
            </button>
          </div>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/timeline`)}>
            时间线
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/foreshadows`)}>
            伏笔
          </button>
          <button onClick={() => navigate(`/projects/${projectId}/suggestions`)}>
            变更确认
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {view === "relations" && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
          <select
            value={relFilter}
            onChange={(e) => setRelFilter(e.target.value)}
            style={{ maxWidth: 180 }}
          >
            <option value="">全部状态</option>
            {REL_STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {REL_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </div>
      )}

      {view === "characters" && (
        <>
      {/* 人物卡片表单 */}
      <div className="card" style={{ marginBottom: 18 }}>
        <h3 className="panel-title">{editing ? `编辑人物：${editing.name}` : "新建人物"}</h3>
        <form onSubmit={submitForm}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div className="field">
              <label>姓名 *</label>
              <input
                value={form.name}
                onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                required
              />
            </div>
            <div className="field">
              <label>别名（用顿号分隔）</label>
              <input value={aliasesText} onChange={(e) => setAliasesText(e.target.value)} />
            </div>
            <div className="field">
              <label>定位</label>
              <select
                value={form.role}
                onChange={(e) => setForm((p) => ({ ...p, role: e.target.value }))}
              >
                {ROLE_OPTIONS.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>状态</label>
              <select
                value={form.status}
                onChange={(e) => setForm((p) => ({ ...p, status: e.target.value }))}
              >
                {STATUS_OPTIONS.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>外貌 / 性格 / 背景</label>
            <textarea
              value={form.profile}
              onChange={(e) => setForm((p) => ({ ...p, profile: e.target.value }))}
              style={{ minHeight: 70 }}
            />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div className="field">
              <label>当前目标</label>
              <input
                value={form.goals}
                onChange={(e) => setForm((p) => ({ ...p, goals: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>已知秘密</label>
              <input
                value={form.secrets}
                onChange={(e) => setForm((p) => ({ ...p, secrets: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>能力</label>
              <input
                value={form.abilities}
                onChange={(e) => setForm((p) => ({ ...p, abilities: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>当前位置</label>
              <input
                value={form.current_location}
                onChange={(e) => setForm((p) => ({ ...p, current_location: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>当前时间线状态</label>
              <input
                value={form.current_timeline_status}
                onChange={(e) => setForm((p) => ({ ...p, current_timeline_status: e.target.value }))}
              />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <div className="field">
                <label>首次出场章节</label>
                <select
                  value={form.first_chapter_id || ""}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, first_chapter_id: e.target.value || null }))
                  }
                >
                  <option value="">—</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>最后出场章节</label>
                <select
                  value={form.last_chapter_id || ""}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, last_chapter_id: e.target.value || null }))
                  }
                >
                  <option value="">—</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button type="submit" disabled={formBusy}>
              {formBusy ? "保存中…" : editing ? "保存修改" : "创建人物"}
            </button>
            {editing && (
              <button type="button" className="secondary" onClick={startCreate}>
                取消编辑
              </button>
            )}
          </div>
        </form>
      </div>

      {/* 人物列表 */}
      <div className="card" style={{ marginBottom: 18 }}>
        <h3 className="panel-title">人物列表（{chars.length}）</h3>
        <div className="list">
          {chars.length === 0 ? (
            <div className="empty">暂无人物，创建第一个人物卡吧</div>
          ) : (
            chars.map((c) => (
              <div key={c.id} className="list-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ flex: 1, fontWeight: 600 }}>
                    {c.name}
                    {(c.aliases || []).length > 0 && (
                      <span style={{ color: "var(--text-dim)", fontSize: 13, marginLeft: 8 }}>
                        （{c.aliases.join("、")}）
                      </span>
                    )}
                    <span className="tag" style={{ marginLeft: 8 }}>
                      {c.role}
                    </span>
                    <span className="tag" style={{ marginLeft: 4 }}>
                      {c.status}
                    </span>
                  </div>
                  <button className="secondary" onClick={() => void toggleSnapshots(c.id)}>
                    {snapCharId === c.id ? "收起快照" : "快照"}
                  </button>
                  <button className="secondary" onClick={() => startEdit(c)}>
                    编辑
                  </button>
                  <button className="danger-btn" onClick={() => void removeCharacter(c.id)}>
                    删除
                  </button>
                </div>
                <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 4 }}>
                  首次出场：
                  {c.first_chapter_id ? (
                    <a href={`/projects/${projectId}/chapters/${c.first_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${c.first_chapter_id}`); }}>
                      {chapterName(c.first_chapter_id)}
                    </a>
                  ) : (
                    "—"
                  )}{" "}
                  · 最后出场：
                  {c.last_chapter_id ? (
                    <a href={`/projects/${projectId}/chapters/${c.last_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${c.last_chapter_id}`); }}>
                      {chapterName(c.last_chapter_id)}
                    </a>
                  ) : (
                    "—"
                  )}
                  {c.current_location && ` · 现在：${c.current_location}`}
                  {c.goals && ` · 目标：${c.goals}`}
                </div>
                {c.profile && (
                  <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 2 }}>
                    {c.profile.slice(0, 120)}
                    {c.profile.length > 120 ? "…" : ""}
                  </div>
                )}
                {snapCharId === c.id && (
                  <div style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                      状态快照轨迹
                      {snapBusy && <span className="spin" style={{ marginLeft: 8 }} />}
                    </div>
                    {snapshots.length === 0 ? (
                      <div className="empty" style={{ padding: 8 }}>
                        暂无快照（确认变更建议或手工创建后会在此生成逐章状态轨迹）
                      </div>
                    ) : (
                      snapshots.map((s) => (
                        <div key={s.id} style={{ fontSize: 13, marginBottom: 4 }}>
                          <span className="tag">
                            {SNAP_SOURCE_LABEL[s.source] || s.source}
                          </span>{" "}
                          <span className="tag">
                            {SNAP_STATUS_LABEL[s.status || ""] || s.status || "待确认"}
                          </span>{" "}
                          {s.chapter_id ? (
                            <a href={`/projects/${projectId}/chapters/${s.chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${s.chapter_id}`); }}>
                              {chapterName(s.chapter_id)}
                            </a>
                          ) : (
                            chapterName(s.chapter_id)
                          )}{" "}
                          · {JSON.stringify(s.state_json)}
                          {s.confirmed_at &&
                            ` · 确认于 ${new Date(s.confirmed_at).toLocaleString()}`}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      {/* 角色关系 */}
      <div className="card">
        <h3 className="panel-title">
          角色关系（{relations.length}）
          <button
            className="secondary"
            style={{ marginLeft: 10, padding: "2px 10px" }}
            onClick={startRelCreate}
          >
            {relOpen ? "收起" : "+ 新建关系"}
          </button>
          {view === "characters" && (
            <select
              value={relFilter}
              onChange={(e) => setRelFilter(e.target.value)}
              style={{ marginLeft: 10, maxWidth: 160 }}
            >
              <option value="">全部状态</option>
              {REL_STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {REL_STATUS_LABEL[s]}
                </option>
              ))}
            </select>
          )}
        </h3>
        {relOpen && (
          <form onSubmit={submitRelation} className="inline-form">
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
              <div className="field">
                <label>人物 A *</label>
                <select
                  value={relForm.source_character_id}
                  onChange={(e) =>
                    setRelForm((p) => ({ ...p, source_character_id: e.target.value }))
                  }
                >
                  <option value="">请选择</option>
                  {chars.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>人物 B *</label>
                <select
                  value={relForm.target_character_id}
                  onChange={(e) =>
                    setRelForm((p) => ({ ...p, target_character_id: e.target.value }))
                  }
                >
                  <option value="">请选择</option>
                  {chars.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>关系类型 *</label>
                <select
                  value={relForm.relation_type}
                  onChange={(e) =>
                    setRelForm((p) => ({ ...p, relation_type: e.target.value }))
                  }
                >
                  {RELATION_OPTIONS.map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="field">
              <label>关系描述</label>
              <input
                value={relForm.description}
                onChange={(e) => setRelForm((p) => ({ ...p, description: e.target.value }))}
              />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
              <div className="field">
                <label>起始章节</label>
                <select
                  value={relForm.start_chapter_id}
                  onChange={(e) =>
                    setRelForm((p) => ({ ...p, start_chapter_id: e.target.value }))
                  }
                >
                  <option value="">—</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>结束章节</label>
                <select
                  value={relForm.end_chapter_id}
                  onChange={(e) => setRelForm((p) => ({ ...p, end_chapter_id: e.target.value }))}
                >
                  <option value="">—</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>状态</label>
                <select
                  value={relForm.status}
                  onChange={(e) => setRelForm((p) => ({ ...p, status: e.target.value }))}
                >
                  {REL_STATUS_OPTIONS.map((s) => (
                    <option key={s} value={s}>
                      {REL_STATUS_LABEL[s]}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <button type="submit" disabled={relBusy}>
              {relBusy ? "保存中…" : relEditing ? "保存修改" : "创建关系"}
            </button>
            {relEditing && (
              <button type="button" className="secondary" onClick={() => setRelEditing(null)}>
                取消编辑
              </button>
            )}
          </form>
        )}
        <div className="list">
          {relations.length === 0 ? (
            <div className="empty">暂无关系</div>
          ) : (
            relations
              .filter((r) => (relFilter ? r.status === relFilter : true))
              .map((r) => (
                <div key={r.id} className="list-item">
                  <div style={{ flex: 1 }}>
                    <span style={{ fontWeight: 600 }}>
                      {nameMap[r.source_character_id] || "?"} → {nameMap[r.target_character_id] || "?"}
                    </span>
                    <span className="tag" style={{ marginLeft: 8 }}>
                      {r.relation_type}
                    </span>
                    <span className="tag" style={{ marginLeft: 4 }}>
                      {REL_STATUS_LABEL[r.status] || r.status}
                    </span>
                    {r.status === "draft" && (
                      <button className="secondary" style={{ marginLeft: 8, padding: "1px 8px" }} onClick={() => void confirmRelation(r)}>
                        确认
                      </button>
                    )}
                    {r.description && (
                      <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{r.description}</div>
                    )}
                    <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                      起始：
                      {r.start_chapter_id ? (
                        <a href={`/projects/${projectId}/chapters/${r.start_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${r.start_chapter_id}`); }}>
                          {chapterName(r.start_chapter_id)}
                        </a>
                      ) : (
                        "—"
                      )}{" "}
                      · 结束：
                      {r.end_chapter_id ? (
                        <a href={`/projects/${projectId}/chapters/${r.end_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${r.end_chapter_id}`); }}>
                          {chapterName(r.end_chapter_id)}
                        </a>
                      ) : (
                        "—"
                      )}
                    </div>
                  </div>
                  <button className="secondary" onClick={() => startRelEdit(r)}>
                    编辑
                  </button>
                  <button className="danger-btn" onClick={() => void removeRelation(r.id)}>
                    删除
                  </button>
                </div>
              ))
          )}
        </div>
      </div>
      </>
      )}
    </div>
  );
}
