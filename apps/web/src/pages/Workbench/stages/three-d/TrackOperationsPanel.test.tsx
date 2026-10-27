import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";

const api = vi.hoisted(() => ({
  getSceneTrack: vi.fn(),
  listSceneTrackOperations: vi.fn(),
  listPointCloudTrackOperationCandidates: vi.fn(),
  previewSceneTrackCommand: vi.fn(),
  executeSceneTrackCommand: vi.fn(),
  revertSceneTrackOperation: vi.fn(),
}));
const pushToast = vi.hoisted(() => vi.fn());

vi.mock("@/api/tasks", () => ({ tasksApi: api }));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: { push: typeof pushToast }) => unknown) =>
    selector({ push: pushToast }),
}));

import { TrackOperationsPanel } from "./TrackOperationsPanel";

function preview(kind: "split" | "merge" | "mark_absent" | "resume" | "terminate") {
  return {
    contract_version: 1 as const,
    kind,
    scene_id: "scene-1",
    scene_name: "scene-0061",
    track_id: "trk-primary",
    secondary_track_id: kind === "merge" ? "trk-secondary" : null,
    frame_index: kind === "merge" ? null : 12,
    resume_frame: null,
    source_revisions: { "trk-primary": 4 },
    before_intervals: {
      "trk-primary": [
        { id: "interval-1", start_frame: 10, end_frame: 14, source: "manual", version: 1 },
      ],
    },
    after_intervals: {
      "trk-primary": [
        { id: "interval-2", start_frame: 10, end_frame: 12, source: "manual", version: 1 },
      ],
    },
    affected_members: {
      total: kind === "merge" ? 5 : 2,
      by_temporal_role: kind === "terminate" ? { keyframe: 2 } : { derived: 2 },
      frames: [13, 14],
      requires_confirmation: kind === "terminate",
    },
    snapshot_token: "a".repeat(64),
  };
}

function renderPanel(patch: Partial<React.ComponentProps<typeof TrackOperationsPanel>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onCompleted = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <TrackOperationsPanel
        taskId="task-12"
        currentFrame={12}
        sceneStartFrame={0}
        sceneEndFrame={38}
        selectedTrackId="trk-primary"
        selectedAnnotationId="annotation-12"
        readOnly={false}
        onCompleted={onCompleted}
        {...patch}
      />
    </QueryClientProvider>,
  );
  return { onCompleted, queryClient };
}

