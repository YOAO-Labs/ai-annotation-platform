import type { AnnotationResponse, Keypoint, VideoRotatedBboxGeometry } from "@/types";
import type { DiffMode } from "../modes/types";
import { classColor, getTrackColor } from "./colors";
import {
  deriveTrackNumber,
  isVideoBbox,
  isVideoMaskTrack,
  isVideoPolygon,
  isVideoPolygonTrack,
  isVideoPolyline,
  isVideoPolylineTrack,
  isVideoRotatedBbox,
  isVideoKeypoint,
  isVideoTrack,
  nearestPointsTrackKeyframe,
  rotatedBboxCorners,
  resolveTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
  trackReferenceAtFrame,
} from "./videoStageGeometry";
import { isFrameOutside } from "./videoTrackOutside";
import { gridPrev } from "./videoSamplingGrid";
import {
  buildTrackLabelText,
  shouldShowLabel,
  type AnnotationVisualConfig,
} from "./annotationVisual";
import type { VideoStageGeom } from "./videoStageTypes";
import type { VideoReferenceConfig } from "./videoReferencePredict";

const DEFAULT_REFERENCE_CONFIG: VideoReferenceConfig = { mode: "off", preset: "stable" };

/**
 * 视频标注层的渲染派生(纯函数,栈无关)。
 *
 * 把「当前帧应显示哪些框 / 轨迹预览 / ghost / 标签」从 VideoStage 的内联 useMemo 抽出,
 * 供 VideoKonvaTracksLayer / OverlayLayer 消费。
 *
 * **不含颜色→hex 转换 / scale 抵消**(那是各 Konva 层在 render 时按 DOM/缩放做的),返回的
 * `color` 是原始 CSS 色串、`geom`/`points` 是归一化坐标,保持本模块纯净可测。
 */

export type VideoEntryView = {
  key: string;
  id: string;
  geom: VideoStageGeom;
  /** polygon/polyline track 当前帧的归一化顶点; 存在时 Konva 层画 <Line> 而非 <Rect>。 */
  points?: [number, number][];
  /** true = polyline (开路径, Line 不闭合); 缺省/false = polygon (闭合)。 */
  open?: boolean;
  /** OBB 保留原始中心/尺寸/角度，渲染与命中不再退化成外接多边形。 */
  rotatedBbox?: VideoRotatedBboxGeometry;
  /** 关键点保留 COCO 可见性。 */
  keypoints?: Keypoint[];
  /** 原始 CSS 色(轨迹色 / 类别色);Konva 层 render 时转 hex。 */
  color: string;
  selected: boolean;
  /** 插值帧或遮挡 → 虚线。 */
  dashed: boolean;
  occluded: boolean;
  /** 当前帧是 AI 追出的关键帧 (source==="prediction") → 常态加区别于插值虚线的 AI 线索。 */
  predicted: boolean;
  labelText: string;
  /** 类别名(供 QC 密度/重叠率计算消费)。 */
  className: string;
};

export type VideoPreviewPoint = { frame: number; x: number; y: number; occluded: boolean };

export type VideoTrackPreviewView = {
  key: string;
  id: string;
  color: string;
  selected: boolean;
  /** 可见(非 outside)关键帧中心点,归一化;>1 点画预览线,选中时画关键帧圆点。 */
  points: VideoPreviewPoint[];
};

export type VideoGhostView = {
  id: string;
  geom: VideoStageGeom;
  /** polygon/polyline track 的参考顶点(归一化); 存在时渲染层画 <Line> 而非外接框 <Rect>。 */
  points?: [number, number][];
  /** true = polyline(开路径, Line 不闭合); 缺省/false = polygon(闭合)。 */
  open?: boolean;
  color: string;
  labelText: string;
  /** kalman 预测的位置不确定度(归一化标准差);存在时画淡色误差椭圆。其它模式 undefined。 */
  uncertainty?: { sx: number; sy: number; sw: number; sh: number };
};

export type VideoLabelView = {
  key: string;
  geom: VideoStageGeom;
  color: string;
  text: string;
  opacity?: number;
};

function annotationRenderKey(ann: AnnotationResponse): string {
  return ann.render_key ?? ann.id;
}

