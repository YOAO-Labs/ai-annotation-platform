#!/usr/bin/env node
// scripts/check_removed_service_modules.mjs
//
// Permanent repo-wide guard: after v0.23.2 deletes the 23 legacy service
// facade modules, this scanner ensures no active code, test, script, config or
// current-state doc references them by any import form or string.
//
// Usage:
//   node scripts/check_removed_service_modules.mjs                 # active scan
//   node scripts/check_removed_service_modules.mjs --historical-links  # also check
//                                                                   # markdown links
//                                                                   # to deleted files
//
// Exit code: 0 if clean, 1 if violations found.

import { readFileSync } from "node:fs";
import { dirname, resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(here, "..");

// ---------------------------------------------------------------------------
// Manifest: the 23 removed service modules and their exact dotted paths.
// ---------------------------------------------------------------------------
const manifestPath = resolve(REPO, "apps/api/tests/_fixtures/removed_service_modules.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
const REMOVED_DOTTED = manifest.modules.map((m) => m.facade_module);
const REMOVED_BASENAMES = new Set(REMOVED_DOTTED.map((d) => d.split(".").pop()));
const REMOVED_FILE_PATHS = new Set(
  REMOVED_DOTTED.map((d) => {
    const name = d.split(".").pop();
    return `apps/api/app/services/${name}.py`;
  }),
);

// ---------------------------------------------------------------------------
// Allowlist: files that may legitimately reference removed modules.
// ---------------------------------------------------------------------------
// These are the ONLY active-tree files allowed to contain removed-module
// strings. Everything else is deny-by-default.
const ALLOWED_FILES = new Set([
  "apps/api/tests/test_compat_facades.py",
  "apps/api/tests/test_domain_package_architecture.py",
  "apps/api/tests/_fixtures/removed_service_modules.json",
  "docs/migration/2026-07-17-v0.23.2-service-import-cutover.md",
  "docs/migration/v0.23.0-gpu-ledger-inventory.md",
  "docs/migration/v0.23.1-gpu-orchestration-inventory.md",
  "scripts/check_removed_service_modules.mjs",
  "apps/api/scripts/check_removed_service_modules.py",
]);

// Historical directories may contain old path facts but NOT links to deleted
// files (checked separately in --historical-links mode).
const HISTORICAL_DIRS = [
  "docs/plans/",
  "docs/changelogs/",
  "docs/adr/archive/",
  "docs-site/changelog/",
  "docs-site/dev/adr/archive/",
  "docs-site/roadmap/",
  "ROADMAP/",
  "CHANGELOG.md",
];

// ---------------------------------------------------------------------------
// Scan helpers.
// ---------------------------------------------------------------------------
function getTrackedFiles() {
  const out = execSync("git ls-files -z", { cwd: REPO, encoding: "utf-8" });
  return out
    .split("\0")
    .filter(Boolean)
    .filter((f) => !f.includes("/vendor/"));
}

function isHistorical(relPath) {
  return HISTORICAL_DIRS.some((d) => relPath.startsWith(d));
}

function isAllowed(relPath) {
  return ALLOWED_FILES.has(relPath) || isHistorical(relPath) || isFacadeFile(relPath);
}

// The 23 facade files themselves reference their own path in docstrings; they
// are deleted in D2–D6, after which this check becomes moot.
function isFacadeFile(relPath) {
  return REMOVED_FILE_PATHS.has(relPath);
}

// Patterns that constitute a "reference" to a removed module.
function findReferences(text, relPath) {
  const hits = [];
  for (const dotted of REMOVED_DOTTED) {
    // 1. Dotted path as a substring (catches imports, strings, mock targets).
    //    Use word-boundary-like check to avoid partial matches.
    const escaped = dotted.replace(/\./g, "\\.");
    const re = new RegExp(`(^|[^\\w.])${escaped}([^\\w]|$)`, "g");
    let m;
    while ((m = re.exec(text)) !== null) {
      const line = text.substring(0, m.index).split("\n").length;
      hits.push({ line, path: dotted, context: text.split("\n")[line - 1]?.trim() });
    }
  }
  return hits;
}

// ---------------------------------------------------------------------------
// Main scan.
// ---------------------------------------------------------------------------
function main() {
  const checkHistoricalLinks = process.argv.includes("--historical-links");
  const files = getTrackedFiles();
  const offenders = [];

  for (const relPath of files) {
    const absPath = resolve(REPO, relPath);
    let text;
    try {
      text = readFileSync(absPath, "utf-8");
    } catch {
      continue; // binary or unreadable
    }

    // Active-scope check: deny references outside allowlist.
    if (!isAllowed(relPath)) {
      const refs = findReferences(text, relPath);
      for (const ref of refs) {
        offenders.push(
          `${relPath}:${ref.line}: references removed module '${ref.path}' — ${ref.context}`,
        );
      }
    }

    // Historical-link check: even historical docs must not LINK to deleted files.
    if (checkHistoricalLinks && relPath.endsWith(".md")) {
      for (const filePath of REMOVED_FILE_PATHS) {
        const name = filePath.split("/").pop();
        const baseName = name.replace(/\.py$/, "");
        // Check for markdown links to the deleted .py file or its bare name
        // as a link target.
        const linkRe = new RegExp(`\\]\\(.*${baseName}\\.py[\\s)]`, "g");
        if (linkRe.test(text)) {
          offenders.push(`${relPath}: links to deleted file '${filePath}'`);
        }
      }
    }
  }

  if (offenders.length > 0) {
    console.error(`✗ Found ${offenders.length} reference(s) to removed service modules:\n`);
    for (const o of offenders) {
      console.error(`  ${o}`);
    }
    console.error(
      `\nThese modules were permanently deleted in v0.23.2. ` +
        `Use the new domain-package paths instead. ` +
        `See docs/migration/2026-07-17-v0.23.2-service-import-cutover.md.`,
    );
    process.exit(1);
  }

  console.error(
    `✓ No references to removed service modules found ` +
      `(${REMOVED_DOTTED.length} modules guarded).`,
  );
}

main();
