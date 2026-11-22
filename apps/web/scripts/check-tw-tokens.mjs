#!/usr/bin/env node

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const SCAN_DIR = join(ROOT, "src");
const CI = !!process.env.GITHUB_ACTIONS;

const SEMANTIC_HUES = [
  "sky",
  "emerald",
  "violet",
  "amber",
  "rose",
  "red",
  "green",
  "blue",
  "yellow",
  "orange",
  "purple",
  "indigo",
  "teal",
  "cyan",
];

function walkSourceFiles(dir, predicate, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const s = statSync(p);
    if (s.isDirectory()) {
      if (name === "node_modules" || name === "dist" || name === "coverage") continue;
      walkSourceFiles(p, predicate, out);
    } else if (s.isFile() && predicate(name)) {
      out.push(p);
    }
  }
  return out;
}

function stripComments(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/\/\/[^\n]*/g, (m) => m.replace(/[^\n]/g, " "));
}

function extractClassNameChunks(text) {
  const chunks = [];
  const re = /className\s*=\s*/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    let i = m.index + m[0].length;
    const ch = text[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      let j = i + 1;
      while (j < text.length && text[j] !== quote) {
        if (text[j] === "\\") j++;
        j++;
      }
      chunks.push({ value: text.slice(i + 1, j), index: i + 1 });
      re.lastIndex = j + 1;
    } else if (ch === "{") {
      let depth = 0;
      let j = i;
      for (; j < text.length; j++) {
        const c = text[j];
        if (c === "{") depth++;
        else if (c === "}") {
          depth--;
          if (depth === 0) break;
        }
      }
      chunks.push({ value: text.slice(i + 1, j), index: i + 1 });
      re.lastIndex = j + 1;
    }
  }
  return chunks;
}

function lineOf(text, index) {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i++) {
    if (text[i] === "\n") line++;
  }
  return line;
}

