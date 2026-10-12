/**
 * VitePress 自定义主题入口。
 * 继承默认主题，注册全局组件，加载文档站视觉基线。
 */
import DefaultTheme from "vitepress/theme";
import DocsLayout from "./DocsLayout.vue";
import AutoImage from "./components/AutoImage.vue";
import ExcalidrawDiagram from "./components/ExcalidrawDiagram.vue";
import DocsHome from "./components/DocsHome.vue";
import DocLinkCard from "./components/DocLinkCard.vue";
import ApiReferenceFrame from "./components/ApiReferenceFrame.vue";
import DocsVideo from "./components/DocsVideo.vue";
import { setupMermaidZoom } from "./mermaid-zoom";
import { setupImageZoom } from "./image-zoom";
import { setupPlatformKeys } from "./platform-keys";
// 全局阅读基线优先于其他样式加载：先建立正文阅读 token，再叠加首页品牌 token
import "./docs-theme.css";
import "./docs-home.css";
import "./mermaid-zoom.css";
import type { Theme } from "vitepress";

export default {
  extends: DefaultTheme,
  Layout: DocsLayout,
  enhanceApp({ app }) {
    app.component("AutoImage", AutoImage);
    app.component("ExcalidrawDiagram", ExcalidrawDiagram);
    app.component("DocsHome", DocsHome);
    app.component("DocLinkCard", DocLinkCard);
    app.component("ApiReferenceFrame", ApiReferenceFrame);
    app.component("DocsVideo", DocsVideo);
    setupMermaidZoom();
    setupImageZoom();
    setupPlatformKeys();
  },
} satisfies Theme;
