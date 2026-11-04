import { copyFileSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { describe, it, expect } from "vitest";
import { parseSize, fmt, globMatch } from "./check-bundle-size.mjs";

describe("parseSize", () => {
  it("解析裸数字为字节（默认单位 B）", () => {
    expect(parseSize("512")).toBe(512);
    expect(parseSize("0")).toBe(0);
  });

  it("解析 B / KB / MB（1024 进制）", () => {
    expect(parseSize("100 B")).toBe(100);
    expect(parseSize("1 KB")).toBe(1024);
    expect(parseSize("1 MB")).toBe(1024 * 1024);
    expect(parseSize("500 KB")).toBe(500 * 1024);
  });

  it("单位大小写不敏感、允许无空格与小数", () => {
    expect(parseSize("1kb")).toBe(1024);
    expect(parseSize("2Mb")).toBe(2 * 1024 * 1024);
    expect(parseSize("1.5 KB")).toBe(1.5 * 1024);
  });

  it("非法输入抛错", () => {
    expect(() => parseSize("abc")).toThrow(/invalid size/);
    expect(() => parseSize("10 GB")).toThrow(/invalid size/);
    expect(() => parseSize("")).toThrow(/invalid size/);
  });
});

describe("fmt", () => {
  it("< 1 MB 用 KB 一位小数", () => {
    expect(fmt(1024)).toBe("1.0 KB");
    expect(fmt(1536)).toBe("1.5 KB");
  });

  it(">= 1 MB 用 MB 两位小数", () => {
    expect(fmt(1024 * 1024)).toBe("1.00 MB");
    expect(fmt(1.5 * 1024 * 1024)).toBe("1.50 MB");
  });
});

describe("globMatch", () => {
  it("`*` 匹配任意字符（含 hash 后缀）", () => {
    expect(globMatch("index-*.js", "index-a1b2c3.js")).toBe(true);
    expect(globMatch("vendor-konva-*.js", "vendor-konva-deadbeef.js")).toBe(true);
  });

  it("非匹配返回 false", () => {
    expect(globMatch("index-*.js", "vendor-konva-x.js")).toBe(false);
    expect(globMatch("index-*.js", "index-a1b2c3.css")).toBe(false);
  });

  it("正则元字符（`.`）按字面匹配，不当通配", () => {
    expect(globMatch("a.b.js", "axbxjs")).toBe(false);
    expect(globMatch("a.b.js", "a.b.js")).toBe(true);
  });
});

describe("required chunk CLI gate", () => {
  it("fails missing, duplicate or oversized required chunks and accepts exactly one", () => {
    const dir = realpathSync(mkdtempSync(join(tmpdir(), "bundle-size-test-")));
    try {
      mkdirSync(join(dir, "scripts"));
      mkdirSync(join(dir, "dist/assets"), { recursive: true });
      copyFileSync(
        join(process.cwd(), "scripts/check-bundle-size.mjs"),
        join(dir, "scripts/check.mjs"),
      );
      writeFileSync(
        join(dir, ".size-limit.json"),
        JSON.stringify([
          { name: "dockview", pattern: "vendor-dockview-*.js", required: true, max: "300 KB" },
        ]),
      );
      const check = () => spawnSync(process.execPath, [join(dir, "scripts/check.mjs")]).status;
      expect(check()).toBe(1);
      writeFileSync(join(dir, "dist/assets/vendor-dockview-a.js"), "export {};");
      expect(check()).toBe(0);
      writeFileSync(join(dir, "dist/assets/vendor-dockview-b.js"), "export {};");
      expect(check()).toBe(1);
      rmSync(join(dir, "dist/assets/vendor-dockview-b.js"));
      writeFileSync(join(dir, "dist/assets/vendor-dockview-a.js"), "x".repeat(300 * 1024 + 1));
      expect(check()).toBe(1);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
