import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AnnotationResponse, CocoRleMaskRef } from "@/types";
import type { VideoTrackerPreviewResult } from "@/api/videoTracker";
import { rasterMasksApi } from "@/api/rasterMasks";
import { videoTrackerApi } from "@/api/videoTracker";
import type { AiBox } from "../state/transforms";
import { decodeCocoRle, type CocoRle } from "./shared/geometry/maskRle";
import {
  buildTintedMaskRgba,
  closeRasterMaskImage,
  rasterMaskAlphaBounds,
} from "./shared/rasterMaskRender";
import { isVideoMask, isVideoMaskTrack, resolveVideoMaskTrackAtFrame } from "./videoStageGeometry";

export { buildTintedMaskRgba };

const MAX_CACHED_MASKS = 96;

export interface VideoMaskCandidate {
  jobId: string;
  result: VideoTrackerPreviewResult;
  className?: string;
}

export interface VideoMaskRenderRecord {
  id: string;
  source: "annotation" | "prediction" | "tracker";
  image: CanvasImageSource;
  alpha: Uint8Array;
  width: number;
  height: number;
  geom: { x: number; y: number; w: number; h: number };
  color: string;
  zOrder: number;
  selected: boolean;
  isTrack: boolean;
  cacheKey: string;
}

interface MaskDescriptor {
  id: string;
  source: VideoMaskRenderRecord["source"];
  ref: CocoRleMaskRef;
  color: string;
  zOrder: number;
  selected: boolean;
  isTrack: boolean;
  cachePrefix: string;
  frameKey: number;
  load: () => Promise<CocoRle>;
}

interface CachedMask {
  image: CanvasImageSource;
  alpha: Uint8Array;
  width: number;
  height: number;
  geom: VideoMaskRenderRecord["geom"];
}

function closeImage(image: CanvasImageSource) {
  closeRasterMaskImage(image);
}

export function maskAlphaBounds(alpha: Uint8Array, width: number, height: number) {
  return rasterMaskAlphaBounds(alpha, width, height);
}

async function createMaskImage(
  alpha: Uint8Array,
  width: number,
  height: number,
  color: string,
  geom: VideoMaskRenderRecord["geom"],
) {
  const cropX = Math.max(0, Math.min(width - 1, Math.round(geom.x * width)));
  const cropY = Math.max(0, Math.min(height - 1, Math.round(geom.y * height)));
  const cropWidth = Math.max(1, Math.min(width - cropX, Math.round(geom.w * width)));
  const cropHeight = Math.max(1, Math.min(height - cropY, Math.round(geom.h * height)));
  const cropped = new Uint8Array(cropWidth * cropHeight);
  if (geom.w > 0 && geom.h > 0) {
    for (let row = 0; row < cropHeight; row += 1) {
      const sourceStart = (cropY + row) * width + cropX;
      cropped.set(alpha.subarray(sourceStart, sourceStart + cropWidth), row * cropWidth);
    }
  }
  const rgba = buildTintedMaskRgba(cropped, color);
  const imageData = new ImageData(new Uint8ClampedArray(rgba), cropWidth, cropHeight);
  if (typeof createImageBitmap === "function") return createImageBitmap(imageData);
  const canvas = document.createElement("canvas");
  canvas.width = cropWidth;
  canvas.height = cropHeight;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("2D canvas context is unavailable");
  context.putImageData(imageData, 0, 0);
  return canvas;
}

function cacheKey(descriptor: MaskDescriptor) {
  return `${descriptor.cachePrefix}:frame:${descriptor.frameKey}:${descriptor.ref.sha256}:${descriptor.color}`;
}

