import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  classifyMedia,
  collectPublishedMedia,
  mediaAuditFailed,
  REPO_ROOT,
  USER_GUIDE_ROOT,
} from "./media-review-lib.mjs";

test("missing media is broken", () => {
  assert.equal(
    classifyMedia({
      exists: false,
      currentHash: null,
      review: null,
      changedPaths: [],
      ageDays: 0,
      maxAgeDays: 30,
    }).status,
    "broken",
  );
});

test("unreviewed media is review due", () => {
  assert.equal(
    classifyMedia({
      exists: true,
      currentHash: "abc",
      review: null,
      changedPaths: [],
      ageDays: 0,
      maxAgeDays: 30,
    }).status,
    "review_due",
  );
});

test("hash or watched source change makes a review stale", () => {
  assert.equal(
    classifyMedia({
      exists: true,
      currentHash: "new",
      review: { sha256: "old" },
      changedPaths: ["apps/web/src/example.tsx"],
      ageDays: 1,
      maxAgeDays: 30,
    }).status,
    "stale",
  );
});

test("unchanged recent review is current", () => {
  assert.equal(
    classifyMedia({
      exists: true,
      currentHash: "same",
      review: { sha256: "same" },
      changedPaths: [],
      ageDays: 3,
      maxAgeDays: 30,
    }).status,
    "current",
  );
});

test("homepage static image imports remain in the published media inventory", () => {
  const records = collectPublishedMedia();
  for (const name of ["video-track", "pointcloud", "review", "data-manager"]) {
    const entry = records.get(`docs-site/.vitepress/theme/assets/home/hero/${name}.webp`);
    assert.ok(entry, `${name} Hero import is missing`);
    assert.ok(entry.kinds.has("image"));
    assert.ok(entry.sources.has("docs-site/.vitepress/theme/components/home/DataAtlasHero.vue"));
  }
});

function imageCheck(script) {
  return JSON.parse(
    execFileSync(process.execPath, [path.join(REPO_ROOT, "docs-site/scripts", script), "--json"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    }),
  );
}

test("image coverage gates recognize screenshots used only by the homepage Vue component", () => {
  const key = "docs-site/user-guide/images/superadmin/model-market/list.png";
  const entry = collectPublishedMedia().get(key);
  assert.ok(entry?.sources.has("docs-site/.vitepress/theme/components/home/ProductProof.vue"));
  assert.equal(
    imageCheck("check-image-manifest.mjs").failures.some((failure) => failure.includes(key)),
    false,
    "the manifest gate must recognize the homepage import",
  );
  assert.equal(
    imageCheck("check-orphan-images.mjs").orphans.includes("superadmin/model-market/list.png"),
    false,
    "the orphan gate must recognize the homepage import",
  );
});

test("image coverage still rejects an image without any published consumer", (t) => {
  const directory = fs.mkdtempSync(path.join(USER_GUIDE_ROOT, "images/.reference-check-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const image = path.join(directory, "orphan.svg");
  fs.writeFileSync(image, '<svg xmlns="http://www.w3.org/2000/svg" width="7" height="9"/>');
  const relative = path.relative(path.join(USER_GUIDE_ROOT, "images"), image).replaceAll("\\", "/");
  assert.ok(imageCheck("check-orphan-images.mjs").orphans.includes(relative));
});

test("PR integrity allows stale and overdue reviews but blocks missing files and source hash mismatch", () => {
  const report = {
    counts: { broken: 0, stale: 1, review_due: 1, current: 0 },
    assets: [{ provenance_issues: [] }],
  };
  assert.equal(mediaAuditFailed(report, { integrity: true }), false);
  assert.equal(mediaAuditFailed(report, { strict: true }), true);
  assert.equal(mediaAuditFailed(report, { release: true }), true);
  report.counts.broken = 1;
  assert.equal(mediaAuditFailed(report, { integrity: true }), true);
  assert.equal(mediaAuditFailed(report), false);
  report.counts.broken = 0;
  report.assets[0].provenance_issues = ["文件哈希与生成来源清单不一致"];
  assert.equal(mediaAuditFailed(report, { integrity: true }), true);
  assert.equal(mediaAuditFailed(report), false);
});

test("release retains overdue-review and provenance requirements even when integrity is selected", () => {
  const report = {
    counts: { broken: 0, stale: 0, review_due: 1, current: 0 },
    assets: [{ provenance_issues: [] }],
  };
  assert.equal(mediaAuditFailed(report, { strict: true }), false);
  assert.equal(mediaAuditFailed(report, { integrity: true, release: true }), true);
  report.counts.review_due = 0;
  report.assets[0].provenance_issues = ["没有生成来源清单"];
  assert.equal(mediaAuditFailed(report, { integrity: true }), false);
  assert.equal(mediaAuditFailed(report, { strict: true }), false);
  assert.equal(mediaAuditFailed(report, { release: true }), true);
  report.assets[0].provenance_issues = [];
  assert.equal(mediaAuditFailed(report, { release: true }), false);
});
