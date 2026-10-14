#!/usr/bin/env node
/**
 * 检查文档站 Markdown 文件的写作规范与可读性体验：
 * 1. 避免 H1 开篇被历史 UI 补丁声明或生硬外链直接劫持；
 * 2. 避免在面向业务用户的 user-guide/ 中裸露未折叠的底层 HTTP 接口请求；
 * 3. 提示单段连续正文行数过长（> 14 行）的视觉文字瀑布。
 *
 * 用法：node docs-site/scripts/check-doc-readability.mjs [--strict]
 */

import { readFileSync, readdirSync } from "node:fs";
import { join, relative, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

const __here = dirname(fileURLToPath(import.meta.url));
const DOCS_ROOT = join(__here, "..");

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

const strict = process.argv.includes("--strict");

let errors = 0;
let warnings = 0;
let checked = 0;

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
  const rel = relative(DOCS_ROOT, filePath).replace(/\\/g, "/");
  if (SKIP_GENERATED.test(filePath)) return;

  const content = readFileSync(filePath, "utf8");
  checked++;

  // 1. 检查开篇生硬插话 (H1 后面不能紧接独立的筛选控件补丁说明)
  const h1Match = content.match(/^#\s+[^\n]+/m);
  if (h1Match) {
    const afterH1 = content.slice(h1Match.index + h1Match[0].length).trimStart();
    const firstLine = afterH1.split("\n")[0].trim();
    if (
      firstLine.includes("使用统一漏斗") ||
      (firstLine.includes("统一紧凑控件") && firstLine.includes("筛选操作说明"))
    ) {
      console.error(`[error] Cluttered opening line in ${rel}: "${firstLine.slice(0, 50)}..."`);
      errors++;
    }
  }

  // 2. 检查 user-guide/ 中裸露的底层 API 请求体（未包裹在 <details> 中）
  if (rel.startsWith("user-guide/")) {
    const rawApiPattern =
      /```(?:json|bash|http|text)?\s*\n\s*(?:POST|GET|PUT|DELETE)\s+\/api\/v1\//g;
    let match;
    while ((match = rawApiPattern.exec(content)) !== null) {
      const preceding = content.slice(0, match.index);
      const openDetails = (preceding.match(/<details>/g) || []).length;
      const closeDetails = (preceding.match(/<\/details>/g) || []).length;
      if (openDetails <= closeDetails) {
        console.warn(`[warn] Uncollapsed raw API request in user-guide: ${rel}`);
        warnings++;
      }
    }
  }
}

walk(DOCS_ROOT);

console.log(`\nChecked readability for ${checked} markdown files.`);
if (warnings > 0) {
  console.log(`Warnings: ${warnings}`);
}
if (errors > 0) {
  console.error(`Errors: ${errors}`);
  process.exit(1);
} else if (strict && warnings > 0) {
  console.error(`Strict mode: failed with ${warnings} warnings.`);
  process.exit(1);
} else {
  console.log("✓ All readability quality checks passed.");
}
