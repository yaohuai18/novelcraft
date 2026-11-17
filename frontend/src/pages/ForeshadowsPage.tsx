// 文26 P2「人物、关系、时间线和伏笔正式管理」：伏笔管理面板
// 伏笔清单 CRUD + 状态流转（planned/planted/resolved/abandoned）+ 从章节记忆导入
// 每条伏笔显示「来自第几章」的来源链接（埋设 / 计划回收 / 已回收）
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  type ForeshadowData,
  type ForeshadowImportResult,
  type OutlineNode,
} from "../api/client";

type ForeshadowStatus = ForeshadowData["status"];

const STATUS_LABEL: Record<string, string> = {
  planned: "计划中",
  planted: "已埋设",
  resolved: "已回收",
  abandoned: "已废弃",
};

const STATUS_COLOR: Record<string, string> = {
  planned: "#86909c",
  planted: "#1d39c4",
  resolved: "#389e0d",
  abandoned: "#bfbfbf",
};

const STATUS_OPTIONS: ForeshadowStatus[] = ["planned", "planted", "resolved", "abandoned"];

export default function ForeshadowsPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [items, setItems] = useState<ForeshadowData[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  // 新建 / 编辑表单
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formTitle, setFormTitle] = useState("");
  const [formContent, setFormContent] = useState("");
  const [formImportance, setFormImportance] = useState("");
  const [formPlantedChapter, setFormPlantedChapter] = useState("");
  const [formPayoffChapter, setFormPayoffChapter] = useState("");
  const [formResolvedChapter, setFormResolvedChapter] = useState("");
  const [formPayoff, setFormPayoff] = useState("");
  const [formStatus, setFormStatus] = useState<ForeshadowStatus>("planned");
  const [formNote, setFormNote] = useState("");

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
      const [fs, chs] = await Promise.all([
        api.listForeshadows(projectId, statusFilter || undefined),
        api.listOutlines(projectId, "chapter"),
      ]);
      setItems(fs);
      setChapters(chs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载伏笔失败");
    } finally {
      setLoading(false);
    }
  }, [projectId, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setEditingId(null);
    setFormTitle("");
    setFormContent("");
    setFormImportance("");
    setFormPlantedChapter("");
    setFormPayoffChapter("");
    setFormResolvedChapter("");
    setFormPayoff("");
    setFormStatus("planned");
    setFormNote("");
    setError("");
    setShowForm(true);
  }

  function openEdit(f: ForeshadowData) {
    setEditingId(f.id);
    setFormTitle(f.title || "");
    setFormContent(f.content);
    setFormImportance(f.importance || "");
    setFormPlantedChapter(f.planted_chapter_id || "");
    setFormPayoffChapter(f.planned_payoff_chapter_id || "");
    setFormResolvedChapter(f.resolved_chapter_id || "");
    setFormPayoff(f.planned_payoff || "");
    setFormStatus(f.status);
    setFormNote(f.note || "");
    setError("");
    setShowForm(true);
  }

  async function saveForm() {
    if (!formContent.trim()) {
      setError("伏笔内容不能为空");
      return;
    }
    setBusy(true);
    setError("");
    setHint("");
    try {
      const data = {
        title: formTitle.trim() || null,
        content: formContent.trim(),
        importance: formImportance.trim() || null,
        planted_chapter_id: formPlantedChapter || null,
        planned_payoff_chapter_id: formPayoffChapter || null,
        resolved_chapter_id: formResolvedChapter || null,
        planned_payoff: formPayoff.trim() || null,
        status: formStatus,
        source: "manual",
        note: formNote.trim() || null,
      };
      if (editingId) {
        await api.updateForeshadow(projectId, editingId, data);
        setHint("伏笔已更新");
      } else {
        await api.createForeshadow(projectId, data);
        setHint("伏笔已创建");
      }
      setShowForm(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function deleteItem(f: ForeshadowData) {
    if (!window.confirm(`确认删除伏笔「${f.content.slice(0, 30)}」？此操作不可恢复。`)) return;
    setBusy(true);
    setError("");
    setHint("");
    try {
      await api.deleteForeshadow(projectId, f.id);
      setHint("伏笔已删除");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  async function markResolved(f: ForeshadowData) {
    setBusy(true);
    setError("");
    setHint("");
    try {
      await api.updateForeshadow(projectId, f.id, {
        status: "resolved",
        resolved_chapter_id: f.planted_chapter_id || null,
      });
      setHint("已标记为回收");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "标记回收失败");
    } finally {
      setBusy(false);
    }
  }

  async function importFromMemory() {
    setBusy(true);
    setError("");
    setHint("");
    try {
      const res: ForeshadowImportResult = await api.importForeshadows(projectId, {
        mark_planted: true,
        replace_duplicates: false,
      });
      setHint(`已从章节记忆导入 ${res.imported} 条伏笔，跳过重复 ${res.skipped_duplicates} 条`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setBusy(false);
    }
  }

  const openCount = items.filter((f) => f.status === "planted" || f.status === "planned").length;
  const dueCount = items.filter(
    (f) => f.status === "planted" && f.planned_payoff_chapter_id
  ).length;

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载伏笔…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>伏笔管理</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            style={{ padding: "6px 8px" }}
          >
            <option value="">全部状态</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
          <button className="secondary" onClick={() => void importFromMemory()} disabled={busy}>
            从章节记忆导入
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button onClick={openCreate} disabled={busy}>
            新建伏笔
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}
      {hint && <div className="form-hint">{hint}</div>}

      <div className="card" style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", gap: 24, fontSize: 14 }}>
          <span>
            未回收伏笔：<b>{openCount}</b> 条
          </span>
          <span style={{ color: openCount > 0 ? "#cf1322" : "inherit" }}>
            已埋设且计划回收：<b>{dueCount}</b> 条（到期未回收将在一致性检查中提醒）
          </span>
        </div>
      </div>

      <div className="card">
        <h3 className="panel-title">伏笔清单（{items.length}）</h3>
        {items.length === 0 ? (
          <div className="empty">
            暂无伏笔记录。可点击「从章节记忆导入」自动同步已埋设伏笔，或手动新建。
          </div>
        ) : (
          <div className="list">
            {items.map((f) => (
              <div
                key={f.id}
                className="list-item"
                style={{
                  flexDirection: "column",
                  alignItems: "stretch",
                  borderLeft:
                    f.status === "planted" && f.planned_payoff_chapter_id
                      ? "4px solid #faad14"
                      : undefined,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span
                    className="tag"
                    style={{
                      color: "#fff",
                      background: STATUS_COLOR[f.status] || "#86909c",
                      border: "none",
                    }}
                  >
                    {STATUS_LABEL[f.status] || f.status}
                  </span>
                  {f.status === "planted" && f.planned_payoff_chapter_id && (
                    <span className="tag" style={{ color: "#cf1322" }}>
                      到期未回收风险
                    </span>
                  )}
                  {f.importance && (
                    <span className="tag" style={{ marginLeft: 4 }}>
                      重要度：{f.importance}
                    </span>
                  )}
                  <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
                    来源：
                    {f.source === "chapter_memory"
                      ? "章节记忆"
                      : f.source === "outline"
                        ? "规划大纲"
                        : f.source === "ai_check"
                          ? "一致性检查"
                          : "手动"}
                  </span>
                </div>
                <div style={{ flex: 1, fontWeight: 600, marginTop: 6 }}>
                  {f.title || f.content.slice(0, 40)}
                </div>
                {f.title && (
                  <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 2 }}>
                    {f.content}
                  </div>
                )}
                <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 4 }}>
                  埋设：
                  {f.planted_chapter_id ? (
                    <a href={`/projects/${projectId}/chapters/${f.planted_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${f.planted_chapter_id}`); }}>
                      {chapterName(f.planted_chapter_id)}
                    </a>
                  ) : (
                    "—"
                  )}{" "}
                  ｜ 计划回收：
                  {f.planned_payoff_chapter_id ? (
                    <a href={`/projects/${projectId}/chapters/${f.planned_payoff_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${f.planned_payoff_chapter_id}`); }}>
                      {chapterName(f.planned_payoff_chapter_id)}
                    </a>
                  ) : (
                    "—"
                  )}
                  {f.planned_payoff ? `（${f.planned_payoff}）` : ""}
                </div>
                {f.resolved_chapter_id && (
                  <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 2 }}>
                    已回收：
                    <a href={`/projects/${projectId}/chapters/${f.resolved_chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${f.resolved_chapter_id}`); }}>
                      {chapterName(f.resolved_chapter_id)}
                    </a>
                    {f.paid_off_note ? `（${f.paid_off_note}）` : ""}
                  </div>
                )}
                {f.note && (
                  <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
                    备注：{f.note}
                  </div>
                )}
                <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                  {f.status !== "resolved" && f.status !== "abandoned" && (
                    <button className="secondary" onClick={() => void markResolved(f)} disabled={busy}>
                      标记回收
                    </button>
                  )}
                  <button className="secondary" onClick={() => openEdit(f)} disabled={busy}>
                    编辑
                  </button>
                  <button
                    className="danger-btn"
                    onClick={() => void deleteItem(f)}
                    disabled={busy}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {showForm && (
        <div className="drawer-overlay" onClick={() => setShowForm(false)}>
          <div className="check-drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-header">
              <span className="drawer-title">{editingId ? "编辑伏笔" : "新建伏笔"}</span>
              <button className="drawer-close" onClick={() => setShowForm(false)}>×</button>
            </div>
            <div className="drawer-body">
              {error && <div className="error-box">{error}</div>}
              <div className="field">
                <label>伏笔标题（可选）</label>
                <input
                  value={formTitle}
                  onChange={(e) => setFormTitle(e.target.value)}
                  placeholder="如：玉佩暗藏身世之谜"
                />
              </div>
              <div className="field">
                <label>伏笔内容 *</label>
                <textarea
                  value={formContent}
                  onChange={(e) => setFormContent(e.target.value)}
                  rows={3}
                  placeholder="描述伏笔内容，如：主角幼年时收到的玉佩暗藏玄机"
                />
              </div>
              <div className="field">
                <label>重要程度（可选）</label>
                <input
                  value={formImportance}
                  onChange={(e) => setFormImportance(e.target.value)}
                  placeholder="如：高 / 中 / 低 或自定义"
                />
              </div>
              <div className="field">
                <label>埋设章节</label>
                <select
                  value={formPlantedChapter}
                  onChange={(e) => setFormPlantedChapter(e.target.value)}
                >
                  <option value="">— 未指定 —</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>计划回收章节</label>
                <select
                  value={formPayoffChapter}
                  onChange={(e) => setFormPayoffChapter(e.target.value)}
                >
                  <option value="">— 未指定 —</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>已回收章节</label>
                <select
                  value={formResolvedChapter}
                  onChange={(e) => setFormResolvedChapter(e.target.value)}
                >
                  <option value="">— 未指定 —</option>
                  {chapters.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>计划回收方式（可选）</label>
                <input
                  value={formPayoff}
                  onChange={(e) => setFormPayoff(e.target.value)}
                  placeholder="如：玉佩在决战时揭示身世"
                />
              </div>
              <div className="field">
                <label>状态</label>
                <select value={formStatus} onChange={(e) => setFormStatus(e.target.value as ForeshadowStatus)}>
                  {STATUS_OPTIONS.map((s) => (
                    <option key={s} value={s}>
                      {STATUS_LABEL[s]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>备注（可选）</label>
                <input
                  value={formNote}
                  onChange={(e) => setFormNote(e.target.value)}
                  placeholder="补充说明"
                />
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <button onClick={() => void saveForm()} disabled={busy}>
                  {editingId ? "保存修改" : "创建"}
                </button>
                <button className="secondary" onClick={() => setShowForm(false)}>
                  取消
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
