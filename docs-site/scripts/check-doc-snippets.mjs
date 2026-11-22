#!/usr/bin/env node

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const DOCS_ROOT = resolve(here, "..");
const REPO_ROOT = resolve(here, "../..");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (name.endsWith(".md")) out.push(p);
  }
  return out;
}

const SNIPPET_RE =
  /<!--\s*snippet:(\S+?)\s*-->\s*\n```[^\n]*\n([\s\S]*?)\n```\s*\n<!--\s*\/snippet\s*-->/g;

function parseSpec(spec) {
  const range = spec.match(/^(.+):(\d+)-(\d+)$/);
  if (range) return { path: range[1], mode: "range", start: +range[2], end: +range[3] };
  const sym = spec.match(/^(.+)#([A-Za-z_][A-Za-z0-9_]*)$/);
  if (sym) return { path: sym[1], mode: "symbol", symbol: sym[2] };
  return { path: spec, mode: "whole" };
}

function locatePythonSymbol(srcLines, symbol) {
  const headerRe = new RegExp(`^(\\s*)(?:async\\s+def|def|class)\\s+${symbol}\\b`);
  let defIdx = -1;
  let baseIndent = 0;
  for (let i = 0; i < srcLines.length; i++) {
    const m = srcLines[i].match(headerRe);
    if (m) {
      defIdx = i;
      baseIndent = m[1].length;
      break;
    }
  }
  if (defIdx < 0) return null;

  let startIdx = defIdx;
  for (let i = defIdx - 1; i >= 0; i--) {
    const line = srcLines[i];
    const m = line.match(/^(\s*)@\w/);
    if (m && m[1].length === baseIndent) startIdx = i;
    else break;
  }

  let parens = 0;
  let headerEnd = defIdx;
  for (let i = defIdx; i < srcLines.length; i++) {
    const line = srcLines[i];
    for (const ch of line) {
      if (ch === "(" || ch === "[" || ch === "{") parens++;
      else if (ch === ")" || ch === "]" || ch === "}") parens--;
    }
    headerEnd = i;
    if (parens === 0 && /:\s*(#.*)?$/.test(line)) break;
  }

  let endIdx = headerEnd;
  for (let i = headerEnd + 1; i < srcLines.length; i++) {
    const line = srcLines[i];
    if (line.trim() === "") {
      endIdx = i;
      continue;
    }
    const indent = line.match(/^(\s*)/)[0].length;
    if (indent > baseIndent) {
      endIdx = i;
      continue;
    }
    break;
  }
  while (endIdx > headerEnd && srcLines[endIdx].trim() === "") endIdx--;
  return { start: startIdx + 1, end: endIdx + 1 };
}

let failed = 0;
let checked = 0;

for (const mdPath of walk(DOCS_ROOT)) {
  const text = readFileSync(mdPath, "utf8");
  for (const m of text.matchAll(SNIPPET_RE)) {
    checked++;
    const [, specRaw, blockBody] = m;
    const spec = parseSpec(specRaw);
    const srcAbs = resolve(REPO_ROOT, spec.path);
    const mdRel = relative(REPO_ROOT, mdPath);

    let srcText;
    try {
      srcText = readFileSync(srcAbs, "utf8");
    } catch {
      console.error(`[check-doc-snippets] ${mdRel}: 源文件不存在 ${spec.path}`);
      failed++;
      continue;
    }
    const srcLines = srcText.replace(/\n$/, "").split("\n");

    let start;
    let end;
    let label;
    if (spec.mode === "range") {
      start = spec.start;
      end = spec.end;
      label = `${spec.path}:${start}-${end}`;
    } else if (spec.mode === "symbol") {
      if (!spec.path.endsWith(".py")) {
        console.error(`[check-doc-snippets] ${mdRel}: 符号锚点目前只支持 .py（${specRaw}）`);
        failed++;
        continue;
      }
      const loc = locatePythonSymbol(srcLines, spec.symbol);
      if (!loc) {
        console.error(
          `[check-doc-snippets] ${mdRel}: 在 ${spec.path} 中找不到符号 #${spec.symbol}`,
        );
        failed++;
        continue;
      }
      start = loc.start;
      end = loc.end;
      label = `${spec.path}#${spec.symbol} (L${start}-${end})`;
    } else {
      start = 1;
      end = srcLines.length;
      label = `${spec.path} (whole file, L${start}-${end})`;
    }

    if (start < 1 || end > srcLines.length || start > end) {
      console.error(
        `[check-doc-snippets] ${mdRel}: 行号越界 ${label}（源文件共 ${srcLines.length} 行）`,
      );
      failed++;
      continue;
    }

    const expected = srcLines.slice(start - 1, end).join("\n");
    if (expected !== blockBody) {
      failed++;
      console.error(`\n[check-doc-snippets] FAIL: ${mdRel} ↔ ${label}`);
      const exLines = expected.split("\n");
      const blLines = blockBody.split("\n");
      const max = Math.max(exLines.length, blLines.length);
      for (let i = 0; i < max; i++) {
        const a = blLines[i] ?? "<EOF>";
        const b = exLines[i] ?? "<EOF>";
        if (a !== b) {
          console.error(`  L${start + i}`);
          console.error(`    doc:    ${JSON.stringify(a)}`);
          console.error(`    source: ${JSON.stringify(b)}`);
        }
      }
    }
  }
}

if (failed > 0) {
  console.error(
    `\n[check-doc-snippets] ${failed} snippet 不一致 / ${checked} 检查；请同步文档代码块或修正锚点。`,
  );
  process.exit(1);
}
console.log(`[check-doc-snippets] OK · ${checked} snippet 全部一致`);
