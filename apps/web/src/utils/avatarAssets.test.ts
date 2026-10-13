import { readFile, readdir } from "node:fs/promises";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { resolveAvatarUrl } from "./avatar";

/**
 * 内置像素头像是**提交进仓库的生成产物**，manifest 与 SVG 文件必须严格一一对应：
 * 多一个 / 少一个 / id 重复都会让某个已入库的 `users.avatar_ref` 变成破图。
 *
 * 用 `process.cwd()`（vitest 的根目录即 apps/web）而不是 `import.meta.url`：本仓所有
 * 测试都在 jsdom 环境下跑，`import.meta.url` 是 http URL，无法定位文件系统。
 */
const PIXEL_DIR = path.resolve(process.cwd(), "public/avatars/pixel");

interface PresetManifest {
  style: string;
  creator: string;
  license: string;
  licenseUrl: string;
  sourceUrl: string;
  command: string;
  items: Array<{ id: string; label: string; options: Record<string, unknown> }>;
}

async function loadManifest(): Promise<PresetManifest> {
  return JSON.parse(
    await readFile(path.join(PIXEL_DIR, "manifest.json"), "utf8"),
  ) as PresetManifest;
}

describe("内置像素头像产物", () => {
  it("manifest 记录来源与许可（CC0）", async () => {
    const manifest = await loadManifest();
    expect(manifest.style).toBe("dicebear/pixel-art");
    expect(manifest.creator).toBe("DiceBear");
    expect(manifest.license).toBe("CC0 1.0");
    expect(manifest.licenseUrl).toContain("creativecommons.org/publicdomain/zero/1.0");
    expect(manifest.sourceUrl).toContain("dicebear.com");
    expect(manifest.command).toContain("gen:avatars");
  });

  it("manifest 条目与 SVG 文件一一对应", async () => {
    const manifest = await loadManifest();
    const files = (await readdir(PIXEL_DIR)).filter((name) => name.endsWith(".svg")).sort();
    const ids = manifest.items.map((item) => item.id).sort();

    expect(ids).toEqual(files.map((name) => name.replace(/\.svg$/, "")));
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("每个 id 都能解析成可用的静态路径", async () => {
    const manifest = await loadManifest();
    for (const item of manifest.items) {
      expect(resolveAvatarUrl(`preset:${item.id}`)).toBe(`/avatars/pixel/${item.id}.svg`);
    }
  });

  it("每个条目都显式固定了形象，不依赖随机种子", async () => {
    const manifest = await loadManifest();
    for (const item of manifest.items) {
      expect(item.options.hairVariant).toBeTruthy();
      expect(item.options.clothesVariant).toBeTruthy();
      expect(item.options.skinColor).toBeTruthy();
      expect(item.label).toContain("像素头像");
    }
  });

  it("SVG 保持整型像素渲染（crispEdges），避免缩放后模糊", async () => {
    const manifest = await loadManifest();
    const first = await readFile(path.join(PIXEL_DIR, `${manifest.items[0].id}.svg`), "utf8");
    expect(first).toContain('shape-rendering="crispEdges"');
    expect(first).toContain('viewBox="0 0 16 16"');
  });
});
