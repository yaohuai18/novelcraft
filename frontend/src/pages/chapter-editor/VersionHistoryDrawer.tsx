// 历史版本抽屉（批次 6 拆分：纯展示组件）
import type { ChapterVersion } from "../../api/client";
import { SOURCE_LABEL, formatTime } from "./labels";

export interface DiffLine {
  kind: "same" | "removed" | "added" | "changed";
  line: string;
}

export function VersionHistoryDrawer({
  open,
  title,
  versions,
  loading,
  error,
  busy,
  selected,
  currentVersion,
  diffLines,
  onSelect,
  onRestore,
  onClose,
}: {
  open: boolean;
  title: string;
  versions: ChapterVersion[];
  loading: boolean;
  error: string;
  busy: boolean;
  selected: ChapterVersion | null;
  currentVersion: number;
  diffLines: DiffLine[];
  onSelect: (v: ChapterVersion) => void;
  onRestore: (id: string) => void;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span className="drawer-title">历史版本（{title}）</span>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>
        <div className="drawer-body">
          {error && <div className="error-box">{error}</div>}
          {loading ? (
            <div className="empty">
              <span className="spin" /> 加载中…
            </div>
          ) : versions.length === 0 ? (
            <div className="empty">暂无版本记录</div>
          ) : (
            <div className="drawer-split">
              <div className="drawer-col">
                {versions.map((v) => (
                  <div
                    key={v.id}
                    className={`drawer-item ${selected?.id === v.id ? "active" : ""}`}
                    onClick={() => onSelect(v)}
                  >
                    <div className="drawer-item-title">
                      v{v.version}
                      {v.is_current && <span className="current-badge">当前</span>}
                    </div>
                    <div className="drawer-item-meta">
                      {SOURCE_LABEL[v.source] || v.source} · {v.word_count} 字 · {formatTime(v.created_at)}
                    </div>
                    {v.change_summary && <div className="drawer-item-meta">{v.change_summary}</div>}
                  </div>
                ))}
              </div>
              <div className="drawer-col drawer-preview">
                {selected ? (
                  <>
                    <div className="drawer-preview-head">
                      <span>
                        v{selected.version} · {SOURCE_LABEL[selected.source] || selected.source} ·{" "}
                        {selected.word_count} 字
                      </span>
                      <button
                        className="secondary"
                        disabled={busy || selected.is_current}
                        onClick={() => void onRestore(selected.id)}
                      >
                        {busy ? "恢复中…" : selected.is_current ? "已是当前" : "恢复为新版本"}
                      </button>
                    </div>
                    <div className="diff-view">
                      <div className="diff-title">与当前版本（v{currentVersion}）对比</div>
                      {diffLines.map((l, i) => (
                        <div key={i} className={`diff-line diff-${l.kind}`}>
                          <span className="diff-mark">
                            {l.kind === "added" ? "+" : l.kind === "removed" ? "−" : l.kind === "changed" ? "±" : " "}
                          </span>
                          <span>{l.line}</span>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="empty">选择左侧版本查看内容</div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