const BARE_COLOR_RE = /#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?|oklch|oklab|color)\s*\(/;
const ARBITRARY_COLOR_RE =
  /\b(?:bg|text|border|ring|ring-offset|fill|stroke|from|via|to|outline|shadow|decoration|caret|accent|divide)-\[\s*(#[0-9a-fA-F]{3,8}|(?:rgba?|hsla?|oklch|oklab|color)\s*\(|var\(--)/;
const STATUS_CLASS_BY_HUE = {
  rose: "danger",
  amber: "caution",
  emerald: "positive",
  violet: "info",
  sky: "info-alt",
};
const STATUS_TEXT_RE = new RegExp(
  `\\b(?:[a-z-]+:)*!?text-(${Object.keys(STATUS_CLASS_BY_HUE).join("|")})-600(?:/[0-9]+)?!?\\b`,
  "g",
);
const STATUS_SOFT_BG_RE = new RegExp(
  `\\b(?:[a-z-]+:)*!?bg-(${Object.keys(STATUS_CLASS_BY_HUE).join("|")})-500/10!?\\b`,
  "g",
);
const ARBITRARY_TEXT_SIZE_RE = /(?:^|[\s"'`])((?:[^\s"'`]+:)*!?text-\[[0-9.]+px\]!?)(?=$|[\s"'`])/g;
const RAW_Z_INDEX_RE =
  /(?:^|[\s"'`])((?:[^\s"'`]+:)*!?z-(?:\[[0-9]+\]|[1-9][0-9]*)!?)(?=$|[\s"'`])/g;
const SHADCN_CSS = "src/styles/shadcn.css";
const ALLOWED_SHADCN_TAILWIND_PALETTE_RE =
  /^--color-(rose|amber|emerald|violet|sky)-(400|500|600)$/;

function checkChunk(value) {
  const issues = [];

  if (BARE_COLOR_RE.test(value)) {
    const hit = value.match(BARE_COLOR_RE);
    issues.push({ kind: "bare-color", detail: hit[0].trim() });
  }
  if (ARBITRARY_COLOR_RE.test(value)) {
    const hit = value.match(ARBITRARY_COLOR_RE);
    issues.push({ kind: "arbitrary-color", detail: hit[0].trim() });
  }

  const hueRe = new RegExp(`\\b(!)?(text|bg|border)-(${SEMANTIC_HUES.join("|")})-600\\b`, "g");
  let hm;
  while ((hm = hueRe.exec(value)) !== null) {
    const [, , prefix, hue] = hm;
    const darkRe = new RegExp(`dark:[a-z!:-]*${prefix}-${hue}-400\\b`);
    if (!darkRe.test(value)) {
      issues.push({
        kind: "dark-pair",
        detail: `${prefix}-${hue}-600 缺 dark:${prefix}-${hue}-400`,
      });
    }
  }

  return issues;
}

function checkStatusSemanticHints(value) {
  const hints = [];

  for (const match of value.matchAll(STATUS_TEXT_RE)) {
    const hue = match[1];
    hints.push({
      kind: "status-color",
      detail: `${match[0]} 可替换为 text-status-${STATUS_CLASS_BY_HUE[hue]}`,
    });
  }
  for (const match of value.matchAll(STATUS_SOFT_BG_RE)) {
    const hue = match[1];
    hints.push({
      kind: "status-color",
      detail: `${match[0]} 可替换为 bg-status-${STATUS_CLASS_BY_HUE[hue]}-soft`,
    });
  }

  return hints;
}

function checkScaleSemanticHints(value) {
  const hints = [];

  for (const match of value.matchAll(ARBITRARY_TEXT_SIZE_RE)) {
    hints.push({
      kind: "font-scale",
      detail: `${match[1]} 可替换为 text-2xs/text-xs/text-sm 或命名字号 token`,
    });
  }
  for (const match of value.matchAll(RAW_Z_INDEX_RE)) {
    hints.push({
      kind: "z-scale",
      detail: `${match[1]} 可替换为语义 z-* utility`,
    });
  }

  return hints;
}

function checkCssColorTokenRefs() {
  const files = walkSourceFiles(SCAN_DIR, (name) => name.endsWith(".css"));
  const usages = [];

  for (const file of files) {
    const raw = readFileSync(file, "utf8");
    const text = stripComments(raw);
    for (const match of text.matchAll(/var\(\s*(--color-[a-z0-9-]+)/g)) {
      const filePath = relative(ROOT, file);
      if (filePath === SHADCN_CSS && ALLOWED_SHADCN_TAILWIND_PALETTE_RE.test(match[1])) {
        continue;
      }
      usages.push({
        token: match[1],
        file: filePath,
        line: lineOf(text, match.index),
      });
    }
  }

  return usages.map((usage) => ({
    kind: "legacy-css-token",
    detail: `${usage.token} is a legacy CSS token; use --sc-* in CSS`,
    file: usage.file,
    line: usage.line,
  }));
}

function main() {
  const files = walkSourceFiles(SCAN_DIR, (name) => name.endsWith(".tsx"));
  const findings = [];
  const warnings = [];

  for (const file of files) {
    const raw = readFileSync(file, "utf8");
    const text = stripComments(raw);
    for (const { value, index } of extractClassNameChunks(text)) {
      const issues = checkChunk(value);
      for (const issue of issues) {
        findings.push({
          file: relative(ROOT, file),
          line: lineOf(text, index),
          ...issue,
        });
      }
      for (const warning of checkStatusSemanticHints(value)) {
        warnings.push({
          file: relative(ROOT, file),
          line: lineOf(text, index),
          ...warning,
        });
      }
      for (const warning of checkScaleSemanticHints(value)) {
        warnings.push({
          file: relative(ROOT, file),
          line: lineOf(text, index),
          ...warning,
        });
      }
    }
  }
  findings.push(...checkCssColorTokenRefs());

  if (findings.length === 0) {
    printWarnings(warnings);
    console.log(
      "✓ check-tw-tokens: className 无裸色 / 任意色值, 语义色暗色配对完整, CSS 无旧 --color-* 引用.",
    );
    process.exit(0);
  }

  const byKind = {};
  for (const f of findings) (byKind[f.kind] ??= []).push(f);

  const KIND_LABEL = {
    "bare-color": "裸色字面 (#hex / rgb / oklch) 写进 className",
    "arbitrary-color": "Tailwind 任意色值 util-[...] (绕过 token / 调色板)",
    "dark-pair": "语义色缺暗色配对 (设计规范 §2.2)",
    "legacy-css-token": "CSS 引用了旧 --color-* token",
  };

  printWarnings(warnings);
  console.warn(`✗ check-tw-tokens (blocking): ${findings.length} 处违反颜色规范\n`);
  for (const kind of ["bare-color", "arbitrary-color", "dark-pair", "legacy-css-token"]) {
    const list = byKind[kind];
    if (!list?.length) continue;
    console.warn(`▶ ${kind} (${list.length}): ${KIND_LABEL[kind]}`);
    for (const f of list) {
      console.warn(`  ${f.file}:${f.line}  ${f.detail}`);
      if (CI) {
        const msg = `[check-tw-tokens] ${KIND_LABEL[kind]}: ${f.detail}`;
        console.log(`::warning file=apps/web/${f.file},line=${f.line}::${msg}`);
      }
    }
    console.warn("");
  }

  console.warn(
    "修复指引: 中性色走 --sc-* (bg-background/text-foreground/border-border), " +
      "语义彩色走固定调色板 + 柔底 /10 + dark:提亮. 见设计规范 §2.2/§2.4. ",
  );
  process.exit(1);
}

function printWarnings(warnings) {
  if (warnings.length === 0) return;

  const WARNING_KIND_LABEL = {
    "status-color": "状态色应走 text-status-* / bg-status-*-soft",
    "font-scale": "任意字号应走紧凑字号 scale",
    "z-scale": "z-index 应走语义 z-* utility",
  };

  console.warn(`⚠ check-tw-tokens (warning): ${warnings.length} 处样式可继续语义化\n`);
  for (const f of warnings) {
    console.warn(`  ${f.file}:${f.line}  ${f.detail}`);
    if (CI) {
      const msg = `[check-tw-tokens] ${WARNING_KIND_LABEL[f.kind] ?? "样式可语义化"}: ${f.detail}`;
      console.log(`::warning file=apps/web/${f.file},line=${f.line}::${msg}`);
    }
  }
  console.warn("");
}

main();
