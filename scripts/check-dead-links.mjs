#!/usr/bin/env node

import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { dirname, resolve, relative, basename, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(here, "..");

const SKIP_DIRS = new Set([
  "node_modules",
  ".git",
  "dist",
  "build",
  ".venv",
  "venv",
  "__pycache__",
  ".next",
  ".turbo",
  ".pnpm-store",
  ".cache",
]);

const SKIP_PATH_PREFIXES = [".claude/worktrees/"];

const SKIP_PATH_SEGMENTS = ["/vendor/", "/third_party/", "/third-party/"];

function shouldSkipDir(name) {
  return SKIP_DIRS.has(name) || name.startsWith(".");
}

function shouldSkipFile(relPath) {
  const p = relPath.split(sep).join("/");
  if (SKIP_PATH_PREFIXES.some((prefix) => p.startsWith(prefix))) return true;
  const padded = "/" + p;
  if (SKIP_PATH_SEGMENTS.some((seg) => padded.includes(seg))) return true;
  return false;
}

function walk(root, out = []) {
  for (const entry of readdirSync(root)) {
    const full = resolve(root, entry);
    let st;
    try {
      st = statSync(full);
    } catch {
      continue;
    }
    if (st.isDirectory()) {
      if (shouldSkipDir(entry)) continue;
      walk(full, out);
    } else if (st.isFile() && entry.endsWith(".md")) {
      const rel = relative(REPO, full);
      if (shouldSkipFile(rel)) continue;
      out.push(full);
    }
  }
  return out;
}

function extractLinks(text) {
  function maskCode(src) {
    const lines = src.split("\n");
    let inFence = false;
    for (let i = 0; i < lines.length; i++) {
      if (/^\s*```/.test(lines[i])) {
        inFence = !inFence;
        lines[i] = " ".repeat(lines[i].length);
        continue;
      }
      if (inFence) {
        lines[i] = " ".repeat(lines[i].length);
        continue;
      }
      lines[i] = lines[i].replace(/`[^`\n]*`/g, (m) => " ".repeat(m.length));
    }
    return lines.join("\n");
  }
  const masked = maskCode(text);

  const links = [];

  const lineStarts = [0];
  for (let i = 0; i < masked.length; i++) {
    if (masked.charCodeAt(i) === 10) lineStarts.push(i + 1);
  }
  const lineOf = (off) => {
    let lo = 0,
      hi = lineStarts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (lineStarts[mid] <= off) lo = mid;
      else hi = mid - 1;
    }
    return lo + 1;
  };

  const inlineRe = /\[(?:[^\]\\]|\\.)*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
  let m;
  while ((m = inlineRe.exec(masked))) {
    links.push({ target: m[1], offset: m.index, line: lineOf(m.index) });
  }

  const refRe = /^[ \t]*\[(?:[^\]\\]|\\.)+\]:\s*(\S+)/gm;
  while ((m = refRe.exec(masked))) {
    links.push({ target: m[1], offset: m.index, line: lineOf(m.index) });
  }

  return links;
}

function isExternal(target) {
  return /^(https?:|mailto:|ftp:|javascript:|data:|tel:)/i.test(target);
}

function stripFragment(target) {
  const i = target.indexOf("#");
  return i >= 0 ? target.slice(0, i) : target;
}

function stripQuery(target) {
  const i = target.indexOf("?");
  return i >= 0 ? target.slice(0, i) : target;
}

function stripLineSuffix(target) {
  return target.replace(/:\d+(?:-\d+)?$/, "");
}

function resolveTarget(sourceFile, target) {
  if (!target || target.startsWith("#")) return null;
  if (isExternal(target)) return null;

  let t = stripLineSuffix(stripQuery(stripFragment(target)));
  if (!t) return null;

  try {
    t = decodeURIComponent(t);
  } catch {}

  let abs;
  if (t.startsWith("/")) {
    abs = resolve(REPO, "." + t);
  } else {
    abs = resolve(dirname(sourceFile), t);
  }
  return abs;
}

