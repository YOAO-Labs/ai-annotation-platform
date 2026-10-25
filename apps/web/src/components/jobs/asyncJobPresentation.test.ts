import { describe, expect, it } from "vitest";
import {
  exportDownloadState,
  jobResultSummary,
  jobProgressText,
  jobNotificationVerb,
  jobStage,
} from "./asyncJobPresentation";

it("迁移清单只报告清单完整性，通知和阶段不承诺复制完成", () => {
  const job = {
    kind: "storage_migration_inventory",
    status: "running",
    progress_pct: 75,
    result: { counts: { objects: 0, issues: 0 }, complete: false },
    payload: {},
  } satisfies Parameters<typeof jobStage>[0] & { progress_pct: number };
  expect(jobResultSummary(job)).toBe("清单不完整 / 对象版本 0 / 问题 0");
  expect(jobResultSummary({ ...job, result: {} })).toBeNull();
  expect(jobProgressText(job)).toBe("清单总量未知 · 读取结束后确认完整性");
  expect(jobStage(job)).toBe("读取配置源并核验内容");
  expect(jobProgressText({ ...job, result: { cancel_requested: true } })).toContain(
    "等待底层读取收尾",
  );
  expect(jobProgressText({ ...job, status: "completed" })).toContain("未执行复制或切换");
  expect(jobNotificationVerb("job.completed", job.kind)).toBe("配置源迁移清单完成");
});

it("初次复制区分准备、整体结果和选定子集，不将零对象或部分成功写成迁移完成", () => {
  const kind = "storage_migration_copy";
  expect(jobResultSummary({ kind, result: { prepared: false } })).toBe("复制计划尚未封存");
  expect(
    jobResultSummary({
      kind,
      result: { prepared: true, operation: "prepare", counts: { verified: 0 } },
    }),
  ).toBe("计划已封存，可审阅后开始初次复制");
  expect(
    jobResultSummary({
      kind,
      result: { prepared: true, operation: "prepare", generation: 2 },
    }),
  ).toBe("计划已封存，可审阅后开始增量复制");
  expect(jobNotificationVerb("job.completed", kind, { generation: 2 })).toBe("增量复制任务已结束");
  expect(
    jobResultSummary({
      kind,
      result: { prepared: true, decisions: { copy: 0 }, copy_subset_complete: true },
    }),
  ).toBe("无可复制对象");
  expect(
    jobResultSummary({
      kind,
      result: { prepared: true, operation: "prepare", decisions: { retain: 4 } },
    }),
  ).toBe("计划已封存，无可复制对象");
  expect(
    jobResultSummary({
      kind,
      result: {
        prepared: true,
        selected_count: 2,
        selected_verified_count: 2,
        copy_subset_complete: false,
      },
    }),
  ).toBe("选定子集已核验 2 / 2 · 整体计划仍有未完成范围");
  expect(jobStage({ kind, status: "running", result: { operation: "verify" }, payload: {} })).toBe(
    "只读核验保存的目标版本",
  );
  expect(jobNotificationVerb("job.completed", kind)).toBe("初次复制任务已结束");
  expect(jobNotificationVerb("job.completed", kind, { operation: "prepare", prepared: true })).toBe(
    "复制计划准备完成",
  );
});

describe("后台任务结果", () => {
  it("视频资产处理不展示虚构百分比，并解释派发未确认", () => {
    const job = {
      kind: "video_asset_retry",
      status: "running",
      progress_pct: 0,
      result: {},
    } as Parameters<typeof jobProgressText>[0];
    expect(jobProgressText(job)).toBe("处理完成后显示本次资产的实际结果");
    expect(
      jobProgressText({ ...job, status: "pending", result: { stage: "dispatch_pending" } }),
    ).toContain("可回到视频资产失败列表重试");
    expect(
      jobResultSummary({ kind: job.kind, result: { success_count: 0, failed_count: 1 } }),
    ).toBe("成功 0 / 失败 1");
  });
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

it("对象盘点用已处理桶数显示进度，不伪造当前桶总量", () => {
  expect(
    jobProgressText({
      kind: "storage_usage",
      status: "running",
      progress_pct: 33,
      result: { success_count: 2, failed_count: 1 },
    }),
  ).toBe("已处理 3 个桶 · 当前桶对象总量未知");
});

it("原件核验只报告本次观测，取消请求等待实际读取收尾", () => {
  const job = {
    kind: "managed_original_verification",
    status: "running",
    progress_pct: 99,
    result: {},
  } as Parameters<typeof jobProgressText>[0];
  expect(jobProgressText(job)).toBe("核验单条原件 · 完整读取结束后给出结果");
  expect(jobProgressText({ ...job, result: { cancel_requested: true } })).toBe(
    "已请求取消，等待底层读取收尾",
  );
  expect(jobResultSummary({ kind: job.kind, result: { outcome: "matched" } })).toBe(
    "本次内容与历史摘要相符",
  );
  expect(jobResultSummary({ kind: job.kind, result: { outcome: "missing" } })).toBe(
    "本次未找到对象",
  );
  expect(jobResultSummary({ kind: job.kind, result: { outcome: "unrecognized" } })).toBeNull();
});
