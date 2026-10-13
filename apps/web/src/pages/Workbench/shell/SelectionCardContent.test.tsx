import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import type { AnnotationResponse } from "@/types";
import type { VideoTrackAnnotation } from "../stage/videoStageTypes";
import { SelectionCardContent } from "./SelectionCardContent";

const confirmDialog = vi.fn();
vi.mock("@/components/ui/decisionDialog", () => ({
  confirmDialog: (input: unknown) => confirmDialog(input),
}));

// 只捕获批量轨迹卡 onDelete，其余渲染为 null —— 便于直接驱动确认回调。
let capturedBatchDelete: (() => void) | null = null;
vi.mock("./VideoTrackBatchCardContent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./VideoTrackBatchCardContent")>();
  return {
    ...actual,
    VideoTrackBatchCardContent: (props: { onDelete?: () => void }) => {
      capturedBatchDelete = props.onDelete ?? null;
      return null;
    },
  };
});

function track(id: string, trackId: string): VideoTrackAnnotation {
  return {
    id,
    task_id: "t1",
    version: 1,
    class_name: "car",
    is_active: true,
    is_locked: false,
    is_hidden: false,
    geometry: { type: "video_track_bbox", track_id: trackId, keyframes: [] },
  } as unknown as VideoTrackAnnotation;
}

const t1 = track("a1", "trk_1");
const t2 = track("a2", "trk_2");

let annotationsRef: { current: AnnotationResponse[] };
let onVideoBatchDelete: ReturnType<typeof vi.fn>;

function renderCard() {
  return render(
    <SelectionCardContent
      stageKind="video"
      isLocked={false}
      multi={false}
      count={2}
      ann={null}
      selectedAiBox={null}
      selectedIds={[]}
      imageWidth={1920}
      imageHeight={1080}
      videoFps={30}
      videoFrameIndex={3}
      setVideoFrameIndex={vi.fn()}
      imageMaskPersistenceMode="native"
      userBoxes={[]}
      visibleAnnotations={[t1, t2]}
      videoBatchTracks={[t1, t2]}
      annotationsSnapshotRef={annotationsRef}
      classes={[]}
      hiddenVideoTrackIds={new Set()}
      lockedVideoTrackIds={new Set()}
      rasterMaskStatus={undefined}
      rasterMaskRetry={vi.fn()}
      renderTrackCard={null}
      setSelectedId={vi.fn()}
      handleSelectBox={vi.fn()}
      requestVideoTool={vi.fn()}
      onStartBatchChangeClass={vi.fn()}
      onJoinSelectedPolygons={vi.fn()}
      onBatchPatchFlag={vi.fn()}
      onBatchDelete={vi.fn()}
      onVideoBatchDelete={onVideoBatchDelete}
      onBatchTrack={vi.fn()}
      onComposeTracks={vi.fn()}
      onVideoBatchRename={vi.fn()}
      onStartChangeClass={vi.fn()}
      onDeleteBox={vi.fn()}
      onUpdateAttributes={vi.fn()}
      onPatchShapeFlag={vi.fn()}
      acceptPrediction={vi.fn()}
      rejectPrediction={vi.fn()}
      refinePrediction={vi.fn()}
      openAnnotationConversion={vi.fn()}
      enterImageRasterMaskEdit={vi.fn()}
      toggleHiddenVideoTrack={vi.fn()}
      toggleLockedVideoTrack={vi.fn()}
      openPropagateDialog={vi.fn()}
    />,
  );
}

describe("SelectionCardContent 轨迹批量删除确认", () => {
  let resolveConfirm!: (confirmed: boolean) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    capturedBatchDelete = null;
    annotationsRef = { current: [t1, t2] };
    onVideoBatchDelete = vi.fn();
    confirmDialog.mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          resolveConfirm = resolve;
        }),
    );
    renderCard();
  });

  it("确认 await 之后迟到读取活跃标注：并发已删轨迹不再提交", async () => {
    await act(async () => {
      capturedBatchDelete?.();
    });
    expect(confirmDialog).toHaveBeenCalledOnce();

    // 打开确认后、解决前：并发删除 a2（活跃标注迟到变化）。
    await act(async () => {
      annotationsRef.current = [t1];
      resolveConfirm(true);
    });

    expect(onVideoBatchDelete).toHaveBeenCalledTimes(1);
    expect(onVideoBatchDelete).toHaveBeenCalledWith([t1]);
  });

  it("确认后轨迹全部消失时不提交删除", async () => {
    await act(async () => {
      capturedBatchDelete?.();
    });
    expect(confirmDialog).toHaveBeenCalledOnce();

    await act(async () => {
      annotationsRef.current = [];
      resolveConfirm(true);
    });

    expect(onVideoBatchDelete).not.toHaveBeenCalled();
  });
});
