#!/usr/bin/env node

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
        rebuilt += parts[j].replace(/</g, "&lt;");
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

function rewriteLinks(text, srcRel) {
  const REL = "(?:\\.\\.?\\/)*";
  const HASH = "(#[^)\\s]*)?";

  let rewritten = text
    .replace(
      new RegExp(`\\]\\(${REL}docs\\/changelogs\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      "](/changelog/$1$2)",
    )
    .replace(new RegExp(`\\]\\(${REL}docs\\/changelogs\\/?\\)`, "g"), "](/changelog/)")
    .replace(new RegExp(`\\]\\(${REL}CHANGELOG\\.md${HASH}\\)`, "g"), "](/changelog/$1)")
    .replace(
      new RegExp(`\\]\\(${REL}ROADMAP\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      (_m, stem, hash = "") => `](/roadmap/${encodeRouteStem(stem)}${hash})`,
    )
    .replace(new RegExp(`\\]\\(${REL}ROADMAP\\.md${HASH}\\)`, "g"), "](/roadmap/$1)")
    .replace(
      new RegExp(`\\]\\(${REL}(?:docs\\/)?adr\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      "](/dev/adr/$1$2)",
    )
    .replace(
      new RegExp(`\\]\\(${REL}docs\\/(plans|research)\\/([^)#\\s]+?\\.md)${HASH}\\)`, "g"),
      `](${GITHUB_BLOB}/docs/$1/$2$3)`,
    )
    .replace(
      /\]\((?:\.\/)?archive\/([^)#\s]+?)\.md(#[^)\s]*)?\)/g,
      (_m, stem, hash = "") => `](/roadmap/archived-${stem}${hash})`,
    )
    .replace(
      /\]\((?:\.\/)?(\[archived\][^)#\s]+?)\.md(#[^)\s]*)?\)/g,
      (_m, stem, hash = "") => `](./${encodeRouteStem(stem)}${hash})`,
    )
    .replace(/\]\((?:\.\/)?(\d+(?:\.\d+)*\.x)\.md(#[^)\s]*)?\)/g, (_m, stem, hash = "") =>
      srcRel.startsWith("ROADMAP/archive/")
        ? `](/roadmap/archived-${stem}${hash})`
        : `](./${stem}${hash})`,
    )
    .replace(
      new RegExp(`\\]\\(${REL}docs-site\/maintainers\/([^)#\\s]+?)\.md${HASH}\\)`, "g"),
      (_m, rel, hash = "") => `](${GITHUB_BLOB}/docs-site/maintainers/${rel}.md${hash})`,
    )
    .replace(
      new RegExp(`\\]\\(${REL}docs-site\\/([^)#\\s]+?)\\.md${HASH}\\)`, "g"),
      (_m, rel, hash = "") => `](/${rel}${hash})`,
    )
    .replace(
      new RegExp(`\\]\\(${REL}(DEV|AGENTS|README)\\.md${HASH}\\)`, "g"),
      (_m, name, hash = "") => `](${GITHUB_BLOB}/${name}.md${hash})`,
    )
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

  if (!existsSync(rootSrc)) {
    console.error(`[mirror-changelog] 源文件不存在：${rootSrc}`);
    process.exit(1);
  }
  mirrorOne({
    srcFile: rootSrc,
    srcRel: rootSrcRel,
    dstFile: resolve(dstDir, "index.md"),
  });

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
