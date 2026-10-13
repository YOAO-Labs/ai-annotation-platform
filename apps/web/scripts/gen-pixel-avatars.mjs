#!/usr/bin/env node
/**
 * 生成内置像素头像资产（`public/avatars/pixel/pixel-NN.svg` + `manifest.json`）。
 *
 * 真值源是**本脚本**：每次运行都用固定的选项表重新渲染，manifest 里不含时间戳，
 * 因此重复执行是无差异的 no-op（`--check` 只比对、不写入）。
 *
 * 产物提交进仓库而不是运行期生成，理由：运行期零依赖、可离线、不新增外部网络来源、
 * 生成结果能被 code review。`@dicebear/core` / `@dicebear/styles` 只作为 devDependency。
 *
 * 素材来源与许可（CC0 1.0，可商用、无署名义务）:
 *   https://www.dicebear.com/styles/pixel-art/
 *   https://www.dicebear.com/licenses/
 *
 * 用法:
 *   pnpm --filter @anno/web gen:avatars          # 重新生成
 *   pnpm --filter @anno/web gen:avatars --check  # 只校验产物是否与脚本一致
 */
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Avatar, Style } from "@dicebear/core";
import pixelArtDefinition from "@dicebear/styles/pixel-art.json" with { type: "json" };

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, "../public/avatars/pixel");
const CHECK_ONLY = process.argv.includes("--check");

/** id 是**永久**标识：已入库的 avatar_ref 指向它。只能追加，不得重排或复用。 */
const COUNT = 32;

// 调色板刻意避开极亮/极暗两端，让头像在深浅两套主题的中性圆片底上都可辨。
const SKIN = ["f4c7a1", "e8b48c", "d99b72", "b97a4f", "8d5524", "ffd9b8"];
const HAIR = ["2f2a25", "4a3b31", "7c4a21", "b45309", "a1662f", "d1d5db", "8b5cf6", "0f766e"];
const CLOTHES = ["f8fafc", "cbd5e1", "38bdf8", "2563eb", "22c55e", "f59e0b", "ef4444", "7c3aed"];

const HAIR_VARIANTS = [
  ...Array.from({ length: 21 }, (_, index) => `long${String(index + 1).padStart(2, "0")}`),
  ...Array.from({ length: 24 }, (_, index) => `short${String(index + 1).padStart(2, "0")}`),
];
const CLOTHES_VARIANTS = Array.from(
  { length: 23 },
  (_, index) => `variant${String(index + 1).padStart(2, "0")}`,
);
const EYES_VARIANTS = Array.from(
  { length: 12 },
  (_, index) => `variant${String(index + 1).padStart(2, "0")}`,
);
const HAT_VARIANTS = Array.from(
  { length: 10 },
  (_, index) => `variant${String(index + 1).padStart(2, "0")}`,
);
const MOUTH_VARIANTS = Array.from(
  { length: 13 },
  (_, index) => `happy${String(index + 1).padStart(2, "0")}`,
);
const GLASSES_VARIANTS = [
  ...Array.from({ length: 7 }, (_, index) => `dark0${index + 1}`),
  ...Array.from({ length: 5 }, (_, index) => `light0${index + 1}`),
];
const BEARD_VARIANTS = Array.from({ length: 8 }, (_, index) => `variant0${index + 1}`);

/**
 * 第 index 张头像的**完整**渲染选项。全部显式指定，不依赖 seed 随机，
 * 因此任何一次生成、任何一台机器上的结果都逐字节一致。
 */
