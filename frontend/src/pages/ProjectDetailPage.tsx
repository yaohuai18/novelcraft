import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type Project,
  type OutlineNode,
  type WorldSetting,
  type ExportCreateInput,
  type ExportTask,
} from "../api/client";
import { demoProjects, demoVolumes, demoChapters, demoWorldSettings } from "../api/mock";

const CATEGORIES = [
  { value: "faction", label: "势力" },
  { value: "location", label: "地点" },
  { value: "power", label: "力量体系" },
  { value: "history", label: "历史事件" },
  { value: "race", label: "种族" },
  { value: "item", label: "物品" },
  { value: "rule", label: "世界规则" },
  { value: "taboo", label: "禁忌" },
];

export default function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [project, setProject] = useState<Project | null>(null);
  const [settings, setSettings] = useState<WorldSetting[]>([]);
  const [summaries, setSummaries] = useState<OutlineNode[]>([]);
  const [volumes, setVolumes] = useState<OutlineNode[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [isDemo, setIsDemo] = useState(false);
  const [error, setError] = useState("");

  // 新建设定
  const [wsCategory, setWsCategory] = useState("faction");
  const [wsName, setWsName] = useState("");
  const [wsContent, setWsContent] = useState("");
  const [wsImportance, setWsImportance] = useState("normal");
  const [wsBusy, setWsBusy] = useState(false);

  // 新建分卷
  const [volTitle, setVolTitle] = useState("");
  const [volSummary, setVolSummary] = useState("");
  const [volBusy, setVolBusy] = useState(false);

  // 新建章节
  const [chTitle, setChTitle] = useState("");
  const [chSummary, setChSummary] = useState("");
  const [chVolume, setChVolume] = useState("");
  const [chBusy, setChBusy] = useState(false);

  // 导出
  const [exportOpen, setExportOpen] = useState(false);
  const [exportFormat, setExportFormat] = useState("markdown");
  const [exportScope, setExportScope] = useState("whole_project");
  const [exportOutlineId, setExportOutlineId] = useState("");
  const [exportChapterId, setExportChapterId] = useState("");
  const [exportInclude, setExportInclude] = useState({
    include_outline_summaries: true,
    include_characters: true,
    include_ai_records: true,
    include_version_history: false,
    include_toc: false,
  });
  const [exportBusy, setExportBusy] = useState(false);
  const [exportMessage, setExportMessage] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [proj, ws, sums, vols, chs] = await Promise.all([
        api.getProject(projectId),
        api.listWorldSettings(projectId),
        api.listOutlines(projectId, "summary"),
        api.listOutlines(projectId, "volume"),
        api.listOutlines(projectId, "chapter"),
      ]);
      setProject(proj);
      setSettings(ws);
      setSummaries(sums);
      setVolumes(vols);
      setChapters(chs);
      setIsDemo(false);
      if (!chVolume && vols.length > 0) setChVolume(vols[0].id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("登录已过期，请重新登录");
      } else {
        const demoProj =
          demoProjects.find((p) => p.id === projectId) || demoProjects[0];
        setProject(demoProj);
        setSettings(demoWorldSettings);
        setSummaries(demoVolumes.slice(0, 1).map((v) => ({ ...v, outline_type: "summary", id: `demo-sum-${v.id}`, parent_id: null })));
        setVolumes(demoVolumes);
        setChapters(demoChapters);
        if (!chVolume && demoVolumes.length > 0) setChVolume(demoVolumes[0].id);
        setIsDemo(true);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const refreshChapters = useCallback(async () => {
    try {
      const chs = await api.listOutlines(projectId, "chapter");
      setChapters(chs);
      setIsDemo(false);
    } catch {
      setChapters(demoChapters);
      setIsDemo(true);
    }
  }, [projectId]);

  async function handleAddWorldSetting(e: React.FormEvent) {
    e.preventDefault();
    if (!wsName.trim() || !wsContent.trim()) return;
    setWsBusy(true);
    setError("");
    try {
      const created = await api.createWorldSetting(projectId, {
        category: wsCategory,
        name: wsName.trim(),
        content: wsContent.trim(),
        importance: wsImportance,
      });
      setSettings((prev) => [...prev, created]);
      setWsName("");
      setWsContent("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加设定失败");
    } finally {
      setWsBusy(false);
    }
  }

  async function handleDeleteWorldSetting(id: string) {
    setError("");
    try {
      await api.deleteWorldSetting(projectId, id);
      setSettings((prev) => prev.filter((s) => s.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除设定失败");
    }
  }

  async function handleAddVolume(e: React.FormEvent) {
    e.preventDefault();
    if (!volTitle.trim()) return;
    setVolBusy(true);
    setError("");
    try {
      // 后端约束：volume 必须挂在 summary（概要）下；项目尚无概要时自动创建
      let parentId = summaries[0]?.id;
      if (!parentId) {
        const summary = await api.createOutline(projectId, {
          title: volSummary.trim() ? `${volSummary.trim()}（概要）` : "作品概要",
          outline_type: "summary",
          summary: volSummary.trim() || undefined,
          status: "active",
        });
        setSummaries((prev) => [...prev, summary]);
        parentId = summary.id;
      }
      const created = await api.createOutline(projectId, {
        title: volTitle.trim(),
        outline_type: "volume",
        parent_id: parentId,
        summary: volSummary.trim() || undefined,
        sort_order: volumes.length + 1,
        status: "active",
      });
      setVolumes((prev) => [...prev, created]);
      if (!chVolume) setChVolume(created.id);
      setVolTitle("");
      setVolSummary("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建分卷失败");
    } finally {
      setVolBusy(false);
    }
  }

  async function handleAddChapter(e: React.FormEvent) {
    e.preventDefault();
    if (!chTitle.trim()) return;
    if (!chVolume) {
      setError("请先创建分卷，章节需要挂在分卷下");
      return;
    }
    setChBusy(true);
    setError("");
    try {
      const created = await api.createOutline(projectId, {
        title: chTitle.trim(),
        outline_type: "chapter",
        parent_id: chVolume,
        summary: chSummary.trim() || undefined,
        sort_order: chapters.length + 1,
        status: "confirmed",
      });
      await refreshChapters();
      setChTitle("");
      setChSummary("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建章节失败");
    } finally {
      setChBusy(false);
    }
  }

  async function handleExport() {
    if (!projectId) return;
    setExportBusy(true);
    setExportMessage("");
    setError("");
    try {
      const payload: ExportCreateInput = {
        export_format: exportFormat,
        scope: exportScope,
        options: exportInclude,
        outline_id: exportScope === "volume" && exportOutlineId ? exportOutlineId : null,
        chapter_id: exportScope === "chapter" && exportChapterId ? exportChapterId : null,
      };
      const task: ExportTask = await api.createExport(projectId, payload);
      setExportMessage("导出任务已创建，正在处理…");
      let t = task;
      for (let i = 0; i < 90 && (t.status === "pending" || t.status === "processing"); i++) {
        await new Promise((r) => setTimeout(r, 1000));
        t = await api.getExportTask(t.id);
      }
      if (t.status !== "completed") {
        setExportMessage(`导出未完成：${t.error_message || t.status}`);
        return;
      }
      const res = await api.getExportResult(t.id);
      const ext = t.export_format === "text" ? "txt" : t.export_format;
      const fname = `${(project?.title || "project").replace(/[\\/:*?"<>|]/g, "_")}_export.${ext}`;
      const blob = new Blob([res.content], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setExportMessage(`导出完成：${fname}（${res.content_size} 字节）`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    } finally {
      setExportBusy(false);
    }
  }

  const pubLabel = (status: string) => {
    const map: Record<string, string> = {
      draft: "草稿",
      review: "审核中",
      published: "已发布",
      unlisted: "未上架",
      archived: "已归档",
    };
    return map[status] || status;
  };

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载作品…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>
          {project?.title || "作品详情"}
          {isDemo && <span className="demo-badge">演示数据</span>}
        </h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="secondary" onClick={() => navigate("/projects")}>
            ← 返回作品库
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/characters`)}>
            人物库
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/relationships`)}>
            关系
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/timeline`)}>
            时间线
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/suggestions`)}>
            变更确认
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/foreshadows`)}>
            伏笔管理
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/consistency`)}>
            一致性检查
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/tasks`)}>
            AI 任务中心
          </button>
          <button className="secondary" onClick={() => setExportOpen(true)}>
            导出
          </button>
          <button onClick={() => navigate(`/publish?project=${projectId}`)}>去发布</button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      {project && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            {project.genre && <span className="tag">{project.genre}</span>}
            <span className="tag">{pubLabel(project.publication_status)}</span>
            <span className="tag">{project.status === "archived" ? "已归档" : "创作中"}</span>
          </div>
          <p style={{ margin: "4px 0", color: "var(--text-dim)" }}>
            {project.background || "暂无简介"}
          </p>
          {project.world_setting && (
            <p style={{ margin: "4px 0", color: "var(--text-dim)", fontSize: 13 }}>
              世界观：{project.world_setting}
            </p>
          )}
          <p style={{ margin: "4px 0", color: "var(--text-dim)", fontSize: 13 }}>
            创建于 {new Date(project.created_at).toLocaleDateString("zh-CN")} · 更新于{" "}
            {new Date(project.updated_at).toLocaleDateString("zh-CN")}
          </p>
        </div>
      )}

      <div className="detail-grid">
        {/* 作品设定 */}
        <div className="card">
          <h3 className="panel-title">作品设定（{settings.length}）</h3>
          <form onSubmit={handleAddWorldSetting} className="inline-form">
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <div className="field">
                <label>分类</label>
                <select value={wsCategory} onChange={(e) => setWsCategory(e.target.value)}>
                  {CATEGORIES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>重要性</label>
                <select value={wsImportance} onChange={(e) => setWsImportance(e.target.value)}>
                  <option value="normal">普通</option>
                  <option value="high">重要</option>
                  <option value="low">次要</option>
                </select>
              </div>
            </div>
            <div className="field">
              <label>名称 *</label>
              <input value={wsName} onChange={(e) => setWsName(e.target.value)} placeholder="例如：曲率跃迁引擎" required />
            </div>
            <div className="field">
              <label>内容 *</label>
              <textarea value={wsContent} onChange={(e) => setWsContent(e.target.value)} style={{ minHeight: 70 }} required />
            </div>
            <button type="submit" disabled={wsBusy}>
              {wsBusy ? "添加中…" : "+ 添加设定"}
            </button>
          </form>
          <div className="list">
            {settings.length === 0 ? (
              <div className="empty">暂无设定条目</div>
            ) : (
              settings.map((s) => (
                <div key={s.id} className="list-item">
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600 }}>
                      {s.name}
                      <span className="tag" style={{ marginLeft: 8 }}>
                        {CATEGORIES.find((c) => c.value === s.category)?.label || s.category}
                      </span>
                      {s.importance === "high" && (
                        <span className="tag" style={{ marginLeft: 6, color: "var(--warn)" }}>
                          重要
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{s.content}</div>
                  </div>
                  <button
                    className="danger-btn"
                    onClick={() => void handleDeleteWorldSetting(s.id)}
                  >
                    删除
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* 分卷与章节 */}
        <div className="card">
          <h3 className="panel-title">分卷管理（{volumes.length}）</h3>
          <form onSubmit={handleAddVolume} className="inline-form">
            <div className="field">
              <label>分卷名称 *</label>
              <input value={volTitle} onChange={(e) => setVolTitle(e.target.value)} placeholder="例如：第一卷 启航" required />
            </div>
            <div className="field">
              <label>分卷简介</label>
              <input value={volSummary} onChange={(e) => setVolSummary(e.target.value)} />
            </div>
            <button type="submit" disabled={volBusy}>
              {volBusy ? "创建中…" : "+ 新建分卷"}
            </button>
          </form>
          <div className="list">
            {volumes.length === 0 ? (
              <div className="empty">暂无分卷，先创建一个吧</div>
            ) : (
              volumes.map((v) => (
                <div key={v.id} className="list-item">
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600 }}>{v.title}</div>
                    {v.summary && (
                      <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{v.summary}</div>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>

          <h3 className="panel-title" style={{ marginTop: 18 }}>
            章节（{chapters.length}）
          </h3>
          <form onSubmit={handleAddChapter} className="inline-form">
            <div className="field">
              <label>所属分卷 *</label>
              <select value={chVolume} onChange={(e) => setChVolume(e.target.value)}>
                {volumes.length === 0 ? (
                  <option value="">请先创建分卷</option>
                ) : (
                  volumes.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.title}
                    </option>
                  ))
                )}
              </select>
            </div>
            <div className="field">
              <label>章节标题 *</label>
              <input value={chTitle} onChange={(e) => setChTitle(e.target.value)} placeholder="例如：第一章 寂静的信号" required />
            </div>
            <div className="field">
              <label>章节摘要</label>
              <input value={chSummary} onChange={(e) => setChSummary(e.target.value)} />
            </div>
            <button type="submit" disabled={chBusy || volumes.length === 0}>
              {chBusy ? "创建中…" : "+ 新建章节"}
            </button>
          </form>
          <div className="list">
            {chapters.length === 0 ? (
              <div className="empty">暂无章节，创建后即可开始写作</div>
            ) : (
              chapters.map((c) => (
                <div
                  key={c.id}
                  className="list-item chapter-card"
                  onClick={() => navigate(`/projects/${projectId}/chapters/${c.id}`)}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600 }}>{c.title}</div>
                    {c.summary && (
                      <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{c.summary}</div>
                    )}
                  </div>
                  <span className="tag">
                    {c.status === "confirmed" ? "已确认" : c.status === "archived" ? "已归档" : "草稿"}
                  </span>
                  <span style={{ color: "var(--text-dim)", fontSize: 13 }}>写作 →</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* 导出弹窗 */}
      {exportOpen && (
        <div className="modal-overlay" onClick={() => setExportOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">导出作品</div>
            <div className="modal-body">
              {exportMessage && (
                <div className="save-hint" style={{ marginBottom: 12, display: "block" }}>
                  {exportMessage}
                </div>
              )}
              <div className="field">
                <label>导出格式</label>
                <select value={exportFormat} onChange={(e) => setExportFormat(e.target.value)}>
                  <option value="markdown">Markdown</option>
                  <option value="text">TXT</option>
                  <option value="html">HTML</option>
                  <option value="json">项目档案 JSON</option>
                  <option value="csv">设定资料 CSV</option>
                </select>
              </div>
              <div className="field">
                <label>导出范围</label>
                <select value={exportScope} onChange={(e) => setExportScope(e.target.value)}>
                  <option value="whole_project">全文</option>
                  <option value="volume">指定分卷</option>
                  <option value="chapter">指定章节</option>
                </select>
              </div>
              {exportScope === "volume" && (
                <div className="field">
                  <label>选择分卷</label>
                  <select value={exportOutlineId} onChange={(e) => setExportOutlineId(e.target.value)}>
                    <option value="">请选择分卷</option>
                    {volumes.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.title}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {exportScope === "chapter" && (
                <div className="field">
                  <label>选择章节</label>
                  <select value={exportChapterId} onChange={(e) => setExportChapterId(e.target.value)}>
                    <option value="">请选择章节</option>
                    {chapters.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.title}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="field">
                <label>包含项</label>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: 13 }}>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={exportInclude.include_outline_summaries}
                      onChange={(e) =>
                        setExportInclude((p) => ({ ...p, include_outline_summaries: e.target.checked }))
                      }
                    />{" "}
                    大纲与章节摘要
                  </label>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={exportInclude.include_characters}
                      onChange={(e) =>
                        setExportInclude((p) => ({ ...p, include_characters: e.target.checked }))
                      }
                    />{" "}
                    人物卡
                  </label>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={exportInclude.include_ai_records}
                      onChange={(e) =>
                        setExportInclude((p) => ({ ...p, include_ai_records: e.target.checked }))
                      }
                    />{" "}
                    生成记录
                  </label>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={exportInclude.include_version_history}
                      onChange={(e) =>
                        setExportInclude((p) => ({ ...p, include_version_history: e.target.checked }))
                      }
                    />{" "}
                    版本历史
                  </label>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={exportInclude.include_toc}
                      onChange={(e) => setExportInclude((p) => ({ ...p, include_toc: e.target.checked }))}
                    />{" "}
                    目录
                  </label>
                </div>
              </div>
            </div>
            <div className="modal-actions">
              <button className="secondary" onClick={() => setExportOpen(false)}>
                取消
              </button>
              <button onClick={() => void handleExport()} disabled={exportBusy}>
                {exportBusy ? "导出中…" : "开始导出"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
