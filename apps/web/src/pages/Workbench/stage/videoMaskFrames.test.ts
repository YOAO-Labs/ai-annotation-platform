import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnnotationResponse } from "@/types";
import type { AiBox } from "../state/transforms";
import {
  buildTintedMaskRgba,
  maskAlphaBounds,
  useVideoMaskFrames,
  type VideoMaskCandidate,
} from "./videoMaskFrames";

const apiMocks = vi.hoisted(() => ({
  annotationRasterMaskContent: vi.fn(),
  annotationVideoMaskContent: vi.fn(),
  predictionVideoMaskContent: vi.fn(),
  trackerMaskContent: vi.fn(),
}));

vi.mock("@/api/rasterMasks", () => ({
  rasterMasksApi: {
    annotationRasterMaskContent: apiMocks.annotationRasterMaskContent,
    annotationVideoMaskContent: apiMocks.annotationVideoMaskContent,
    predictionVideoMaskContent: apiMocks.predictionVideoMaskContent,
  },
}));

vi.mock("@/api/videoTracker", () => ({
  videoTrackerApi: { maskContent: apiMocks.trackerMaskContent },
}));

describe("video mask frame helpers", () => {
  it("builds a transparent RGBA overlay from row-major alpha", () => {
    expect([...buildTintedMaskRgba(Uint8Array.from([0, 255]), "#102030")]).toEqual([
      0, 0, 0, 0, 16, 32, 48, 255,
    ]);
  });

  it("computes normalized bounds without treating empty pixels as hits", () => {
    const alpha = Uint8Array.from([0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 255, 0]);
    expect(maskAlphaBounds(alpha, 4, 3)).toEqual({ x: 0.25, y: 1 / 3, w: 0.5, h: 2 / 3 });
    expect(maskAlphaBounds(new Uint8Array(4), 2, 2)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });
});

describe("useVideoMaskFrames", () => {
  afterEach(() => vi.unstubAllGlobals());

  beforeEach(() => {
    apiMocks.annotationRasterMaskContent.mockReset();
    apiMocks.annotationVideoMaskContent.mockReset();
    apiMocks.predictionVideoMaskContent.mockReset();
    apiMocks.trackerMaskContent.mockReset();
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ close: vi.fn() })),
    );
    vi.stubGlobal(
      "ImageData",
      class {
        constructor(
          public data: Uint8ClampedArray,
          public width: number,
          public height: number,
        ) {}
      },
    );
  });

  it("retires the previous review job's Mask immediately while the new job is decoding", async () => {
    const rle = {
      encoding: "coco_rle" as const,
      size: [2, 3] as [number, number],
      counts: [1, 2, 3],
    };
    const candidate = (jobId: string): VideoMaskCandidate => ({
      jobId,
      result: {
        frame_index: 4,
        instance_id: "A",
        geometry: {
          type: "mask",
          mask: {
            encoding: "coco_rle_ref",
            size: [2, 3],
            object_key: `raster-masks/sha256/aa/aa/${"a".repeat(64)}.json`,
            sha256: "a".repeat(64),
            runs: 3,
            bytes: 64,
          },
        },
      },
    });
    let resolveMask!: (value: typeof rle) => void;
    apiMocks.trackerMaskContent.mockResolvedValueOnce(rle).mockImplementationOnce(
      () =>
        new Promise<typeof rle>((resolve) => {
          resolveMask = resolve;
        }),
    );
    const color = () => "#ff0000";
    const annotations: AnnotationResponse[] = [];
    const predictions: AiBox[] = [];
    const { result, rerender } = renderHook(
      ({ candidates }: { candidates: VideoMaskCandidate[] }) =>
        useVideoMaskFrames({
          taskId: "task-1",
          annotations,
          predictions,
          candidates,
          frameIndex: 4,
          selectedId: null,
          colorForAnnotation: color,
          colorForPrediction: color,
        }),
      { initialProps: { candidates: [candidate("job-a")] } },
    );
    await waitFor(() =>
      expect(result.current.map((record) => record.id)).toEqual(["tracker:job-a:A:4"]),
    );
    rerender({ candidates: [candidate("job-b")] });
    expect(result.current).toEqual([]);
    await act(async () => resolveMask(rle));
    expect(result.current.map((record) => record.id)).toEqual(["tracker:job-b:A:4"]);
    let resolveRetired!: (value: typeof rle) => void;
    apiMocks.trackerMaskContent.mockImplementationOnce(
      () =>
        new Promise<typeof rle>((resolve) => {
          resolveRetired = resolve;
        }),
    );
    rerender({ candidates: [candidate("job-c")] });
    expect(result.current).toEqual([]);
    rerender({ candidates: [candidate("job-a")] });
    await waitFor(() =>
      expect(result.current.map((record) => record.id)).toEqual(["tracker:job-a:A:4"]),
    );
    await act(async () => resolveRetired(rle));
    expect(result.current.map((record) => record.id)).toEqual(["tracker:job-a:A:4"]);
    rerender({ candidates: [] });
    expect(result.current).toEqual([]);
  });

  it("单帧 video_mask 等待真实 ID，并只在所属帧加载静态内容", async () => {
    const digest = "a".repeat(64);
    const annotation = {
      id: "mask-frame",
      task_id: "task-1",
      project_id: "project-1",
      user_id: "user-1",
      source: "manual",
      annotation_type: "video_mask",
      class_name: "Car",
      geometry: {
        type: "video_mask",
        frame_index: 4,
        mask: {
          encoding: "coco_rle_ref",
          size: [2, 3],
          object_key: `raster-masks/sha256/aa/aa/${digest}.json`,
          sha256: digest,
          runs: 3,
          bytes: 64,
        },
      },
      confidence: 1,
      parent_prediction_id: null,
      parent_annotation_id: null,
      lead_time: null,
      is_active: true,
      ground_truth: false,
      version: 1,
      created_at: "2026-07-23T00:00:00Z",
      updated_at: null,
    } satisfies AnnotationResponse;
    apiMocks.annotationRasterMaskContent.mockResolvedValue({
      encoding: "coco_rle",
      size: [2, 3],
      counts: [1, 2, 3],
    });

    const { result, rerender } = renderHook(
      ({ frameIndex, currentAnnotation }) =>
        useVideoMaskFrames({
          taskId: "task-1",
          annotations: [currentAnnotation],
          candidates: [],
          predictions: [],
          frameIndex,
          selectedId: annotation.id,
          colorForAnnotation: () => "#ff0000",
          colorForPrediction: () => "#00ff00",
        }),
      {
        initialProps: { frameIndex: 4, currentAnnotation: { ...annotation, id: "tmp_mask-frame" } },
      },
    );

    expect(apiMocks.annotationRasterMaskContent).not.toHaveBeenCalled();
    expect(result.current).toEqual([]);
    rerender({ frameIndex: 4, currentAnnotation: annotation });
    await waitFor(() => expect(result.current).toHaveLength(1));
    expect(result.current[0]).toMatchObject({
      id: annotation.id,
      selected: true,
      isTrack: false,
      color: "#ff0000",
    });
    const rendered = vi.mocked(createImageBitmap).mock.calls[0][0] as ImageData;
    expect(rendered.width * rendered.height).toBeLessThan(3 * 2);
    expect(apiMocks.annotationRasterMaskContent).toHaveBeenCalledWith(annotation.id);
    expect(apiMocks.annotationVideoMaskContent).not.toHaveBeenCalled();

    rerender({ frameIndex: 5, currentAnnotation: annotation });
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it.each(["video_mask", "video_track_mask"] as const)(
    "%s waits for a persisted id while preserving other masks and the optimistic render key",
    async (type) => {
      const frameIndex = 6;
      const savedId = "58213cd2-a17b-4ab9-a13e-8a5b46a74634";
      const temporaryId = `tmp_${savedId}`;
      const mask = {
        encoding: "coco_rle_ref" as const,
        size: [2, 3] as [number, number],
        object_key: `raster-masks/sha256/aa/aa/${"a".repeat(64)}.json`,
        sha256: "a".repeat(64),
        runs: 3,
        bytes: 64,
      };
      const annotation = (id: string): AnnotationResponse => ({
        id,
        task_id: "task-1",
        project_id: "project-1",
        user_id: "user-1",
        source: "manual",
        annotation_type: type,
        class_name: "Car",
        geometry:
          type === "video_mask"
            ? { type, frame_index: frameIndex, mask }
            : {
                type,
                track_id: id,
                keyframes: [{ frame_index: frameIndex, mask, source: "manual" }],
              },
        confidence: 1,
        parent_prediction_id: null,
        parent_annotation_id: null,
        lead_time: null,
        is_active: true,
        ground_truth: false,
        version: 1,
        created_at: "2026-09-08T00:00:00Z",
        updated_at: null,
      });
      const persisted = annotation("7fbd7a31-a1c1-4895-9ead-e91726529d69");
      const optimistic = {
        ...annotation(temporaryId),
        version: undefined,
        render_key: temporaryId,
      };
      const load =
        type === "video_mask"
          ? apiMocks.annotationRasterMaskContent
          : apiMocks.annotationVideoMaskContent;
      load.mockImplementation(async (input: string | AnnotationResponse) => {
        const id = typeof input === "string" ? input : input.id;
        if (id === temporaryId) throw new Error("annotation_id must be a UUID");
        return { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 3] };
      });
      const initial: Parameters<typeof useVideoMaskFrames>[0] = {
        taskId: "task-1",
        annotations: [persisted],
        candidates: [],
        predictions: [],
        frameIndex,
        selectedId: null,
        colorForAnnotation: () => "#ff0000",
        colorForPrediction: () => "#00ff00",
      };
      const { result, rerender } = renderHook(useVideoMaskFrames, { initialProps: initial });
      await waitFor(() =>
        expect(result.current.map((record) => record.id)).toEqual([persisted.id]),
      );
      const existingImage = result.current[0].image;

      await act(async () => {
        rerender({ ...initial, annotations: [persisted, optimistic], selectedId: temporaryId });
      });
      expect(
        load.mock.calls.map(([input]) => (typeof input === "string" ? input : input.id)),
      ).not.toContain(temporaryId);
      expect(result.current.map((record) => record.id)).toEqual([persisted.id]);
      expect(result.current[0].image).toBe(existingImage);

      const saved = { ...annotation(savedId), render_key: temporaryId };
      rerender({ ...initial, annotations: [persisted, saved], selectedId: saved.id });
      await waitFor(() =>
        expect(result.current.map((record) => record.id)).toEqual([persisted.id, saved.id]),
      );
      expect(load).toHaveBeenCalledWith(
        ...(type === "video_mask" ? [saved.id] : [saved, frameIndex, mask]),
      );
      expect(result.current[1]).toMatchObject({
        id: saved.id,
        selected: true,
        isTrack: type === "video_track_mask",
      });
      expect([...result.current[1].alpha]).toEqual([0, 255, 0, 255, 0, 0]);
    },
  );

  it("外部 video_track_mask 候选复用任务鉴权内容端点并保持 AI 选择 id", async () => {
    const digest = "b".repeat(64);
    const reference = {
      encoding: "coco_rle_ref" as const,
      size: [2, 3] as [number, number],
      object_key: `raster-masks/sha256/bb/bb/${digest}.json`,
      sha256: digest,
      runs: 3,
      bytes: 64,
    };
    const prediction = {
      id: "pred-p1-0",
      predictionId: "p1",
      shapeIndex: 0,
      cls: "Car",
      geometry: {
        type: "video_track_mask",
        track_id: "mask-track",
        keyframes: [{ frame_index: 1, mask: reference, source: "prediction" }],
        outside: [{ from: 8, to: 9, source: "prediction" }],
      },
    } as AiBox;
    apiMocks.predictionVideoMaskContent.mockResolvedValue({
      encoding: "coco_rle",
      size: [2, 3],
      counts: [1, 2, 3],
    });

    const { result, rerender } = renderHook(
      ({ frameIndex }) =>
        useVideoMaskFrames({
          taskId: "task-1",
          annotations: [],
          candidates: [],
          predictions: [prediction],
          frameIndex,
          selectedId: prediction.id,
          colorForAnnotation: () => "#ff0000",
          colorForPrediction: () => "#00ff00",
        }),
      { initialProps: { frameIndex: 5 } },
    );

    await waitFor(() => expect(result.current).toHaveLength(1));
    expect(result.current[0]).toMatchObject({
      id: prediction.id,
      source: "prediction",
      selected: true,
      isTrack: true,
      color: "#00ff00",
    });
    expect(apiMocks.predictionVideoMaskContent).toHaveBeenCalledWith("task-1", "p1", 0, 5);

    rerender({ frameIndex: 8 });
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it("版本切换后先让 Konva 提交新 Mask，再释放旧 ImageBitmap", async () => {
    const animationFrames: FrameRequestCallback[] = [];
    class FakeImageBitmap {
      width = 2;
      height = 2;
      close = vi.fn(() => {
        this.width = 0;
        this.height = 0;
      });
    }
    vi.stubGlobal("ImageBitmap", FakeImageBitmap);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      animationFrames.push(callback);
      return animationFrames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => new FakeImageBitmap()),
    );
    apiMocks.annotationVideoMaskContent.mockResolvedValue({
      encoding: "coco_rle",
      size: [2, 3],
      counts: [1, 2, 3],
    });
    const colorForAnnotation = () => "#ff0000";
    const colorForPrediction = () => "#00ff00";
    const annotation = (version: number) =>
      ({
        id: "mask-track",
        task_id: "task-1",
        project_id: "project-1",
        user_id: "user-1",
        source: "manual",
        annotation_type: "video_track_mask",
        class_name: "Car",
        geometry: {
          type: "video_track_mask",
          track_id: "track-1",
          keyframes: [
            {
              frame_index: 1,
              mask: {
                encoding: "coco_rle_ref",
                size: [2, 3],
                object_key: `raster-masks/sha256/aa/aa/${"a".repeat(64)}.json`,
                sha256: "a".repeat(64),
                runs: 3,
                bytes: 64,
              },
              source: "manual",
            },
          ],
        },
        confidence: 1,
        parent_prediction_id: null,
        parent_annotation_id: null,
        lead_time: null,
        is_active: true,
        ground_truth: false,
        version,
        created_at: "2026-07-23T00:00:00Z",
        updated_at: null,
      }) satisfies AnnotationResponse;

    const { result, rerender } = renderHook(
      ({ value }) =>
        useVideoMaskFrames({
          taskId: "task-1",
          annotations: [value],
          candidates: [],
          predictions: [],
          frameIndex: 1,
          selectedId: value.id,
          colorForAnnotation,
          colorForPrediction,
        }),
      { initialProps: { value: annotation(1) } },
    );

    await waitFor(() => expect(result.current).toHaveLength(1));
    const firstBitmap = result.current[0].image as unknown as FakeImageBitmap;
    rerender({ value: annotation(2) });
    await waitFor(() => expect(result.current[0]?.cacheKey).toContain("version:2"));

    expect(firstBitmap.close).not.toHaveBeenCalled();
    act(() => {
      for (const callback of animationFrames.splice(0)) callback(performance.now());
    });
    expect(firstBitmap.close).toHaveBeenCalledTimes(1);
  });
});
