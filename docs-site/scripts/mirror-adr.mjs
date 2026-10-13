#!/usr/bin/env node
// 把 docs/adr/*.md 镜像到 docs-site/dev/adr/，让 VitePress 能渲染（VitePress 只
// 渲染 docs-site/ 内的 .md），同时输出 sidebar.generated.json 给 .vitepress/config.ts
// 注入到「开发文档 → ADR」侧边栏组。
//
// 顶层 docs/adr/*.md（Proposed / Draft）→ docs-site/dev/adr/*.md（进 sidebar）
// docs/adr/archive/*.md（Accepted 归档）→ docs-site/dev/adr/archive/*.md（不进 sidebar）
//
// 镜像后文件头部插入一行 ⚠ 警告，提示读者去 docs/adr/ 改源文件。
// ADR 之间的相对引用（`[0023](0023-...)`）会按被引用编号在 top / archive 中的位置
// 重写为 VitePress 站点绝对路径 `/dev/adr/00XX-` 或 `/dev/adr/archive/00XX-`。

import { readFileSync, readdirSync, writeFileSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "../../docs/adr");
const SRC_ARCHIVE = resolve(SRC, "archive");
const DST = resolve(here, "../dev/adr");
const DST_ARCHIVE = resolve(DST, "archive");
const SIDEBAR = resolve(here, "../dev/adr/sidebar.generated.json");

if (!existsSync(SRC)) {
  console.error(`[mirror-adr] 源目录不存在：${SRC}`);
  process.exit(1);
}

// 清空 dst，避免源端删除 ADR 后镜像残留
if (existsSync(DST)) rmSync(DST, { recursive: true, force: true });
mkdirSync(DST, { recursive: true });
mkdirSync(DST_ARCHIVE, { recursive: true });

const banner = (srcRel) =>
  `> ⚠️ **自动镜像** · 此页由 \`docs-site/scripts/mirror-adr.mjs\` 从 \`${srcRel}\` 生成，请勿直接编辑此处；改源文件后 \`pnpm docs:build\` 会自动同步。\n\n`;

// inline code 里的 `{{...}}`（如 `style={{...}}`、`<style={{}}>`）会被 VitePress 的
// Vue compiler 当模板插值解析失败。把含 `{{` 或 `}}` 的 inline code 从反引号改写为
// `<code v-pre>...</code>`，关掉该片段的 Vue 编译。
function escapeForVue(text) {
  const lines = text.split("\n");
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const parts = line.split("`");
    let rebuilt = "";
    for (let j = 0; j < parts.length; j++) {
      if (j % 2 === 0) {
        rebuilt += parts[j];
      } else {
        const code = parts[j];
        if (/\{\{|\}\}/.test(code)) {
          const escaped = code.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
          rebuilt += `<code v-pre>${escaped}</code>`;
        } else {
          rebuilt += "`" + code + "`";
        }
      }
    }
    lines[i] = rebuilt;
  }
  return lines.join("\n");
}

// —— 收集顶层 + archive 的 ADR 文件清单 ——
function listAdrFiles(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".md"))
    .sort();
}

const topFiles = listAdrFiles(SRC);
const archiveFiles = listAdrFiles(SRC_ARCHIVE);

// 建立「编号 → 位置」映射，用于重写 ADR 之间的相对引用
const NUM_RE = /^(\d{4})-[a-z0-9-]+\.md$/i;
const locationByNum = new Map(); // number(int) -> "top" | "archive"
for (const f of topFiles) {
  const m = f.match(NUM_RE);
  if (m) locationByNum.set(parseInt(m[1], 10), "top");
}
for (const f of archiveFiles) {
  const m = f.match(NUM_RE);
  if (m) locationByNum.set(parseInt(m[1], 10), "archive");
}

function resolveAdrLink(num) {
  const loc = locationByNum.get(num);
  const stem = (loc === "top" ? topFiles : archiveFiles).find((f) => {
    const m = f.match(NUM_RE);
    return m && parseInt(m[1], 10) === num;
  });
  if (!stem) return null;
  const bare = stem.replace(/\.md$/, "");
  return loc === "top" ? `/dev/adr/${bare}` : `/dev/adr/archive/${bare}`;
}

