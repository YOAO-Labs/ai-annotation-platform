#!/usr/bin/env node

import { readFileSync, readdirSync } from "node:fs";
import { join, relative, extname, basename } from "node:path";
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
const SKIP_PATHS = new Set(["index.md"]);

const thresholdArg = process.argv.find((a) => a.startsWith("--threshold="));
const THRESHOLD_DAYS = thresholdArg ? parseInt(thresholdArg.split("=")[1], 10) : 180;
const THRESHOLD_MS = THRESHOLD_DAYS * 24 * 60 * 60 * 1000;

const now = Date.now();
let stale = 0;
let missing = 0;
let checked = 0;

function parseFrontmatterField(content, field) {
  if (!content.startsWith("---")) return null;
  const end = content.indexOf("\n---", 3);
  if (end === -1) return null;
  const block = content.slice(0, end);
  const m = block.match(new RegExp(`^${field}:\\s*(.+)$`, "m"));
  return m ? m[1].trim() : null;
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
  if (SKIP_GENERATED.test(filePath)) return;
  if (basename(filePath) === "index.md" && !content.startsWith("---")) return;

  checked++;
  const rawDate = parseFrontmatterField(content, "last_reviewed");

  if (!rawDate) {
    console.warn(`[warn] Missing last_reviewed: ${rel}`);
    missing++;
    return;
  }

  const reviewedMs = Date.parse(rawDate);
  if (isNaN(reviewedMs)) {
    console.warn(`[warn] Invalid last_reviewed date "${rawDate}": ${rel}`);
    missing++;
    return;
  }

  const ageDays = Math.floor((now - reviewedMs) / (24 * 60 * 60 * 1000));
  if (now - reviewedMs > THRESHOLD_MS) {
    console.warn(`[warn] Stale (${ageDays}d since review): ${rel}`);
    stale++;
  }
}

walk(DOCS_ROOT);

console.log(`\nChecked ${checked} files (threshold: ${THRESHOLD_DAYS} days).`);
if (stale > 0 || missing > 0) {
  console.warn(`${stale} stale file(s), ${missing} without last_reviewed. Consider reviewing.`);
} else {
  console.log("All files are fresh.");
}
