#!/usr/bin/env node
// 把仓库根 CHANGELOG.md / ROADMAP.md 与 docs/changelogs/* / ROADMAP/* 镜像到
// docs-site/changelog/、docs-site/roadmap/，让 VitePress 能渲染并部署到 Pages。
// 同时输出 sidebar.generated.json 给 .vitepress/config.ts 注入。
// Roadmap 的活跃文件位于 ROADMAP/，归档文件位于 ROADMAP/archive/；
// 镜像仍保留原有 /roadmap/archived-* 公开 URL。
//
// 与 mirror-adr.mjs 同样模式：源文件不动；镜像产物 gitignore；改源后 build 时重建。

import {
  readFileSync,
  readdirSync,
  writeFileSync,
  mkdirSync,
  rmSync,
  existsSync,
  statSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(here, "../..");

const banner = (srcRel) =>
  `> ⚠️ **自动镜像** · 此页由 \`docs-site/scripts/mirror-changelog.mjs\` 从 \`${srcRel}\` 生成，请勿直接编辑此处；改源文件后 \`pnpm docs:build\` 会自动同步。\n\n`;

// 转义代码块外的尖括号：CHANGELOG/ROADMAP 里有大量 `<reason>` `<JWT>` `<AppShell>`
// 这种 placeholder 文本，写在代码里没事，但 VitePress 的 Vue compiler 会把代码块外的
// 裸 `<word>` 当成未闭合标签报错。这里把代码 fence (```...```) 与 inline code (`...`)
// 之外的所有 `<` 转成实体。
//
// 同时：inline code 里的 `{{...}}`（例如 `style={{...}}`、`grep -c "style={{"`）会被
// Vue compiler 当成模板插值导致解析失败。把含 `{{` 或 `}}` 的 inline code 从反引号
// 改写为 `<code v-pre>...</code>`，直接关掉 Vue 编译。
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
    // 行内：按 `...` 切片，奇数下标是 inline code，偶数下标是文本
    const parts = line.split("`");
    let rebuilt = "";
    for (let j = 0; j < parts.length; j++) {
      if (j % 2 === 0) {
        // 文本：只转义 `<` 防 Vue 把 `<reason>` / `<JWT>` 当未闭合标签
        rebuilt += parts[j].replace(/</g, "&lt;");
      } else {
        // inline code：含 `{{`/`}}` 的改用 <code v-pre> 关掉 Vue 编译
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

const GITHUB_BLOB = "https://github.com/yyq19990828/ai-annotation-platform/blob/main";

function encodeRouteStem(stem) {
  if (stem.startsWith("archive/")) {
    return `archived-${stem.slice("archive/".length)}`;
  }
  return stem
    .replace(/^\[archived\]/, "archived-")
    .replace(/\[/g, "")
    .replace(/\]/g, "");
}

// 链接改写：把仓库内引用改成 VitePress 站点路径或 GitHub blob URL。
// 涵盖 root（CHANGELOG.md / ROADMAP.md）与子目录（docs/changelogs/, ROADMAP/）两种
// 视角，因此同时匹配 `./xxx`、`xxx`、`../xxx` 三种相对前缀。
function rewriteLinks(text, srcRel) {
  // 通用的「相对前缀」段：(?:\.\.?\/)*  匹配任意数量的 ./ 或 ../
  const REL = "(?:\\.\\.?\\/)*";
  const HASH = "(#[^)\\s]*)?";

  let rewritten = text
    // changelogs 子文件 → /changelog/<ver>
    .replace(
      new RegExp(`\\]\\(${REL}docs\\/changelogs\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      "](/changelog/$1$2)",
    )
    // changelogs 目录 → /changelog/
    .replace(new RegExp(`\\]\\(${REL}docs\\/changelogs\\/?\\)`, "g"), "](/changelog/)")
    // CHANGELOG.md → /changelog/
    .replace(new RegExp(`\\]\\(${REL}CHANGELOG\\.md${HASH}\\)`, "g"), "](/changelog/$1)")
    // ROADMAP/<ver>.md → /roadmap/<ver>
    .replace(
      new RegExp(`\\]\\(${REL}ROADMAP\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      (_m, stem, hash = "") => `](/roadmap/${encodeRouteStem(stem)}${hash})`,
    )
    // ROADMAP.md → /roadmap/
    .replace(new RegExp(`\\]\\(${REL}ROADMAP\\.md${HASH}\\)`, "g"), "](/roadmap/$1)")
    // ADR → /dev/adr/<id>
    .replace(
      new RegExp(`\\]\\(${REL}(?:docs\\/)?adr\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      "](/dev/adr/$1$2)",
    )
    // plans / research 不进站点：指向 GitHub blob URL（保留 hash）
    .replace(
      new RegExp(`\\]\\(${REL}docs\\/(plans|research)\\/([^)#\\s]+?\\.md)${HASH}\\)`, "g"),
      `](${GITHUB_BLOB}/docs/$1/$2$3)`,
    )
    // 同目录 ./0.10.x.md（在 ROADMAP/ 内或 docs/changelogs/ 内的版本互引）→ 站点干净 URL
    .replace(
      /\]\((?:\.\/)?archive\/([^)#\s]+?)\.md(#[^)\s]*)?\)/g,
      (_m, stem, hash = "") => `](/roadmap/archived-${stem}${hash})`,
    )
    // 兼容旧 Roadmap 源里的 `[archived]xxx.md` 相对链接。
    .replace(
      /\]\((?:\.\/)?(\[archived\][^)#\s]+?)\.md(#[^)\s]*)?\)/g,
      (_m, stem, hash = "") => `](./${encodeRouteStem(stem)}${hash})`,
    )
    .replace(/\]\((?:\.\/)?(\d+(?:\.\d+)*\.x)\.md(#[^)\s]*)?\)/g, (_m, stem, hash = "") =>
      srcRel.startsWith("ROADMAP/archive/")
        ? `](/roadmap/archived-${stem}${hash})`
        : `](./${stem}${hash})`,
    )
    // maintainers/ 被排除在站点渲染之外，镜像中应链到仓库源文件。
    .replace(
      new RegExp(`\\]\\(${REL}docs-site\/maintainers\/([^)#\\s]+?)\.md${HASH}\\)`, "g"),
      (_m, rel, hash = "") => `](${GITHUB_BLOB}/docs-site/maintainers/${rel}.md${hash})`,
    )
    // docs-site/<x>.md 引用：CHANGELOG / ROADMAP 用相对仓库根的路径指向站点页面，
    // 镜像后落到站点内需要改成站点绝对 URL。
    .replace(
      new RegExp(`\\]\\(${REL}docs-site\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      (_m, rel, hash = "") => `](/${rel}${hash})`,
    )
    // DEV.md / AGENTS.md / README.md 在仓库根，没有站点镜像 → 指向 GitHub blob URL
    .replace(
      new RegExp(`\\]\\(${REL}(DEV|AGENTS|README)\\.md${HASH}\\)`, "g"),
      (_m, name, hash = "") => `](${GITHUB_BLOB}/${name}.md${hash})`,
    )
    // 仓库根配置文件（.env.example / docker-compose.yml 等，无站点镜像）→ GitHub blob URL
    .replace(
      new RegExp(`\\]\\(${REL}(\\.env\\.example|\\.env|docker-compose\\.ya?ml)${HASH}\\)`, "g"),
      (_m, name, hash = "") => `](${GITHUB_BLOB}/${name}${hash})`,
    );

  if (srcRel.startsWith("ROADMAP/archive/")) {
    rewritten = rewritten
      .replace(
        /\]\(\.\.\/(\d{4}-[^)#\s]+?)\.md(#[^)\s]*)?\)/g,
        (_m, stem, hash = "") => `](/roadmap/${stem}${hash})`,
      )
      .replace(
        /\]\((?:\.\/)?(\d{4}-[^)#\s]+?)\.md(#[^)\s]*)?\)/g,
        (_m, stem, hash = "") => `](/roadmap/archived-${stem}${hash})`,
      );
  }

  return rewritten;
}

// 按版本号的各个数值段倒序排列，不按文件名字典序排列。
function compareVersionDesc(a, b) {
  const pa = a
    .replace(/\.md$/, "")
    .split(".")
    .map((s) => parseInt(s, 10) || 0);
  const pb = b
    .replace(/\.md$/, "")
    .split(".")
    .map((s) => parseInt(s, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] ?? 0;
    const db = pb[i] ?? 0;
    if (da !== db) return db - da;
  }
  return 0;
}

function mirrorOne({ srcFile, srcRel, dstFile }) {
  const text = readFileSync(srcFile, "utf8");
  const body = escapeForVue(rewriteLinks(text, srcRel));
  writeFileSync(dstFile, banner(srcRel) + body);
}

function buildGroup({
  name,
  rootSrc,
  rootSrcRel,
  dirSrc,
  dirSrcRel,
  archiveDirSrc,
  archiveDirSrcRel,
  dstDir,
  urlPrefix,
}) {
  if (existsSync(dstDir)) rmSync(dstDir, { recursive: true, force: true });
  mkdirSync(dstDir, { recursive: true });

  // 1. 根入口 → index.md
  if (!existsSync(rootSrc)) {
    console.error(`[mirror-changelog] 源文件不存在：${rootSrc}`);
    process.exit(1);
  }
  mirrorOne({
    srcFile: rootSrc,
    srcRel: rootSrcRel,
    dstFile: resolve(dstDir, "index.md"),
  });

  // 2. 子目录 → <name>.md
  const versionFiles =
    existsSync(dirSrc) && statSync(dirSrc).isDirectory()
      ? readdirSync(dirSrc)
          .filter((f) => f.endsWith(".md"))
          .sort(compareVersionDesc)
      : [];
  const archivedFiles =
    archiveDirSrc && existsSync(archiveDirSrc) && statSync(archiveDirSrc).isDirectory()
      ? readdirSync(archiveDirSrc)
          .filter((f) => f.endsWith(".md"))
          .sort(compareVersionDesc)
      : [];
  const entries = [
    ...versionFiles.map((file) => ({
      file,
      srcFile: resolve(dirSrc, file),
      srcRel: `${dirSrcRel}/${file}`,
      routeStem: encodeRouteStem(file.replace(/\.md$/, "")),
      sidebarText: `v${file.replace(/\.md$/, "")}`,
    })),
    ...archivedFiles.map((file) => ({
      file,
      srcFile: resolve(archiveDirSrc, file),
      srcRel: `${archiveDirSrcRel}/${file}`,
      routeStem: `archived-${file.replace(/\.md$/, "")}`,
      sidebarText: `归档 · ${file.replace(/\.md$/, "")}`,
    })),
  ];
  for (const entry of entries) {
    mirrorOne({
      srcFile: entry.srcFile,
      srcRel: entry.srcRel,
      dstFile: resolve(dstDir, `${entry.routeStem}.md`),
    });
  }

  // 3. sidebar：index 在最前，其余按版本倒序
  const sidebar = [{ text: `${name} 总览`, link: `${urlPrefix}/` }];
  for (const entry of entries) {
    sidebar.push({ text: entry.sidebarText, link: `${urlPrefix}/${entry.routeStem}` });
  }
  writeFileSync(resolve(dstDir, "sidebar.generated.json"), JSON.stringify(sidebar, null, 2) + "\n");

  console.log(
    `[mirror-changelog] ${name}: 1 root + ${versionFiles.length} active + ${archivedFiles.length} archived → ${dstDir}`,
  );
}

buildGroup({
  name: "更新日志",
  rootSrc: resolve(REPO, "CHANGELOG.md"),
  rootSrcRel: "CHANGELOG.md",
  dirSrc: resolve(REPO, "docs/changelogs"),
  dirSrcRel: "docs/changelogs",
  dstDir: resolve(here, "../changelog"),
  urlPrefix: "/changelog",
});

buildGroup({
  name: "Roadmap",
  rootSrc: resolve(REPO, "ROADMAP.md"),
  rootSrcRel: "ROADMAP.md",
  dirSrc: resolve(REPO, "ROADMAP"),
  dirSrcRel: "ROADMAP",
  archiveDirSrc: resolve(REPO, "ROADMAP/archive"),
  archiveDirSrcRel: "ROADMAP/archive",
  dstDir: resolve(here, "../roadmap"),
  urlPrefix: "/roadmap",
});
