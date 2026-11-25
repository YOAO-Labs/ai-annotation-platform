#!/usr/bin/env node

import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(here, "..");

let latestTag = "";
try {
  latestTag = execSync("git describe --tags --abbrev=0 2>/dev/null", {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).trim();
} catch {
  console.log("changelog version check: no git tag found, skipping");
  process.exit(0);
}

if (!latestTag) {
  console.log("changelog version check: no git tag found, skipping");
  process.exit(0);
}

const tagVersion = latestTag.replace(/^v/, "");
const changelog = readFileSync(resolve(REPO_ROOT, "CHANGELOG.md"), "utf8");
const m = changelog.match(/##\s*\[?v?(\d+\.\d+\.\d+)\]?/);
if (!m) {
  console.log(`::warning::CHANGELOG.md 顶部未找到版本号（最新 tag: ${latestTag}）`);
  process.exit(0);
}

const changelogVersion = m[1];
if (cmp(changelogVersion, tagVersion) < 0) {
  console.log(`::warning::CHANGELOG.md 最新版本 v${changelogVersion} 落后于 git tag ${latestTag}`);
  process.exit(0);
}
console.log(`changelog version check OK: CHANGELOG v${changelogVersion} >= tag ${latestTag}`);

function cmp(a, b) {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return 0;
}
