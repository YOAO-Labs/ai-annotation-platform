#!/usr/bin/env node
/**
 * 校验 scene ↔ manifest ↔ 磁盘文件 ↔ 文档引用的四方一致性。
 *
 * --strict  : 任何失链、孤儿 manifest 项、哈希/尺寸或 scene 映射错误时失败。
 * --release : 在 strict 基础上，要求全部当前 scene 已产出且 seed revision 为当前值。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { walkFiles } from "../../scripts/image-reference-utils.mjs";
import { collectPublishedMedia } from "./media-review-lib.mjs";
import {
  readImageDimensions,
  readScreenshotManifest,
  sha256File,
} from "../../scripts/screenshot-manifest-utils.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../..");
const MANIFEST_PATH = path.join(REPO_ROOT, "apps/web/e2e/screenshots/outputs/manifest.json");
const SCENES_ROOT = path.join(REPO_ROOT, "apps/web/e2e/screenshots/scenes");
const SEED_SPEC_PATH = path.join(REPO_ROOT, "apps/api/app/services/screenshot_seed_spec.py");
const SCREENSHOT_IMAGE_PREFIX = "docs-site/user-guide/images/";

const strict = process.argv.includes("--strict") || process.argv.includes("--release");
const release = process.argv.includes("--release");
const json = process.argv.includes("--json");

function currentSeedRevision() {
  const match = fs.readFileSync(SEED_SPEC_PATH, "utf8").match(/^SEED_REVISION\s*=\s*["']([^"']+)/m);
  if (!match) throw new Error("无法从 screenshot_seed_spec.py 读取 SEED_REVISION");
  return match[1];
}

function collectSceneInventory() {
  const inventory = new Map();
  const sceneRe = /^    name:\s*["']([^"']+)["'],([\s\S]*?)^    target:\s*["']([^"']+)["'],/gm;
  for (const sourcePath of walkFiles(SCENES_ROOT, (name) => name.endsWith(".ts"))) {
    const source = path.relative(REPO_ROOT, sourcePath).replace(/\\/g, "/");
    const content = fs.readFileSync(sourcePath, "utf8");
    let match;
    while ((match = sceneRe.exec(content)) !== null) {
      const [, scene, , target] = match;
      if (inventory.has(scene)) throw new Error(`重复 screenshot scene: ${scene}`);
      inventory.set(scene, { source, target });
    }
  }
  return inventory;
}

function targetBelongsToScene(target, canonical) {
  if (target === canonical) return true;
  const extension = path.extname(canonical);
  const stem = canonical.slice(0, -extension.length);
  return target.startsWith(`${stem}.`) && target.endsWith(extension);
}

const expectedSeedRevision = currentSeedRevision();
const sceneInventory = collectSceneInventory();
const { schemaVersion, metadata, entries } = readScreenshotManifest(MANIFEST_PATH);
const allReferences = collectPublishedMedia();
const staticReferences = new Map(
  [...allReferences].filter(
    ([key]) => key.startsWith(SCREENSHOT_IMAGE_PREFIX) && !/\.gif$/i.test(key),
  ),
);

const failures = [];
const warnings = [];
const auto = [];
const manual = [];

if (!fs.existsSync(MANIFEST_PATH)) failures.push("manifest.json 不存在");
if (schemaVersion !== 2) failures.push(`manifest schema v${schemaVersion} 已过期，需重建为 v2`);

for (const [key, reference] of staticReferences) {
  if (!entries[key]) {
    failures.push(`${key}: 文档已引用但 manifest 未登记（${[...reference.sources].join(", ")}）`);
  }
  if (!fs.existsSync(reference.absolute)) {
    failures.push(`${key}: 文档已引用但磁盘文件不存在`);
  }
}

for (const [key, entry] of Object.entries(entries).sort(([left], [right]) =>
  left.localeCompare(right),
)) {
  const absolute = path.join(REPO_ROOT, key);
  if (entry.target && entry.target !== key) failures.push(`${key}: entry.target 与 key 不一致`);
  if (!fs.existsSync(absolute)) {
    failures.push(`${key}: manifest 已登记但磁盘文件不存在`);
    continue;
  }
  if (!staticReferences.has(key)) failures.push(`${key}: manifest 已登记但文档未引用`);

  if (schemaVersion === 2) {
    const actualHash = sha256File(absolute);
    if (entry.sha256 !== actualHash) failures.push(`${key}: SHA-256 与磁盘文件不一致`);
    try {
      const dimensions = readImageDimensions(absolute);
      if (entry.width !== dimensions.width || entry.height !== dimensions.height) {
        failures.push(
          `${key}: manifest 尺寸 ${entry.width}×${entry.height} 与文件 ${dimensions.width}×${dimensions.height} 不一致`,
        );
      }
    } catch (error) {
      failures.push(error instanceof Error ? error.message : String(error));
    }
  }

  if (!entry.auto) {
    manual.push({ key, note: entry.note });
    continue;
  }

  const expectedScene = sceneInventory.get(entry.scene);
  if (!expectedScene) {
    failures.push(`${key}: scene ${entry.scene ?? "<missing>"} 不存在`);
  } else {
    if (entry.source !== expectedScene.source) {
      failures.push(
        `${key}: source 应为 ${expectedScene.source}，实际为 ${entry.source ?? "<missing>"}`,
      );
    }
    if (!targetBelongsToScene(key, expectedScene.target)) {
      failures.push(`${key}: 不是 scene ${entry.scene} 的 target`);
    }
  }
  if (entry.provenance === "legacy") warnings.push(`${key}: 仍是 catalog 规范前的旧资产`);
  auto.push({ key, scene: entry.scene, generatedAt: entry.generated_at ?? entry.lastRun });
}

const missingSceneTargets = [...sceneInventory.entries()]
  .filter(([, scene]) => !entries[scene.target])
  .map(([scene, value]) => ({ scene, target: value.target }));
for (const missing of missingSceneTargets) {
  warnings.push(`${missing.scene}: 当前 scene 尚未产出 ${missing.target}`);
}

if (metadata.seed_revision !== expectedSeedRevision) {
  warnings.push(
    `manifest seed revision ${metadata.seed_revision ?? "<missing>"} 不是当前 ${expectedSeedRevision}`,
  );
}
if (release) {
  for (const warning of warnings) failures.push(`release gate: ${warning}`);
  if (missingSceneTargets.length > 0) failures.push("release gate: 存在未产出 scene");
}

const results = {
  schemaVersion,
  expectedSeedRevision,
  manifestSeedRevision: metadata.seed_revision,
  referenced: staticReferences.size,
  scenes: sceneInventory.size,
  auto,
  manual,
  missingSceneTargets,
  warnings,
  failures: [...new Set(failures)],
};

if (json) {
  console.log(JSON.stringify(results, null, 2));
} else {
  console.log(`\n图片 manifest 校验 — schema v${schemaVersion}\n${"─".repeat(72)}`);
  console.log(
    `scene ${sceneInventory.size} · 文档静态图 ${staticReferences.size} · manifest ${Object.keys(entries).length}`,
  );
  for (const warning of warnings) console.log(`⚠  ${warning}`);
  for (const failure of results.failures) console.log(`✗  ${failure}`);
  if (results.failures.length === 0) console.log("✓  manifest、磁盘文件、scene 与文档引用一致");
  console.log("");
}

if (strict && results.failures.length > 0) process.exit(1);
