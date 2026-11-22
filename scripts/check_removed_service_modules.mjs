#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(here, "..");

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

function isFacadeFile(relPath) {
  return REMOVED_FILE_PATHS.has(relPath);
}

function findReferences(text, relPath) {
  const hits = [];
  for (const dotted of REMOVED_DOTTED) {
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
      continue;
    }

    if (!isAllowed(relPath)) {
      const refs = findReferences(text, relPath);
      for (const ref of refs) {
        offenders.push(
          `${relPath}:${ref.line}: references removed module '${ref.path}' — ${ref.context}`,
        );
      }
    }

    if (checkHistoricalLinks && relPath.endsWith(".md")) {
      for (const filePath of REMOVED_FILE_PATHS) {
        const name = filePath.split("/").pop();
        const baseName = name.replace(/\.py$/, "");
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
      `\nUse the domain-package paths instead of the removed service modules. ` +
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
