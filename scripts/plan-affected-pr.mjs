import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";

const { affected_pr: suites } = JSON.parse(
  readFileSync(new URL("./test-suites.json", import.meta.url), "utf8"),
);
const pythonPolicy = fileURLToPath(new URL("./affected-pr-python.py", import.meta.url));
const full = (reason) => ({ scope: "full", reason, docs: false, web: [], api: [] });
const fullSha = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
const docPath =
  /^(?:(?:README|DEV|ROADMAP|CHANGELOG)\.md|(?:ROADMAP(?:\/archive)?|docs\/(?:adr(?:\/(?:archive|evidence))?|plans(?:\/(?:archive|backlog|evidence\/[\w-]+))?|articles|changelogs|migration|research))\/[\w-]+(?:\.(?:\d+|x)[\w-]*)*\.md)$/;
const activeMarkdown =
  /<<<|\{\{|\{%|@(?:include|\[code\])|!\s*(?:include|\[!include)|^\s*(?:\.{2}\s+[\w-]+::|:{2,}|import\s|export\s)|(?:javascript|vbscript|data)\s*:/im;
const staticTag = /^<\/?(?:a|br|div|h[1-6]|img|p|strong|sub|table|td|tr)\b[^<>]*>/i;

function protectedMarkdown(text) {
  if (/^(?:\uFEFF|---\s*\n|\+\+\+\s*\n)/.test(text) || activeMarkdown.test(text)) return null;
  const protectedLines = [];
  let fence = "";
  let comment = false;
  for (const line of text.split("\n")) {
    const marker = line.trim();
    if (fence) {
      protectedLines.push(line);
      if (marker.startsWith(fence) && [...marker].every((c) => c === fence[0])) fence = "";
    } else if (comment || line.includes("<!--")) {
      protectedLines.push(line);
      comment = !line.includes("-->");
    } else if (/`{3}|~{3}/.test(line)) {
      const open = /^ {0,3}(`{3,}|~{3,})[^`~]*$/.exec(line);
      if (!open) return null;
      fence = open[1];
      protectedLines.push(line);
    } else {
      for (const tag of line.matchAll(/<(?=[/!a-z])/gi)) {
        if (!staticTag.test(line.slice(tag.index))) return null;
      }
      if (/[<>{}\\\p{Cc}]/u.test(line) || /^(?: {4}|\s*(?:#!|---$|\+\+\+$))/.test(line)) {
        protectedLines.push(line);
      }
    }
  }
  return fence || comment ? null : protectedLines.join("\n");
}

const frameworkFallback =
  /^( +: String\(run\.config\.framework \?\? "[^"\\\r\n]*"\)\})(?:\{" "\})?(?:\n +<span className="[\w -]+">\n +\{String\(run\.config\.framework_version \?\? "[^"\\\r\n]*"\)\}\n +<\/span>)?$(?=\n +<\/td>)/gm;
function frameworkCell(text) {
  return text.replace(frameworkFallback, "$1");
}

function splitProportions(text) {
  return text
    .replace(
      /^  const total = (?:known|unknown|\d+)(?: [+-] (?:known|unknown|\d+))*;$/gm,
      "<TOTAL>",
    )
    .replace(
      /\{total \? (\(\(splits\[split\]\.images \/ total\) \* 100\)\.toFixed\(1\)) : "0\.0"\}%/g,
      "{$1}%",
    )
    .replace(
      /(\{splits\[split\]\.images\} 张 ·)(?:\{" "\})?\s+(?=\{\(\(splits\[split\]\.images \/ total\))/g,
      "$1 ",
    );
}

function addedChartAssertions(before, after) {
  const original = before.split("\n");
  const candidate = after.split("\n");
  let i = 0;
  while (i < original.length && original[i] === candidate[i]) i++;
  const added = candidate.length - original.length;
  const assertion =
    /^    expect\(screen\.(?:getByText|getByLabelText)\("[^"\\\r\n]*"\)\)\.toBeVisible\(\);$/;
  return (
    added > 0 &&
    [original[i - 1], original[i]].some((line) => line?.startsWith("    expect(")) &&
    candidate.slice(i, i + added).every((line) => assertion.test(line)) &&
    original.slice(i).every((line, offset) => line === candidate[i + added + offset])
  );
}

function reviewedEdit(suite, path, before, after, approvePython) {
  const production = path === suite.source;
  switch (suite.rule) {
    case "framework-cell":
      return production && frameworkCell(before) === frameworkCell(after);
    case "split-proportions":
      return production
        ? splitProportions(before) === splitProportions(after)
        : addedChartAssertions(before, after);
    case "monthly-summary":
      return approvePython(production ? "calculation" : "test", before, after);
    default:
      return false;
  }
}

export function planAffected(changes, approvePython) {
  const checks = { web: new Set(), api: new Set() };
  let owner;
  let docs = false;
  for (const { path, status, oldMode, newMode, before, after } of changes) {
    const at = JSON.stringify(path);
    if (
      !((status === "A" && oldMode === "000000") || (status === "M" && oldMode === "100644")) ||
      newMode !== "100644"
    ) {
      return full(`Deleted, moved or non-regular path: ${at}`);
    }
    if (docPath.test(path)) {
      const protectedBefore = protectedMarkdown(before);
      if (protectedBefore === null || protectedBefore !== protectedMarkdown(after)) {
        return full(`Not a passive Markdown edit: ${at}`);
      }
      docs = true;
      continue;
    }
    const suite = suites.find(({ source, test }) => source === path || test === path);
    if (!suite || status !== "M") return full(`No reviewed change rule for ${at}`);
    if (!reviewedEdit(suite, path, before, after, approvePython)) {
      return full(`Outside the ${suite.rule} edit rule: ${at}`);
    }
    if (owner && owner !== suite.owner)
      return full(`Multiple owners (${owner}, ${suite.owner}): ${at}`);
    owner = suite.owner;
    checks[suite.runner].add(suite.test);
  }
  if (!docs && !owner) return full("Empty diff");
  return {
    scope: "affected",
    reason: owner
      ? `Reviewed ${owner} change${docs ? " with documentation" : ""}`
      : "Passive repository Markdown",
    docs,
    web: [...checks.web].sort(),
    api: [...checks.api].sort(),
  };
}

const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
function git(...args) {
  return utf8.decode(
    execFileSync("git", ["--no-replace-objects", ...args], {
      stdio: ["ignore", "pipe", "pipe"],
      maxBuffer: 4 * 1024 * 1024,
    }),
  );
}

function* gitChanges(base, tested) {
  const fields = git(
    "diff",
    "--raw",
    "--no-abbrev",
    "-z",
    "--no-renames",
    "--no-relative",
    "--no-ext-diff",
    "--ignore-submodules=none",
    base,
    tested,
    "--",
  ).split("\0");
  if (fields.pop() !== "" || fields.length % 2) throw new Error("Incomplete raw diff");
  for (let i = 0; i < fields.length; i += 2) {
    const [, oldMode, newMode, oldId, newId, status] =
      /^:(\d{6}) (\d{6}) ([a-f0-9]+) ([a-f0-9]+) ([A-Z])$/.exec(fields[i]);
    const path = fields[i + 1];
    const change = { path, oldMode, newMode, status };
    if (
      newMode === "100644" &&
      (docPath.test(path) || suites.some((s) => s.source === path || s.test === path))
    ) {
      change.before = oldMode === "100644" ? git("cat-file", "blob", oldId) : "";
      change.after = git("cat-file", "blob", newId);
      if (change.before.includes("\0") || change.after.includes("\0"))
        throw new Error("Binary blob");
    }
    yield change;
  }
}

function approvePython(kind, before, after) {
  return (
    spawnSync("python3", ["-I", "-S", pythonPolicy, kind], {
      input: JSON.stringify([before, after]),
      stdio: ["pipe", "ignore", "pipe"],
      timeout: 5000,
    }).status === 0
  );
}

function planCheckout(base, tested) {
  if (
    !fullSha.test(tested) ||
    git("rev-parse", "--is-inside-work-tree").trim() !== "true" ||
    git("rev-parse", "--verify", "HEAD^{commit}").trim() !== tested
  ) {
    throw new Error("TESTED_SHA must be the full commit ID of the checked-out HEAD");
  }
  if (!fullSha.test(base)) return full("No full base commit ID");
  try {
    if (git("rev-parse", "--verify", `${base}^{commit}`).trim() !== base)
      return full("Base is not a commit");
    const plan = planAffected(gitChanges(base, tested), approvePython);
    const targets = [...plan.web, ...plan.api];
    if (targets.length) {
      const entries = git("ls-tree", "--full-tree", "-z", tested, "--", ...targets)
        .split("\0")
        .slice(0, -1);
      if (
        entries.length !== targets.length ||
        !entries.every((e) => e.startsWith("100644 blob "))
      ) {
        return full("A registered test is missing or is not a regular file at TESTED_SHA");
      }
    }
    return plan;
  } catch {
    return full("Base commit, complete diff or source text unavailable");
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.length !== 4) throw new Error("Usage: plan-affected-pr.mjs BASE_SHA TESTED_SHA");
  console.log(JSON.stringify(planCheckout(process.argv[2], process.argv[3])));
}
