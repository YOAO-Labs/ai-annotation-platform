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
      { text: "任务与标注", link: "/api/guides/tasks-and-annotations" },
      { text: "预测与作业", link: "/api/guides/predictions" },
    ],
  },
  {
    text: "数据集与存储",
    collapsed: true,
    items: [
      { text: "数据集上传与扫描", link: "/api/guides/datasets" },
      { text: "目录来源与观测扫描", link: "/api/guides/dataset-sources" },
      { text: "受控存储位置与迁移", link: "/api/guides/storage-locations" },
      { text: "存储连接器", link: "/api/guides/storage-connections" },
      { text: "内容核验与严格读取", link: "/api/guides/verified-content" },
      { text: "只读站点 Agent", link: "/api/guides/site-agents" },
      { text: "视频派生媒体", link: "/api/guides/video-derivatives" },
    ],
  },
  {
    text: "异步任务与导出",
    collapsed: true,
    items: [
      { text: "异步任务", link: "/api/guides/async-jobs" },
      { text: "视频追踪作业", link: "/api/guides/video-tracker-jobs" },
      { text: "导出", link: "/api/guides/export" },
      { text: "预测导入", link: "/api/guides/import" },
    ],
  },
  {
    text: "模型、训练与治理",
    collapsed: true,
    items: [
      { text: "在线学习与模型训练", link: "/api/guides/training-datasets" },
      { text: "ML Backend", link: "/api/guides/ml-backend" },
      { text: "WebSocket", link: "/api/guides/websocket" },
      { text: "系统设置", link: "/api/guides/system-settings" },
      { text: "审计归档", link: "/api/guides/audit-archives" },
      { text: "BUG 反馈", link: "/api/guides/bug-reports" },
    ],
  },
  {
    text: "完整参考",
    collapsed: true,
    items: [{ text: "全部路由索引", link: "/api/guides/_routes.generated" }],
  },
];
