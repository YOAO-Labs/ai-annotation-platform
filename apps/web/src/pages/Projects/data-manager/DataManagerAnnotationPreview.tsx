/**
 * Data Manager 只读标注预览。
 *
 * 在 TaskMatchesSheet / EntityDetailSheet 内渲染当前任务的正式保存标注
 * (boxes / 旋转框 / 带 holes 与 multi-part 的多边形 / 折线 / 关键点骨架 /
 * 像素掩码)，几何与图像像素共用同一 scale + offset（含 letterbox）。
 *
 * 只读约束：不挂任何 move/resize/rotate/mutation handler；编辑入口仍是
 * 工作台链接。读取仅限当前任务：任务媒体走任务读 API，标注走既有的
 * `/tasks/{id}/annotations/page` (200/页) 游标分页，请求带 AbortSignal，
 * 关闭/切换任务时取消过期请求。配色与骨骼配置按项目显式传入，不依赖
 * Workbench 上次写入的全局类别色。
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Image as KonvaImage, Layer, Stage } from "react-konva";

import { useProject } from "@/hooks/useProjects";
import { tasksApi } from "@/api/tasks";
import { rasterMasksApi } from "@/api/rasterMasks";
import { useAuthStore } from "@/stores/authStore";
import type { AttributeField, ClassesConfig } from "@/api/projects";
import type { AnnotationResponse, KeypointSchema, RotatedBboxGeometry } from "@/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { classColorForCanvas } from "@/pages/Workbench/stage/colors";
import { DEFAULT_ANNOTATION_VISUAL } from "@/pages/Workbench/stage/annotationVisual";
import { RasterMaskVisual } from "@/pages/Workbench/stage/RasterMaskVisual";
import {
  KonvaBox,
  KonvaKeypoint,
  KonvaPolygon,
  KonvaPolyline,
  KonvaRotatedBox,
} from "@/pages/Workbench/stage/ImageStageShapes";
import { useRasterMaskRecords } from "@/pages/Workbench/stage/shared/useRasterMaskRecords";
import { useAbortableImage } from "@/pages/Workbench/stage/useAbortableImage";
import {
  useWorkbenchImageSource,
  workbenchImagePreviewUrl,
} from "@/pages/Workbench/stage/useWorkbenchImageSource";
import { fitToCanvas } from "@/pages/Workbench/stage/shared/viewport/fit";
import { annotationToBox, collectOccludedKeys } from "@/pages/Workbench/state/transforms";
import { toolUnitForGeometryType } from "@/pages/Workbench/stage/tools/toolUnits";

const ANNOTATION_PAGE_LIMIT = 200;
const PREVIEW_VISUAL = {
  ...DEFAULT_ANNOTATION_VISUAL,
  labelVisibility: "selected",
} as const;

export interface DataManagerAnnotationPreviewProps {
  projectId: string;
  taskId: string;
  /** 高亮的已保存标注 (对象视图选中的 annotation_id)。 */
  highlightAnnotationId?: string | null;
  className?: string;
}

const EMPTY_CLASSES_CONFIG: ClassesConfig = {};

function previewToolUnit(annotation: AnnotationResponse) {
  return annotation.tool_unit_id ?? toolUnitForGeometryType(annotation.geometry.type);
}

function previewProjectConfig(toolBindings: Record<string, unknown> | null | undefined) {
  const configs: Record<string, ClassesConfig> = {};
  const schemas: Record<string, KeypointSchema> = {};
  // v0.11.27 · 遮挡样式 key 跨工具单位并集（含禁用单位），与工作台/复核
  // annotationToBox 的输入一致，保证预览的虚线+半透遮挡视觉与工作台相同。
  const occludedKeys = new Set<string>();
  for (const [unit, raw] of Object.entries(toolBindings ?? {})) {
    if (!raw || typeof raw !== "object") continue;
    const binding = raw as {
      enabled?: boolean;
      classes?: Array<{
        name: string;
        color?: string | null;
        order?: number;
        alias?: string | null;
      }>;
      keypoint_schema?: KeypointSchema | null;
      attribute_schema?: { fields?: AttributeField[] | null } | null;
    };
    for (const key of collectOccludedKeys(binding.attribute_schema?.fields ?? [])) {
      occludedKeys.add(key);
    }
    if (binding.enabled === false) continue;
    if (binding.keypoint_schema) schemas[unit] = binding.keypoint_schema;
    const config: ClassesConfig = {};
    for (const entry of binding.classes ?? []) {
      if (!entry?.name) continue;
      config[entry.name] = {
        color: entry.color ?? null,
        order: entry.order ?? 0,
        alias: entry.alias ?? null,
      };
    }
    configs[unit] = config;
  }
  return { configs, schemas, occludedKeys };
}

