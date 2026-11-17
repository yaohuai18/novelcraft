import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type Project } from "../api/client";
import { demoProjects } from "../api/mock";
import type { AiWorkSnapshot } from "./CreatePage";

function loadSnapshots(): AiWorkSnapshot[] {
  const list: AiWorkSnapshot[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith("novelcraft_ai_works_")) continue;
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      const snap = JSON.parse(raw) as AiWorkSnapshot;
      if (snap && snap.title && snap.chapters) list.push(snap);
    }
  } catch {
    /* ignore */
  }
  list.sort((a, b) => (b.savedAt || "").localeCompare(a.savedAt || ""));
  return list;
}

export default function ProjectsPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [snapshots, setSnapshots] = useState<AiWorkSnapshot[]>(loadSnapshots);
  const [loading, setLoading] = useState(true);
  const [isDemo, setIsDemo] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newGenre, setNewGenre] = useState("");
  const [error, setError] = useState("");
  const [showTrash, setShowTrash] = useState(false);
  const [trash, setTrash] = useState<Project[]>([]);
  const [trashLoading, setTrashLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const list = await api.listProjects();
      setProjects(list);
      setIsDemo(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setProjects(demoProjects);
        setIsDemo(true);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setSnapshots(loadSnapshots());
  }, [projects]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!newTitle.trim()) return;
    try {
      const created = await api.createProject({
        title: newTitle.trim(),
        genre: newGenre.trim() || undefined,
      });
      setProjects((prev) => [created, ...prev]);
      setShowCreate(false);
      setNewTitle("");
      setNewGenre("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    }
  }

  async function handleDelete(p: Project) {
    if (!window.confirm(`确定删除作品「${p.title}」吗？\n\n删除后进入回收站，30 天内可在回收站恢复。`)) return;
    setError("");
    try {
      await api.deleteProject(p.id);
      setProjects((prev) => prev.filter((x) => x.id !== p.id));
      setSnapshots(loadSnapshots());
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setProjects((prev) => prev.filter((x) => x.id !== p.id));
        setSnapshots(loadSnapshots());
        setError("该作品在后端已不存在，已从列表移除");
      } else {
        setError(err instanceof Error ? err.message : "删除失败");
      }
    }
  }

  async function loadTrash() {
    setTrashLoading(true);
    setError("");
    try {
      setTrash(await api.listTrashProjects());
    } catch (err) {
      setError(err instanceof Error ? err.message : "回收站加载失败");
    } finally {
      setTrashLoading(false);
    }
  }

  async function handleRestore(p: Project) {
    setError("");
    try {
      await api.restoreProjectFromTrash(p.id);
      setTrash((prev) => prev.filter((x) => x.id !== p.id));
      setProjects((prev) => [p, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "恢复失败");
    }
  }

  async function handlePurge(p: Project) {
    if (!window.confirm(`彻底删除「${p.title}」？\n\n该操作不可恢复（删除前系统会自动保留一份快照 30 天）。`)) return;
    setError("");
    try {
      await api.purgeProject(p.id);
      setTrash((prev) => prev.filter((x) => x.id !== p.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "彻底删除失败");
    }
  }

  function openProject(id: string) {
    navigate(`/projects/${id}/chapters`);
  }

  function editProject(p: Project) {
    navigate(`/create?edit=${p.id}`);
  }

  return (
    <div>
      <div className="topbar">
        <h1>
          作品库
          {isDemo && <span className="demo-badge">演示数据</span>}
        </h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button
            className="secondary"
            onClick={() => {
              const next = !showTrash;
              setShowTrash(next);
              if (next) void loadTrash();
            }}
          >
            {showTrash ? "返回作品库" : "🗑 回收站"}
          </button>
          <button onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? "收起" : "+ 新建作品"}
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {showTrash && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>回收站（删除 30 天后自动彻底清除）</div>
          {trashLoading ? (
            <div className="empty">加载中…</div>
          ) : trash.length === 0 ? (
            <div className="empty">回收站是空的</div>
          ) : (
            trash.map((p) => (
              <div
                key={p.id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "8px 0",
                  borderBottom: "1px solid var(--border, #ddd)",
                }}
              >
                <div>
                  <span style={{ fontWeight: 500 }}>{p.title}</span>
                  <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
                    {" "}删除于 {p.deleted_at ? new Date(p.deleted_at).toLocaleDateString() : "—"}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="secondary" onClick={() => void handleRestore(p)}>
                    恢复
                  </button>
                  <button className="danger-btn" onClick={() => void handlePurge(p)}>
                    彻底删除
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 18 }}>
          <form onSubmit={handleCreate}>
            <div className="field">
              <label>作品标题</label>
              <input
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="例如：星海彼岸"
                required
              />
            </div>
            <div className="field">
              <label>类型（可选）</label>
              <input
                value={newGenre}
                onChange={(e) => setNewGenre(e.target.value)}
                placeholder="例如：科幻 / 古风悬疑 / 奇幻"
              />
            </div>
            <button type="submit">创建作品</button>
          </form>
        </div>
      )}

      {loading ? (
        <div className="empty">
          <span className="spin" /> 正在加载作品…
        </div>
      ) : projects.length === 0 && snapshots.length === 0 ? (
        <div className="empty">
          <p>还没有作品，去「创作」页填写设定生成第一部小说吧。</p>
        </div>
      ) : (
        <div className="grid">
          {projects.map((p) => (
            <div key={p.id} className="project-card">
              <div style={{ cursor: "pointer" }} onClick={() => openProject(p.id)}>
                <h3>{p.title}</h3>
                <div className="project-meta">
                  {p.genre && <span className="tag">{p.genre}</span>}
                  <span className="tag">{p.status === "archived" ? "已归档" : "创作中"}</span>
                  {isDemo && <span className="tag" style={{ color: "var(--warn)" }}>演示</span>}
                </div>
                <div className="project-desc">{p.background || "暂无简介"}</div>
                <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                  {p.updated_at ? new Date(p.updated_at).toLocaleDateString("zh-CN") : "-"} 更新
                </div>
              </div>
              <div className="card-actions">
                <button onClick={() => openProject(p.id)}>
                  继续创作
                </button>
                <button className="secondary" onClick={() => editProject(p)} title="进入灵感/规划页：重新生成整本规划或调整设定">
                  AI 规划
                </button>
                <button className="danger-btn" onClick={() => void handleDelete(p)}>
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {snapshots.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <h3 className="panel-title">本地 AI 作品快照（localStorage 持久化）</h3>
          <div className="grid">
            {snapshots.map((s) => (
              <div key={s.projectId} className="project-card">
                <div style={{ cursor: "pointer" }} onClick={() => navigate(`/create?edit=${s.projectId}`)}>
                  <h3>{s.title}</h3>
                  <div className="project-meta">
                    <span className="tag">{s.settings.genre || "未设题材"}</span>
                    <span className="tag">{s.chapters.length} 章</span>
                    <span className="tag" style={{ color: "var(--warn)" }}>
                      {s.settings.words_per_chapter || "-"}字/章
                    </span>
                  </div>
                  <div className="project-desc">
                    主线：{s.settings.main_plot || "（未填写）"}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                    {s.savedAt ? new Date(s.savedAt).toLocaleString("zh-CN") : "-"} 保存
                  </div>
                </div>
                <div className="card-actions">
                  <button className="secondary" onClick={() => navigate(`/create?edit=${s.projectId}`)}>
                    打开快照
                  </button>
                  {!projects.some((p) => p.id === s.projectId) && (
                    <button
                      className="secondary"
                      onClick={() => {
                        navigate("/create", {
                          state: { importSnapshot: s.settings },
                        });
                      }}
                    >
                      载入设定
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
