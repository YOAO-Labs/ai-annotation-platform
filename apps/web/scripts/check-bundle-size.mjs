#!/usr/bin/env node
/**
 * 前端 bundle size budget。
 *
 * 在 `vite build` 之后运行：扫描 `dist/assets/*.js`，按 `.size-limit.json`
 * 中的 glob 规则匹配并对比 max 字节数。任何文件超出 → 进程非零退出 →
 * CI 阻断 PR。不依赖第三方工具，只用 Node std lib。
 *
 * .size-limit.json 形如：
 *   [
 *     { "name": "main", "pattern": "index-*.js", "max": "500 KB" },
 *     { "name": "konva", "pattern": "vendor-konva-*.js", "max": "300 KB" }
 *   ]
 *
 * 单位支持：B / KB / MB（1024 进制）。
 */
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
  // 仅支持 `*` 通配（满足当前所有 budget 模式）
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

// 仅在直接执行时运行主流程；被单测 import 时只暴露纯函数，不触发副作用。
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
