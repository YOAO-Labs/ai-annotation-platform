import { defineConfig, type DefaultTheme } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { rewrites, srcExclude } from "./content.mjs";
import { apiSidebar } from "./navigation/api";
import { createDevSidebar } from "./navigation/dev";
import { nav } from "./navigation/nav";
import { opsSidebar } from "./navigation/ops";
import { userGuideSidebar } from "./navigation/user-guide";

const __here = dirname(fileURLToPath(import.meta.url));
function loadSidebar(rel: string): DefaultTheme.SidebarItem[] {
  const p = resolve(__here, rel);
  return existsSync(p) ? (JSON.parse(readFileSync(p, "utf8")) as DefaultTheme.SidebarItem[]) : [];
}

const adrSidebarItems = loadSidebar("../dev/adr/sidebar.generated.json");
const changelogSidebarItems = loadSidebar("../changelog/sidebar.generated.json");
const roadmapSidebarItems = loadSidebar("../roadmap/sidebar.generated.json");

export default withMermaid(
  defineConfig({
    title: "YOAO",
    description: "标注平台文档（用户 / 开发 / API）",
    lang: "zh-CN",
    base: process.env.DOCS_BASE ?? "/",
    cleanUrls: true,
    lastUpdated: true,
    rewrites,
    srcExclude,
    head: [
      ["link", { rel: "icon", type: "image/svg+xml", href: "/ai-annotation-platform-icon.svg" }],
    ],
    sitemap: {
      hostname: "https://yoao-labs.github.io/ai-annotation-platform/",
      transformItems: (items) =>
        items.filter((item) => !item.url.includes("user-guide/projects/annotation-guide")),
    },
    ignoreDeadLinks: [
      /^https?:\/\/localhost(:\d+)?(\/|$)/,
      (url) => /\.(tsx?|py|json|ya?ml|sh|toml|Dockerfile\w*)$/.test(url),
      (url) => /\/(apps|infra)\//.test(url),
      (url) =>
        /(^|\/)\.\.\/plans\/(archive\/)?\d{4}-/.test(url) ||
        /\/plans\/(archive\/)?\d{4}-/.test(url),
      (url) => /\/cvat\//.test(url) || /\/cvat-(sdk|cli)(\/|$)/.test(url),
    ],

    vite: {
      resolve: {
        alias: [{ find: /^dayjs\/dayjs\.min\.js$/, replacement: "dayjs/esm/index.js" }],
      },
      optimizeDeps: {
        include: ["dayjs/esm/index.js", "@braintree/sanitize-url", "debug"],
      },
      build: {
        chunkSizeWarningLimit: 2048,
        rollupOptions: {
          output: {
            manualChunks(id: string) {
              if (!id.includes("node_modules")) return;
              if (id.includes("/@scalar/")) return "vendor-api-reference";
              if (id.includes("/minisearch/")) return "vendor-search";
              if (id.includes("/vitepress/")) return "vendor-vitepress";
              if (id.includes("/vue/") || id.includes("/@vue/")) return "vendor-vue";
            },
          },
        },
      },
    },

    themeConfig: {
      logo: "/ai-annotation-platform-icon.svg",
      nav,
      sidebar: {
        "/user-guide/": userGuideSidebar,
        "/dev/": createDevSidebar(adrSidebarItems),
        "/ops/": opsSidebar,
        "/changelog/": changelogSidebarItems,
        "/roadmap/": roadmapSidebarItems,
        "/api/": apiSidebar,
      },

      socialLinks: [
        { icon: "github", link: "https://github.com/YOAO-Labs/ai-annotation-platform" },
      ],

      outline: { level: [2, 3], label: "本页内容" },
      sidebarMenuLabel: "文档目录",
      returnToTopLabel: "回到顶部",
      skipToContentLabel: "跳转到正文",
      darkModeSwitchLabel: "主题",
      lightModeSwitchTitle: "切换到浅色主题",
      darkModeSwitchTitle: "切换到深色主题",
      docFooter: { prev: "上一篇", next: "下一篇" },
      lastUpdated: { text: "最后更新" },
      search: {
        provider: "local",
        options: {
          translations: {
            button: { buttonText: "搜索文档", buttonAriaLabel: "搜索文档" },
            modal: {
              displayDetails: "显示详细内容",
              resetButtonTitle: "清除搜索",
              backButtonTitle: "返回搜索",
              noResultsText: "没有找到相关文档",
              footer: {
                selectText: "打开",
                selectKeyAriaLabel: "回车键",
                navigateText: "选择",
                navigateUpKeyAriaLabel: "向上键",
                navigateDownKeyAriaLabel: "向下键",
                closeText: "关闭",
                closeKeyAriaLabel: "退出键",
              },
            },
          },
        },
      },

      footer: {
        message: "Released under the MIT License.",
        copyright: "© 2026 YOAO",
      },

      editLink: {
        pattern: ({ filePath }) => {
          let sourcePath = `docs-site/${filePath}`;
          if (filePath === "changelog/index.md") sourcePath = "CHANGELOG.md";
          else if (filePath.startsWith("changelog/")) {
            sourcePath = `docs/changelogs/${filePath.slice("changelog/".length)}`;
          } else if (filePath === "roadmap/index.md") sourcePath = "ROADMAP.md";
          else if (filePath.startsWith("roadmap/archived-")) {
            sourcePath = `ROADMAP/archive/${filePath.slice("roadmap/archived-".length)}`;
          } else if (filePath.startsWith("roadmap/")) {
            sourcePath = `ROADMAP/${filePath.slice("roadmap/".length)}`;
          } else if (filePath === "dev/adr/index.md") sourcePath = "docs/adr/README.md";
          else if (filePath.startsWith("dev/adr/")) {
            sourcePath = `docs/adr/${filePath.slice("dev/adr/".length)}`;
          }
          return `https://github.com/YOAO-Labs/ai-annotation-platform/edit/main/${sourcePath}`;
        },
        text: "在 GitHub 编辑此页",
      },
    },
  }),
);
