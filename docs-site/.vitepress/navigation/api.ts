import type { DefaultTheme } from "vitepress";

export const apiSidebar: DefaultTheme.SidebarItem[] = [
  {
    text: "开始接入",
    items: [
      { text: "API 总览", link: "/api/" },
      { text: "认证", link: "/api/guides/auth" },
    ],
  },
  {
    text: "资源与标注",
    collapsed: true,
    items: [
      { text: "项目", link: "/api/guides/projects" },
      { text: "独立素材目录", link: "/api/guides/assets-catalog" },
      { text: "独立视频媒体", link: "/api/guides/assets-media" },
      { text: "场景、位姿与标定", link: "/api/guides/assets-spatial" },
      { text: "任务与标注", link: "/api/guides/tasks-and-annotations" },
      { text: "预测与作业", link: "/api/guides/predictions" },
    ],
  },
  {
    text: "异步任务",
    collapsed: true,
    items: [
      { text: "异步任务", link: "/api/guides/async-jobs" },
      { text: "视频追踪作业", link: "/api/guides/video-tracker-jobs" },
    ],
  },
  {
    text: "模型与集成",
    collapsed: true,
    items: [
      { text: "ML Backend", link: "/api/guides/ml-backend" },
      { text: "Inference runtime 与命名编排", link: "/api/guides/inference-runtime" },
      { text: "独立素材探测", link: "/api/guides/media-probe" },
      { text: "预测导入", link: "/api/guides/import" },
      { text: "WebSocket", link: "/api/guides/websocket" },
      { text: "导出", link: "/api/guides/export" },
      { text: "在线学习与模型训练", link: "/api/guides/training-datasets" },
      { text: "存储连接器", link: "/api/guides/storage-connections" },
      { text: "审计运行时", link: "/api/guides/audit-runtime" },
      { text: "系统设置", link: "/api/guides/system-settings" },
    ],
  },
  {
    text: "完整参考",
    collapsed: true,
    items: [{ text: "全部路由索引", link: "/api/guides/_routes.generated" }],
  },
];