describe("TrackOperationsPanel", () => {
  beforeEach(() => {
    Object.values(api).forEach((mock) => mock.mockReset());
    pushToast.mockReset();
    api.getSceneTrack.mockResolvedValue({
      contract_version: 1,
      revision: 4,
      presence_mode: "inferred",
      intervals: [
        { id: "interval-1", start_frame: 10, end_frame: 14, source: "manual", version: 1 },
      ],
      members: { by_temporal_role: { keyframe: 2, derived: 3, sample: 0 } },
      available_commands: ["split", "merge", "mark_absent", "terminate", "revert"],
    });
    api.listSceneTrackOperations.mockResolvedValue({ contract_version: 1, operations: [] });
    api.listPointCloudTrackOperationCandidates.mockResolvedValue({
      contract_version: 1,
      candidates: [
        {
          track_id: "trk-secondary",
          class_name: "car",
          member_count: 2,
          first_frame: 15,
          last_frame: 16,
        },
      ],
      truncated: false,
    });
    api.previewSceneTrackCommand.mockImplementation(
      (
        _taskId: string,
        body: { kind: "split" | "merge" | "mark_absent" | "resume" | "terminate" },
      ) => Promise.resolve(preview(body.kind)),
    );
    api.executeSceneTrackCommand.mockImplementation(
      (
        _taskId: string,
        body: { kind: "split" | "merge" | "mark_absent" | "resume" | "terminate" },
      ) =>
        Promise.resolve({
          ...preview(body.kind),
          operation_id: "operation-1",
          status: "committed",
          created_track_id: body.kind === "split" ? "trk-new" : null,
          result_revisions: { "trk-primary": 5 },
        }),
    );
    api.revertSceneTrackOperation.mockResolvedValue({});
  });

  it("requires a selected 3D track", () => {
    renderPanel({ selectedTrackId: null, selectedAnnotationId: null });

    expect(screen.getByText(/请先在当前帧选择/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "预览影响" })).toBeNull();
  });

  it("previews and confirms a split through the unified command API", async () => {
    const { onCompleted } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "预览影响" }));
    await screen.findByText(/影响 2 个成员/);
    expect(api.previewSceneTrackCommand).toHaveBeenCalledWith("task-12", {
      kind: "split",
      track_id: "trk-primary",
      frame_index: 12,
    });

    fireEvent.click(screen.getByRole("button", { name: "确认拆分" }));
    await waitFor(() => expect(api.executeSceneTrackCommand).toHaveBeenCalled());
    const payload = api.executeSceneTrackCommand.mock.calls[0][1];
    expect(payload).toMatchObject({
      kind: "split",
      track_id: "trk-primary",
      frame_index: 12,
      snapshot_token: "a".repeat(64),
    });
    expect(payload.idempotency_key).toMatch(/^scene-track-split-/);
    expect(onCompleted).toHaveBeenCalledOnce();
  });

  it("loads safe merge candidates", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "合并" }));

    const select = await screen.findByRole("combobox", { name: "合并候选轨迹" });
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "trk-secondary" } });
    fireEvent.click(screen.getByRole("button", { name: "预览影响" }));

    await screen.findByText(/影响 5 个成员/);
    expect(api.previewSceneTrackCommand).toHaveBeenCalledWith("task-12", {
      kind: "merge",
      track_id: "trk-primary",
      secondary_track_id: "trk-secondary",
    });
  });
  it("preserves a removed merge candidate without previewing a different track", async () => {
    const { queryClient } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "合并" }));
    const select = await screen.findByRole("combobox", { name: "合并候选轨迹" });
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "trk-secondary" } });
    expect(screen.getByRole("button", { name: "预览影响" })).toBeEnabled();
    api.listPointCloudTrackOperationCandidates.mockResolvedValue({
      contract_version: 1,
      candidates: [
        {
          track_id: "trk-another",
          class_name: "car",
          first_frame: 15,
          last_frame: 17,
          member_count: 2,
        },
      ],
    });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ["point-cloud-track-operation-candidates"] });
    });
    expect(select).toHaveValue("trk-secondary");
    await waitFor(() => expect(select).toHaveDisplayValue("已选轨迹（已不在合并候选中）"));
    expect(screen.getByRole("button", { name: "预览影响" })).toBeDisabled();
    expect(api.previewSceneTrackCommand).not.toHaveBeenCalled();
    queryClient.clear();
  });

  it("requires explicit confirmation before deactivating keyframes", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "终止轨迹" }));
    fireEvent.click(screen.getByRole("button", { name: "预览影响" }));

    const confirm = await screen.findByRole("button", { name: "确认终止轨迹" });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(confirm).not.toBeDisabled();
  });

  it("previews a bounded absence with an explicit resume frame", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "标记缺席" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "缺席后的恢复帧" }), {
      target: { value: "18" },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览影响" }));

    await waitFor(() =>
      expect(api.previewSceneTrackCommand).toHaveBeenCalledWith("task-12", {
        kind: "mark_absent",
        track_id: "trk-primary",
        frame_index: 12,
        resume_frame: 18,
      }),
    );
  });

  it("drops a stale confirmation and requires a fresh preview", async () => {
    api.executeSceneTrackCommand.mockRejectedValue(
      new ApiError(409, "stale", { reason: "track_snapshot_stale" }),
    );
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "预览影响" }));
    await screen.findByText(/影响 2 个成员/);
    fireEvent.click(screen.getByRole("button", { name: "确认拆分" }));

    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith({
        msg: "轨迹已变化，请重新预览",
        kind: "warning",
      }),
    );
    expect(screen.queryByRole("button", { name: "确认拆分" })).toBeNull();
  });

  it("blocks previews in read-only mode", () => {
    renderPanel({ readOnly: true });

    expect(screen.getByText("当前任务只读，不能修改轨迹。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "预览影响" })).toBeDisabled();
  });

  it("disables commands that do not match the current lifecycle state", async () => {
    api.getSceneTrack.mockResolvedValue({
      contract_version: 1,
      revision: 5,
      presence_mode: "explicit",
      intervals: [
        { id: "interval-2", start_frame: 20, end_frame: 25, source: "manual", version: 1 },
      ],
      members: { by_temporal_role: { keyframe: 1, derived: 0, sample: 0 } },
      available_commands: ["merge", "resume"],
    });
    renderPanel();

    await waitFor(() => expect(screen.getByRole("button", { name: "拆分" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "恢复出现" })).not.toBeDisabled();
  });
});