function rewriteBody(text, { isReadme } = {}) {
  let body = text;

  // 1) ADR 内互引：`[text](0023-...)` / `(./0023-...)` / `(archive/0023-...)`
  //    → 按被引用编号在 top / archive 的位置改写为 VitePress 站点绝对路径。
  body = body.replace(
    /\]\((?:\.\.?\/)*(?:archive\/)?(\d{4})-[\w-]+\.md(#[^)\s]*)?\)/g,
    (m, num, hash = "") => {
      const site = resolveAdrLink(parseInt(num, 10));
      return site ? `](${site}${hash})` : m;
    },
  );

  // 2) README 里可能出现 `(archive/)` 形态的目录链接（不指向具体文件）
  if (isReadme) {
    body = body.replace(/\]\(archive\/\)/g, "](/dev/adr/archive/)");
  }

  // 3) 源在 docs/adr/ 时对 docs-site/ 的相对路径改站点绝对路径
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/\.\.\/docs-site\/([^)#\s]+?)(\.md)?(#[^)\s]*)?\)/g,
    (_m, rel, _md, hash = "") => `](/${rel}${hash})`,
  );
  // 归档 ADR 深一层，也支持 ../../../docs-site/
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/\.\.\/\.\.\/docs-site\/([^)#\s]+?)(\.md)?(#[^)\s]*)?\)/g,
    (_m, rel, _md, hash = "") => `](/${rel}${hash})`,
  );

  // 4) ../../CHANGELOG.md → /changelog/
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/(?:\.\.\/)?(?:\.\.\/)?CHANGELOG\.md(#[^)\s]*)?\)/g,
    (_m, hash = "") => `](/changelog/${hash})`,
  );

  // 5) ../../ROADMAP/<file>.md → /roadmap/<encoded-stem>
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/(?:\.\.\/)?(?:\.\.\/)?ROADMAP\/([^)#\s]+?)\.md(#[^)\s]*)?\)/g,
    (_m, stem, hash = "") => {
      const encoded = stem.startsWith("archive/")
        ? `archived-${stem.slice("archive/".length)}`
        : stem
            .replace(/^\[archived\]/, "archived-")
            .replace(/\[/g, "")
            .replace(/\]/g, "");
      return `](/roadmap/${encoded}${hash})`;
    },
  );

  // 6) ../../ROADMAP.md → /roadmap/
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/(?:\.\.\/)?(?:\.\.\/)?ROADMAP\.md(#[^)\s]*)?\)/g,
    (_m, hash = "") => `](/roadmap/${hash})`,
  );

  return body;
}

const sidebar = [];
let totalMirrored = 0;

// 处理顶层 ADR（README 变 index，其余进 sidebar）
for (const name of topFiles) {
  const srcRel = `docs/adr/${name}`;
  const text = readFileSync(resolve(SRC, name), "utf8");

  const isReadme = name === "README.md";
  const dstName = isReadme ? "index.md" : name;

  const titleMatch = text.match(/^#\s+(.+?)\s*$/m);
  const title = titleMatch ? titleMatch[1].trim() : name.replace(/\.md$/, "");

  const body = rewriteBody(text, { isReadme });
  writeFileSync(resolve(DST, dstName), banner(srcRel) + escapeForVue(body));
  totalMirrored++;

  if (!isReadme && !name.startsWith("TEMPLATE")) {
    sidebar.push({
      text: title,
      link: `/dev/adr/${name.replace(/\.md$/, "")}`,
    });
  }
}

// 处理 archive ADR（都不进 sidebar，只做镜像让链接可解析）
for (const name of archiveFiles) {
  const srcRel = `docs/adr/archive/${name}`;
  const text = readFileSync(resolve(SRC_ARCHIVE, name), "utf8");
  const body = rewriteBody(text, { isReadme: false });
  writeFileSync(resolve(DST_ARCHIVE, name), banner(srcRel) + escapeForVue(body));
  totalMirrored++;
}

// archive 目录索引页：让 `[archive/](archive/)` 之类的目录链接可解析。
{
  const items = archiveFiles.map((name) => {
    const text = readFileSync(resolve(SRC_ARCHIVE, name), "utf8");
    const titleMatch = text.match(/^#\s+(.+?)\s*$/m);
    const title = titleMatch ? titleMatch[1].trim() : name.replace(/\.md$/, "");
    const bare = name.replace(/\.md$/, "");
    return `- [${title}](/dev/adr/archive/${bare})`;
  });
  const indexBody =
    "# ADR 归档\n\n" +
    "> 已采纳（Accepted）状态的 ADR 集中在此目录，编号连续性仍受 `check-adr.mjs` 校验；不进 sidebar，通过 [ADR 索引](/dev/adr/) 分组导航。\n\n" +
    items.join("\n") +
    "\n";
  writeFileSync(
    resolve(DST_ARCHIVE, "index.md"),
    banner("docs-site/scripts/mirror-adr.mjs (auto index)") + indexBody,
  );
}

// 写 sidebar；放 README/index 在最前
sidebar.unshift({ text: "ADR 索引", link: "/dev/adr/" });
writeFileSync(SIDEBAR, JSON.stringify(sidebar, null, 2) + "\n");

console.log(
  `[mirror-adr] mirrored ${totalMirrored} files (${topFiles.length} top + ${archiveFiles.length} archive) → ${DST}; sidebar with ${sidebar.length} entries`,
);
