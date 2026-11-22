#!/usr/bin/env node
import fs from "fs";
import path from "path";
import { createHash } from "crypto";
import { fileURLToPath } from "url";
import { IMAGE_EXTENSION_RE, walkFiles } from "../../scripts/image-reference-utils.mjs";
import { collectAssetReferences } from "./asset-references.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../..");
const IMAGES_ROOT = path.join(REPO_ROOT, "docs-site/user-guide/images");

const strict = process.argv.includes("--strict");
const json = process.argv.includes("--json");

const DUPLICATE_ALLOWLIST = new Map([
  [
    ["polygon/close-hint.png", "polygon/vertex-edit.png"].join("\n"),
    "两个场景分别表达闭合提示和顶点编辑；当前截图尚未捕获交互差异，待重拍后移除此豁免。",
  ],
]);

const imageFiles = [...walkFiles(IMAGES_ROOT, (name) => IMAGE_EXTENSION_RE.test(name))];

const filesByHash = new Map();
for (const abs of imageFiles) {
  const hash = createHash("sha256").update(fs.readFileSync(abs)).digest("hex");
  const rel = path.relative(IMAGES_ROOT, abs).replace(/\\/g, "/");
  const group = filesByHash.get(hash) ?? [];
  group.push(rel);
  filesByHash.set(hash, group);
}

const duplicateGroups = [];
const allowedDuplicateGroups = [];
for (const [hash, files] of filesByHash) {
  if (files.length < 2) continue;
  files.sort();
  const reason = DUPLICATE_ALLOWLIST.get(files.join("\n"));
  const group = { hash, files, ...(reason ? { reason } : {}) };
  (reason ? allowedDuplicateGroups : duplicateGroups).push(group);
}
duplicateGroups.sort((a, b) => a.files[0].localeCompare(b.files[0]));
allowedDuplicateGroups.sort((a, b) => a.files[0].localeCompare(b.files[0]));

const referenceRecords = collectAssetReferences(REPO_ROOT);
const referenced = new Set([...referenceRecords.values()].map((record) => record.absolute));

const orphans = imageFiles
  .filter((abs) => !referenced.has(abs))
  .map((abs) => path.relative(IMAGES_ROOT, abs).replace(/\\/g, "/"))
  .sort();

const brokenReferences = [...referenceRecords]
  .filter(([, { absolute }]) => !fs.statSync(absolute, { throwIfNoEntry: false })?.isFile())
  .map(([file, { sources }]) => ({ file, sources: [...sources].sort() }))
  .sort((a, b) => a.file.localeCompare(b.file));

const failed = orphans.length + brokenReferences.length + duplicateGroups.length;

if (json) {
  console.log(
    JSON.stringify(
      {
        total: imageFiles.length,
        orphans,
        brokenReferences,
        duplicateGroups,
        allowedDuplicateGroups,
      },
      null,
      2,
    ),
  );
  process.exit(strict && failed > 0 ? 1 : 0);
}

console.log(`\n文档资源检查 — images/ 下 ${imageFiles.length} 张图\n${"─".repeat(64)}`);
if (orphans.length === 0) {
  console.log("✓  全部图片都已被文档页面引用，无孤儿。");
} else {
  for (const rel of orphans) console.log(`✗  未引用图片: images/${rel}`);
  console.log("   → 在对应文档中引用图片，或删除不再使用的文件。");
}
if (brokenReferences.length > 0) {
  for (const { file, sources } of brokenReferences) {
    console.log(`✗  引用的资源文件不存在: ${file}`);
    console.log(`   引用自：${sources.join("、")} → 添加文件或修正引用路径。`);
  }
}
if (duplicateGroups.length > 0) {
  for (const { hash, files } of duplicateGroups) {
    console.log(`✗  重复内容(SHA-256 ${hash.slice(0, 12)}…):`);
    for (const rel of files) console.log(`   images/${rel}`);
  }
  console.log(`   发现 ${duplicateGroups.length} 组未豁免的重复图片。`);
  console.log("   → 让文档复用同一个 canonical 图片并删除副本，或修正尚未捕获语义差异的截图。");
} else {
  console.log("✓  未发现未豁免的重复图片。");
}
for (const { hash, files, reason } of allowedDuplicateGroups) {
  const paths = files.map((rel) => `images/${rel}`).join(" ↔ ");
  console.log(`○  已知重复(SHA-256 ${hash.slice(0, 12)}…): ${paths}`);
  console.log(`   理由：${reason}`);
}
console.log("");

if (strict && failed > 0) process.exit(1);
