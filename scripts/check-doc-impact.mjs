#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");
const mapPath = resolve(repoRoot, ".github/docs-impact-map.json");

function parseArgs(argv) {
  const out = {
    base: "",
    head: "",
    staged: false,
    files: [],
    format: "text",
    write: "",
    writeMarkdown: "",
  };
  for (const arg of argv) {
    if (arg.startsWith("--base=")) out.base = arg.slice("--base=".length);
    else if (arg.startsWith("--head=")) out.head = arg.slice("--head=".length);
    else if (arg === "--staged") out.staged = true;
    else if (arg.startsWith("--files=")) {
      out.files = arg
        .slice("--files=".length)
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean);
    } else if (arg.startsWith("--format=")) out.format = arg.slice("--format=".length);
    else if (arg.startsWith("--write=")) out.write = arg.slice("--write=".length);
    else if (arg.startsWith("--write-markdown=")) {
      out.writeMarkdown = arg.slice("--write-markdown=".length);
    }
  }
  return out;
}

function runGit(args) {
  return execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
  }).trim();
}

function listChangedFiles(opts) {
  if (opts.files.length > 0) {
    return opts.files;
  }
  let output = "";
  if (opts.base && opts.head) {
    output = runGit(["diff", "--name-only", `${opts.base}...${opts.head}`]);
  } else if (opts.staged) {
    output = runGit(["diff", "--name-only", "--cached"]);
  } else {
    output = runGit(["diff", "--name-only", "HEAD"]);
  }
  return output
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
}

function loadMap() {
  return JSON.parse(readFileSync(mapPath, "utf8"));
}

const globCache = new Map();

// 支持三种 matcher：目录前缀（以 `/` 结尾）、`*` / `**` 通配、精确文件路径。
// `*` 只跨路径段内字符，`**` 跨 `/`。这样文件改名 / 拆分后 map 不易失效。
function globToRegExp(glob) {
  let regexp = globCache.get(glob);
  if (regexp) return regexp;
  const escaped = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  const source = escaped
    .replace(/\*\*/g, "\u0000")
    .replace(/\*/g, "[^/]*")
    .replace(/\u0000/g, ".*");
  regexp = new RegExp(`^${source}$`);
  globCache.set(glob, regexp);
  return regexp;
}

function matches(file, matcher) {
  if (matcher.endsWith("/")) return file.startsWith(matcher);
  if (matcher.includes("*")) return globToRegExp(matcher).test(file);
  return file === matcher;
}

function uniq(values) {
  return [...new Set(values)];
}

function buildReport(changedFiles, impactMap) {
  const matchedRules = [];

  for (const rule of impactMap.rules) {
    const matchedFiles = changedFiles.filter((file) =>
      rule.codeMatchers.some((matcher) => matches(file, matcher)),
    );
    if (matchedFiles.length === 0) continue;

    const docsTouched = changedFiles.filter((file) =>
      (rule.acceptDocs || []).some((matcher) => matches(file, matcher)),
    );

    matchedRules.push({
      id: rule.id,
      name: rule.name,
      matchedFiles: uniq(matchedFiles),
      recommendDocs: uniq(rule.recommendDocs || []),
      docsTouched: uniq(docsTouched),
      hasAcceptedDocChanges: docsTouched.length > 0,
    });
  }

  const recommendedDocs = uniq(matchedRules.flatMap((rule) => rule.recommendDocs)).sort();

  const unmatchedRules = matchedRules.filter((rule) => !rule.hasAcceptedDocChanges);

  return {
    changedFiles,
    matchedRules,
    recommendedDocs,
    unmatchedRules,
    hasPotentialMiss: unmatchedRules.length > 0,
  };
}

function toMarkdown(report) {
  const lines = ["## Docs Impact Check", ""];

  if (report.matchedRules.length === 0) {
    lines.push("- No docs-impact rules matched this diff.");
    return lines.join("\n");
  }

  lines.push(`- Matched rules: ${report.matchedRules.length}`);
  lines.push(`- Recommended docs/artifacts to review: ${report.recommendedDocs.length}`);
  lines.push(`- Potential missing doc updates: ${report.hasPotentialMiss ? "yes" : "no"}`);
  lines.push("");
  lines.push("### Recommended Docs");
  for (const doc of report.recommendedDocs) {
    lines.push(`- \`${doc}\``);
  }
  lines.push("");
  lines.push("### Matched Rules");
  for (const rule of report.matchedRules) {
    const changed = rule.matchedFiles.map((x) => `\`${x}\``).join(", ");
    const touched = rule.docsTouched.length
      ? rule.docsTouched.map((x) => `\`${x}\``).join(", ")
      : "_none_";
    lines.push(`#### ${rule.name}`);
    lines.push(`- Changed code: ${changed}`);
    lines.push(`- Suggested docs: ${rule.recommendDocs.map((x) => `\`${x}\``).join(", ")}`);
    lines.push(`- Docs touched in this PR: ${touched}`);
  }

  if (report.unmatchedRules.length > 0) {
    lines.push("");
    lines.push("### Suggested Follow-up");
    for (const rule of report.unmatchedRules) {
      lines.push(
        `- \`${rule.id}\` matched code changes but none of its mapped docs/artifacts were updated.`,
      );
    }
  }

  return lines.join("\n");
}

function toText(report) {
  if (report.matchedRules.length === 0) {
    return "No docs-impact rules matched this diff.\n";
  }

  const lines = [
    `Matched rules: ${report.matchedRules.length}`,
    `Recommended docs/artifacts: ${report.recommendedDocs.length}`,
    `Potential missing updates: ${report.hasPotentialMiss ? "yes" : "no"}`,
    "",
    "Recommended docs/artifacts:",
    ...report.recommendedDocs.map((doc) => `- ${doc}`),
  ];

  for (const rule of report.matchedRules) {
    lines.push("");
    lines.push(`[${rule.id}] ${rule.name}`);
    lines.push(`changed code: ${rule.matchedFiles.join(", ")}`);
    lines.push(`suggested docs: ${rule.recommendDocs.join(", ")}`);
    lines.push(`docs touched: ${rule.docsTouched.length ? rule.docsTouched.join(", ") : "none"}`);
  }

  return lines.join("\n") + "\n";
}

function writeFileIfNeeded(targetPath, content) {
  if (!targetPath) return;
  const abs = resolve(repoRoot, targetPath);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, content);
}

const args = parseArgs(process.argv.slice(2));
const changedFiles = listChangedFiles(args);
const report = buildReport(changedFiles, loadMap());
const markdown = toMarkdown(report);
const text = toText(report);

writeFileIfNeeded(args.write, JSON.stringify(report, null, 2) + "\n");
writeFileIfNeeded(args.writeMarkdown, markdown + "\n");

if (args.format === "json") {
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
} else if (args.format === "markdown") {
  process.stdout.write(markdown + "\n");
} else {
  process.stdout.write(text);
}
