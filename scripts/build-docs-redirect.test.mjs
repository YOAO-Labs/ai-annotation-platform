import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { buildRedirectBundle, computeRedirectUrl, TARGET_ORIGIN } from "./build-docs-redirect.mjs";

test("maps legacy YOAO and ai-annotation-platform paths while preserving query and hash", () => {
  assert.equal(computeRedirectUrl("/YOAO/"), `${TARGET_ORIGIN}/`);
  assert.equal(computeRedirectUrl("/ai-annotation-platform/"), `${TARGET_ORIGIN}/`);
  assert.equal(
    computeRedirectUrl(
      "/ai-annotation-platform/dev/reference/ml-backend-protocol",
      "?tab=v2",
      "#schema",
    ),
    `${TARGET_ORIGIN}/dev/reference/ml-backend-protocol?tab=v2#schema`,
  );
  assert.equal(
    computeRedirectUrl("/YOAO/user-guide/workbench/video-track", "", "#timeline"),
    `${TARGET_ORIGIN}/user-guide/workbench/video-track#timeline`,
  );
  assert.equal(computeRedirectUrl("/api/"), `${TARGET_ORIGIN}/api/`);
});

test("builds index.html, 404.html, and .nojekyll under 20 KB", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "yoao-docs-redirect-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  buildRedirectBundle(dir);
  const index = readFileSync(join(dir, "index.html"), "utf8");
  const notFound = readFileSync(join(dir, "404.html"), "utf8");
  assert.equal(index, notFound);
  assert.match(index, new RegExp(TARGET_ORIGIN));
  const totalBytes = statSync(join(dir, "index.html")).size + statSync(join(dir, "404.html")).size;
  assert.ok(totalBytes < 20 * 1024, `expected < 20KB, got ${totalBytes}`);
});
