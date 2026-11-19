#!/usr/bin/env node
/**
 * Tailwind className 颜色规范检查 (blocking 模式) — Tailwind 时代的暗色护栏.
 *
 * 随 UI 全量迁移到 shadcn/Tailwind,颜色逐步从 *.module.css 移到 *.tsx 的 className.
 * 本脚本扫 className 和残留 CSS,对齐设计规范禁则:
 *   1. 裸色: className 里出现 #hex / rgb() / rgba() / hsl() / oklch() / oklab() / color()
 *   2. 任意色值: bg-[#...] / text-[rgb(...)] / border-[hsl(...)] 等 Tailwind arbitrary color value
 *   3. (建议级) 暗色配对: 语义色 text/bg/border-<hue>-600 应伴随 dark:...-<hue>-400 (设计规范 §2.2)
 *   4. (warning) 状态色应走 text-status-* / bg-status-*-soft,不再手写状态 hue + dark: 对.
 *   5. (warning) 任意字号 text-[Npx] 应走紧凑字号 scale.
 *   6. (warning) 裸数字 z-index 应走语义 z-* utility.
 *
 * 中性色/表面/边框走 shadcn 的 --sc-* token (bg-background / text-foreground / border-border ...),
 * 语义彩色走固定调色板 (sky/emerald/violet/amber/rose) + 柔底 /10 + 暗色提亮. 不在 className 里写裸色.
 *
 * 有发现时 exit 1,阻断 CI。
 *
 * 唯一可信来源: docs-site/dev/reference/design-system.md.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const SCAN_DIR = join(ROOT, "src");
const CI = !!process.env.GITHUB_ACTIONS;

// 设计规范 §2.2 的固定语义色相 + 常见基础色相 (限定范围, 降噪).
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

// 去掉行注释 // ... 与块注释 /* ... */ (避免注释里举例的色值误报), 用空格替换保留行号.
function stripComments(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/\/\/[^\n]*/g, (m) => m.replace(/[^\n]/g, " "));
}

// 抽取所有 className=... 的值块. 支持:
//   className="..."  className='...'  className={ ...任意表达式(cn(), 模板串)... }
// 返回 [{ value, index }] (index 为值块在原文中的起始偏移, 用于定位行号).
function extractClassNameChunks(text) {
  const chunks = [];
  const re = /className\s*=\s*/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    let i = m.index + m[0].length;
    const ch = text[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      // 引号字符串 (className="..." 或模板串字面)
      const quote = ch;
      let j = i + 1;
      while (j < text.length && text[j] !== quote) {
        if (text[j] === "\\") j++;
        j++;
      }
      chunks.push({ value: text.slice(i + 1, j), index: i + 1 });
      re.lastIndex = j + 1;
    } else if (ch === "{") {
      // 表达式块 className={ ... } — 平衡括号扫描
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

// 裸色字面: #hex (3/4/6/8 位) / rgb( / rgba( / hsl( / hsla( / oklch( / oklab( / color(
const BARE_COLOR_RE = /#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?|oklch|oklab|color)\s*\(/;
// Tailwind 任意色值: <util>-[<color>]  (color = #hex / rgb()/hsl()/oklch() / var(--...))
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

  // 暗色配对: 对每个 (text|bg|border)-<hue>-600 检查同 className 是否有 dark:(...)-<hue>-400.
  // 修正匹配 — 容许 `!` 修饰符 + 中间变体前缀(hover:/focus: 等).
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
