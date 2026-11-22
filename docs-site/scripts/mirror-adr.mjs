#!/usr/bin/env node

import {
  cpSync,
  readFileSync,
  readdirSync,
  writeFileSync,
  mkdirSync,
  rmSync,
  existsSync,
} from "node:fs";
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

if (existsSync(DST)) rmSync(DST, { recursive: true, force: true });
mkdirSync(DST, { recursive: true });
mkdirSync(DST_ARCHIVE, { recursive: true });
const downloads = resolve(here, "../public/dev/adr/evidence");
rmSync(downloads, { recursive: true, force: true });
cpSync(resolve(SRC, "evidence"), downloads, { recursive: true });

const banner = (srcRel) =>
  `> ⚠️ **自动镜像** · 此页由 \`docs-site/scripts/mirror-adr.mjs\` 从 \`${srcRel}\` 生成，请勿直接编辑此处；改源文件后 \`pnpm docs:build\` 会自动同步。\n\n`;

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

function listAdrFiles(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".md"))
    .sort();
}

const topFiles = listAdrFiles(SRC);
const archiveFiles = listAdrFiles(SRC_ARCHIVE);

const adrLinks = new Map();
for (const [prefix, files] of [
  ["", topFiles],
  ["archive/", archiveFiles],
]) {
  for (const file of files) {
    if (/^\d{4}-[\w-]+\.md$/.test(file)) {
      adrLinks.set(file, `/dev/adr/${prefix}${file.slice(0, -3)}`);
    }
  }
}

function rewriteBody(text, { isReadme } = {}) {
  let body = text;

  body = body.replace(
    /\]\((?:\.\.?\/)*(?:archive\/)?(\d{4}-[\w-]+\.md)(#[^)\s]*)?\)/g,
    (m, file, hash = "") => (adrLinks.has(file) ? `](${adrLinks.get(file)}${hash})` : m),
  );
  body = body.replace(/\]\(\.\.\/evidence\/(\d{4}-decisions\.tsv)\)/g, "](/dev/adr/evidence/$1)");

  if (isReadme) {
    body = body.replace(/\]\(archive\/\)/g, "](/dev/adr/archive/)");
  }

  body = body.replace(
    /\]\((?:\.\/)?\.\.\/\.\.\/docs-site\/([^)#\s]+?)(\.md)?(#[^)\s]*)?\)/g,
    (_m, rel, _md, hash = "") => `](/${rel}${hash})`,
  );
  body = body.replace(
    /\]\((?:\.\/)?\.\.\/\.\.\/\.\.\/docs-site\/([^)#\s]+?)(\.md)?(#[^)\s]*)?\)/g,
    (_m, rel, _md, hash = "") => `](/${rel}${hash})`,
  );

  body = body.replace(
    /\]\((?:\.\/)?\.\.\/(?:\.\.\/)?(?:\.\.\/)?CHANGELOG\.md(#[^)\s]*)?\)/g,
    (_m, hash = "") => `](/changelog/${hash})`,
  );

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

  body = body.replace(
    /\]\((?:\.\/)?\.\.\/(?:\.\.\/)?(?:\.\.\/)?ROADMAP\.md(#[^)\s]*)?\)/g,
    (_m, hash = "") => `](/roadmap/${hash})`,
  );

  return body;
}

const sidebar = [];
let totalMirrored = 0;

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

for (const name of archiveFiles) {
  const srcRel = `docs/adr/archive/${name}`;
  const text = readFileSync(resolve(SRC_ARCHIVE, name), "utf8");
  const body = rewriteBody(text, { isReadme: false });
  writeFileSync(resolve(DST_ARCHIVE, name), banner(srcRel) + escapeForVue(body));
  totalMirrored++;
}

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

sidebar.unshift({ text: "ADR 索引", link: "/dev/adr/" });
writeFileSync(SIDEBAR, JSON.stringify(sidebar, null, 2) + "\n");

console.log(
  `[mirror-adr] mirrored ${totalMirrored} files (${topFiles.length} top + ${archiveFiles.length} archive) → ${DST}; sidebar with ${sidebar.length} entries`,
);
