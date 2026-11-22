import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { collectAssetReferences } from "./asset-references.mjs";

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CHECKER = "docs-site/scripts/check-orphan-images.mjs";

test("collects homepage image imports, local video sources and posters", () => {
  const records = collectAssetReferences(REPO_ROOT);
  const theme = "docs-site/.vitepress/theme";
  const hero = `${theme}/components/home/DataAtlasHero.vue`;
  const proof = `${theme}/components/home/ProductProof.vue`;
  for (const name of ["video-track", "pointcloud", "review", "data-manager"]) {
    assert.ok(records.get(`${theme}/assets/home/hero/${name}.webp`)?.sources.has(hero), name);
  }
  for (const name of [
    "public/home/ai-assisted-annotation.webm",
    "public/home/ai-assisted-annotation.mp4",
    "public/home/ai-assisted-annotation-poster.webp",
    "user-guide/images/superadmin/model-market/list.png",
  ]) {
    assert.ok(records.get(`docs-site/${name}`)?.sources.has(proof), name);
  }
  assert.ok(records.has("docs-site/user-guide/images/getting-started/e2e.gif"));
});

test("strict checking accepts ad-hoc assets and fails for missing references and orphans", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "docs-references-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const file of [
    CHECKER,
    "docs-site/scripts/asset-references.mjs",
    "scripts/image-reference-utils.mjs",
  ]) {
    const destination = path.join(root, file);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(path.join(REPO_ROOT, file), destination);
  }

  const page = "docs-site/user-guide/test.md";
  const images = "docs-site/user-guide/images";
  const media = path.join(root, "docs-site/public/media");
  fs.mkdirSync(path.join(root, images), { recursive: true });
  fs.mkdirSync(media, { recursive: true });
  const diagram = '<svg xmlns="http://www.w3.org/2000/svg" width="7" height="9"/>';
  fs.writeFileSync(path.join(root, images, "capture.svg"), diagram);
  fs.writeFileSync(path.join(media, "clip.mp4"), "");
  fs.writeFileSync(path.join(media, "poster.svg"), diagram);
  fs.writeFileSync(
    path.join(root, page),
    '![Ad-hoc screenshot](./images/capture.svg)\n<DocsVideo src="/media/clip.mp4" poster="/media/poster.svg" />\n',
  );

  function check() {
    const result = spawnSync(process.execPath, [CHECKER, "--json", "--strict"], {
      cwd: root,
      encoding: "utf8",
    });
    assert.ifError(result.error);
    return { status: result.status, report: JSON.parse(result.stdout) };
  }
  const valid = check();
  assert.equal(valid.status, 0);
  assert.deepEqual(valid.report.orphans, []);
  assert.deepEqual(valid.report.brokenReferences, []);

  fs.writeFileSync(
    path.join(root, images, "orphan.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="9"/>',
  );
  fs.appendFileSync(
    path.join(root, page),
    [
      '<img src="./images/missing.webp">',
      "![Missing GIF](./images/missing.gif)",
      '<video poster="./images/missing-poster.webp"><source src="./missing.webm"></video>',
      "",
    ].join("\n"),
  );
  const broken = check();
  assert.equal(broken.status, 1);
  assert.deepEqual(broken.report.orphans, ["orphan.svg"]);
  assert.equal(broken.report.brokenReferences.length, 4);
  for (const file of [
    `${images}/missing.webp`,
    `${images}/missing.gif`,
    `${images}/missing-poster.webp`,
    "docs-site/user-guide/missing.webm",
  ]) {
    assert.deepEqual(broken.report.brokenReferences.find((entry) => entry.file === file)?.sources, [
      page,
    ]);
  }
});
