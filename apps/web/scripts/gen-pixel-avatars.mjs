#!/usr/bin/env node
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Avatar, Style } from "@dicebear/core";
import pixelArtDefinition from "@dicebear/styles/pixel-art.json" with { type: "json" };

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, "../public/avatars/pixel");
const CHECK_ONLY = process.argv.includes("--check");

const COUNT = 32;

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
    glassesProbability: index % 4 === 1 ? 100 : 0,
    glassesVariant: GLASSES_VARIANTS[(index * 3) % GLASSES_VARIANTS.length],
    hatProbability: index % 3 === 0 ? 100 : 0,
    hatVariant: HAT_VARIANTS[(index * 3) % HAT_VARIANTS.length],
    beardProbability: index % 5 === 2 ? 100 : 0,
    beardVariant: BEARD_VARIANTS[(index * 3) % BEARD_VARIANTS.length],
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
        command: "pnpm --filter web gen:avatars",
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
      console.error("请运行 pnpm --filter web gen:avatars 并提交产物。");
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
