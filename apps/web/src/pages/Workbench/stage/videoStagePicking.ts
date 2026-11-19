import type {
  VideoFrameEntry,
  VideoPoint,
  VideoStageGeom,
  VideoTrackGhost,
} from "./videoStageTypes";
import type { VideoMaskRenderRecord } from "./videoMaskFrames";
import type { Keypoint, VideoRotatedBboxGeometry } from "@/types";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import { pickTopRasterMaskAt } from "./shared/rasterMaskRender";

export type PickableVideoEntry = VideoFrameEntry | VideoTrackGhost;

type PickOptions = {
  padding?: number;
  size?: VideoPixelSize;
};

function containsPoint(geom: VideoStageGeom, point: VideoPoint, padding: number) {
  return (
    point.x >= geom.x - padding &&
    point.x <= geom.x + geom.w + padding &&
    point.y >= geom.y - padding &&
    point.y <= geom.y + geom.h + padding
  );
}

function containsRotatedBbox(
  geom: VideoRotatedBboxGeometry,
  point: VideoPoint,
  padding: number,
  size: VideoPixelSize,
) {
  const rad = (geom.angle * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const dx = (point.x - geom.cx) * size.w;
  const dy = (point.y - geom.cy) * size.h;
  const localX = dx * cos + dy * sin;
  const localY = -dx * sin + dy * cos;
  return (
    Math.abs(localX) <= (geom.w * size.w) / 2 + padding * size.w &&
    Math.abs(localY) <= (geom.h * size.h) / 2 + padding * size.h
  );
}

function containsKeypoint(
  points: Keypoint[],
  point: VideoPoint,
  padding: number,
  size?: VideoPixelSize,
) {
  const px = size ? point.x * size.w : point.x;
  const py = size ? point.y * size.h : point.y;
  const radius = size ? Math.max(6, padding * Math.min(size.w, size.h)) : Math.max(0.008, padding);
  return points.some((item) => {
    if (item.v === 0) return false;
    const x = size ? item.x * size.w : item.x;
    const y = size ? item.y * size.h : item.y;
    return Math.hypot(px - x, py - y) <= radius;
  });
}

// 约束放宽到「带 geom 的对象」:命中只读 .geom,泛型保持回传原对象。
// 让 Konva 栈用轻量 { id, geom } 视图复用同一套 z 序逆序 + padding 容差命中,
// 不必造第二份命中实现(SVG 栈仍传完整 PickableVideoEntry,行为零变化)。
export function pickTopVideoEntryAt<
  T extends {
    geom: VideoStageGeom;
    rotatedBbox?: VideoRotatedBboxGeometry;
    keypoints?: Keypoint[];
  },
>(entries: readonly T[], point: VideoPoint, options: PickOptions = {}): T | null {
  const padding = options.padding ?? 0;
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (
      entry.rotatedBbox &&
      options.size &&
      containsRotatedBbox(entry.rotatedBbox, point, padding, options.size)
    )
      return entry;
    if (entry.rotatedBbox) continue;
    if (entry.keypoints && containsKeypoint(entry.keypoints, point, padding, options.size))
      return entry;
    if (entry.keypoints) continue;
    if (containsPoint(entry.geom, point, padding)) return entry;
  }
  return null;
}

export function pickTopVideoMaskAt(
  records: readonly VideoMaskRenderRecord[],
  point: VideoPoint,
): VideoMaskRenderRecord | null {
  return pickTopRasterMaskAt(records, point, (record) => ({
    sourceWidth: record.width,
    sourceHeight: record.height,
    crop: {
      x: 0,
      y: 0,
      width: record.width,
      height: record.height,
      alpha: record.alpha,
    },
  }));
}
