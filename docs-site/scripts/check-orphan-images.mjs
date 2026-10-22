#!/usr/bin/env node
/**
 * check-orphan-images.mjs
 *
 * 与 check-image-manifest.mjs **反向**：那个查「文档引用的图是否产出」，这个查
 * 「产出的图是否被文档引用」。扫描 docs-site/user-guide/images/ 下所有图片文件，
 * 找出**没有任何 Markdown 页面或主题组件引用**的孤儿图（产出了却忘记回填），
 * 并按文件内容 hash 检查由不同路径保存的重复图片。
 *
 * 背景：截图/GIF 自动落到 images/ 并登记 maintainers/image-checklist.md，但嵌入文档页是独立一步，
 * 容易漏。漏了就成孤儿资源——本检查在 CI 拦住它。
 *
 * 用法：
 *   node docs-site/scripts/check-orphan-images.mjs
 *   node docs-site/scripts/check-orphan-images.mjs --strict   # 有孤儿时退出码 1
 *
 * 在 CI visual-regression job 中以 --strict 模式运行（紧随 check-image-manifest）。
 */
import fs from "fs";
import path from "path";
import { createHash } from "crypto";
import { fileURLToPath } from "url";
import { walkFiles } from "../../scripts/image-reference-utils.mjs";
import { collectPublishedMedia } from "./media-review-lib.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../..");
const DOCS_ROOT = path.join(REPO_ROOT, "docs-site");
const IMAGES_ROOT = path.join(REPO_ROOT, "docs-site/user-guide/images");

const strict = process.argv.includes("--strict");
const json = process.argv.includes("--json");

const IMG_EXT = /\.(png|gif|jpe?g|webp|svg)$/i;

// 仅允许路径集合完全一致的已知重复组；新增豁免必须写清不同语义和清理条件。
const DUPLICATE_ALLOWLIST = new Map([
  [
    ["polygon/close-hint.png", "polygon/vertex-edit.png"].join("\n"),
    "两个场景分别表达闭合提示和顶点编辑；当前截图尚未捕获交互差异，待重拍后移除此豁免。",
  ],
]);

// ── 收集 images/ 下所有图片文件（绝对路径）─────────────────────────
const imageFiles = [...walkFiles(IMAGES_ROOT, (name) => IMG_EXT.test(name))];
const imageFileSet = new Set(imageFiles);

// ── 重复内容：不同路径的图片文件具有相同 SHA-256 ──────────────────
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

// ── 收集 Markdown / img / AutoImage 与主题组件的真实媒体引用──────
const referenceRecords = collectPublishedMedia({ scanRoot: DOCS_ROOT });
const referenced = new Map(
  [...referenceRecords.values()].map((record) => [record.absolute, [...record.sources].sort()[0]]),
);

// ── 孤儿：磁盘有图但无任何文档引用 ────────────────────────────────
const orphans = imageFiles
  .map((abs) => path.relative(IMAGES_ROOT, abs).replace(/\\/g, "/"))
  .filter((rel) => !referenced.has(path.join(IMAGES_ROOT, rel)))
  .sort();

// ── 失链 GIF：文档引用了 images/ 下的 .gif 但磁盘上不存在 ──────────
// （PNG 等静态图的「引用但缺失」由 check-image-manifest 负责；GIF 不入 manifest，故在此兜底。）
const brokenGifRefs = [...referenced.entries()]
  .filter(([abs]) => abs.startsWith(IMAGES_ROOT + path.sep) && /\.gif$/i.test(abs))
  .filter(([abs]) => !imageFileSet.has(abs))
  .map(([abs, md]) => ({ rel: path.relative(IMAGES_ROOT, abs).replace(/\\/g, "/"), md }))
  .sort((a, b) => a.rel.localeCompare(b.rel));

const failed = orphans.length + brokenGifRefs.length + duplicateGroups.length;

if (json) {
  console.log(
    JSON.stringify(
      {
        total: imageFiles.length,
        orphans,
        brokenGifRefs,
        duplicateGroups,
        allowedDuplicateGroups,
      },
      null,
      2,
    ),
  );
  process.exit(strict && failed > 0 ? 1 : 0);
}

// ── 人类可读输出 ─────────────────────────────────────────────────
console.log(`\n图片资源检查 — images/ 下 ${imageFiles.length} 张图\n${"─".repeat(64)}`);
if (orphans.length === 0) {
  console.log("✓  全部图片都已被文档页面引用，无孤儿。");
} else {
  for (const rel of orphans) console.log(`✗  孤儿(产出未引用): images/${rel}`);
  console.log(`   发现 ${orphans.length} 张孤儿图（产出但无任何 .md 引用）。`);
  console.log(
    "   → 在对应 user-guide 页面加 ![](../images/…) 引用；或删文件；或加进 IGNORE 白名单。",
  );
}
if (brokenGifRefs.length > 0) {
  for (const { rel, md } of brokenGifRefs) {
    console.log(`✗  失链 GIF(引用但文件不存在): images/${rel}`);
    console.log(`   引用自：${md} → 跑 pnpm screenshots:flows 录制，或修正引用路径。`);
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
