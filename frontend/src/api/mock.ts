// 演示数据兜底：后端不可用时展示，并标注「演示数据」
import type { Project, OutlineNode, ChapterContent, WorldSetting, PreviewProject, LocaleInfo, NotificationPref } from "./client";

export const demoProjects: Project[] = [
  {
    id: "demo-proj-0001",
    title: "星海彼岸",
    genre: "科幻",
    background: "人类在深空殖民时代，一艘失联多年的殖民船突然发回信号。",
    world_setting: "公元 2287 年，近地轨道与比邻星系之间",
    main_conflict: "信号来源究竟是幸存者，还是另一种存在？",
    narrative_perspective: "第三人称",
    writing_style: "冷峻写实",
    status: "active",
    publication_status: "draft",
    created_at: "2026-09-01T10:00:00",
    updated_at: "2026-09-20T10:00:00",
  },
  {
    id: "demo-proj-0002",
    title: "长安夜行录",
    genre: "古风悬疑",
    background: "盛唐长安，一场连环失踪案牵出朝堂秘辛。",
    world_setting: "开元年间长安城",
    main_conflict: "办案的年轻捕快发现真相与皇权有关",
    narrative_perspective: "第一人称",
    writing_style: "细腻古雅",
    status: "active",
    publication_status: "draft",
    created_at: "2026-08-12T10:00:00",
    updated_at: "2026-09-25T10:00:00",
  },
  {
    id: "demo-proj-0003",
    title: "异世界的便利店",
    genre: "奇幻日常",
    background: "深夜便利店店员被传送到异世界，继续经营他的小店。",
    world_setting: "魔法大陆的边境小镇",
    main_conflict: "异世界的冒险者与魔王军都爱来店里买关东煮",
    narrative_perspective: "第三人称",
    writing_style: "轻松幽默",
    status: "active",
    publication_status: "draft",
    created_at: "2026-09-10T10:00:00",
    updated_at: "2026-09-28T10:00:00",
  },
];

export const demoChapters: OutlineNode[] = [
  {
    id: "demo-ch-0001",
    project_id: "demo-proj-0001",
    parent_id: "demo-vol-0001",
    title: "第一章 寂静的信号",
    outline_type: "chapter",
    summary: "观测站接收到来自柯伊伯带深处的重复信号。",
    content: "",
    sort_order: 1,
    status: "confirmed",
    visibility: "private",
    chapter_slug: null,
    published_at: null,
  },
  {
    id: "demo-ch-0002",
    project_id: "demo-proj-0001",
    parent_id: "demo-vol-0001",
    title: "第二章 启航",
    outline_type: "chapter",
    summary: "舰队出发，前往信号源。",
    content: "",
    sort_order: 2,
    status: "draft",
    visibility: "private",
    chapter_slug: null,
    published_at: null,
  },
  {
    id: "demo-ch-0003",
    project_id: "demo-proj-0001",
    parent_id: "demo-vol-0001",
    title: "第三章 深渊回响",
    outline_type: "chapter",
    summary: "抵达信号源，发现殖民船残骸。",
    content: "",
    sort_order: 3,
    status: "draft",
    visibility: "private",
    chapter_slug: null,
    published_at: null,
  },
];

export const demoVolumes: OutlineNode[] = [
  {
    id: "demo-vol-0001",
    project_id: "demo-proj-0001",
    parent_id: null,
    title: "第一卷 启航",
    outline_type: "volume",
    summary: "观测站发现异常信号，舰队远征。",
    content: "",
    sort_order: 1,
    status: "active",
    visibility: "private",
    chapter_slug: null,
    published_at: null,
  },
  {
    id: "demo-vol-0002",
    project_id: "demo-proj-0001",
    parent_id: null,
    title: "第二卷 深渊",
    outline_type: "volume",
    summary: "抵达信号源，真相逐渐浮出水面。",
    content: "",
    sort_order: 2,
    status: "active",
    visibility: "private",
    chapter_slug: null,
    published_at: null,
  },
];

export const demoContent: ChapterContent = {
  outline_id: "demo-ch-0001",
  content:
    "观测站的警报在凌晨三点十七分响起。\n\n林晚揉着干涩的眼睛看向屏幕，那段信号已经重复播放了整整七个小时，像是一颗心脏在冰冷的宇宙深处跳动。\n\n「三千光年外的回声」，她在值班日志上写下这几个字，然后按下了通讯键。",
  word_count: 86,
  version: 1,
};

export const demoWorldSettings: WorldSetting[] = [
  {
    id: "demo-ws-0001",
    project_id: "demo-proj-0001",
    category: "location",
    name: "柯伊伯带观测站",
    content: "位于太阳系边缘的深空监测前哨，负责监听远距离信号。",
    importance: "high",
  },
  {
    id: "demo-ws-0002",
    project_id: "demo-proj-0001",
    category: "faction",
    name: "深空殖民联合会",
    content: "负责组织深空移民与远征的地球联合机构。",
    importance: "normal",
  },
  {
    id: "demo-ws-0003",
    project_id: "demo-proj-0001",
    category: "power",
    name: "曲率跃迁引擎",
    content: "允许舰队进行超光速航行的核心科技。",
    importance: "high",
  },
];

export const demoPreview: PreviewProject = {
  project_id: "demo-proj-0001",
  title: "星海彼岸",
  subtitle: "深空殖民时代的一封回信",
  description: "人类在深空殖民时代，一艘失联多年的殖民船突然发回信号。",
  cover_text: "星海彼岸 · 科幻长篇小说",
  genre: "科幻",
  publication_status: "draft",
  slug: null,
  preview: true,
};

export const demoLocale: LocaleInfo = {
  ui_language: "zh-CN",
  content_language: "zh-CN",
  notification_language: "zh-CN",
  timezone: "Asia/Shanghai",
  locale: "zh-CN",
  date_format: "YYYY-MM-DD",
  time_format: "HH:mm",
  region: "CN",
};

export const demoPrefs: NotificationPref[] = [
  { user_id: "demo-user", notification_type: "comment", in_app_enabled: true, email_enabled: false, push_enabled: false },
  { user_id: "demo-user", notification_type: "collaboration", in_app_enabled: true, email_enabled: true, push_enabled: false },
  { user_id: "demo-user", notification_type: "system", in_app_enabled: true, email_enabled: true, push_enabled: false },
  { user_id: "demo-user", notification_type: "ai_task", in_app_enabled: true, email_enabled: false, push_enabled: true },
];
