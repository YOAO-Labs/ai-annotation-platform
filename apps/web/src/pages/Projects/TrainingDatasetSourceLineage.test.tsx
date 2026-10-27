import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { TrainingDatasetSampleDetail } from "@/api/trainingDatasets";
import { TrainingDatasetSourceLineage } from "./TrainingDatasetSourceLineage";

const recorded: NonNullable<TrainingDatasetSampleDetail["source_lineage"]> = {
  status: "recorded",
  reasons: [],
  evidence: {
    source_id: "frozen-source-id",
    location_id: "frozen-location-id",
    path_digest: "a".repeat(64),
    scan_run_id: "frozen-scan-id",
    accepted_revision_id: "frozen-revision-id",
    revision_number: 1,
    previous_revision_id: null,
    accepted_at: "2026-10-23T10:00:00Z",
    scan_completed_at: "2026-10-23T09:00:00Z",
    content_verified_at: "2026-10-23T09:30:00Z",
    source_config_revision: 2,
    location_config_revision: 3,
  },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("discloses retained evidence and copies the frozen ID", async () => {
  const user = userEvent.setup();
  const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
  render(<TrainingDatasetSourceLineage lineage={recorded} />);
  expect(screen.getByText("来源 ID")).not.toBeVisible();
  await user.click(screen.getByText("冻结来源"));
  expect(screen.getByText("来源 ID")).toBeVisible();
  expect(screen.getByText("frozen-source-id")).toBeVisible();
  expect(screen.getByText("a".repeat(64))).toBeVisible();
  expect(screen.getByText("来源 2 · 位置 3")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "复制冻结来源 ID" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith("frozen-source-id"));
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});

it("explains partial persisted facts without inventing verification time", async () => {
  const user = userEvent.setup();
  render(
    <TrainingDatasetSourceLineage
      lineage={{
        ...recorded,
        status: "partial",
        reasons: ["verification_time_unknown"],
        evidence: { ...recorded.evidence!, content_verified_at: null },
      }}
    />,
  );
  await user.click(screen.getByText("冻结来源"));
  expect(screen.getByText("部分证据")).toBeVisible();
  expect(screen.getByText(/内容验证时间未保留/)).toBeVisible();
  expect(screen.getByText("未知")).toBeVisible();
});

it("shows absent historical provenance as unknown with no live lookup", async () => {
  const user = userEvent.setup();
  render(<TrainingDatasetSourceLineage lineage={undefined} />);
  await user.click(screen.getByText("冻结来源"));
  expect(screen.getByText(/历史版本未保存来源证据/)).toBeVisible();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.queryByText("来源 ID")).not.toBeInTheDocument();
});

it.each(["dataset_asset", "task_original"] as const)(
  "explains frozen managed %s without inventing reference history",
  async (ownerKind) => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(
      <TrainingDatasetSourceLineage
        lineage={{
          kind: "managed_original",
          status: "recorded",
          reasons: [],
          evidence: null,
          managed_evidence: {
            schema: 1,
            owner_kind: ownerKind,
            content_id: "frozen-original",
            location_id: "frozen-location",
            sha256: "a".repeat(64),
            size_bytes: 4096,
          },
        }}
      />,
    );
    expect(screen.queryByText("原件类型")).not.toBeVisible();
    await user.click(screen.getByText("冻结来源"));
    expect(
      screen.getByText(ownerKind === "dataset_asset" ? "数据集原件" : "任务自有原件"),
    ).toBeVisible();
    expect(screen.getByText("4,096")).toBeVisible();
    expect(screen.getByText(/不查询当前来源是否在线/)).toBeVisible();
    expect(screen.queryByText("扫描完成时间")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "复制冻结内容 ID" }));
    await waitFor(() => expect(copy).toHaveBeenCalledWith("frozen-original"));
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  },
);

it("keeps missing managed evidence explicitly unrecorded", async () => {
  const user = userEvent.setup();
  render(
    <TrainingDatasetSourceLineage
      lineage={{
        kind: "unknown",
        status: "unrecorded",
        reasons: ["managed_source_unrecorded"],
        evidence: null,
        managed_evidence: null,
      }}
    />,
  );
  await user.click(screen.getByText("冻结来源"));
  expect(screen.getByText(/托管原件证据未保留/)).toBeVisible();
  expect(screen.queryByText("原件类型")).not.toBeInTheDocument();
});
