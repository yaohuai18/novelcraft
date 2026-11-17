// 文26 P2「人物、关系、时间线和伏笔正式管理」：关系管理独立页
// 支持手工创建/编辑/删除/筛选，显示来源章节链接（start/end chapter）
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  type CharacterData,
  type CharacterRelationshipData,
  type OutlineNode,
} from "../api/client";

const RELATION_OPTIONS = ["师徒", "敌对", "同盟", "亲属", "怀疑", "爱慕", "同僚", "主从", "其他"];
const REL_STATUS_OPTIONS: Array<"draft" | "confirmed" | "archived"> = ["draft", "confirmed", "archived"];
const REL_STATUS_LABEL: Record<string, string> = {
  draft: "待确认",
  confirmed: "已确认",
  archived: "已归档",
};

const emptyRelForm = {
  source_character_id: "",
  target_character_id: "",
  relation_type: "同盟",
  description: "",
  start_chapter_id: "",
  end_chapter_id: "",
  status: "confirmed",
};

export default function RelationshipsPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [rels, setRels] = useState<CharacterRelationshipData[]>([]);
  const [chars, setChars] = useState<CharacterData[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // 筛选
  const [statusFilter, setStatusFilter] = useState("");
  const [keyword, setKeyword] = useState("");

  // 新建 / 编辑
  const [relOpen, setRelOpen] = useState(false);
  const [relEditing, setRelEditing] = useState<CharacterRelationshipData | null>(null);
  const [relForm, setRelForm] = useState(emptyRelForm);

  const nameMap = useMemo(() => {
    const m: Record<string, string> = {};
    chars.forEach((c) => (m[c.id] = c.name));
    return m;
  }, [chars]);

  const chapterTitle = useCallback(
    (id?: string | null) => {
      if (!id) return "";
      const c = chapters.find((x) => x.id === id);
      return c ? c.title : "";
    },
    [chapters]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rs, cs, chs] = await Promise.all([
        api.listRelationships(projectId, statusFilter || undefined),
        api.listCharacters(projectId),
        api.listOutlines(projectId, "chapter"),
      ]);
      setRels(rs);
      setChars(cs);
      setChapters(chs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载关系失败");
    } finally {
      setLoading(false);
    }
  }, [projectId, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    if (!keyword.trim()) return rels;
    const kw = keyword.trim().toLowerCase();
    return rels.filter((r) => {
      const src = nameMap[r.source_character_id] || "";
      const tgt = nameMap[r.target_character_id] || "";
      return (
        src.toLowerCase().includes(kw) ||
        tgt.toLowerCase().includes(kw) ||
        r.relation_type.toLowerCase().includes(kw) ||
        (r.description || "").toLowerCase().includes(kw)
      );
    });
  }, [rels, keyword, nameMap]);

  function startCreate() {
    setRelEditing(null);
    setRelForm(emptyRelForm);
    setRelOpen(true);
  }

  function startEdit(r: CharacterRelationshipData) {
    setRelEditing(r);
    setRelForm({
      source_character_id: r.source_character_id,
      target_character_id: r.target_character_id,
      relation_type: r.relation_type,
      description: r.description || "",
      start_chapter_id: r.start_chapter_id || "",
      end_chapter_id: r.end_chapter_id || "",
      status: r.status,
    });
    setRelOpen(true);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!relForm.source_character_id || !relForm.target_character_id) {
      setError("关系双方必选");
      return;
    }
    setBusy(true);
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
        setRels((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      } else {
        const created = await api.createRelationship(projectId, payload);
        setRels((prev) => [...prev, created]);
      }
      setRelOpen(false);
      setRelEditing(null);
      setRelForm(emptyRelForm);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存关系失败");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该关系？")) return;
    setBusy(true);
    setError("");
    try {
      await api.deleteRelationship(projectId, id);
      setRels((prev) => prev.filter((r) => r.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除关系失败");
    } finally {
      setBusy(false);
    }
  }

  async function confirmRel(r: CharacterRelationshipData) {
    if (r.status === "confirmed") return;
    setBusy(true);
    setError("");
    try {
      const updated = await api.updateRelationship(projectId, r.id, { status: "confirmed" });
      setRels((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认关系失败");
    } finally {
      setBusy(false);
    }
  }

  async function archiveRel(r: CharacterRelationshipData) {
    if (r.status === "archived") return;
    setBusy(true);
    setError("");
    try {
      const updated = await api.updateRelationship(projectId, r.id, { status: "archived" });
      setRels((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "归档关系失败");
    } finally {
      setBusy(false);
    }
  }

  function chapterLink(id?: string | null) {
    if (!id) return null;
    const title = chapterTitle(id);
    return (
      <a href={`/projects/${projectId}/chapters/${id}`} className="chapter-link">
        {title ? `第 ${id} 章 · ${title}` : `第 ${id} 章`}
      </a>
    );
  }

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载关系…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>关系管理</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            style={{ padding: "6px 8px" }}
          >
            <option value="">全部状态</option>
            {REL_STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {REL_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索人物 / 关系类型 / 描述"
            style={{ padding: "6px 8px", width: 220 }}
          />
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/characters`)}>
            人物
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/timeline`)}>
            时间线
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/foreshadows`)}>
            伏笔
          </button>
          <button onClick={startCreate} disabled={busy}>
            新建关系
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {relOpen && (
        <div className="card" style={{ marginBottom: 18 }}>
          <h3>{relEditing ? "编辑关系" : "新建关系"}</h3>
          <form onSubmit={(e) => void submit(e)} style={{ display: "grid", gap: 10, gridTemplateColumns: "1fr 1fr" }}>
            <label>
              发起方（必填）
              <select
                value={relForm.source_character_id}
                onChange={(e) => setRelForm((p) => ({ ...p, source_character_id: e.target.value }))}
              >
                <option value="">请选择人物</option>
                {chars.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              目标方（必填）
              <select
                value={relForm.target_character_id}
                onChange={(e) => setRelForm((p) => ({ ...p, target_character_id: e.target.value }))}
              >
                <option value="">请选择人物</option>
                {chars.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              关系类型
              <select
                value={relForm.relation_type}
                onChange={(e) => setRelForm((p) => ({ ...p, relation_type: e.target.value }))}
              >
                {RELATION_OPTIONS.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            </label>
            <label>
              状态
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
            </label>
            <label style={{ gridColumn: "1 / -1" }}>
              描述
              <textarea
                value={relForm.description}
                onChange={(e) => setRelForm((p) => ({ ...p, description: e.target.value }))}
                rows={2}
              />
            </label>
            <label>
              起始章节（来自第几章）
              <select
                value={relForm.start_chapter_id}
                onChange={(e) => setRelForm((p) => ({ ...p, start_chapter_id: e.target.value }))}
              >
                <option value="">未指定</option>
                {chapters.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              结束章节
              <select
                value={relForm.end_chapter_id}
                onChange={(e) => setRelForm((p) => ({ ...p, end_chapter_id: e.target.value }))}
              >
                <option value="">未指定（持续至今）</option>
                {chapters.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </select>
            </label>
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8 }}>
              <button type="submit" className="btn-primary" disabled={busy}>
                保存
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setRelOpen(false);
                  setRelEditing(null);
                  setRelForm(emptyRelForm);
                }}
              >
                取消
              </button>
            </div>
          </form>
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="empty">暂无关系数据，点击"新建关系"创建。</div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr>
                <th>发起方</th>
                <th>关系</th>
                <th>目标方</th>
                <th>描述</th>
                <th>起始章节</th>
                <th>结束章节</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.id}>
                  <td>{nameMap[r.source_character_id] || "未知"}</td>
                  <td>
                    <span className="tag">{r.relation_type}</span>
                  </td>
                  <td>{nameMap[r.target_character_id] || "未知"}</td>
                  <td>{r.description || "—"}</td>
                  <td>{chapterLink(r.start_chapter_id)}</td>
                  <td>{chapterLink(r.end_chapter_id)}</td>
                  <td>
                    <span className={`tag tag-${r.status}`}>{REL_STATUS_LABEL[r.status] || r.status}</span>
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 6 }}>
                      {r.status === "draft" && (
                        <button className="secondary" onClick={() => void confirmRel(r)}>
                          确认
                        </button>
                      )}
                      {r.status !== "archived" && (
                        <button className="secondary" onClick={() => void archiveRel(r)}>
                          归档
                        </button>
                      )}
                      <button className="secondary" onClick={() => startEdit(r)}>
                        编辑
                      </button>
                      <button className="danger" onClick={() => void remove(r.id)}>
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
