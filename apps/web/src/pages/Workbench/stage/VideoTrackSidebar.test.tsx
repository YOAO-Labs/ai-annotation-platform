import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import type { AnnotationResponse, VideoTrackGeometry, VideoTrackOutsideRange } from "@/types";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { trackRangesOverlap, VideoTrackSidebar } from "./VideoTrackSidebar";
import type { VideoTrackAnnotation } from "./videoStageTypes";
import type { VideoSelectionCommand } from "../state/videoSelectionCommand";

const box = { x: 0.1, y: 0.2, w: 0.3, h: 0.4 };

function track(
  id: string,
  className: string,
  frames: number[],
  outside?: VideoTrackOutsideRange[],
): VideoTrackAnnotation {
  const geometry: VideoTrackGeometry = {
    type: "video_track_bbox",
    track_id: `trk_${id}`,
    keyframes: frames.map((frame_index) => ({ frame_index, bbox: box, source: "manual" })),
    ...(outside ? { outside } : {}),
  };
  return {
    id,
    task_id: "task-1",
    project_id: "project-1",
    user_id: "user-1",
    source: "manual",
    annotation_type: "video_track_bbox",
    class_name: className,
    geometry,
    confidence: 1,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    attributes: {},
    created_at: "2026-05-21T00:00:00Z",
    updated_at: null,
  };
}

describe("trackRangesOverlap (join eligibility)", () => {
  it("returns false for disjoint visible frame ranges", () => {
    const a = track("a", "Car", [0, 5]);
    const b = track("b", "Car", [10, 20]);
    expect(trackRangesOverlap(a, b)).toBe(false);
  });

  it("returns true for overlapping visible frame ranges", () => {
    const a = track("a", "Car", [0, 12]);
    const b = track("b", "Car", [10, 20]);
    expect(trackRangesOverlap(a, b)).toBe(true);
  });

  it("treats touching endpoints as overlapping", () => {
    const a = track("a", "Car", [0, 10]);
    const b = track("b", "Car", [10, 20]);
    expect(trackRangesOverlap(a, b)).toBe(true);
  });

  it("excludes outside frames so masked tails do not overlap", () => {
    // a 关键帧到 F15, 但 F11-F15 被标记 outside, 可见区间仅到 F10。
    const a = track("a", "Car", [0, 10, 15], [{ from: 11, to: 15, source: "manual" }]);
    const b = track("b", "Car", [12, 20]);
    expect(trackRangesOverlap(a, b)).toBe(false);
  });
});

/**
 * 删除确认是异步决策:确认打开期间浏览器历史可能切到别的题或轨迹被并行删除。
 * 回归:await 之后按实时 annotations 复检归属,不再把旧题的标注交给 onDeleteTracks。
 */
