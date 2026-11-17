import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type Project, type PreviewProject, type PublishResult } from "../api/client";
import { demoProjects, demoPreview } from "../api/mock";

const STATUS_OPTIONS = [
  { value: "draft", label: "草稿" },
  { value: "review", label: "审核中" },
  { value: "published", label: "已发布" },
  { value: "unlisted", label: "未上架" },
  { value: "archived", label: "已归档" },
];

export default function PublishPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const preselect = searchParams.get("project") || "";

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(preselect);
  const [project, setProject] = useState<Project | null>(null);
  const [preview, setPreview] = useState<PreviewProject | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [isDemo, setIsDemo] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  // 发布设置表单
  const [pubTitle, setPubTitle] = useState("");
  const [pubSubtitle, setPubSubtitle] = useState("");
  const [pubDesc, setPubDesc] = useState("");
  const [pubCover, setPubCover] = useState("");
  const [pubStatus, setPubStatus] = useState("draft");
  const [pubSlug, setPubSlug] = useState("");

  async function loadProjects() {
    setLoading(true);
    setError("");
    try {
      const list = await api.listProjects();
      setProjects(list);
      setIsDemo(false);
      if (!projectId && list.length > 0) setProjectId(list[0].id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        setProjects(demoProjects);
        if (!projectId) setProjectId(demoProjects[0].id);
        setIsDemo(true);
      }
    } finally {
      setLoading(false);
    }
  }

  async function loadDetail(id: string) {
    if (!id) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      const [proj, pv] = await Promise.all([api.getProject(id), api.getPreview(id)]);
      setProject(proj);
      setPreview(pv);
      setPubTitle(proj.title || "");
      setPubSubtitle(proj.subtitle || "");
      setPubDesc(proj.description || "");
      setPubCover(proj.cover_text || "");
      setPubStatus(proj.publication_status || "draft");
      setPubSlug(proj.project_slug || "");
      setIsDemo(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        const demoProj = demoProjects.find((p) => p.id === id) || demoProjects[0];
        setProject(demoProj);
        setPreview(demoPreview);
        setPubTitle(demoProj.title);
        setPubStatus(demoProj.publication_status);
        setIsDemo(true);
      }
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (projectId) void loadDetail(projectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function handlePublish() {
    if (!projectId) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      const result: PublishResult = await api.publishProject(projectId, false);
      setOk(result.message || `发布状态：${result.status}`);
      await loadDetail(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "发布失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdatePublication(e: React.FormEvent) {
    e.preventDefault();
    if (!projectId) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.updatePublication(projectId, {
        title: pubTitle.trim() || undefined,
        subtitle: pubSubtitle.trim() || undefined,
        description: pubDesc.trim() || undefined,
        cover_text: pubCover.trim() || undefined,
        status: pubStatus,
        project_slug: pubSlug.trim() || undefined,
      });
      setOk("发布设置已保存");
      await loadDetail(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存发布设置失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleRestore() {
    if (!projectId) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      const result: PublishResult = await api.restoreProject(projectId);
      setOk(result.message || "已恢复为草稿");
      await loadDetail(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "恢复失败");
    } finally {
      setBusy(false);
    }
  }

  const statusLabel = (s: string) => STATUS_OPTIONS.find((o) => o.value === s)?.label || s;

  return (
    <div style={{ maxWidth: 860 }}>
      <div className="topbar">
        <h1>
          发布
          {isDemo && <span className="demo-badge">演示数据</span>}
        </h1>
        <button className="secondary" onClick={() => navigate("/projects")}>
          ← 返回作品库
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {ok && <div className="ok-box">{ok}</div>}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="field">
          <label>选择作品</label>
          <select
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={loading || projects.length === 0}
          >
            {projects.length === 0 && <option value="">暂无作品</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
        </div>
      </div>

      {busy && (
        <div className="empty">
          <span className="spin" /> 正在加载发布信息…
        </div>
      )}

      {!busy && project && (
        <>
          <div className="card" style={{ marginBottom: 18 }}>
            <h3 className="panel-title">发布状态</h3>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
              <span className="tag">{statusLabel(project.publication_status)}</span>
              {project.project_slug && <span className="tag">链接 /s/{project.project_slug}</span>}
              {project.published_at && (
                <span className="tag">
                  发布于 {new Date(project.published_at).toLocaleDateString("zh-CN")}
                </span>
              )}
            </div>
            {preview && (
              <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 12 }}>
                {preview.cover_text ? `${preview.cover_text} · ` : ""}
                {preview.title}
                {preview.subtitle ? ` — ${preview.subtitle}` : ""}
              </div>
            )}
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              <button onClick={handlePublish} disabled={busy}>
                发布作品
              </button>
              {(project.publication_status !== "draft" && project.publication_status !== "archived") && (
                <button className="secondary" onClick={handleRestore} disabled={busy}>
                  恢复为草稿
                </button>
              )}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 10 }}>
              发布前系统会做完整性检查；存在高等级问题时将阻止发布（发布请求体支持 force 忽略低等级问题）。
            </div>
          </div>

          <div className="card">
            <h3 className="panel-title">发布设置</h3>
            <form onSubmit={handleUpdatePublication}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <div className="field">
                  <label>作品名称</label>
                  <input value={pubTitle} onChange={(e) => setPubTitle(e.target.value)} />
                </div>
                <div className="field">
                  <label>副标题</label>
                  <input value={pubSubtitle} onChange={(e) => setPubSubtitle(e.target.value)} />
                </div>
              </div>
              <div className="field">
                <label>简介</label>
                <textarea value={pubDesc} onChange={(e) => setPubDesc(e.target.value)} style={{ minHeight: 80 }} />
              </div>
              <div className="field">
                <label>封面文字</label>
                <input value={pubCover} onChange={(e) => setPubCover(e.target.value)} />
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <div className="field">
                  <label>发布状态</label>
                  <select value={pubStatus} onChange={(e) => setPubStatus(e.target.value)}>
                    {STATUS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>公开链接 slug（小写字母/数字/中划线）</label>
                  <input value={pubSlug} onChange={(e) => setPubSlug(e.target.value)} placeholder="my-novel-01" />
                </div>
              </div>
              <button type="submit" disabled={busy}>
                {busy ? "保存中…" : "保存发布设置"}
              </button>
            </form>
          </div>
        </>
      )}

      {!busy && !project && !loading && (
        <div className="empty">
          <p>暂无作品，请先到作品库创建作品。</p>
          <button onClick={() => navigate("/create")}>去新建作品</button>
        </div>
      )}
    </div>
  );
}
