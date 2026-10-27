import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { collectMarkdownImageReferences, walkFiles } from "../../scripts/image-reference-utils.mjs";
import { readScreenshotManifest } from "../../scripts/screenshot-manifest-utils.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "../..");
export const DOCS_ROOT = path.join(REPO_ROOT, "docs-site");
export const USER_GUIDE_ROOT = path.join(DOCS_ROOT, "user-guide");
export const REVIEW_PATH = path.join(DOCS_ROOT, "maintainers/media-reviews.json");
export const FLOW_MANIFEST_PATH = path.join(
  REPO_ROOT,
  "apps/web/e2e/screenshots/outputs/flow-manifest.json",
);
export const SCREENSHOT_MANIFEST_PATH = path.join(
  REPO_ROOT,
  "apps/web/e2e/screenshots/outputs/manifest.json",
);

function repoKey(absolute) {
  return path.relative(REPO_ROOT, absolute).replaceAll("\\", "/");
}

function addRecord(records, absolute, source, kind) {
  const key = repoKey(absolute);
  if (key.startsWith("../") || path.isAbsolute(key)) return;
  const existing = records.get(key) ?? { absolute, sources: new Set(), kinds: new Set() };
  existing.sources.add(source);
  existing.kinds.add(kind);
  records.set(key, existing);
}