export function useVideoMaskFrames(params: {
  taskId: string | null;
  annotations: readonly AnnotationResponse[];
  candidates: readonly VideoMaskCandidate[];
  predictions: readonly AiBox[];
  frameIndex: number;
  selectedId: string | null;
  colorForAnnotation: (annotation: AnnotationResponse) => string;
  colorForPrediction: (prediction: AiBox) => string;
}): VideoMaskRenderRecord[] {
  const {
    taskId,
    annotations,
    candidates,
    predictions,
    frameIndex,
    selectedId,
    colorForAnnotation,
    colorForPrediction,
  } = params;
  const cacheRef = useRef(new Map<string, CachedMask>());
  const retiredImagesRef = useRef(new Set<CanvasImageSource>());
  const generationRef = useRef(0);
  const [records, setRecords] = useState<VideoMaskRenderRecord[]>([]);
  const retireImage = useCallback((image: CanvasImageSource) => {
    retiredImagesRef.current.add(image);
  }, []);

  useEffect(() => {
    if (retiredImagesRef.current.size === 0) return;
    const closeRetired = () => {
      const activeImages = new Set(records.map((record) => record.image));
      for (const image of retiredImagesRef.current) {
        if (activeImages.has(image)) continue;
        closeImage(image);
        retiredImagesRef.current.delete(image);
      }
    };
    if (typeof requestAnimationFrame === "function") {
      const frame = requestAnimationFrame(closeRetired);
      return () => cancelAnimationFrame(frame);
    }
    const timer = window.setTimeout(closeRetired, 0);
    return () => window.clearTimeout(timer);
  }, [records]);

  const descriptors = useMemo<MaskDescriptor[]>(() => {
    const committed = annotations.flatMap((annotation) => {
      if (annotation.id.startsWith("tmp_")) return [];
      if (isVideoMask(annotation)) {
        if (annotation.geometry.frame_index !== frameIndex) return [];
        return [
          {
            id: annotation.id,
            source: "annotation" as const,
            ref: annotation.geometry.mask,
            color: colorForAnnotation(annotation),
            zOrder: annotation.z_order ?? 0,
            selected: annotation.id === selectedId,
            isTrack: false,
            cachePrefix: `annotation:${annotation.id}:version:${annotation.version ?? 0}`,
            frameKey: frameIndex,
            load: () => rasterMasksApi.annotationRasterMaskContent(annotation.id),
          },
        ];
      }
      if (!isVideoMaskTrack(annotation)) return [];
      const resolved = resolveVideoMaskTrackAtFrame(annotation.geometry, frameIndex);
      if (!resolved) return [];
      return [
        {
          id: annotation.id,
          source: "annotation" as const,
          ref: resolved.mask,
          color: colorForAnnotation(annotation),
          zOrder: annotation.z_order ?? 0,
          selected: annotation.id === selectedId,
          isTrack: true,
          cachePrefix: `annotation:${annotation.id}:version:${annotation.version ?? 0}`,
          frameKey: resolved.keyframeFrame,
          load: () => rasterMasksApi.annotationVideoMaskContent(annotation.id, frameIndex),
        },
      ];
    });
    const staged = candidates.flatMap((candidate, index) => {
      const geometry = candidate.result.geometry;
      if (
        candidate.result.outside ||
        geometry.type !== "mask" ||
        candidate.result.frame_index !== frameIndex
      )
        return [];
      const instance = candidate.result.instance_id ?? String(index + 1);
      return [
        {
          id: `tracker:${candidate.jobId}:${instance}:${frameIndex}`,
          source: "tracker" as const,
          ref: geometry.mask,
          color: "#a855f7",
          zOrder: Number.MAX_SAFE_INTEGER - index,
          selected: false,
          isTrack: false,
          cachePrefix: `tracker:${candidate.jobId}:instance:${instance}`,
          frameKey: frameIndex,
          load: () => videoTrackerApi.maskContent(candidate.jobId, geometry.mask.sha256),
        },
      ];
    });
    const imported = predictions.flatMap((prediction, index) => {
      const geometry = prediction.geometry;
      if (!taskId || geometry?.type !== "video_track_mask") return [];
      const resolved = resolveVideoMaskTrackAtFrame(geometry, frameIndex);
      if (!resolved) return [];
      return [
        {
          id: prediction.id,
          source: "prediction" as const,
          ref: resolved.mask,
          color: colorForPrediction(prediction),
          zOrder: Number.MAX_SAFE_INTEGER - candidates.length - index,
          selected: prediction.id === selectedId,
          isTrack: true,
          cachePrefix: `prediction:${prediction.predictionId}:shape:${prediction.shapeIndex}`,
          frameKey: resolved.keyframeFrame,
          load: () =>
            rasterMasksApi.predictionVideoMaskContent(
              taskId,
              prediction.predictionId,
              prediction.shapeIndex,
              frameIndex,
            ),
        },
      ];
    });
    return [...committed, ...staged, ...imported];
  }, [
    annotations,
    candidates,
    colorForAnnotation,
    colorForPrediction,
    frameIndex,
    predictions,
    selectedId,
    taskId,
  ]);

  useEffect(() => {
    generationRef.current += 1;
    for (const cached of cacheRef.current.values()) retireImage(cached.image);
    cacheRef.current.clear();
    setRecords((current) => (current.length > 0 ? [] : current));
  }, [retireImage, taskId]);

  useEffect(() => {
    const generation = ++generationRef.current;
    let cancelled = false;
    if (descriptors.length === 0) {
      setRecords((current) => (current.length > 0 ? [] : current));
      return () => {
        cancelled = true;
      };
    }
    const activeKeys = new Set(descriptors.map(cacheKey));
    const committedIds = new Set(
      descriptors
        .filter((descriptor) => descriptor.source === "annotation")
        .map((descriptor) => descriptor.id),
    );
    for (const [key, cached] of cacheRef.current) {
      const superseded = [...committedIds].some((id) => {
        const descriptor = descriptors.find(
          (item) => item.id === id && item.source === "annotation",
        );
        return (
          key.startsWith(`annotation:${id}:version:`) &&
          descriptor != null &&
          !key.startsWith(`${descriptor.cachePrefix}:`)
        );
      });
      if (superseded) {
        retireImage(cached.image);
        cacheRef.current.delete(key);
      }
    }

    const resolveRecords = async () => {
      const next = await Promise.all(
        descriptors.map(async (descriptor) => {
          const key = cacheKey(descriptor);
          let cached = cacheRef.current.get(key);
          if (!cached) {
            const rle = await descriptor.load();
            if (rle.size[0] !== descriptor.ref.size[0] || rle.size[1] !== descriptor.ref.size[1]) {
              throw new Error("mask content size does not match its reference");
            }
            const alpha = decodeCocoRle(rle);
            const [height, width] = rle.size;
            const geom = maskAlphaBounds(alpha, width, height);
            const image = await createMaskImage(alpha, width, height, descriptor.color, geom);
            if (cancelled || generation !== generationRef.current) {
              closeImage(image);
              return null;
            }
            cached = { image, alpha, width, height, geom };
            cacheRef.current.set(key, cached);
          } else {
            cacheRef.current.delete(key);
            cacheRef.current.set(key, cached);
          }
          return {
            id: descriptor.id,
            source: descriptor.source,
            image: cached.image,
            alpha: cached.alpha,
            width: cached.width,
            height: cached.height,
            geom: cached.geom,
            color: descriptor.color,
            zOrder: descriptor.zOrder,
            selected: descriptor.selected,
            isTrack: descriptor.isTrack,
            cacheKey: key,
          } satisfies VideoMaskRenderRecord;
        }),
      );
      if (cancelled || generation !== generationRef.current) return;
      while (cacheRef.current.size > MAX_CACHED_MASKS) {
        const evictKey = [...cacheRef.current.keys()].find((key) => !activeKeys.has(key));
        if (!evictKey) break;
        const evicted = cacheRef.current.get(evictKey);
        if (evicted) retireImage(evicted.image);
        cacheRef.current.delete(evictKey);
      }
      setRecords(next.filter((record): record is VideoMaskRenderRecord => record !== null));
    };

    void resolveRecords().catch(() => {
      if (!cancelled && generation === generationRef.current) {
        setRecords((current) => (current.length > 0 ? [] : current));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [descriptors, retireImage]);

  useEffect(
    () => () => {
      generationRef.current += 1;
      for (const cached of cacheRef.current.values()) closeImage(cached.image);
      cacheRef.current.clear();
      for (const image of retiredImagesRef.current) closeImage(image);
      retiredImagesRef.current.clear();
    },
    [],
  );

  return useMemo(() => {
    const activeTrackerKeys = new Set(
      descriptors.filter((descriptor) => descriptor.source === "tracker").map(cacheKey),
    );
    return records.filter(
      (record) => record.source !== "tracker" || activeTrackerKeys.has(record.cacheKey),
    );
  }, [descriptors, records]);
}
