// 生成记录抽屉（批次 6 拆分：纯展示组件）
import type { GenerationRecord } from "../../api/client";
import { ACTION_TYPE_LABEL, formatTime } from "./labels";

export function GenerationRecordsDrawer({
  open,
  records,
  loading,
  error,
  expandedId,
  versionMap,
  traceChapter,
  onToggle,
  onClose,
}: {
  open: boolean;
  records: GenerationRecord[];
  loading: boolean;
  error: string;
  expandedId: string | null;
  versionMap: Record<string, number>;
  traceChapter: (g: GenerationRecord) => number;
  onToggle: (id: string | null) => void;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span className="drawer-title">生成记录（本项目）</span>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>
        <div className="drawer-body">
          {error && <div className="error-box">{error}</div>}
          {loading ? (
            <div className="empty">
              <span className="spin" /> 加载中…
            </div>
          ) : records.length === 0 ? (
            <div className="empty">暂无生成记录</div>
          ) : (
            <div className="gen-list">
              {records.map((g) => (
                <div key={g.id} className="gen-item">
                  <div
                    className="gen-item-head"
                    onClick={() => onToggle(expandedId === g.id ? null : g.id)}
                  >
                    <div className="drawer-item-title">
                      {ACTION_TYPE_LABEL[g.action_type] || g.action_type}
                      <span className={`gen-status gen-status-${g.status}`}>
                        {g.status === "succeeded" ? "成功" : g.status === "failed" ? "失败" : g.status === "cancelled" ? "已取消" : "进行中"}
                      </span>
                    </div>
                    <div className="drawer-item-meta">
                      {g.model_name || g.model_provider || "未知模型"} · {formatTime(g.created_at)}
                      {g.input_tokens != null && g.output_tokens != null
                        ? ` · ${g.input_tokens}→${g.output_tokens} tokens`
                        : ""}
                    </div>
                  </div>
                  {expandedId === g.id && (
                    <div className="gen-detail">
                      {g.error_message && <div className="error-box">{g.error_message}</div>}
                      <div className="gen-detail-row">
                        <span className="gen-detail-label">系统提示词</span>
                        <pre className="gen-detail-pre">{g.system_prompt || "（无）"}</pre>
                      </div>
                      <div className="gen-detail-row">
                        <span className="gen-detail-label">用户提示词</span>
                        <pre className="gen-detail-pre">{g.user_prompt || "（无）"}</pre>
                      </div>
                      <div className="gen-detail-row">
                        <span className="gen-detail-label">上下文快照</span>
                        <pre className="gen-detail-pre">
                          {g.context_snapshot ? g.context_snapshot.slice(0, 600) + (g.context_snapshot.length > 600 ? "\n…（已截断）" : "") : "（无）"}
                        </pre>
                      </div>
                      <div className="gen-detail-row">
                        <span className="gen-detail-label">生成结果</span>
                        <pre className="gen-detail-pre">
                          {g.response_content ? g.response_content.slice(0, 400) + (g.response_content.length > 400 ? "\n…（已截断）" : "") : "（无）"}
                        </pre>
                      </div>
                      <div className="gen-detail-row">
                        <span className="gen-detail-label">参数</span>
                        <div className="gen-detail-pre">
                          temperature={g.temperature ?? "-"} · max_tokens={g.max_tokens ?? "-"} · provider={g.model_provider ?? "-"}
                        </div>
                      </div>
                      {(g.outline_plan_version_id || g.chapter_plan_id || g.title_snapshot) && (
                        <div className="gen-detail-row">
                          <span className="gen-detail-label">依据（已确认大纲）</span>
                          <pre className="gen-detail-pre">
                            {(() => {
                              const tn = traceChapter(g);
                              const vn = g.outline_plan_version_id ? versionMap[g.outline_plan_version_id] : null;
                              const lines: string[] = [];
                              if (g.outline_plan_version_id) {
                                lines.push(`依据：已确认大纲 ${vn ? `v${vn}` : `#${g.outline_plan_version_id.slice(0, 8)}`}`);
                              }
                              if (g.chapter_plan_id) {
                                lines.push(
                                  `章节：第 ${tn || "?"} 章${g.title_snapshot ? `《${g.title_snapshot}》` : ""}（chapterId ${g.chapter_plan_id.slice(0, 8)}…）`
                                );
                              } else if (g.title_snapshot) {
                                lines.push(`章节：${g.title_snapshot}`);
                              }
                              return lines.length > 0 ? lines.join("\n") : "（旧记录无版本追溯）";
                            })()}
                          </pre>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
