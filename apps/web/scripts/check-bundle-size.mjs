#!/usr/bin/env node
import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(__dirname, "..");
const distDir = join(rootDir, "dist", "assets");
const budgetPath = join(rootDir, ".size-limit.json");

export function parseSize(s) {
  const m = String(s)
    .trim()
    .match(/^(\d+(?:\.\d+)?)\s*(B|KB|MB)?$/i);
  if (!m) throw new Error(`invalid size: ${s}`);
  const n = parseFloat(m[1]);
  const unit = (m[2] ?? "B").toUpperCase();
  return unit === "MB" ? n * 1024 * 1024 : unit === "KB" ? n * 1024 : n;
}

export function fmt(bytes) {
  return bytes >= 1024 * 1024
    ? (bytes / 1024 / 1024).toFixed(2) + " MB"
    : (bytes / 1024).toFixed(1) + " KB";
}

export function globMatch(pattern, name) {
  const re = new RegExp(
    "^" + pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$",
  );
  return re.test(name);
}

function main() {
  const budget = JSON.parse(readFileSync(budgetPath, "utf8"));
  let failed = false;

  let files;
  try {
    files = readdirSync(distDir);
  } catch {
    console.error(`[size-limit] dist/assets/ 不存在 — 先运行 \`pnpm build\``);
    process.exit(1);
  }

  console.log(`[size-limit] checking ${budget.length} budgets against ${files.length} dist files`);
  for (const entry of budget) {
    const matches = files.filter((f) => globMatch(entry.pattern, f));
    if (entry.required && matches.length !== 1) {
      console.error(`[size-limit] ${entry.name}: expected one chunk, found ${matches.length}`);
      failed = true;
      continue;
    }
    if (matches.length === 0) {
      console.warn(
        `[size-limit] ⚠ no file matches pattern '${entry.pattern}' (${entry.name}) — skipping`,
      );
      continue;
    }
    const maxBytes = parseSize(entry.max);
    for (const m of matches) {
      const actual = statSync(join(distDir, m)).size;
      const status = actual > maxBytes ? "FAIL" : "OK";
      const flag = actual > maxBytes ? "✗" : "✓";
      console.log(
        `${flag} ${entry.name.padEnd(16)} ${m.padEnd(48)} ${fmt(actual).padStart(10)} / ${fmt(maxBytes).padStart(10)}  [${status}]`,
      );
      if (actual > maxBytes) failed = true;
    }
  }

  if (failed) {
    console.error("\n[size-limit] ✗ bundle size budget exceeded");
    process.exit(1);
  }
  console.log("\n[size-limit] ✓ all bundles within budget");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