export interface VideoFrameViews {
  entries: VideoEntryView[];
  previews: VideoTrackPreviewView[];
  ghost: VideoGhostView | null;
  /**
   * 「跨网格帧续写」参考框:恰好上一网格帧有关键帧、未锁定、当前帧还没画的**非选中**
   * 轨迹(选中那条由 `ghost` 承)。让新网格帧上「其余待续轨迹」可见 + 可命中,不必回上一帧 / 右栏选。
   */
  carryOverGhosts: VideoGhostView[];
  labels: VideoLabelView[];
}

export interface DeriveVideoFrameViewsInput {
  annotations: AnnotationResponse[];
  frameIndex: number;
  /**
   * 编辑焦点(单数): 决定 ghost 参考框、track 工具往哪条轨迹落关键帧。多选时它是 primary。
   * 只画高亮请用 selectedIds —— 二者语义不同, 别混用。
   */
  selectedId: string | null;
  /** 全部选中项(含 primary), 驱动画布高亮。缺省回落 [selectedId], 保持单选调用方不变。 */
  selectedIds?: readonly string[];
  hiddenTrackIds?: Set<string>;
  /** 已锁定轨迹:锁定后不再显示参考框(视为已确认,不需逐帧提示)。 */
  lockedTrackIds?: Set<string>;
  reviewDisplayMode?: DiffMode;
  trackColorOverrides?: Record<string, string>;
  visual: AnnotationVisualConfig;
  /** 参考框运动预测配置(实验);off=最近关键帧,linear=恒速外推,kalman=完整卡尔曼。 */
  referenceConfig?: VideoReferenceConfig;
  /** pending 草稿(画一半 / 待确认);仅当前帧透传。 */
  pendingDraft?: { geom: VideoStageGeom; className: string } | null;
  /** 采样网格步长;用于「上一网格帧」(gridPrev)判定「跨网格帧续写」集合 S。缺省 1。 */
  samplingStep?: number;
}

const EMPTY_SET = new Set<string>();

/** 复审显示模式过滤(与 VideoStage.visibleInReviewMode 逐位一致)。 */
export function visibleInReviewMode(source: string | null, mode?: DiffMode): boolean {
  if (!mode || mode === "diff") return true;
  if (!source) return false;
  if (mode === "raw") return source === "prediction" || source === "interpolated";
  return source === "manual" || source === "legacy";
}

