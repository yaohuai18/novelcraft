// 文26 P2「人物、关系、时间线和伏笔正式管理」：时间线管理页
// 按时间/章节排序的事件列表，支持新建/编辑/删除/确认标记/状态筛选
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type OutlineNode,
  type TimelineEventData,
  type TimelineEventInput,
} from "../api/client";

const TL_STATUS_OPTIONS: Array<"draft" | "confirmed" | "archived"> = ["draft", "confirmed", "archived"];
const TL_STATUS_LABEL: Record<string, string> = {
  draft: "待确认",
  confirmed: "已确认",
  archived: "已归档",
};

export default function TimelinePage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();

  const [events, setEvents] = useState<TimelineEventData[]>([]);
  const [chapters, setChapters] = useState<OutlineNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<TimelineEventData | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const emptyForm: TimelineEventInput = {
    chapter_id: null,
    event_date_label: "",
    time_label: "",
    sequence_order: null,
    location_id: null,
    participants: [],
    title: "",
    description: "",
    source: "manual",
    confirmed: false,
    status: "draft",
  };
  const [form, setForm] = useState<TimelineEventInput>(emptyForm);
  const [participantsText, setParticipantsText] = useState("");

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
      const [evs, chs] = await Promise.all([
        api.listTimelineEvents(projectId),
        api.listOutlines(projectId, "chapter"),
      ]);
      setEvents(evs);
      setChapters(chs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载时间线失败");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  function startEdit(ev: TimelineEventData) {
    setEditing(ev);
    setForm({
      chapter_id: ev.chapter_id || null,
      event_date_label: ev.event_date_label || "",
      time_label: ev.time_label || "",
      sequence_order: ev.sequence_order ?? null,
      location_id: ev.location_id || null,
      participants: ev.participants || [],
      title: ev.title,
      description: ev.description || "",
      source: ev.source,
      confirmed: ev.confirmed,
      status: ev.status || "draft",
    });
    setParticipantsText((ev.participants || []).join("、"));
  }

  function startCreate() {
    setEditing(null);
    setForm(emptyForm);
    setParticipantsText("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.title.trim()) {
      setError("事件标题必填");
      return;
    }
    setBusy(true);
    setError("");
    const participants = participantsText
      .split(/[、,，;；]/)
      .map((s) => s.trim())
      .filter(Boolean);
    const payload: TimelineEventInput = {
      ...form,
      title: form.title.trim(),
      participants,
      sequence_order: form.sequence_order === null ? null : Number(form.sequence_order) || null,
      chapter_id: form.chapter_id || null,
      location_id: form.location_id || null,
    };
    try {
      if (editing) {
        const updated = await api.updateTimelineEvent(projectId, editing.id, payload);
        setEvents((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      } else {
        const created = await api.createTimelineEvent(projectId, payload);
        setEvents((prev) => [...prev, created]);
      }
      setForm(emptyForm);
      setParticipantsText("");
      setEditing(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存事件失败");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该时间线事件？")) return;
    setError("");
    try {
      await api.deleteTimelineEvent(projectId, id);
      setEvents((prev) => prev.filter((x) => x.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除事件失败");
    }
  }

  async function confirmEvent(ev: TimelineEventData) {
    setError("");
    try {
      const updated = await api.confirmTimelineEvent(projectId, ev.id);
      setEvents((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认事件失败");
    }
  }

  if (loading) {
    return (
      <div className="empty">
        <span className="spin" /> 正在加载时间线…
      </div>
    );
  }

  return (
    <div>
      <div className="topbar">
        <h1>时间线管理</h1>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}`)}>
            ← 返回作品
          </button>
          <button className="secondary" onClick={() => navigate(`/projects/${projectId}/characters`)}>
            人物库
          </button>
          <button onClick={() => navigate(`/projects/${projectId}/suggestions`)}>
            变更确认
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      <div className="card" style={{ marginBottom: 18 }}>
        <h3 className="panel-title">{editing ? `编辑事件：${editing.title}` : "新建时间线事件"}</h3>
        <form onSubmit={submit}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
            <div className="field">
              <label>事件标题 *</label>
              <input
                value={form.title}
                onChange={(e) => setForm((p) => ({ ...p, title: e.target.value }))}
                required
              />
            </div>
            <div className="field">
              <label>日期标签（如 景历二十三年秋）</label>
              <input
                value={form.event_date_label || ""}
                onChange={(e) => setForm((p) => ({ ...p, event_date_label: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>时间标签</label>
              <input
                value={form.time_label || ""}
                onChange={(e) => setForm((p) => ({ ...p, time_label: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>排序序号</label>
              <input
                type="number"
                value={form.sequence_order ?? ""}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    sequence_order: e.target.value === "" ? null : Number(e.target.value),
                  }))
                }
              />
            </div>
            <div className="field">
              <label>所属章节</label>
              <select
                value={form.chapter_id || ""}
                onChange={(e) => setForm((p) => ({ ...p, chapter_id: e.target.value || null }))}
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
              <label>参与者（顿号分隔）</label>
              <input
                value={participantsText}
                onChange={(e) => setParticipantsText(e.target.value)}
              />
            </div>
            <div className="field">
              <label>地点 ID（可选）</label>
              <input
                value={form.location_id || ""}
                onChange={(e) => setForm((p) => ({ ...p, location_id: e.target.value || null }))}
              />
            </div>
            <div className="field">
              <label>状态</label>
              <select
                value={form.status || "draft"}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    status: e.target.value as "draft" | "confirmed" | "archived",
                  }))
                }
              >
                {TL_STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {TL_STATUS_LABEL[s]}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>事件描述</label>
            <textarea
              value={form.description || ""}
              onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
              style={{ minHeight: 60 }}
            />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" disabled={busy}>
              {busy ? "保存中…" : editing ? "保存修改" : "创建事件"}
            </button>
            {editing && (
              <button type="button" className="secondary" onClick={startCreate}>
                取消编辑
              </button>
            )}
          </div>
        </form>
      </div>

      <div className="card">
        <h3 className="panel-title">
          事件列表（{events.length}）
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            style={{ marginLeft: 10, maxWidth: 160 }}
          >
            <option value="">全部状态</option>
            {TL_STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {TL_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </h3>
        <div className="list">
          {events.length === 0 ? (
            <div className="empty">暂无时间线事件</div>
          ) : (
            events
              .filter((ev) => (statusFilter ? (ev.status || "draft") === statusFilter : true))
              .map((ev) => (
                <div key={ev.id} className="list-item">
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600 }}>
                      {ev.title}
                      <span className="tag" style={{ marginLeft: 8 }}>
                        {ev.event_date_label || "未标注日期"}
                      </span>
                      {ev.time_label && (
                        <span className="tag" style={{ marginLeft: 4 }}>
                          {ev.time_label}
                        </span>
                      )}
                      <span className="tag" style={{ marginLeft: 4 }}>
                        {TL_STATUS_LABEL[ev.status || "draft"] || "待确认"}
                      </span>
                      {(ev.status === "draft" || (!ev.status && !ev.confirmed)) && (
                        <button
                          className="secondary"
                          style={{ marginLeft: 8, padding: "1px 8px" }}
                          onClick={() => void confirmEvent(ev)}
                        >
                          确认
                        </button>
                      )}
                    </div>
                    {ev.description && (
                      <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{ev.description}</div>
                    )}
                    <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                      章节：
                      {ev.chapter_id ? (
                        <a href={`/projects/${projectId}/chapters/${ev.chapter_id}`} onClick={(e) => { e.preventDefault(); navigate(`/projects/${projectId}/chapters/${ev.chapter_id}`); }}>
                          {chapterName(ev.chapter_id)}
                        </a>
                      ) : (
                        "—"
                      )}{" "}
                      · 顺序：
                      {ev.sequence_order ?? "—"} · 参与者：
                      {(ev.participants || []).join("、") || "—"} · 来源：
                      {ev.source === "ai_extracted" ? "AI提取" : "手工"}
                    </div>
                  </div>
                  <button className="secondary" onClick={() => startEdit(ev)}>
                    编辑
                  </button>
                  <button className="danger-btn" onClick={() => void remove(ev.id)}>
                    删除
                  </button>
                </div>
              ))
          )}
        </div>
      </div>
    </div>
  );
}
