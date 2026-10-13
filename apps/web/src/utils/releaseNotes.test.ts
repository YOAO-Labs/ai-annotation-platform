/**
 * releaseNotes 单测:changelog 段落解析 / semver 比较 / 展示判定 / 真实文件连通性。
 */
import { describe, expect, it } from "vitest";
import { version as pkgVersion } from "../../package.json";
import {
  appVersion,
  compareSemver,
  loadCurrentReleaseNotes,
  parseChangelogSection,
  shouldShowReleaseNotes,
} from "./releaseNotes";

const FIXTURE = [
  "# Changelog",
  "",
  "| 版本组 | 文件 |",
  "| --- | --- |",
  "| 1.x | docs/changelogs/1.x.md |",
  "",
  "---",
  "",
  "## [Unreleased]",
  "",
  "## [2.0.0] - 2026-02-03",
  "",
  "### Added",
  "",
  "- 新功能甲,说明跨行书写",
  "  续写的第二行会并入同一条",
  "- 新功能乙",
  "",
  "### Fixed",
  "",
  "- 修复丙",
  "",
  "### Performance",
  "",
  "- 未知分组键按原文展示",
  "",
  "独立段落不进任何分组。",
  "",
  "## [1.9.0] - 2026-01-01",
  "",
  "### Fixed",
  "",
  "- 旧版本修复",
  "",
].join("\n");

describe("parseChangelogSection", () => {
  it("提取目标版本段落:日期、分组标签、软换行合并", () => {
    const notes = parseChangelogSection(FIXTURE, "2.0.0");
    expect(notes).toEqual({
      version: "2.0.0",
      date: "2026-02-03",
      groups: [
        {
          key: "Added",
          label: "新增",
          items: ["新功能甲,说明跨行书写 续写的第二行会并入同一条", "新功能乙"],
        },
        { key: "Fixed", label: "修复", items: ["修复丙"] },
        { key: "Performance", label: "Performance", items: ["未知分组键按原文展示"] },
      ],
    });
  });

  it("目标不是第一个版本段时也能定位", () => {
    const notes = parseChangelogSection(FIXTURE, "1.9.0");
    expect(notes?.date).toBe("2026-01-01");
    expect(notes?.groups).toEqual([{ key: "Fixed", label: "修复", items: ["旧版本修复"] }]);
  });

  it("找不到版本段返回 null;无日期段落的日期为 null", () => {
    expect(parseChangelogSection(FIXTURE, "9.9.9")).toBeNull();
    expect(parseChangelogSection(FIXTURE, "Unreleased")).toEqual({
      version: "Unreleased",
      date: null,
      groups: [],
    });
  });
});

describe("compareSemver", () => {
  it("按 major/minor/patch 比较", () => {
    expect(compareSemver("0.25.4", "0.25.3")).toBeGreaterThan(0);
    expect(compareSemver("0.25.4", "0.26.0")).toBeLessThan(0);
    expect(compareSemver("1.0.0", "0.99.99")).toBeGreaterThan(0);
    expect(compareSemver("2.0.0", "2.0.0")).toBe(0);
  });

  it("空串/非法输入按 0.0.0 处理;忽略 v 前缀与预发布后缀", () => {
    expect(compareSemver("0.1.0", "")).toBeGreaterThan(0);
    expect(compareSemver("v1.2.3", "1.2.3")).toBe(0);
    expect(compareSemver("1.2.3-beta.1", "1.2.2")).toBeGreaterThan(0);
  });
});

describe("shouldShowReleaseNotes", () => {
  it("仅在当前版本更新时提醒;已确认或回滚不提醒", () => {
    expect(shouldShowReleaseNotes(undefined, "2.0.0")).toBe(true);
    expect(shouldShowReleaseNotes("", "2.0.0")).toBe(true);
    expect(shouldShowReleaseNotes("1.9.0", "2.0.0")).toBe(true);
    expect(shouldShowReleaseNotes("2.0.0", "2.0.0")).toBe(false);
    expect(shouldShowReleaseNotes("2.1.0", "2.0.0")).toBe(false);
    expect(shouldShowReleaseNotes("2.0.0", "")).toBe(false);
  });
});

describe("loadCurrentReleaseNotes", () => {
  it("真实 CHANGELOG 能解析出 package.json 当前版本的段落", async () => {
    // 发布流程(aap-release)同步 bump 版本与 changelog;该断言守护虚拟模块注入路径
    // 与「版本已 bump 但 changelog 缺段」的漂移。
    expect(appVersion).toBe(pkgVersion);
    const notes = await loadCurrentReleaseNotes();
    expect(notes?.version).toBe(pkgVersion);
    expect(notes?.date).toBeTruthy();
    expect(notes?.groups.length ?? 0).toBeGreaterThan(0);
  });
});