export function deriveVideoFrameViews(input: DeriveVideoFrameViewsInput): VideoFrameViews {
  const {
    annotations,
    frameIndex,
    selectedId,
    selectedIds,
    hiddenTrackIds = EMPTY_SET,
    lockedTrackIds = EMPTY_SET,
    reviewDisplayMode,
    trackColorOverrides,
    visual,
    referenceConfig = DEFAULT_REFERENCE_CONFIG,
    pendingDraft,
    samplingStep = 1,
  } = input;

  // 高亮集合: 多选时含全部选中项; 未传时回落 primary(单选调用方语义不变)。
  const selectedSet = new Set(selectedIds ?? (selectedId ? [selectedId] : []));

  const videoTracks = annotations.filter(isVideoTrack);
  const polygonTracks = annotations.filter(isVideoPolygonTrack);
  const polylineTracks = annotations.filter(isVideoPolylineTrack);
  const maskTracks = annotations.filter(isVideoMaskTrack);
  // 所有跨帧几何共享同一轨迹编号序列，避免 Mask 标签与侧栏中的其它轨迹编号漂移。
  const trackNumbers = deriveTrackNumber([
    ...videoTracks,
    ...polygonTracks,
    ...polylineTracks,
    ...maskTracks,
  ]);
  const trackContent = visual.labelContent.track;

  // 当前帧应显示的 bbox(legacy bbox + track 解析帧)。
  const entries: VideoEntryView[] = [];
  const currentFrameTrackIds = new Set<string>();
  for (const ann of annotations) {
    if (ann.is_hidden) continue;
    if (isVideoBbox(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      entries.push(
        buildEntryView(
          ann,
          ann.geometry,
          "legacy",
          false,
          undefined,
          selectedSet,
          trackNumbers,
          trackColorOverrides,
          trackContent,
        ),
      );
    } else if (isVideoPolygon(ann) && ann.geometry.frame_index === frameIndex) {
      // 单帧 polygon: 外接盒作 geom (标签/选中锚点) + points 供 <Line closed>。
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const entry = buildEntryView(
        ann,
        boundsOfPoints(ann.geometry.points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: ann.geometry.points });
    } else if (isVideoPolyline(ann) && ann.geometry.frame_index === frameIndex) {
      // 单帧 polyline: 同 polygon 但开路径 (Line 不闭合、不填充)。
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const entry = buildEntryView(
        ann,
        boundsOfPoints(ann.geometry.points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: ann.geometry.points, open: true });
    } else if (isVideoRotatedBbox(ann) && ann.geometry.frame_index === frameIndex) {
      // 单帧 OBB: 四角旋转顶点作闭合 Line (复用 polygon 渲染路径), 外接盒作 geom。
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const corners = rotatedBboxCorners(ann.geometry);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(corners),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: corners, rotatedBbox: ann.geometry });
    } else if (isVideoKeypoint(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const points = ann.geometry.points.map((point) => [point.x, point.y] as [number, number]);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, keypoints: ann.geometry.points });
    } else if (isVideoTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      const resolved = resolveTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      entries.push(
        buildEntryView(
          ann,
          resolved.geom,
          resolved.source,
          Boolean(resolved.occluded),
          ann.geometry.track_id,
          selectedSet,
          trackNumbers,
          trackColorOverrides,
          trackContent,
        ),
      );
    } else if (isVideoPolygonTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      // polygon track: 解析当前帧多边形 → 外接盒作 geom (标签/选中锚点) + points 供 <Line>。
      const resolved = resolveVideoPolygonTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(resolved.points),
        resolved.source,
        Boolean(resolved.occluded),
        ann.geometry.track_id,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: resolved.points });
    } else if (isVideoPolylineTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      // polyline track: 同 polygon 但开路径 (Line 不闭合)。
      const resolved = resolveVideoPolylineTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(resolved.points),
        resolved.source,
        Boolean(resolved.occluded),
        ann.geometry.track_id,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: resolved.points, open: true });
    }
  }

  // 轨迹预览线 + 关键帧圆点:可见且当前帧有解析帧的 track。
  const previews: VideoTrackPreviewView[] = videoTracks
    .filter(
      (ann) =>
        !ann.is_hidden &&
        !hiddenTrackIds.has(ann.geometry.track_id) &&
        currentFrameTrackIds.has(ann.geometry.track_id),
    )
    .map((ann) => {
      const previewTrack = {
        type: "video_track_bbox" as const,
        track_id: ann.geometry.track_id,
        keyframes: ann.geometry.keyframes,
        outside: ann.geometry.outside,
      };
      const points: VideoPreviewPoint[] = [...ann.geometry.keyframes]
        .filter((kf) => !isFrameOutside(previewTrack, kf.frame_index))
        .sort((a, b) => a.frame_index - b.frame_index)
        .map((kf) => ({
          frame: kf.frame_index,
          x: kf.bbox.x + kf.bbox.w / 2,
          y: kf.bbox.y + kf.bbox.h / 2,
          occluded: Boolean(kf.occluded),
        }));
      return {
        key: annotationRenderKey(ann),
        id: ann.id,
        color: getTrackColor(ann.geometry.track_id, ann.class_name, trackColorOverrides),
        selected: selectedSet.has(ann.id),
        points,
      };
    });

  // ghost:选中轨迹当前帧无框时,画参考框(与 VideoTrackSidebar.selectedTrackGhost 一致)。
  // 锁定轨迹视为已确认,不再逐帧提示参考框。
  let ghost: VideoGhostView | null = null;
  const selectedTrack = videoTracks.find((ann) => ann.id === selectedId) ?? null;
  if (
    selectedTrack &&
    !selectedTrack.is_hidden &&
    !hiddenTrackIds.has(selectedTrack.geometry.track_id) &&
    !lockedTrackIds.has(selectedTrack.geometry.track_id) &&
    visibleInReviewMode("manual", reviewDisplayMode) &&
    !entries.some((e) => e.id === selectedTrack.id)
  ) {
    const reference = trackReferenceAtFrame(
      selectedTrack.geometry,
      frameIndex,
      referenceConfig.mode,
      referenceConfig.preset,
    );
    if (reference) {
      const num = trackNumbers.get(selectedTrack.id);
      const refLabel = reference.predicted
        ? `${reference.predictedKind === "kalman" ? "卡尔曼" : "预测"} F${reference.originFrame}↗`
        : `参考 F${reference.originFrame}`;
      ghost = {
        id: selectedTrack.id,
        geom: reference.bbox,
        color: getTrackColor(
          selectedTrack.geometry.track_id,
          selectedTrack.class_name,
          trackColorOverrides,
        ),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${selectedTrack.class_name} · ${refLabel}`,
        uncertainty: reference.uncertainty,
      };
    }
  }

  // 选中 polygon/polyline 轨迹当前帧无实框时的参考虚影(点集几何): 取最近关键帧的顶点,
  // 渲染层据 points 画多边形轮廓 / 折线(不硬塞成外接框)。与上面 bbox ghost 互斥(只有点集轨迹被选时才走)。
  if (!ghost) {
    const selectedPointsTrack =
      polygonTracks.find((ann) => ann.id === selectedId) ??
      polylineTracks.find((ann) => ann.id === selectedId) ??
      null;
    if (
      selectedPointsTrack &&
      !selectedPointsTrack.is_hidden &&
      !hiddenTrackIds.has(selectedPointsTrack.geometry.track_id) &&
      !lockedTrackIds.has(selectedPointsTrack.geometry.track_id) &&
      visibleInReviewMode("manual", reviewDisplayMode) &&
      !entries.some((e) => e.id === selectedPointsTrack.id)
    ) {
      const reference = nearestPointsTrackKeyframe(selectedPointsTrack.geometry, frameIndex);
      if (reference) {
        const num = trackNumbers.get(selectedPointsTrack.id);
        ghost = {
          id: selectedPointsTrack.id,
          geom: boundsOfPoints(reference.points),
          points: reference.points,
          open: isVideoPolylineTrack(selectedPointsTrack),
          color: getTrackColor(
            selectedPointsTrack.geometry.track_id,
            selectedPointsTrack.class_name,
            trackColorOverrides,
          ),
          labelText: `${num !== undefined ? `#${num} · ` : ""}${selectedPointsTrack.class_name} · 参考 F${reference.originFrame}`,
        };
      }
    }
  }

  // 跨网格帧续写参考框(集合 S 的非选中成员):恰好上一网格帧有关键帧、未锁定/未隐藏、
  // 当前帧还没画,且不在当前帧 entries(无插值实框)。选中那条已由上面的 `ghost` 承,这里排除。
  // gridPrev 用 frameIndex 当 maxFrame(上界钳位对「更早的帧」无影响),step=1 时退化为 frameIndex-1。
  const carryOverGhosts: VideoGhostView[] = [];
  const prevGridFrame = gridPrev(frameIndex, samplingStep, frameIndex);
  if (prevGridFrame < frameIndex && visibleInReviewMode("manual", reviewDisplayMode)) {
    for (const ann of videoTracks) {
      if (ann.is_hidden || ann.id === selectedId) continue;
      const tid = ann.geometry.track_id;
      if (hiddenTrackIds.has(tid) || lockedTrackIds.has(tid)) continue;
      const keyframes = ann.geometry.keyframes;
      if (!keyframes.some((kf) => kf.frame_index === prevGridFrame)) continue;
      if (keyframes.some((kf) => kf.frame_index === frameIndex)) continue;
      if (entries.some((e) => e.id === ann.id)) continue;
      const reference = trackReferenceAtFrame(
        ann.geometry,
        frameIndex,
        referenceConfig.mode,
        referenceConfig.preset,
      );
      if (!reference) continue;
      const num = trackNumbers.get(ann.id);
      carryOverGhosts.push({
        id: ann.id,
        geom: reference.bbox,
        color: getTrackColor(tid, ann.class_name, trackColorOverrides),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${ann.class_name}`,
      });
    }
    // 点集轨迹(polygon/polyline)的跨网格帧续写虚影: 同 bbox 判据, 但参考顶点走
    //   nearestPointsTrackKeyframe, 渲染层据 points 画轮廓/折线, 使 Tab 续写流对点集轨迹也生效。
    for (const ann of [...polygonTracks, ...polylineTracks]) {
      if (ann.is_hidden || ann.id === selectedId) continue;
      const tid = ann.geometry.track_id;
      if (hiddenTrackIds.has(tid) || lockedTrackIds.has(tid)) continue;
      const keyframes = ann.geometry.keyframes;
      if (!keyframes.some((kf) => kf.frame_index === prevGridFrame)) continue;
      if (keyframes.some((kf) => kf.frame_index === frameIndex)) continue;
      if (entries.some((e) => e.id === ann.id)) continue;
      const reference = nearestPointsTrackKeyframe(ann.geometry, frameIndex);
      if (!reference) continue;
      const num = trackNumbers.get(ann.id);
      carryOverGhosts.push({
        id: ann.id,
        geom: boundsOfPoints(reference.points),
        points: reference.points,
        open: isVideoPolylineTrack(ann),
        color: getTrackColor(tid, ann.class_name, trackColorOverrides),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${ann.class_name}`,
      });
    }
  }

  // 标签门控(always / selected / none);草稿与 ghost 按 selected=true 门控。
  const visibility = visual.labelVisibility;
  const labels: VideoLabelView[] = [];
  for (const entry of entries) {
    if (shouldShowLabel(entry.selected, visibility)) {
      labels.push({
        key: `entry-${entry.key}`,
        geom: entry.geom,
        color: entry.color,
        text: entry.labelText,
      });
    }
  }
  if (pendingDraft && shouldShowLabel(true, visibility)) {
    labels.push({
      key: "pending-draft",
      geom: pendingDraft.geom,
      color: classColor(pendingDraft.className),
      text: pendingDraft.className,
      opacity: 0.9,
    });
  }
  if (ghost && shouldShowLabel(true, visibility)) {
    labels.push({
      key: `ghost-${ghost.id}`,
      geom: ghost.geom,
      color: ghost.color,
      text: ghost.labelText,
      opacity: 0.86,
    });
  }
  // 跨网格帧续写 ghost 的精简标签(更淡):供识别是哪条待续轨迹(Tab / 点选目标)。
  if (shouldShowLabel(true, visibility)) {
    for (const g of carryOverGhosts) {
      labels.push({
        key: `carryover-${g.id}`,
        geom: g.geom,
        color: g.color,
        text: g.labelText,
        opacity: 0.6,
      });
    }
  }

  return { entries, previews, ghost, carryOverGhosts, labels };
}

/** 多边形顶点的轴对齐外接盒 (归一化); 供 polygon track 的标签/选中锚点定位。 */
function boundsOfPoints(points: [number, number][]): VideoStageGeom {
  if (points.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function buildEntryView(
  ann: AnnotationResponse,
  geom: VideoStageGeom,
  source: "manual" | "prediction" | "interpolated" | "legacy",
  occluded: boolean,
  trackId: string | undefined,
  selectedSet: ReadonlySet<string>,
  trackNumbers: ReadonlyMap<string, number>,
  trackColorOverrides: Record<string, string> | undefined,
  trackContent: AnnotationVisualConfig["labelContent"]["track"],
): VideoEntryView {
  const color = trackId
    ? getTrackColor(trackId, ann.class_name, trackColorOverrides)
    : classColor(ann.class_name);
  const trackNumber = trackNumbers.get(ann.id);
  const renderKey = annotationRenderKey(ann);
  // 状态后缀（· 由 buildTrackLabelText 拼）；插值 / 遮挡互斥。
  const stateSuffix = source === "interpolated" ? "插值" : occluded ? "遮挡" : undefined;
  return {
    key: `${renderKey}-${trackId ?? "legacy"}`,
    id: ann.id,
    geom,
    color,
    selected: selectedSet.has(ann.id),
    dashed: source === "interpolated" || occluded,
    occluded,
    predicted: source === "prediction",
    labelText: buildTrackLabelText(
      {
        className: ann.class_name,
        trackNumber,
        stateSuffix,
        attributes: (ann as { attributes?: Record<string, unknown> | null }).attributes ?? null,
      },
      trackContent,
    ),
    className: ann.class_name,
  };
}