describe("VideoTrackSidebar delete ownership revalidation", () => {
  function maskTrackFixture(id: string): AnnotationResponse {
    return {
      id,
      task_id: "task-1",
      project_id: "project-1",
      user_id: "user-1",
      source: "manual",
      annotation_type: "video_track_mask",
      class_name: "Car",
      geometry: {
        type: "video_track_mask",
        track_id: `trk_${id}`,
        keyframes: [
          {
            frame_index: 0,
            source: "manual",
            mask: {
              encoding: "coco_rle_ref",
              size: [2, 3],
              object_key: "raster-masks/test.json",
              sha256: "a".repeat(64),
              runs: 3,
              bytes: 12,
            },
          },
        ],
        outside: [],
      },
      confidence: null,
      parent_prediction_id: null,
      parent_annotation_id: null,
      lead_time: null,
      is_active: true,
      ground_truth: false,
      version: 1,
      created_at: "2026-07-22T00:00:00Z",
      updated_at: null,
    };
  }

  async function flushTeardown() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  }

  function sidebarWith(
    annotations: AnnotationResponse[],
    onDeleteTracks: (a: AnnotationResponse[]) => void,
  ) {
    return (
      <>
        <VideoTrackSidebar
          annotations={annotations}
          selectedId={null}
          frameIndex={0}
          readOnly={false}
          hiddenTrackIds={new Set()}
          lockedTrackIds={new Set()}
          onSelect={vi.fn()}
          onToggleHiddenTrack={vi.fn()}
          onToggleLockedTrack={vi.fn()}
          onUpdate={vi.fn()}
          onDeleteTracks={onDeleteTracks}
        />
        <DecisionDialogHost />
      </>
    );
  }

  it("确认期间切题后不再提交删除", async () => {
    const annotation = maskTrackFixture("mask-1");
    const onDeleteTracks = vi.fn();
    const view = render(sidebarWith([annotation], onDeleteTracks));

    fireEvent.click(view.getByTitle("删除 Mask 轨迹"));
    await screen.findByRole("alertdialog", { name: "删除这条 Mask 轨迹？" });

    // 确认尚未给出时任务已切走:当前题 annotations 变空。
    view.rerender(sidebarWith([], onDeleteTracks));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await flushTeardown();
    expect(onDeleteTracks).not.toHaveBeenCalled();
  });

  it("轨迹仍在当前题时确认照常提交", async () => {
    const annotation = maskTrackFixture("mask-2");
    const onDeleteTracks = vi.fn();
    const view = render(sidebarWith([annotation], onDeleteTracks));

    fireEvent.click(view.getByTitle("删除 Mask 轨迹"));
    const dialog = await screen.findByRole("alertdialog", { name: "删除这条 Mask 轨迹？" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await flushTeardown();
    expect(onDeleteTracks).toHaveBeenCalledWith([annotation]);
  });
});

describe("VideoTrackSidebar Mask 多选", () => {
  it("Shift 点击把修饰键传给全局选择，并显示多选高亮", () => {
    const onSelect = vi.fn();
    const annotation: AnnotationResponse = {
      id: "mask-1",
      task_id: "task-1",
      project_id: "project-1",
      user_id: "user-1",
      source: "manual",
      annotation_type: "video_track_mask",
      class_name: "Car",
      geometry: {
        type: "video_track_mask",
        track_id: "trk_mask_1",
        keyframes: [
          {
            frame_index: 0,
            source: "manual",
            mask: {
              encoding: "coco_rle_ref",
              size: [2, 3],
              object_key: "raster-masks/test.json",
              sha256: "a".repeat(64),
              runs: 3,
              bytes: 12,
            },
          },
        ],
        outside: [],
      },
      confidence: null,
      parent_prediction_id: null,
      parent_annotation_id: null,
      lead_time: null,
      is_active: true,
      ground_truth: false,
      version: 1,
      created_at: "2026-07-22T00:00:00Z",
      updated_at: null,
    };
    const view = render(
      <VideoTrackSidebar
        annotations={[annotation]}
        selectedId={null}
        selectedIds={[annotation.id]}
        frameIndex={0}
        readOnly={false}
        hiddenTrackIds={new Set()}
        lockedTrackIds={new Set()}
        onSelect={onSelect}
        onToggleHiddenTrack={vi.fn()}
        onToggleLockedTrack={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );

    const button = view.getByRole("button", { name: /Car/ });
    fireEvent.click(button, { shiftKey: true });

    expect(onSelect).toHaveBeenCalledWith("mask-1", { shift: true });
    expect(button.parentElement?.className).toContain("bg-brand/10");
  });
});

describe("VideoTrackSidebar admitted video selection", () => {
  function setup(overrides: Partial<ComponentProps<typeof VideoTrackSidebar>> = {}) {
    const annotations = [track("a", "First", [0, 10]), track("b", "Second", [5, 15])];
    const onSelectionChange = vi.fn();
    const onSelect = vi.fn();
    const onSeekFrame = vi.fn();
    const props: ComponentProps<typeof VideoTrackSidebar> = {
      annotations,
      selectedId: "a",
      frameIndex: 8,
      readOnly: false,
      hiddenTrackIds: new Set(),
      lockedTrackIds: new Set(),
      onSelect,
      onSeekFrame,
      onSelectionChange,
      onToggleHiddenTrack: vi.fn(),
      onToggleLockedTrack: vi.fn(),
      onUpdate: vi.fn(),
      ...overrides,
    };
    const view = render(<VideoTrackSidebar {...props} />);
    const row = (label: string) =>
      view.getByText(label).closest('[data-testid="video-track-row"]')!;
    return { ...view, annotations, props, row, onSelectionChange, onSelect, onSeekFrame };
  }

  it("keeps the frame and roster set unchanged until selection is admitted", () => {
    const onSelectVideoObject = vi.fn<VideoSelectionCommand>();
    const view = setup({ onSelectVideoObject });
    fireEvent.click(view.row("Second"));

    expect(onSelectVideoObject).toHaveBeenCalledWith(
      "b",
      expect.objectContaining({ frameIndex: 5, shift: false, onAdmitted: expect.any(Function) }),
    );
    expect(view.onSelect).not.toHaveBeenCalled();
    expect(view.onSeekFrame).not.toHaveBeenCalled();
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
    expect(view.row("First")).toHaveAttribute("aria-selected", "true");
    expect(view.row("Second")).toHaveAttribute("aria-selected", "false");

    // Rejecting a command does not invoke onAdmitted; the same draft owner may retry.
    fireEvent.click(view.row("Second"));
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
    act(() => onSelectVideoObject.mock.calls[1][1]?.onAdmitted?.());
    view.rerender(<VideoTrackSidebar {...view.props} selectedId="b" frameIndex={5} />);
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[1]]);
    expect(view.row("First")).toHaveAttribute("aria-selected", "false");
    expect(view.row("Second")).toHaveAttribute("aria-selected", "true");
    expect(view.onSeekFrame).not.toHaveBeenCalled();
  });

  it.each(["shiftKey", "ctrlKey", "metaKey"])(
    "%s preserves local toggle semantics and admits removal without reactivating the older track",
    (modifier) => {
      const onSelectVideoObject = vi.fn<VideoSelectionCommand>();
      const view = setup({ onSelectVideoObject });
      fireEvent.click(view.row("Second"), { [modifier]: true });
      expect(onSelectVideoObject).toHaveBeenLastCalledWith(
        "b",
        expect.objectContaining({ frameIndex: undefined, shift: false }),
      );
      expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
      act(() => onSelectVideoObject.mock.calls[0][1]?.onAdmitted?.());
      view.rerender(<VideoTrackSidebar {...view.props} selectedId="b" />);
      expect(view.onSelectionChange).toHaveBeenLastCalledWith(view.annotations);

      fireEvent.click(view.row("Second"), { [modifier]: true });
      expect(onSelectVideoObject).toHaveBeenLastCalledWith(
        "a",
        expect.objectContaining({ activateTrackTool: false, frameIndex: undefined, shift: false }),
      );
      expect(view.onSelectionChange).toHaveBeenLastCalledWith(view.annotations);
      act(() => onSelectVideoObject.mock.calls[1][1]?.onAdmitted?.());
      view.rerender(<VideoTrackSidebar {...view.props} selectedId="a" />);
      expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
      expect(view.onSeekFrame).not.toHaveBeenCalled();
      expect(view.onSelect).not.toHaveBeenCalled();
    },
  );

  it("keeps the final roster member selected when toggled", () => {
    const onSelectVideoObject = vi.fn<VideoSelectionCommand>();
    const view = setup({ onSelectVideoObject });
    fireEvent.click(view.row("First"), { shiftKey: true });
    expect(onSelectVideoObject).toHaveBeenCalledWith(
      "a",
      expect.objectContaining({ activateTrackTool: undefined, frameIndex: undefined }),
    );
    act(() => onSelectVideoObject.mock.calls[0][1]?.onAdmitted?.());
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
  });

  it("defers clearing the roster and marks the native new-track command", () => {
    const onSelectVideoObject = vi.fn<VideoSelectionCommand>();
    const view = setup({ onSelectVideoObject });
    const button = view.getByRole("button", { name: "新建轨迹" });
    expect(button).toHaveAttribute("data-workbench-video-tool-command");
    expect(view.row("First")).toHaveAttribute("data-workbench-video-tool-command");
    fireEvent.click(button);
    expect(onSelectVideoObject).toHaveBeenCalledWith(
      null,
      expect.objectContaining({ onAdmitted: expect.any(Function) }),
    );
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[0]]);
    expect(view.onSelect).not.toHaveBeenCalled();
    act(() => onSelectVideoObject.mock.calls[0][1]?.onAdmitted?.());
    view.rerender(<VideoTrackSidebar {...view.props} selectedId={null} />);
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([]);
  });

  it("routes Mask track selection through admission without adding a seek", () => {
    const onSelectVideoObject = vi.fn<VideoSelectionCommand>();
    const annotation: AnnotationResponse = {
      ...track("mask", "Mask target", [4]),
      annotation_type: "video_track_mask",
      geometry: {
        type: "video_track_mask",
        track_id: "trk-mask",
        keyframes: [
          {
            frame_index: 4,
            source: "manual",
            mask: {
              encoding: "coco_rle_ref",
              size: [2, 3],
              object_key: "raster-masks/test.json",
              sha256: "a".repeat(64),
              runs: 3,
              bytes: 12,
            },
          },
        ],
      },
    };
    const view = setup({ annotations: [annotation], selectedId: null, onSelectVideoObject });
    const button = view.getByTestId("video-mask-track-mask");
    expect(button).toHaveAttribute("data-workbench-video-tool-command");
    fireEvent.click(button, { shiftKey: true });
    expect(onSelectVideoObject).toHaveBeenCalledWith("mask", { shift: true });
    expect(view.onSelect).not.toHaveBeenCalled();
    expect(view.onSeekFrame).not.toHaveBeenCalled();
  });

  it("keeps a locally hidden track row available for restoration", () => {
    const onToggleHiddenTrack = vi.fn();
    const annotation = track("hidden", "Hidden", [0, 4]);
    const view = setup({
      annotations: [annotation],
      selectedId: null,
      hiddenTrackIds: new Set([annotation.geometry.track_id]),
      onToggleHiddenTrack,
    });
    const row = view.getByText("Hidden").closest<HTMLElement>('[data-testid="video-track-row"]');
    expect(row).not.toBeNull();
    fireEvent.click(within(row!).getByRole("button", { name: "显示轨迹" }));
    expect(onToggleHiddenTrack).toHaveBeenCalledWith(annotation.geometry.track_id);
  });

  it("retains legacy seek-before-select, immediate toggle, and unmarked controls", () => {
    const events: string[] = [];
    const view = setup({
      onSelect: (id) => events.push(`select:${id}`),
      onSeekFrame: (frame) => events.push(`seek:${frame}`),
    });
    expect(view.row("Second")).not.toHaveAttribute("data-workbench-video-tool-command");
    expect(view.getByRole("button", { name: "新建轨迹" })).not.toHaveAttribute(
      "data-workbench-video-tool-command",
    );
    fireEvent.click(view.row("Second"));
    expect(events).toEqual(["seek:5", "select:b"]);
    expect(view.onSelectionChange).toHaveBeenLastCalledWith([view.annotations[1]]);
    fireEvent.click(view.row("First"), { shiftKey: true });
    expect(events).toEqual(["seek:5", "select:b", "select:a"]);
    expect(view.onSelectionChange).toHaveBeenLastCalledWith(view.annotations);
  });
});
