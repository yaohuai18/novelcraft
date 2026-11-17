// 追读力分析抽屉（批次 4 功能，批次 6 拆出）
import type { ReadabilityReport } from "../../api/client";

export function ReadabilityDrawer({
  report,
  onClose,
}: {
  report: ReadabilityReport | null;
  onClose: () => void;
}) {
  if (!report) return null;
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span className="drawer-title">追读力分析 · {report.project_title}</span>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>
        <div className="drawer-body">
          <div className="drawer-item-meta">共 {report.chapters_total} 章（规则版分析，零 AI 成本）</div>
          <div className="drawer-item-title">① 钩子覆盖率</div>
          <div className="drawer-item-meta">
            带章末悬念：{report.hook_coverage.chapters_with_hook}/{report.chapters_total}（
            {(report.hook_coverage.rate * 100).toFixed(0)}%）
          </div>
          {report.hook_coverage.without_hook.length > 0 && (
            <div className="drawer-item-meta warn-text">
              无钩子章节：{report.hook_coverage.without_hook.join("、")}
            </div>
          )}
          <div className="drawer-item-title">② 爽点密度</div>
          <div className="drawer-item-meta">
            信号词共 {report.thrill_density.total_hits} 次，
            密度 {report.thrill_density.per_1000_chars}/千字
          </div>
          {report.thrill_density.top_chapters.length > 0 && (
            <div className="drawer-item-meta">
              最密集：{report.thrill_density.top_chapters
                .map((t) => `${t.title}(${t.hits}次)`)
                .join("、")}
            </div>
          )}
          <div className="drawer-item-title">③ 伏笔债务（未回收 {report.foreshadow_debt.open_count} 条）</div>
          {report.foreshadow_debt.items.length === 0 ? (
            <div className="drawer-item-meta">无未回收伏笔</div>
          ) : (
            <ul>
              {report.foreshadow_debt.items.map((f, i) => (
                <li key={i} style={{ fontSize: 13 }}>
                  《{f.title}》[{f.importance}]
                  {f.planned_payoff ? ` — 计划回收：${f.planned_payoff}` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
