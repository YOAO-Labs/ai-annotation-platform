/**
 * 视频右键菜单 builder 纯函数测试(栈无关,SVG + Konva 共用)。
 */
import { describe, expect, it, vi } from "vitest";
import type {
  AnnotationResponse,
  VideoBboxGeometry,
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
} from "@/types";
import { buildVideoContextMenuItems, type VideoContextMenuCtx } from "./videoContextMenuItems";
import type { VideoMaskKeyframeActionHandlers } from "./videoMaskKeyframeActions";
import type { VideoTrackActions } from "./useVideoTrackActions";

function bbox(id: string, frameIndex: number, cls = "car"): AnnotationResponse {
  return {
    id,
    class_name: cls,
    geometry: {
      type: "video_bbox",
      frame_index: frameIndex,
      x: 0.1,
      y: 0.1,
      w: 0.2,
      h: 0.2,
    } satisfies VideoBboxGeometry,
  } as unknown as AnnotationResponse;
}
function track(id: string, trackId = "t1"): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_track_bbox",
      track_id: trackId,
      keyframes: [
        {
          frame_index: 0,
          bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
          source: "manual",
          occluded: false,
        },
      ],
    } satisfies VideoTrackGeometry,
  } as unknown as AnnotationResponse;
}
function maskTrack(outside: VideoTrackMaskGeometry["outside"] = []): AnnotationResponse {
  return {
    id: "mask-1",
    class_name: "car",
    is_locked: false,
    geometry: {
      type: "video_track_mask",
      track_id: "mask-track-1",
      keyframes: [0, 5].map((frame_index) => ({
        frame_index,
        source: "manual" as const,
        mask: {
          encoding: "coco_rle_ref" as const,
          size: [2, 3],
          object_key: `raster-masks/${frame_index}.json`,
          sha256: String(frame_index).padStart(64, "0"),
          runs: 3,
          bytes: 12,
        },
      })),
      outside,
    } satisfies VideoTrackMaskGeometry,
  } as unknown as AnnotationResponse;
}

const trackActions: VideoTrackActions = {
  selectedTrackHidden: false,
  selectedTrackLocked: false,
  currentFrameOutside: false,
  currentFrameOccluded: false,
  canEditSelectedTrack: true,
  markSelectedTrack: vi.fn(),
  toggleSelectedTrackOutside: vi.fn(),
  toggleSelectedTrackOccluded: vi.fn(),
  toggleSelectedTrackHidden: vi.fn(),
  toggleSelectedTrackLocked: vi.fn(),
  propagateSelectedTrack: vi.fn(),
};

const maskKeyframeActions: VideoMaskKeyframeActionHandlers = {
  clipboardLabel: "F0",
  hasClipboard: true,
  busy: false,
  copyCurrent: vi.fn(),
  pasteSameTrack: vi.fn(),
  pasteNewTrack: vi.fn(),
  deleteCurrentKeyframe: vi.fn(),
  toggleCurrentOutside: vi.fn(),
  splitCurrentComponents: vi.fn(),
};

const base: VideoContextMenuCtx = {
  contextMenuAnnotation: null,
  selectedAnnotation: null,
  contextMenuTargetId: null,
  selectedVideoBboxes: [],
  readOnly: false,
  frameIndex: 0,
  trackActions,
  canDeleteSelectedTrackKeyframe: true,
  deleteSelectedTrackKeyframe: () => true,
  onChangeUserBoxClass: vi.fn(),
  onComposeTracks: vi.fn(),
  onConvertToBboxes: vi.fn(),
  onDelete: vi.fn(),
  onPropagateTrack: vi.fn(),
  onToggleHiddenTrack: vi.fn(),
  onToggleLockedTrack: vi.fn(),
};

