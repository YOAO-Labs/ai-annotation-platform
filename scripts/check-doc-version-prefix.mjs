#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const SCAN = [/^docs-site\/.*\.md$/, /^README\.md$/, /^DEV\.md$/];
const EXEMPT = [
  /^CHANGELOG\.md$/,
  /^docs\/adr\//,
  /^docs-site\/dev\/adr\//,
  /\.generated\.md$/,
  /^docs-site\/\.vitepress\/dist\//,
];

const taggedVersion = String.raw`v\d+\.(?:\d+|x)(?:\.(?:\d+|x))*\+?`;
const patchVersion = String.raw`v?\d+\.\d+\.(?:\d+|x)(?![\d.])\+?`;
const version = `(?:${taggedVersion}|${patchVersion})`;
const releaseNote = new RegExp(
  [
    String.raw`(?:\b(?:since|TODO)\s*[:(]?\s*|(?:起始版本|引入版本)\s*[:：]\s*)${version}`,
    String.raw`[（(]\s*${patchVersion}\s*[）)，,；;]`,
    String.raw`(?:^|\s|[（(])${version}\s*(?:[·：:]|起|之前|之后|新增|引入|发布)`,
    String.raw`^\s*(?:#{1,6}\s+|<!--\s*)${taggedVersion}(?:\s|$)`,
  ].join("|"),
  "i",
);

function parseArgs(argv) {
  const out = {
    base: "",
    head: "",
    staged: false,
    files: [],
    format: "text",
    writeMarkdown: "",
    strict: false,
  };
  for (const arg of argv) {
    if (arg.startsWith("--base=")) out.base = arg.slice("--base=".length);
    else if (arg.startsWith("--head=")) out.head = arg.slice("--head=".length);
    else if (arg === "--staged") out.staged = true;
    else if (arg === "--strict") out.strict = true;
    else if (arg.startsWith("--files=")) {
      out.files = arg
        .slice("--files=".length)
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean);
    } else if (arg.startsWith("--format=")) out.format = arg.slice("--format=".length);
    else if (arg.startsWith("--write-markdown="))
      out.writeMarkdown = arg.slice("--write-markdown=".length);
  }
  return out;
}

function runGit(args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" });
}

function isDoc(file) {
  return SCAN.some((r) => r.test(file)) && !EXEMPT.some((r) => r.test(file));
}

function changedDocFiles(opts) {
  if (opts.files.length > 0) return opts.files.filter(isDoc);
  let range;
  if (opts.base && opts.head) range = ["diff", "--name-only", `${opts.base}...${opts.head}`];
  else if (opts.staged) range = ["diff", "--name-only", "--cached"];
  else range = ["diff", "--name-only", "HEAD"];
  return runGit(range)
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean)
    .filter(isDoc);
}

function addedLines(file, opts) {
  if (opts.files.length > 0) {
    let text;
    try {
      text = readFileSync(resolve(repoRoot, file), "utf8");
    } catch {
      return [];
    }
    return text.split("\n").map((content, i) => ({ line: i + 1, content }));
  }
  let diffArgs;
  if (opts.base && opts.head) diffArgs = ["diff", "-U0", `${opts.base}...${opts.head}`, "--", file];
  else if (opts.staged) diffArgs = ["diff", "-U0", "--cached", "--", file];
  else diffArgs = ["diff", "-U0", "HEAD", "--", file];

  const out = runGit(diffArgs).split("\n");
  const added = [];
  let newLine = 0;
  for (const raw of out) {
    const hunk = raw.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      newLine = Number(hunk[1]);
      continue;
    }
    if (raw.startsWith("+++")) continue;
    if (raw.startsWith("+")) {
      added.push({ line: newLine, content: raw.slice(1) });
      newLine++;
    }
  }
  return added;
}

function scanFile(file, opts) {
  const findings = [];
  for (const { line, content } of addedLines(file, opts)) {
    if (releaseNote.test(content)) {
      findings.push({ path: file, line, snippet: content.trim().slice(0, 120) });
    }
  }
  return findings;
}

const args = parseArgs(process.argv.slice(2));
const findings = changedDocFiles(args).flatMap((f) => scanFile(f, args));

const HINT =
  "现行文档只写当前行为；删除正文、注释和 frontmatter 中的产品版本沿革，发布记录留在 CHANGELOG 或 Git 历史。";

function toMarkdown() {
  const lines = ["<!-- doc-version-prefix-check -->", "## Doc Version-Prefix Style Check", ""];
  if (findings.length === 0) {
    lines.push("- No release-history annotations found in changed docs.");
    return lines.join("\n");
  }
  lines.push(`- Findings: ${findings.length} — ${HINT}`, "", "### Findings");
  for (const f of findings) lines.push(`- \`${f.path}:${f.line}\` — \`${f.snippet}\``);
  return lines.join("\n");
}

if (args.writeMarkdown) {
  const abs = resolve(repoRoot, args.writeMarkdown);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, toMarkdown() + "\n");
}

if (args.format === "github") {
  for (const f of findings) {
    process.stdout.write(`::warning file=${f.path},line=${f.line}::doc version: ${HINT}\n`);
  }
}

if (findings.length === 0) {
  process.stdout.write("文档版本号检查: 未发现产品版本沿革标记。\n");
} else {
  process.stdout.write(`文档版本号检查: 发现 ${findings.length} 处建议改写 (${HINT}):\n`);
  for (const f of findings) process.stdout.write(`  ${f.path}:${f.line}  ${f.snippet}\n`);
}

process.exit(args.strict && findings.length > 0 ? 1 : 0);