export default function DataManagerAnnotationPreview({
  projectId,
  taskId,
  highlightAnnotationId = null,
  className,
}: DataManagerAnnotationPreviewProps) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  const ownerScope = `${userId ?? "anonymous"}`;
  const projectQ = useProject(projectId);
  const taskQ = useQuery({
    queryKey: ["dm-preview-task", projectId, taskId, ownerScope, tokenEpoch],
    queryFn: ({ signal }) => tasksApi.get(taskId, { signal }),
    enabled: Boolean(taskId),
    staleTime: 15_000,
    refetchOnMount: "always",
  });
  const task = taskQ.data;

  // Saved annotations only: pending AI/tracker candidates are never rendered
  // here; they stay in the separately labelled match list. Non-image media
  // never triggers annotation reads.
  const annotationsQ = useInfiniteQuery({
    queryKey: ["dm-preview-annotations", projectId, taskId, ownerScope, tokenEpoch],
    queryFn: ({ pageParam, signal }) =>
      tasksApi.getAnnotationsPage(
        taskId,
        { cursor: pageParam, limit: ANNOTATION_PAGE_LIMIT },
        { signal },
      ),
    enabled: Boolean(taskId) && task?.file_type === "image",
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 15_000,
    refetchOnMount: "always",
  });
  const annotations = useMemo(
    () => annotationsQ.data?.pages.flatMap((page) => page.items) ?? [],
    [annotationsQ.data?.pages],
  );
  const hasNextPage = Boolean(annotationsQ.hasNextPage);
  const fetchNextAnnotations = annotationsQ.fetchNextPage;
  const fetchingAnnotations = annotationsQ.isFetching;
  const annotationLoadFailed = annotationsQ.isError;

  const [showAnnotations, setShowAnnotations] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(highlightAnnotationId);
  useEffect(() => {
    setSelectedId(highlightAnnotationId);
  }, [highlightAnnotationId]);

  // Keep walking annotation pages until the highlighted object is found (or
  // the task's pages are exhausted); absence on the first page never implies
  // deletion.
  useEffect(() => {
    if (!highlightAnnotationId || !hasNextPage) return;
    if (annotations.some((item) => item.id === highlightAnnotationId)) return;
    if (fetchingAnnotations || annotationLoadFailed) return;
    void fetchNextAnnotations({ cancelRefetch: false });
  }, [
    annotations,
    annotationLoadFailed,
    fetchNextAnnotations,
    fetchingAnnotations,
    hasNextPage,
    highlightAnnotationId,
  ]);
  const highlightExhausted =
    Boolean(highlightAnnotationId) &&
    annotationsQ.isSuccess &&
    !annotationsQ.isFetching &&
    !hasNextPage &&
    !annotations.some((item) => item.id === highlightAnnotationId);

  const {
    configs: toolClassesConfigs,
    schemas: keypointSchemas,
    occludedKeys,
  } = useMemo(
    () => previewProjectConfig(projectQ.data?.tool_bindings),
    [projectQ.data?.tool_bindings],
  );

  const imageSource = useWorkbenchImageSource(task);
  const previewUrl = workbenchImagePreviewUrl(imageSource.source);
  const [imageAttempt, setImageAttempt] = useState(0);
  const [image, imageStatus] = useAbortableImage(previewUrl ?? "", imageAttempt);
  const refetchTask = taskQ.refetch;
  const refreshImageSource = imageSource.refresh;
  const refreshMedia = useCallback(async () => {
    await Promise.all([refetchTask(), refreshImageSource()]);
    // Signed URLs may intentionally be stable across reads. A fresh attempt
    // must still load the pixels after a transient image-network failure.
    setImageAttempt((value) => value + 1);
  }, [refetchTask, refreshImageSource]);
  // A failed media URL may be an expired signature: refetch task metadata once
  // for a fresh URL, then keep an actionable retry.
  const refreshedOnceRef = useRef(false);
  useEffect(() => {
    if (imageStatus !== "failed" || refreshedOnceRef.current) return;
    refreshedOnceRef.current = true;
    void refreshMedia();
  }, [imageStatus, refreshMedia]);

  const aspectSource =
    task?.image_width && task?.image_height
      ? { w: task.image_width, h: task.image_height }
      : image
        ? { w: image.naturalWidth || 3, h: image.naturalHeight || 2 }
        : null;
  const aspect = aspectSource && aspectSource.h > 0 ? aspectSource.w / aspectSource.h : 3 / 2;

  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  useLayoutEffect(() => {
    if (!container) return;
    const measure = () => setContainerWidth(Math.max(0, Math.floor(container.clientWidth)));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [container]);

  const boxHeight = Math.min(560, Math.max(240, Math.round((containerWidth || 640) / aspect)));
  const imgW = aspectSource?.w ?? 0;
  const imgH = aspectSource?.h ?? 0;
  const viewport = useMemo(
    () => fitToCanvas(containerWidth || 640, boxHeight, imgW || 3, imgH || 2),
    [boxHeight, containerWidth, imgH, imgW],
  );

  const visibleAnnotations = useMemo(
    () => annotations.filter((item) => !item.is_hidden),
    [annotations],
  );
  const boxes = useMemo(
    () =>
      visibleAnnotations
        .map((item) => annotationToBox(item, occludedKeys))
        .sort((a, b) => (a.z_order ?? 0) - (b.z_order ?? 0) || a.id.localeCompare(b.id)),
    [visibleAnnotations, occludedKeys],
  );

  // Mask content fetches are owned by this preview and cancelled when the
  // task/account scope changes or the sheet closes.
  const maskScopeKey = JSON.stringify([ownerScope, tokenEpoch, projectId, taskId]);
  const maskAbortRef = useRef<AbortController | null>(null);
  // Register before useRasterMaskRecords: its layout effects can immediately
  // start a cached annotation's content request on this same commit.
  useLayoutEffect(() => {
    maskAbortRef.current?.abort();
    const controller = new AbortController();
    maskAbortRef.current = controller;
    return () => controller.abort();
  }, [maskScopeKey]);
  const maskDescriptors = useMemo(() => {
    if (!showAnnotations) return [];
    return visibleAnnotations.flatMap((annotation) => {
      if (annotation.geometry.type !== "raster_mask") return [];
      const color = classColorForCanvas(
        annotation.class_name,
        toolClassesConfigs[previewToolUnit(annotation)] ?? EMPTY_CLASSES_CONFIG,
      );
      return [
        {
          id: annotation.id,
          source: "annotation" as const,
          ref: annotation.geometry.mask,
          revision: annotation.version ?? annotation.geometry.mask.sha256,
          color,
          colorRevision: color,
          zOrder: annotation.z_order ?? 0,
          selected: annotation.id === selectedId,
          deduplicateLoad: false,
          load: () =>
            rasterMasksApi.annotationRasterMaskContent(annotation.id, {
              signal: maskAbortRef.current?.signal,
            }),
        },
      ];
    });
  }, [visibleAnnotations, toolClassesConfigs, selectedId, showAnnotations]);
  const {
    records: maskRecords,
    statusById: maskStatusById,
    retry: retryMask,
  } = useRasterMaskRecords({ scopeKey: maskScopeKey, descriptors: maskDescriptors });
  const failedMaskIds = useMemo(
    () =>
      [...maskStatusById.entries()]
        .filter(([, status]) => status.state === "error")
        .map(([id]) => id),
    [maskStatusById],
  );
  const deferredMaskCount = [...maskStatusById.values()].filter(
    (status) => status.state === "deferred",
  ).length;
  const loadingMaskCount = [...maskStatusById.values()].filter(
    (status) => status.state === "loading",
  ).length;
  const annotationById = useMemo(
    () => new Map(annotations.map((item) => [item.id, item])),
    [annotations],
  );

  if (taskQ.isLoading) {
    return (
      <section
        aria-label="标注预览"
        className={cn("rounded-md border border-border bg-card p-2", className)}
      >
        <Skeleton className="h-[280px] w-full rounded-sm" />
      </section>
    );
  }
  if (taskQ.isError || !task) {
    return (
      <section
        aria-label="标注预览"
        className={cn("rounded-md border border-border bg-card p-3 text-sm", className)}
      >
        <div role="alert" className="text-status-danger">
          无法加载任务媒体
        </div>
        <Button size="sm" className="mt-2" onClick={() => void taskQ.refetch()}>
          重试
        </Button>
      </section>
    );
  }
  if (task.file_type !== "image") {
    // Videos, trajectories and point clouds keep their metadata and the
    // Workbench link; dense overlays are out of scope for this preview.
    return null;
  }
  if (imageSource.source?.kind === "pyramid-pending") {
    return (
      <section
        aria-label="标注预览"
        className={cn(
          "flex h-[280px] items-center justify-center rounded-md border border-border bg-card p-3 text-sm text-muted-foreground",
          className,
        )}
      >
        大图切片处理中，稍后自动刷新…
      </section>
    );
  }
  if (imageSource.source?.kind === "pyramid-failed") {
    return (
      <section
        aria-label="标注预览"
        className={cn("rounded-md border border-border bg-card p-3 text-sm", className)}
      >
        <div role="alert" className="text-status-danger">
          大图切片暂不可用，无法渲染预览
        </div>
        <Button size="sm" className="mt-2" onClick={() => void refreshMedia()}>
          重新加载切片状态
        </Button>
      </section>
    );
  }
  if (!previewUrl || imageStatus === "failed") {
    return (
      <section
        aria-label="标注预览"
        className={cn("rounded-md border border-border bg-card p-3 text-sm", className)}
      >
        <div role="alert" className="text-status-danger">
          图片加载失败，签名可能已过期
        </div>
        <Button size="sm" className="mt-2" onClick={() => void refreshMedia()}>
          重新加载预览
        </Button>
      </section>
    );
  }

  const scale = viewport?.scale ?? 1;
  const tx = viewport?.tx ?? 0;
  const ty = viewport?.ty ?? 0;

  return (
    <section
      aria-label="标注预览"
      className={cn("rounded-md border border-border bg-card", className)}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {annotationsQ.data ? (
            <span>已保存标注 {annotations.length.toLocaleString()} 条</span>
          ) : annotationsQ.isPending ? (
            <span role="status">正在加载已保存标注…</span>
          ) : null}
          {hasNextPage && <span className="text-status-caution">部分加载</span>}
          {annotationLoadFailed && (
            <div role="alert" className="text-status-danger">
              {annotationsQ.isFetchNextPageError
                ? "后续标注加载失败，已保留当前内容"
                : "标注加载失败，无法确认完整标注数量"}
              {!annotationsQ.isFetchNextPageError && (
                <Button size="sm" variant="ghost" onClick={() => void annotationsQ.refetch()}>
                  重试加载标注
                </Button>
              )}
            </div>
          )}
          {highlightExhausted && (
            <span className="text-status-caution">未找到选中的对象，可能已被删除</span>
          )}
          {failedMaskIds.length > 0 && (
            <span className="text-status-caution">
              {failedMaskIds.length} 个掩码加载失败
              <button
                type="button"
                className="ml-1 rounded-sm text-foreground underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => failedMaskIds.forEach((id) => retryMask(id))}
              >
                重试掩码
              </button>
            </span>
          )}
          {loadingMaskCount > 0 && <span role="status">{loadingMaskCount} 个掩码正在加载</span>}
          {deferredMaskCount > 0 && (
            <span role="status" className="text-status-caution">
              {deferredMaskCount} 个掩码因内存预算暂未显示，请在工作台查看
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant={showAnnotations ? "default" : "ghost"}
            aria-pressed={showAnnotations}
            onClick={() => setShowAnnotations((value) => !value)}
          >
            <Icon name={showAnnotations ? "eye" : "eyeOff"} size={12} />
            {showAnnotations ? "隐藏标注" : "显示标注"}
          </Button>
        </div>
      </div>
      <div ref={setContainer} className="relative bg-muted">
        <div
          className="h-[var(--dm-preview-height)]"
          // eslint-disable-next-line no-restricted-syntax -- the fitted preview height is a one-off runtime value.
          style={{ "--dm-preview-height": `${boxHeight}px` } as CSSProperties}
        >
          {imageStatus === "loading" && !image ? (
            <Skeleton className="size-full rounded-none" />
          ) : (
            <Stage
              width={containerWidth || 640}
              height={boxHeight}
              x={tx}
              y={ty}
              scaleX={scale}
              scaleY={scale}
            >
              <Layer listening={false}>
                {image && (
                  <KonvaImage
                    image={image}
                    x={0}
                    y={0}
                    width={imgW}
                    height={imgH}
                    listening={false}
                  />
                )}
              </Layer>
              {showAnnotations && (
                <Layer listening={false}>
                  {maskRecords.map((record) => {
                    const annotation = annotationById.get(record.id);
                    const color = classColorForCanvas(
                      annotation?.class_name ?? "mask",
                      (annotation && toolClassesConfigs[previewToolUnit(annotation)]) ??
                        EMPTY_CLASSES_CONFIG,
                    );
                    return (
                      <RasterMaskVisual
                        key={record.cacheKey}
                        id={record.id}
                        image={record.image}
                        bounds={record.bounds}
                        sourceWidth={imgW}
                        sourceHeight={imgH}
                        scale={scale}
                        color={color}
                        selected={record.selected}
                        visual={PREVIEW_VISUAL}
                      />
                    );
                  })}
                </Layer>
              )}
              {showAnnotations && (
                <Layer>
                  {boxes.map((b) => {
                    if (b.geometry?.type === "raster_mask") return null;
                    const annotation = annotationById.get(b.id)!;
                    const classesConfig =
                      toolClassesConfigs[previewToolUnit(annotation)] ?? EMPTY_CLASSES_CONFIG;
                    const selected = selectedId === b.id;
                    const handleClick = () => setSelectedId(b.id);
                    if (b.geometry?.type === "rotated_bbox") {
                      const geometry = b.geometry as RotatedBboxGeometry;
                      return (
                        <KonvaRotatedBox
                          key={b.id}
                          b={b}
                          annotationId={b.id}
                          geometry={geometry}
                          angle={geometry.angle}
                          isAi={false}
                          selected={selected}
                          editable={false}
                          faded={false}
                          visual={PREVIEW_VISUAL}
                          colorConfig={classesConfig}
                          imgW={imgW}
                          imgH={imgH}
                          scale={scale}
                          onClick={handleClick}
                          onMoveStart={null}
                          onRotateStart={null}
                          onResizeStart={null}
                        />
                      );
                    }
                    if (b.polyline && b.polyline.length >= 2) {
                      return (
                        <KonvaPolyline
                          key={b.id}
                          b={b}
                          annotationId={b.id}
                          isAi={false}
                          selected={selected}
                          faded={false}
                          visual={PREVIEW_VISUAL}
                          colorConfig={classesConfig}
                          imgW={imgW}
                          imgH={imgH}
                          scale={scale}
                          points={b.polyline}
                          editable={false}
                          onClick={handleClick}
                          onBodyMouseDown={null}
                        />
                      );
                    }
                    if (b.geometry?.type === "keypoint") {
                      const schema = keypointSchemas[previewToolUnit(annotation)] ?? null;
                      return (
                        <KonvaKeypoint
                          key={b.id}
                          b={b}
                          annotationId={b.id}
                          isAi={false}
                          selected={selected}
                          faded={false}
                          visual={PREVIEW_VISUAL}
                          colorConfig={classesConfig}
                          imgW={imgW}
                          imgH={imgH}
                          scale={scale}
                          schema={schema}
                          editable={false}
                          onClick={handleClick}
                        />
                      );
                    }
                    if (b.polygon && b.polygon.length >= 3) {
                      return (
                        <KonvaPolygon
                          key={b.id}
                          b={b}
                          annotationId={b.id}
                          isAi={false}
                          selected={selected}
                          faded={false}
                          visual={PREVIEW_VISUAL}
                          colorConfig={classesConfig}
                          imgW={imgW}
                          imgH={imgH}
                          scale={scale}
                          points={b.polygon}
                          editable={false}
                          occluded={Boolean(b.occluded)}
                          onClick={handleClick}
                          onBodyMouseDown={null}
                        />
                      );
                    }
                    return (
                      <KonvaBox
                        key={b.id}
                        b={b}
                        annotationId={b.id}
                        isAi={false}
                        selected={selected}
                        editable={false}
                        faded={false}
                        visual={PREVIEW_VISUAL}
                        colorConfig={classesConfig}
                        occluded={Boolean(b.occluded)}
                        imgW={imgW}
                        imgH={imgH}
                        scale={scale}
                        onClick={handleClick}
                        onMoveStart={null}
                        onResizeStart={null}
                      />
                    );
                  })}
                </Layer>
              )}
            </Stage>
          )}
        </div>
      </div>
      {hasNextPage && (
        <div className="flex items-center justify-center border-t border-border px-3 py-2">
          <Button
            size="sm"
            variant="ghost"
            disabled={annotationsQ.isFetching}
            onClick={() => void annotationsQ.fetchNextPage({ cancelRefetch: false })}
          >
            {annotationsQ.isFetchingNextPage
              ? "加载中…"
              : annotationsQ.isFetchNextPageError
                ? "重试加载更多标注"
                : "加载更多标注"}
          </Button>
        </div>
      )}
    </section>
  );
}
