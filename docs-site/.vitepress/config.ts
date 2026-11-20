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

// ADR 侧边栏由 docs-site/scripts/mirror-adr.mjs 在 prebuild/predev 阶段生成。
// 若文件缺失（例如刚 clone 还未跑 prebuild），降级为空数组让 VitePress 仍能启动。
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
    // 站点地图：hostname 用源站，路径前缀由 base 处理。部署地址
    // https://yyq19990828.github.io/ai-annotation-platform/ 。提交给 Search Console 用完整 URL。
    sitemap: {
      hostname: "https://yyq19990828.github.io/ai-annotation-platform/",
      transformItems: (items) =>
        items.filter((item) => !item.url.includes("user-guide/projects/annotation-guide")),
    },
    // 允许指向本地开发服务器的链接，构建期不当 dead link
    ignoreDeadLinks: [
      /^https?:\/\/localhost(:\d+)?(\/|$)/,
      // ROADMAP / ADR mirror files contain relative links to source code files outside docs-site
      (url) => /\.(tsx?|py|json|ya?ml|sh|toml|Dockerfile\w*)$/.test(url),
      (url) => /\/(apps|infra)\//.test(url),
      // ADR mirrors may point back to repo-local docs/plans files, which are not
      // rendered as VitePress pages, including archived plans.
      (url) =>
        /(^|\/)\.\.\/plans\/(archive\/)?\d{4}-/.test(url) ||
        /\/plans\/(archive\/)?\d{4}-/.test(url),
      // ROADMAP/inspiration 文档引用本地 clone 的 CVAT 源码（`../../cvat/...`），
      // 这些不是站点页面，构建期不应判为 dead。
      (url) => /\/cvat\//.test(url) || /\/cvat-(sdk|cli)(\/|$)/.test(url),
    ],

    // mermaid 11.x 的 chunk 直接 import `dayjs/dayjs.min.js`（UMD 文件），Vite 当 ESM
    // 解析失败 → "does not provide an export named 'default'"。alias 指向 ESM 入口。
    vite: {
      resolve: {
        alias: [{ find: /^dayjs\/dayjs\.min\.js$/, replacement: "dayjs/esm/index.js" }],
      },
      optimizeDeps: {
        include: ["dayjs/esm/index.js", "@braintree/sanitize-url", "debug"],
      },
      build: {
        // VitePress local search emits a large generated index for the whole docs site.
        // Keep the threshold above that expected artifact while still warning on accidental
        // multi-megabyte application chunks.
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
        { icon: "github", link: "https://github.com/yyq19990828/ai-annotation-platform" },
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
        // VitePress serializes this callback into the client bundle, so keep it
        // self-contained instead of closing over helpers from config.ts.
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
          return `https://github.com/yyq19990828/ai-annotation-platform/edit/main/${sourcePath}`;
        },
        text: "在 GitHub 编辑此页",
      },
    },
  }),
);