function resolvePublicPath(source) {
  const cleaned = source.split(/[?#]/, 1)[0];
  if (cleaned.startsWith("/media/") || cleaned.startsWith("/home/")) {
    return path.join(DOCS_ROOT, "public", cleaned.slice(1));
  }
  if (cleaned.startsWith("/user-guide/images/")) {
    return path.join(USER_GUIDE_ROOT, "images", cleaned.slice("/user-guide/images/".length));
  }
  return null;
}

export function collectPublishedMedia({ scanRoot = USER_GUIDE_ROOT } = {}) {
  const records = new Map();
  const imageReferences = collectMarkdownImageReferences({
    scanRoot,
    repoRoot: REPO_ROOT,
    docsRoot: DOCS_ROOT,
  });
  for (const [key, record] of imageReferences) {
    records.set(key, {
      absolute: record.absolute,
      sources: new Set(record.sources),
      kinds: new Set(["image"]),
    });
  }

  for (const markdownPath of walkFiles(scanRoot, (name) => name.endsWith(".md"))) {
    const content = fs.readFileSync(markdownPath, "utf8");
    const source = repoKey(markdownPath);
    for (const tag of content.matchAll(/<(?:DocsVideo|video|source)\b[^>]*>/g)) {
      for (const attribute of tag[0].matchAll(/\b(src|poster)\s*=\s*["']([^"']+)["']/g)) {
        const absolute = resolvePublicPath(attribute[2]);
        if (absolute)
          addRecord(records, absolute, source, attribute[1] === "poster" ? "poster" : "video");
      }
    }
  }

  const themeRoot = path.join(DOCS_ROOT, ".vitepress/theme");
  for (const sourcePath of walkFiles(themeRoot, (name) => /\.(?:vue|ts)$/.test(name))) {
    const content = fs.readFileSync(sourcePath, "utf8");
    const source = repoKey(sourcePath);
    for (const match of content.matchAll(
      /\bimport\s+[\w$]+\s+from\s*["'](\.{1,2}\/[^"']+\.(?:png|gif|jpe?g|webp|svg)(?:[?#][^"']*)?)["']/g,
    )) {
      const absolute = path.resolve(path.dirname(sourcePath), match[1].split(/[?#]/, 1)[0]);
      addRecord(records, absolute, source, "image");
    }
    for (const match of content.matchAll(/withBase\(\s*["'](\/(?:home|media)\/[^"']+)["']\s*\)/g)) {
      const absolute = resolvePublicPath(match[1]);
      if (absolute)
        addRecord(records, absolute, source, /poster/i.test(match[1]) ? "poster" : "video");
    }
  }
  return records;
}

function readJson(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

export function readReviewRegistry() {
  const registry = readJson(REVIEW_PATH, {
    schema_version: 1,
    default_max_age_days: 30,
    entries: {},
  });
  if (registry.schema_version !== 1 || !registry.entries) {
    throw new Error(`不支持的媒体复核清单：${REVIEW_PATH}`);
  }
  return registry;
}

export function readProvenance() {
  const screenshot = readScreenshotManifest(SCREENSHOT_MANIFEST_PATH);
  const flow = readJson(FLOW_MANIFEST_PATH, { schema_version: 1, entries: {} });
  if (flow.schema_version !== 1 || !flow.entries) {
    throw new Error(`不支持的流程媒体清单：${FLOW_MANIFEST_PATH}`);
  }
  const entries = new Map();
  for (const [key, entry] of Object.entries(screenshot.entries)) {
    entries.set(key, {
      kind: entry.auto ? "static-auto" : "static-manual",
      captured_commit: entry.source_commit ?? screenshot.metadata.source_commit ?? null,
      source_worktree_dirty:
        entry.source_worktree_dirty ?? screenshot.metadata.source_worktree_dirty ?? false,
      seed_revision: entry.seed_revision ?? screenshot.metadata.seed_revision ?? null,
      source: entry.source ?? null,
      watch_paths: [entry.source].filter(Boolean),
      sha256: entry.sha256 ?? null,
    });
  }
  for (const [key, entry] of Object.entries(flow.entries)) {
    entries.set(key, { kind: "flow", ...entry });
  }
  return { entries, screenshotMetadata: screenshot.metadata, flowMetadata: flow };
}

export function sha256File(filePath) {
  return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

export function currentCommit() {
  return execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).trim();
}

function commitExists(commit) {
  if (!commit || !/^[0-9a-f]{40}$/i.test(commit)) return false;
  try {
    execFileSync("git", ["cat-file", "-e", `${commit}^{commit}`], {
      cwd: REPO_ROOT,
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

export function changedWatchPaths(commit, watchPaths) {
  const paths = [...new Set(watchPaths.filter(Boolean))];
  if (!commitExists(commit)) return ["<复核提交不存在>"];
  if (paths.length === 0) return [];
  const committed = execFileSync(
    "git",
    ["diff", "--name-only", `${commit}..HEAD`, "--", ...paths],
    {
      cwd: REPO_ROOT,
      encoding: "utf8",
    },
  )
    .trim()
    .split("\n")
    .filter(Boolean);
  const working = execFileSync(
    "git",
    ["status", "--porcelain", "--untracked-files=all", "--", ...paths],
    { cwd: REPO_ROOT, encoding: "utf8" },
  )
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => line.slice(3));
  return [...new Set([...committed, ...working])].sort();
}

export function defaultWatchPaths(key, record, provenance) {
  return [
    key,
    ...record.sources,
    ...(Array.isArray(provenance?.watch_paths) ? provenance.watch_paths : []),
    provenance?.source,
  ]
    .filter(Boolean)
    .filter((candidate, index, all) => all.indexOf(candidate) === index)
    .sort();
}

export function classifyMedia({ exists, currentHash, review, changedPaths, ageDays, maxAgeDays }) {
  if (!exists) return { status: "broken", reasons: ["文件不存在"] };
  if (!review) return { status: "review_due", reasons: ["尚未绑定人工复核提交"] };
  const reasons = [];
  if (review.sha256 !== currentHash) reasons.push("文件哈希已变化");
  if (changedPaths.length > 0) reasons.push(`关联路径已变化：${changedPaths.join(", ")}`);
  if (reasons.length > 0) return { status: "stale", reasons };
  if (!Number.isFinite(ageDays) || ageDays > maxAgeDays) {
    return { status: "review_due", reasons: [`距上次复核超过 ${maxAgeDays} 天`] };
  }
  return { status: "current", reasons: [] };
}

export function currentSeedRevision() {
  const source = fs.readFileSync(
    path.join(REPO_ROOT, "apps/api/app/services/screenshot_seed_spec.py"),
    "utf8",
  );
  return source.match(/^SEED_REVISION\s*=\s*["']([^"']+)["']/m)?.[1] ?? null;
}

export function tierAOpenItems() {
  const checklistPath = path.join(DOCS_ROOT, "maintainers/image-checklist.md");
  const lines = fs.readFileSync(checklistPath, "utf8").split("\n");
  return lines
    .filter((line) => /^- \[ \].*\*\*\[Tier A\]\*/.test(line))
    .map((line) => line.replace(/^- \[ \]\s*/, ""));
}

// Keep automatic integrity checks independent of human review freshness.
export function mediaAuditFailed(
  report,
  { integrity = false, strict = false, release = false } = {},
) {
  const integrityFailure =
    report.counts.broken > 0 ||
    report.assets.some((item) => item.provenance_issues.includes("文件哈希与生成来源清单不一致"));
  const strictFailure = integrityFailure || report.counts.stale > 0;
  const releaseFailure =
    strictFailure ||
    report.counts.review_due > 0 ||
    report.assets.some((item) => item.provenance_issues.length > 0);
  return (
    (release && releaseFailure) || (strict && strictFailure) || (integrity && integrityFailure)
  );
}
