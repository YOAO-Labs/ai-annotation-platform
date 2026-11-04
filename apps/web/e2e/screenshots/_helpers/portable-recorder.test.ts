import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import type { Page } from "@playwright/test";
import { archivePortableRecording } from "./portable-recorder.ts";

test("portable encoding retains source and merges only captured assets", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "portable-recorder-test-"));
  try {
    const source = path.join(root, "source.webm");
    execFileSync("ffmpeg", [
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "color=c=blue:s=320x180:r=25:d=0.2",
      "-c:v",
      "libvpx",
      source,
    ]);
    let closed = false;
    const page = {
      video: () => ({
        saveAs: async (target: string) => {
          assert.ok(closed);
          fs.copyFileSync(source, target);
        },
      }),
      close: async () => {
        closed = true;
      },
      context: () => ({ browser: () => ({ version: () => "test-browser" }) }),
      viewportSize: () => ({ width: 320, height: 180 }),
    } as unknown as Page;
    const input = {
      repoRoot: root,
      runId: "test-run",
      source: "flow.ts",
      testTitle: "test",
      seedRevision: "test",
      capturedCommit: "test",
      sourceWorktreeDirty: true,
      capture: { backend_requirements: "none", inference: "none" },
    };
    for (const assetId of ["bbox-draw", "sam-tools/smart-point"]) {
      await archivePortableRecording(page, { ...input, assetId });
    }
    const manifestPath = path.join(root, ".artifacts/recordings/test-run/manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    const before = manifest.entries[".artifacts/recordings/test-run/bbox-draw.mp4"];
    await archivePortableRecording(page, { ...input, assetId: "sam-tools/smart-point" });
    const after = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    assert.equal(Object.keys(after.entries).length, 2);
    assert.deepEqual(after.entries[".artifacts/recordings/test-run/bbox-draw.mp4"], before);
    assert.equal(before.media.codec, "h264");
    assert.equal(before.media.width, 320);
    assert.equal(before.capture.trim, "untrimmed");
    assert.equal(before.capture.backend_requirements, "none");
    assert.equal(before.capture.inference, "none");
    assert.ok(fs.existsSync(path.join(root, before.capture.source)));
    assert.ok(
      !fs.existsSync(path.join(root, "apps/web/e2e/screenshots/outputs/flow-manifest.json")),
    );
    await assert.rejects(
      archivePortableRecording(page, { ...input, assetId: "../escape" }),
      /Unsafe/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
