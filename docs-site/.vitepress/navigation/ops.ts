import type { DefaultTheme } from "vitepress";

export const opsSidebar: DefaultTheme.SidebarItem[] = [
  {
    text: "部署环境",
    items: [
      { text: "概览", link: "/ops/" },
      { text: "选择部署方式", link: "/ops/deploy/" },
      { text: "开发部署（本地）", link: "/ops/deploy/development" },
      { text: "生产部署（Docker Compose）", link: "/ops/deploy/docker-compose" },
      { text: "局域网生产部署", link: "/ops/deploy/lan-production" },
      { text: "端口暴露与网络安全", link: "/ops/deploy/network-security" },
    ],
  },
  {
    text: "升级维护",
    collapsed: true,
    items: [{ text: "升级指南", link: "/ops/upgrade-guide" }],
  },
  {
    text: "监控与安全",
    collapsed: true,
    items: [
      { text: "监控与告警", link: "/ops/observability/" },
      { text: "安全模型", link: "/ops/security/" },
    ],
  },
  {
    text: "故障处理",
    collapsed: true,
    items: [
      { text: "Celery Worker 卡死", link: "/ops/runbooks/celery-worker-stuck" },
      { text: "GPU 显存仲裁验收", link: "/ops/runbooks/gpu-arbitration-acceptance" },
      { text: "ML Backend 不可用", link: "/ops/runbooks/ml-backend-down" },
      { text: "视频帧服务", link: "/ops/runbooks/video-frame-service" },
      { text: "图片金字塔", link: "/ops/runbooks/image-pyramid" },
      { text: "PG 连接池耗尽", link: "/ops/runbooks/postgres-connection-pool-exhausted" },
      { text: "员工项目角色迁移与回滚", link: "/ops/runbooks/project-role-migration" },
    ],
  },
];
