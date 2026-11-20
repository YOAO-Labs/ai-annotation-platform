#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { parseArgs } from "node:util";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const outputRoot = resolve(here, "../.vitepress/theme/assets/home/hero");
const ffmpeg = process.env.FFMPEG_PATH || "ffmpeg";

const media = [
  ["docs-site/user-guide/images/workbench/video-real-scene.png", "video-track.webp"],
  ["docs-site/user-guide/images/workbench/pointcloud-real-scene.png", "pointcloud.webp"],
  ["docs-site/user-guide/images/projects/data-manager-overview.png", "data-manager.webp"],
  ["docs-site/user-guide/images/review/workbench.png", "review.webp"],
];

const { values } = parseArgs({ options: { asset: { type: "string", multiple: true } } });
const selected = values.asset ?? media.map(([, name]) => name.replace(/\.webp$/, ""));
for (const name of selected) {
  if (!media.some(([, output]) => output === `${name}.webp`)) {
    throw new Error(`未知的首页 Hero 素材：${name}`);
  }
}
const probe = spawnSync(ffmpeg, ["-version"], { encoding: "utf8" });
if (probe.status !== 0) {
  throw new Error(
    `ffmpeg 不可用，无法生成首页 Hero WebP。请安装 ffmpeg 或设置 FFMPEG_PATH。\n${probe.stderr || probe.error || ""}`,
  );
}

fs.mkdirSync(outputRoot, { recursive: true });
for (const [inputRelative, outputName] of media) {
  if (!selected.includes(outputName.replace(/\.webp$/, ""))) continue;
  const input = resolve(repoRoot, inputRelative);
  const output = resolve(outputRoot, outputName);
  const temporary = `${output}.${process.pid}.tmp.webp`;
  let result = spawnSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-y",
      "-i",
      input,
      "-frames:v",
      "1",
      "-c:v",
      "libwebp",
      "-quality",
      "85",
      "-compression_level",
      "4",
      temporary,
    ],
    { encoding: "utf8" },
  );
  if (result.status !== 0 && result.stderr?.includes("Unknown encoder 'libwebp'")) {
    result = spawnSync("cwebp", ["-q", "85", "-m", "4", input, "-o", temporary], {
      encoding: "utf8",
    });
  }
  if (result.status !== 0) {
    fs.rmSync(temporary, { force: true });
    throw new Error(`首页 Hero WebP 生成失败：${inputRelative}\n${result.stderr}`);
  }
  fs.renameSync(temporary, output);
  const sizeKiB = (fs.statSync(output).size / 1024).toFixed(1);
  console.log(`[home-hero] ${relative(repoRoot, output)} (${sizeKiB} KiB)`);
}
