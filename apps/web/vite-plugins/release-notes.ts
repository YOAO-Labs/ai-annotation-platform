import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Plugin } from "vite";

const VIRTUAL_ID = "virtual:release-notes";
const RESOLVED_ID = "\0" + VIRTUAL_ID;

/**
 * 版本更新提醒数据源:构建期读取仓库根 `CHANGELOG.md`,只把 package.json 当前
 * 版本的段落注入虚拟模块 `virtual:release-notes`(整文件约 30KB,只随版本增长,
 * 打包全文会无谓吃掉主包预算,因此构建期裁剪到单版本段落)。
 *
 * 发布流程(aap-release)同步 bump 版本号与 changelog,故「版本已 bump 但段落
 * 缺失」只在发版中间态出现:此时注入空串,前端 `parseChangelogSection` 返回
 * null,弹窗回落为简短提示。dev 修改 CHANGELOG.md 后重启 dev server 生效。
 */
export function releaseNotesPlugin(): Plugin {
  return {
    name: "vite-plugin-release-notes",
    resolveId(id) {
      if (id === VIRTUAL_ID) return RESOLVED_ID;
      return null;
    },
    load(id) {
      if (id !== RESOLVED_ID) return null;
      const webRoot = resolve(__dirname, "..");
      const version = JSON.parse(readFileSync(resolve(webRoot, "package.json"), "utf8"))
        .version as string;
      // 打包上下文未携带仓库根 CHANGELOG.md(如精简镜像)时不阻断构建,注入空串,
      // 前端回落为简短提示——与「版本已 bump 但段落缺失」同一降级路径。
      let markdown = "";
      try {
        markdown = readFileSync(resolve(webRoot, "../../CHANGELOG.md"), "utf8");
      } catch {
        markdown = "";
      }
      return `export const sectionMarkdown = ${JSON.stringify(
        extractChangelogSection(markdown, version),
      )};`;
    },
  };
}

/** 切出 `## [version]` 标题行到下一个 `## ` 标题之间的原文;找不到返回空串。 */
function extractChangelogSection(markdown: string, version: string): string {
  const escaped = version.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const start = markdown.search(new RegExp(`^## \\[${escaped}\\]`, "m"));
  if (start === -1) return "";
  const rest = markdown.slice(start + 1);
  const next = rest.search(/^## /m);
  return next === -1 ? markdown.slice(start) : markdown.slice(start, start + 1 + next);
}