function optionsFor(index) {
  const options = {
    hairVariant: HAIR_VARIANTS[(index * 7) % HAIR_VARIANTS.length],
    hairColor: HAIR[(index * 3) % HAIR.length],
    hairProbability: 100,
    clothesVariant: CLOTHES_VARIANTS[(index * 5) % CLOTHES_VARIANTS.length],
    clothingColor: CLOTHES[(index * 5) % CLOTHES.length],
    eyesVariant: EYES_VARIANTS[(index * 5) % EYES_VARIANTS.length],
    mouthVariant: MOUTH_VARIANTS[index % MOUTH_VARIANTS.length],
    skinColor: SKIN[index % SKIN.length],
    // 帽子 / 眼镜 / 胡须按错开的相位分配（约 1/3、1/4、1/5），避免同一张同时戴三样而显得拥挤。
    glassesProbability: index % 4 === 1 ? 100 : 0,
    glassesVariant: GLASSES_VARIANTS[(index * 3) % GLASSES_VARIANTS.length],
    hatProbability: index % 3 === 0 ? 100 : 0,
    hatVariant: HAT_VARIANTS[(index * 3) % HAT_VARIANTS.length],
    beardProbability: index % 5 === 2 ? 100 : 0,
    beardVariant: BEARD_VARIANTS[(index * 3) % BEARD_VARIANTS.length],
    // 无背景：中性圆片底由前端提供，深浅主题共用一套素材。
    backgroundColor: undefined,
    accessoriesProbability: 0,
    flip: "none",
  };
  return Object.fromEntries(Object.entries(options).filter(([, value]) => value !== undefined));
}

function idFor(index) {
  return `pixel-${String(index + 1).padStart(2, "0")}`;
}

function labelFor(index) {
  return `像素头像 ${String(index + 1).padStart(2, "0")}`;
}

function buildManifest(items) {
  return (
    JSON.stringify(
      {
        style: "dicebear/pixel-art",
        generator: "@dicebear/core + @dicebear/styles",
        sourceUrl: "https://www.dicebear.com/styles/pixel-art/",
        creator: "DiceBear",
        license: "CC0 1.0",
        licenseUrl: "https://creativecommons.org/publicdomain/zero/1.0/",
        command: "pnpm --filter @anno/web gen:avatars",
        idPolicy: "pixel-NN 为永久 id,已入库的 users.avatar_ref 指向它;只能追加,不得重排或复用。",
        items,
      },
      null,
      2,
    ) + "\n"
  );
}

async function render() {
  const style = new Style(pixelArtDefinition);
  const items = [];
  const files = new Map();

  for (let index = 0; index < COUNT; index += 1) {
    const id = idFor(index);
    const options = optionsFor(index);
    const avatar = new Avatar(style, options);
    const svg = `${avatar.toString()}\n`;
    files.set(`${id}.svg`, svg);
    items.push({ id, label: labelFor(index), options });
  }

  files.set("manifest.json", buildManifest(items));
  return files;
}

async function readCurrent() {
  const current = new Map();
  let entries;
  try {
    entries = await readdir(OUT_DIR, { withFileTypes: true });
  } catch {
    return current;
  }
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    current.set(entry.name, await readFile(path.join(OUT_DIR, entry.name), "utf8"));
  }
  return current;
}

function diff(expected, actual) {
  const problems = [];
  for (const [name, contents] of expected) {
    const existing = actual.get(name);
    if (existing === undefined) {
      problems.push(`缺失: ${name}`);
    } else if (existing !== contents) {
      problems.push(`内容不一致: ${name}`);
    }
  }
  for (const name of actual.keys()) {
    if (!expected.has(name)) problems.push(`多余文件: ${name}`);
  }
  return problems;
}

async function main() {
  const expected = await render();

  if (CHECK_ONLY) {
    const problems = diff(expected, await readCurrent());
    if (problems.length > 0) {
      console.error(`头像产物与生成脚本不一致（${problems.length} 项）:`);
      for (const problem of problems) console.error(`  - ${problem}`);
      console.error("请运行 pnpm --filter @anno/web gen:avatars 并提交产物。");
      process.exitCode = 1;
      return;
    }
    console.log(`✓ 头像产物与生成脚本一致（${expected.size - 1} 张 + manifest.json）`);
    return;
  }

  await mkdir(OUT_DIR, { recursive: true });
  for (const [name, contents] of expected) {
    await writeFile(path.join(OUT_DIR, name), contents, "utf8");
  }
  console.log(`✓ 已生成 ${expected.size - 1} 张头像 + manifest.json → ${OUT_DIR}`);
}

await main();
