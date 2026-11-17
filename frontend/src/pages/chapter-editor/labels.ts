// 章节编辑页共享常量与格式化（批次 6 拆分：主页面与抽屉组件共用）
export const ACTION_TYPE_LABEL: Record<string, string> = {
  outline: "大纲",
  draft: "起草",
  continue: "续写",
  rewrite: "改写",
  polish: "润色",
  summarize: "摘要",
  extract_state: "状态提取",
  check: "一致性检查",
};

export const SOURCE_LABEL: Record<string, string> = {
  manual: "手工保存",
  ai_draft: "AI 起草",
  ai_continue: "AI 续写",
  ai_rewrite: "AI 改写",
  ai_polish: "AI 润色",
  restore: "恢复版本",
};

export function formatTime(iso: string | undefined | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
