#!/usr/bin/env node

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PROPER_NOUNS = new Set(["Claude", "Code", "Python", "Playwright", "VitePress", "MinIO"]);
const RE_FILE = /^(?:ci|[a-z0-9]+(?:-[a-z0-9]+)+)\.yml$/;
const isAcronym = (word) => /^[A-Z][A-Z0-9]{1,4}$/.test(word);

function checkWord(word, index) {
  if (isAcronym(word) || PROPER_NOUNS.has(word)) return null;
  const lower = word.toLowerCase();
  const capitalized = word[0] === word[0].toUpperCase() && word.slice(1) === lower.slice(1);
  if (capitalized) return index === 0 ? null : `"${word}" should be lowercase in sentence case`;
  if (word === lower) return index === 0 ? `first word "${word}" must start uppercase` : null;
  return `"${word}" has mixed casing`;
}

function scalar(raw) {
  const match = raw.match(/^(?:"([^"\\]*)"|'((?:[^']|'')*)'|([^'"#][^#]*?))(?:\s+#.*)?\s*$/);
  return match ? (match[1] ?? match[2]?.replaceAll("''", "'") ?? match[3]).trim() : "";
}

export function checkWorkflows(files) {
  const findings = [];
  const seen = new Map();
  for (const { file, text } of files) {
    const path = `.github/workflows/${file}`;
    const report = (line, msg) => findings.push({ path, line, msg });
    const sentence = (name, line) => {
      if (!/^[A-Za-z][A-Za-z0-9-]*(?: [A-Za-z][A-Za-z0-9-]*)*$/.test(name)) {
        report(line, "name must be static words in sentence case");
        return;
      }
      name.split(" ").forEach((word, i) => {
        const msg = checkWord(word, i);
        if (msg) report(line, msg);
      });
    };
    if (!RE_FILE.test(file)) report(1, "file name should be `<domain>-<action>.yml` (or ci.yml)");
    const lines = text.split(/\r?\n/);
    const top = lines.findIndex((line) => /^name:/.test(line));
    if (top < 0) report(1, "missing top-level `name:`");
    else sentence(scalar(lines[top].slice(5).trim()), top + 1);

    const start = lines.findIndex((line) => /^jobs:\s*(?:#.*)?$/.test(line));
    if (start < 0) {
      report(1, "missing block-style `jobs:` mapping");
      continue;
    }
    let job = null;
    let count = 0;
    const finish = () => {
      if (job && !job.named) report(job.line, `job "${job.id}" must set an explicit name`);
    };
    for (let i = start + 1; i < lines.length; i++) {
      const line = lines[i];
      if (/^\s*(?:#.*)?$/.test(line)) continue;
      if (/^\S/.test(line)) break;
      const key = line.match(/^  ([A-Za-z_][A-Za-z0-9_-]*):\s*(?:#.*)?$/);
      if (key) {
        finish();
        job = { id: key[1], line: i + 1, named: false };
        count++;
        continue;
      }
      if (!job || !/^ {4,}\S/.test(line)) {
        report(i + 1, "use block-style jobs with two-space keys and four-space properties");
        continue;
      }
      const property = line.match(/^    name:\s*(.*)$/);
      if (!property) continue;
      if (job.named) report(i + 1, `job "${job.id}" has duplicate name properties`);
      job.named = true;
      const name = scalar(property[1]);
      if (!name.includes(" ")) {
        report(i + 1, `job "${job.id}" name must include a domain and responsibility`);
      }
      sentence(name, i + 1);
      if (name) {
        if (seen.has(name))
          report(i + 1, `duplicate job name "${name}"; already used at ${seen.get(name)}`);
        else seen.set(name, `${path}:${i + 1}`);
      }
    }
    finish();
    if (!count) report(start + 1, "jobs must contain block-style job entries");
  }
  return findings;
}

export function main(args = process.argv.slice(2)) {
  const strict = args.includes("--strict");
  const workflowsDir = join(repoRoot, ".github", "workflows");
  const files = readdirSync(workflowsDir)
    .filter((file) => /\.ya?ml$/.test(file))
    .sort()
    .map((file) => ({ file, text: readFileSync(join(workflowsDir, file), "utf8") }));
  const findings = checkWorkflows(files);
  for (const f of findings) {
    process.stdout.write(
      `::${strict ? "error" : "warning"} file=${f.path},line=${f.line}::workflow naming: ${f.msg}\n`,
    );
  }
  process.stdout.write(
    findings.length
      ? `Workflow 命名检查: 发现 ${findings.length} 处问题。\n`
      : "Workflow 命名检查: 全部合规。\n",
  );
  return strict && findings.length > 0 ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