describe("buildVideoContextMenuItems", () => {
  it("单个 bbox → 改类别 + 删除(无聚合)", () => {
    const b = bbox("b1", 0);
    const items = buildVideoContextMenuItems({ ...base, contextMenuAnnotation: b });
    const labels = items.filter((i) => !i.divider).map((i) => i.label);
    expect(labels).toEqual(["改类别", "删除"]);
    expect(items.some((i) => i.id === "bbox-aggregate")).toBe(false);
  });

  it("多选同类不同帧 bbox → 出现「聚合为轨迹」且可用", () => {
    const b1 = bbox("b1", 0);
    const b2 = bbox("b2", 1);
    const items = buildVideoContextMenuItems({
      ...base,
      contextMenuAnnotation: b1,
      selectedVideoBboxes: [b1, b2],
    });
    const agg = items.find((i) => i.id === "bbox-aggregate");
    expect(agg).toBeDefined();
    expect(agg?.disabled).toBe(false);
  });

  it("多选不同类 bbox → 聚合禁用", () => {
    const b1 = bbox("b1", 0, "car");
    const b2 = bbox("b2", 1, "bus");
    const items = buildVideoContextMenuItems({
      ...base,
      contextMenuAnnotation: b1,
      selectedVideoBboxes: [b1, b2],
    });
    expect(items.find((i) => i.id === "bbox-aggregate")?.disabled).toBe(true);
  });

  it("选中 track → 完整轨迹菜单", () => {
    const t = track("trk1");
    const items = buildVideoContextMenuItems({
      ...base,
      selectedAnnotation: t,
      contextMenuTargetId: "trk1",
    });
    const ids = items.filter((i) => !i.divider).map((i) => i.id);
    expect(ids).toEqual([
      "outside",
      "occluded",
      "locked",
      "hidden",
      "propagate",
      "class",
      "split-frame",
      "delete-keyframe",
      "delete-track",
    ]);
    expect(items.find((item) => item.id === "propagate")?.label).toBe("AI 延展此轨迹");
  });

  it("track 锁定 → 标记/拆帧/删轨迹禁用,锁定项仍可用", () => {
    const t = track("trk1");
    const locked: VideoTrackActions = {
      ...trackActions,
      selectedTrackLocked: true,
      canEditSelectedTrack: false,
    };
    const items = buildVideoContextMenuItems({
      ...base,
      selectedAnnotation: t,
      contextMenuTargetId: "trk1",
      trackActions: locked,
    });
    const byId = Object.fromEntries(items.map((i) => [i.id, i]));
    expect(byId["outside"].disabled).toBe(true);
    expect(byId["split-frame"].disabled).toBe(true);
    expect(byId["delete-track"].disabled).toBe(true);
    expect(byId["locked"].disabled).toBe(false); // 解锁始终可点
    expect(byId["locked"].label).toBe("解锁轨迹");
  });

  it("选中 track 但 contextMenuTargetId 不匹配 → 空菜单", () => {
    const t = track("trk1");
    expect(
      buildVideoContextMenuItems({ ...base, selectedAnnotation: t, contextMenuTargetId: "other" }),
    ).toEqual([]);
  });

  it("Mask 轨迹提供剪贴板、帧状态、组件拆轨与整轨操作", () => {
    const annotation = maskTrack();
    const items = buildVideoContextMenuItems({
      ...base,
      contextMenuAnnotation: annotation,
      maskKeyframeActions,
    });
    expect(items.filter((item) => !item.divider).map((item) => item.id)).toEqual([
      "mask-copy-current",
      "mask-paste-current",
      "mask-paste-new",
      "mask-outside",
      "mask-delete-keyframe",
      "mask-split-components",
      "mask-track-class",
      "mask-delete-track",
    ]);
    items.find((item) => item.id === "mask-paste-new")?.onSelect?.();
    expect(maskKeyframeActions.pasteNewTrack).toHaveBeenCalledWith(annotation);
  });

  it("Mask 当前帧 outside 时禁用复制、删除与拆轨，保留恢复 held 入口", () => {
    const items = buildVideoContextMenuItems({
      ...base,
      frameIndex: 5,
      contextMenuAnnotation: maskTrack([{ from: 5, to: 5, source: "manual" }]),
      maskKeyframeActions,
    });
    expect(items.find((item) => item.id === "mask-copy-current")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "mask-delete-keyframe")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "mask-split-components")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "mask-outside")?.label).toBe("恢复保持");
    expect(items.find((item) => item.id === "mask-outside")?.disabled).toBe(false);
  });

  it("Mask 预测 outside 不冒充可恢复的人工 held 状态", () => {
    const items = buildVideoContextMenuItems({
      ...base,
      frameIndex: 5,
      contextMenuAnnotation: maskTrack([{ from: 5, to: 5, source: "prediction" }]),
      maskKeyframeActions,
    });
    expect(items.find((item) => item.id === "mask-outside")?.label).toBe("预测消失");
    expect(items.find((item) => item.id === "mask-outside")?.disabled).toBe(true);
  });
});