function dlDistance(a, b, cap = 64) {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > cap) return cap + 1;
  const m = a.length,
    n = b.length;
  const prev2 = new Array(n + 1);
  const prev = new Array(n + 1);
  const cur = new Array(n + 1);
  for (let j = 0; j <= n; j++) prev[j] = j;
  for (let i = 1; i <= m; i++) {
    cur[0] = i;
    let rowMin = cur[0];
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        cur[j] = Math.min(cur[j], prev2[j - 2] + 1);
      }
      if (cur[j] < rowMin) rowMin = cur[j];
    }
    if (rowMin > cap) return cap + 1;
    for (let j = 0; j <= n; j++) {
      prev2[j] = prev[j];
      prev[j] = cur[j];
    }
  }
  return prev[n];
}

function similarityScore(missingRel, candidateRel) {
  const a = basename(missingRel);
  const b = basename(candidateRel);
  const nameDist = dlDistance(a.toLowerCase(), b.toLowerCase());
  const pathDist = dlDistance(missingRel.toLowerCase(), candidateRel.toLowerCase());
  return nameDist * 4 + pathDist;
}

function pickClosest(missingAbs, repoFiles, limit = 3) {
  const missingRel = relative(REPO, missingAbs);
  const scored = [];
  for (const f of repoFiles) {
    const rel = relative(REPO, f);
    const score = similarityScore(missingRel, rel);
    scored.push({ rel, score });
  }
  scored.sort((a, b) => a.score - b.score);
  return scored.slice(0, limit);
}

function collectAllFiles(root, out = []) {
  for (const entry of readdirSync(root)) {
    const full = resolve(root, entry);
    let st;
    try {
      st = statSync(full);
    } catch {
      continue;
    }
    if (st.isDirectory()) {
      if (shouldSkipDir(entry)) continue;
      collectAllFiles(full, out);
    } else if (st.isFile()) {
      out.push(full);
    }
  }
  return out;
}

const HISTORICAL_SOURCE_PREFIXES = ["docs/plans/", "docs/changelogs/", "docs/adr/archive/"];

function isHistoricalSource(relPath) {
  const p = relPath.split(sep).join("/");
  return HISTORICAL_SOURCE_PREFIXES.some((prefix) => p.startsWith(prefix));
}

function main() {
  const args = process.argv.slice(2);
  const jsonMode = args.includes("--json");
  const includeHistorical = args.includes("--include-historical");
  const positional = args.filter((a) => !a.startsWith("--"));
  const scanRoot = positional[0] ? resolve(REPO, positional[0]) : REPO;

  const mdFiles = walk(scanRoot).filter((f) => {
    if (includeHistorical) return true;
    return !isHistoricalSource(relative(REPO, f));
  });
  const allFiles = collectAllFiles(REPO);

  function existsAny(...paths) {
    return paths.some((p) => existsSync(p));
  }

  function tryResolveWithExtensions(abs) {
    return existsAny(abs, abs + ".md", resolve(abs, "index.md"));
  }

  const dead = [];
  for (const file of mdFiles) {
    const text = readFileSync(file, "utf8");
    const links = extractLinks(text);
    for (const { target, line } of links) {
      const abs = resolveTarget(file, target);
      if (abs == null) continue;
      if (tryResolveWithExtensions(abs)) continue;
      dead.push({
        file: relative(REPO, file),
        line,
        target,
        resolved: relative(REPO, abs),
        suggestions: pickClosest(abs, allFiles),
      });
    }
  }

  if (jsonMode) {
    process.stdout.write(JSON.stringify(dead, null, 2) + "\n");
  } else {
    if (dead.length === 0) {
      console.log(`✓ 未发现死链（扫描 ${mdFiles.length} 个 markdown 文件）`);
    } else {
      console.log(`✗ 发现 ${dead.length} 条死链（扫描 ${mdFiles.length} 个 markdown 文件）\n`);
      for (const d of dead) {
        console.log(`${d.file}:${d.line}`);
        console.log(`  链接：${d.target}`);
        console.log(`  解析为：${d.resolved}（不存在）`);
        if (d.suggestions.length > 0) {
          console.log(`  最相似：`);
          for (const s of d.suggestions) {
            console.log(`    - ${s.rel}  (score=${s.score})`);
          }
        }
        console.log("");
      }
    }
  }

  process.exit(dead.length === 0 ? 0 : 1);
}

main();
