#!/usr/bin/env node

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const ADR_DIR = resolve(here, "../docs/adr");
const ARCHIVE_DIR = join(ADR_DIR, "archive");

const SKIP = new Set(["README.md", "TEMPLATE.md"]);
const NUM_RE = /^(\d{4})-[a-z0-9-]+\.md$/i;

function collect(dir, label) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".md") && !SKIP.has(f))
    .map((f) => ({ f, dir, label }));
}

const files = [...collect(ADR_DIR, "active"), ...collect(ARCHIVE_DIR, "archive")];
const errors = [];
const numbers = new Map();

for (const { f, dir, label } of files) {
  const m = f.match(NUM_RE);
  if (!m) {
    errors.push(`Bad ADR filename [${label}]: ${f}（应为 NNNN-kebab-case.md）`);
    continue;
  }
  const n = parseInt(m[1], 10);
  if (numbers.has(n)) {
    const prev = numbers.get(n);
    errors.push(`Duplicate ADR number ${m[1]}: ${prev.label}/${prev.f} vs ${label}/${f}`);
  }
  numbers.set(n, { f, label });

  const content = readFileSync(join(dir, f), "utf8");
  const statusOk =
    /^\s*[-*]?\s*\**\s*Status\s*\**\s*[:：]/im.test(content) ||
    /^##?\s*Status\b/im.test(content) ||
    /^\**\s*状态\s*\**\s*[:：]/im.test(content);
  if (!statusOk) {
    errors.push(`Missing Status field [${label}]: ${f}`);
  }
}

const sorted = [...numbers.keys()].sort((a, b) => a - b);
for (let i = 1; i < sorted.length; i++) {
  if (sorted[i] !== sorted[i - 1] + 1) {
    errors.push(`ADR numbering gap: ${sorted[i - 1]} → ${sorted[i]}`);
  }
}

if (errors.length) {
  console.error("ADR check failed:");
  for (const e of errors) console.error("  - " + e);
  process.exit(1);
}

console.log(`ADR check OK (${numbers.size} files, ${sorted[0]} → ${sorted.at(-1)})`);
