import { describe, expect, it } from "vitest";
import { exportDownloadState, jobResultSummary, jobProgressText } from "./asyncJobPresentation";

describe("后台任务结果", () => {
  it("目录扫描只报告观测数量，不伪造精确进度", () => {
    const job = {
      kind: "dataset_scan",
      status: "running",
      progress_pct: 80,
      result: { processed_count: 123 },
    } as Parameters<typeof jobProgressText>[0];
    expect(jobProgressText(job)).toBe("已扫描 123 项 · 总量未知");
    expect(jobProgressText({ ...job, status: "failed" })).toContain("未完整扫描");
    expect(jobProgressText({ ...job, status: "completed", result: { complete: true } })).toContain(
      "枚举完成",
    );
  });
  it("批量复核保留成功、冲突与阻止的独立数量", () => {
    expect(
      jobResultSummary({
        kind: "dataset_quality_review",
        result: { applied_count: 8, conflict_count: 2, blocked_count: 0 },
      }),
    ).toBe("已复核 8 / 冲突 2 / 已阻止 0");
  });
  it("准确保留成功零条，不把未知结果写成零", () => {
    expect(jobResultSummary({ kind: "dataset_import", result: { imported: 0, skipped: 3 } })).toBe(
      "导入 0 / 跳过 3",
    );
    expect(jobResultSummary({ kind: "create_tasks", result: { created_tasks: 12 } })).toBe(
      "已建任务 12",
    );
    expect(jobResultSummary({ kind: "dataset_import", result: {} })).toBeNull();
  });

  it("仅有效期内的 HTTP 下载地址可以打开，过期和未知期限给出恢复说明", () => {
    const now = Date.parse("2026-09-09T00:00:00Z");
    const valid = {
      download_url: "https://files.example/result.zip",
      expires_at: "2026-09-10T00:00:00Z",
    };
    expect(exportDownloadState(valid, now).url).toBe(valid.download_url);
    for (const expires_at of ["2026-09-09T00:00:00Z", "invalid", ""]) {
      expect(exportDownloadState({ ...valid, expires_at }, now)).toEqual({
        url: null,
        reason: expect.stringContaining("重新导出"),
      });
    }
    expect(
      exportDownloadState({ ...valid, download_url: "javascript:alert(1)" }, now).url,
    ).toBeNull();
  });
});
