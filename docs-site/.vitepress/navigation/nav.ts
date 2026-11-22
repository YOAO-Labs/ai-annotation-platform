import type { DefaultTheme } from "vitepress";

export const nav: DefaultTheme.NavItem[] = [
  { text: "使用指南", link: "/user-guide/", activeMatch: "^/user-guide/" },
  { text: "开发文档", link: "/dev/", activeMatch: "^/dev/" },
  { text: "API", link: "/api/", activeMatch: "^/api/" },
  { text: "部署运维", link: "/ops/", activeMatch: "^/ops/" },
  {
    text: "更多",
    items: [
      { text: "更新日志", link: "/changelog/" },
      { text: "路线图", link: "/roadmap/" },
    ],
  },
  { text: "快速开始", link: "/user-guide/getting-started" },
];
