#!/usr/bin/env node
/**
 * 检查维护型正文页是否包含稳定的归属与复核 frontmatter。
 * 页面标题取自 H1；发布沿革不属于当前文档的元数据。
 * 跳过目录首页、自动生成文件、redirects/、示例源码、maintainers/ 维护材料，
 * 以及 dev/adr/、changelog/、roadmap/ 等构建期镜像。
 *
 * 用法：node docs-site/scripts/check-doc-frontmatter.mjs [--fix]
 *   --fix  在缺失字段的文件顶部插入空骨架（仅补充缺失字段，不覆盖已有值）
 *
 * CI 集成：在 docs:build 之前运行，失败则退出码 1。
 */

import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, extname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import { currentDocumentDate } from "./document-date.mjs";

const __here = dirname(fileURLToPath(import.meta.url));
const DOCS_ROOT = join(__here, "..");

const REQUIRED_FIELDS = ["audience", "type", "status", "last_reviewed"];
const SKIP_ROOTS = new Set([
  ".vitepress",
  "changelog",
  "dev/adr",
  "dev/examples",
  "maintainers",
  "node_modules",
  "public",
  "redirects",
  "roadmap",
  "scripts",
]);
const SKIP_GENERATED = /\.generated\.md$/;
const SKIP_PATHS = new Set(["index.md"]);

const fix = process.argv.includes("--fix");

let errors = 0;
let warnings = 0;
let checked = 0;

function parseFrontmatter(content) {
  if (!content.startsWith("---")) return null;
  const end = content.indexOf("\n---", 3);
  if (end === -1) return null;
  const raw = content.slice(4, end);
  const fields = {};
  for (const line of raw.split("\n")) {
    const m = line.match(/^(\w[\w_-]*):\s*(.*)$/);
    if (m) fields[m[1]] = m[2].trim();
  }
  return fields;
}

function buildSkeletonFrontmatter(existing = {}) {
  const today = currentDocumentDate();
  const lines = ["---"];
  if (!existing.audience) lines.push("audience: []");
  if (!existing.type) lines.push("type: ''  # tutorial | how-to | reference | explanation");
  if (!existing.status) lines.push("status: stable  # draft | stable | deprecated");
  if (!existing.last_reviewed) lines.push(`last_reviewed: ${today}`);
  lines.push("---");
  return lines.join("\n") + "\n";
}

function walk(dir) {
  const entries = readdirSync(dir, { withFileTypes: true });
  for (const e of entries) {
    if (e.isDirectory()) {
      const child = join(dir, e.name);
      const rel = relative(DOCS_ROOT, child).replace(/\\/g, "/");
      if (!SKIP_ROOTS.has(rel)) walk(child);
    } else if (e.isFile() && extname(e.name) === ".md") {
      checkFile(join(dir, e.name));
    }
  }
}

function checkFile(filePath) {
  const rel = relative(DOCS_ROOT, filePath);
  const content = readFileSync(filePath, "utf8");

  if (SKIP_PATHS.has(rel)) return;
  // Skip auto-generated files
  if (SKIP_GENERATED.test(filePath)) return;

  // Landing pages may rely on their visible H1 and do not need maintenance metadata.
  if (basename(filePath) === "index.md" && !content.startsWith("---")) return;

  checked++;

  const fm = parseFrontmatter(content);
  if (!fm) {
    if (fix) {
      const skeleton = buildSkeletonFrontmatter();
      writeFileSync(filePath, skeleton + content);
      console.log(`[fix] Added frontmatter skeleton: ${rel}`);
    } else {
      console.error(`[error] Missing frontmatter: ${rel}`);
      errors++;
    }
    return;
  }

  const missing = REQUIRED_FIELDS.filter((f) => !fm[f]);
  if (missing.length > 0) {
    if (fix) {
      const extra = buildSkeletonFrontmatter(fm);
      // Insert missing fields after opening ---
      const fmBlock = extra.replace(/^---\n/, "").replace(/\n---\n$/, "");
      const insertAfter = content.indexOf("\n---", 3);
      const newContent =
        content.slice(0, insertAfter) + "\n" + fmBlock + content.slice(insertAfter);
      writeFileSync(filePath, newContent);
      console.log(`[fix] Added missing fields [${missing.join(", ")}] to: ${rel}`);
    } else {
      console.error(`[error] Missing fields [${missing.join(", ")}] in: ${rel}`);
      errors++;
    }
  }
}

walk(DOCS_ROOT);

console.log(`\nChecked ${checked} files.`);
if (errors > 0) {
  console.error(`${errors} file(s) failed frontmatter check. Run with --fix to add skeletons.`);
  process.exit(1);
} else {
  console.log("All files pass frontmatter check.");
}
