import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AttributeSchema } from "@/api/projects";
import type { AnnotationResponse } from "@/types";
import { AttributeForm } from "../AttributeForm";
import { IdentityHeader, annotationSourceKind } from "./IdentityHeader";
import { MetricGrid } from "./MetricGrid";
import { MetaFooter } from "./MetaFooter";
import { ActionBar } from "./ActionBar";
import { geometryMetrics } from "./geometryMetrics";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";
const ATTR_BLOCK_CLASS = "border-t border-border pt-2";
const FRAME_CHIP_CLASS =
  "inline-flex flex-none items-center gap-1 rounded-full px-1.5 py-px text-2xs font-medium tabular-nums whitespace-nowrap bg-brand/10 text-brand";
const FRAME_TIME_CLASS = "text-brand/75";

export interface VideoFrameBoxCardContentProps {
  annotation: AnnotationResponse;
  imageWidth: number | null;
  imageHeight: number | null;
  fps: number | null;
  attributeSchema: AttributeSchema | undefined;
  readOnly: boolean;
  onSeekFrame: (frameIndex: number) => void;
  onChangeClass: (id: string) => void;
  onDelete: (id: string) => void;
  onUpdateAttributes: (id: string, next: Record<string, unknown>) => void;
  onConvert?: (id: string) => void;
  onEditMask?: () => void;
}

function formatTimecode(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function VideoFrameBoxCardContent({
  annotation,
  imageWidth,
  imageHeight,
  fps,
  attributeSchema,
  readOnly,
  onSeekFrame,
  onChangeClass,
  onDelete,
  onUpdateAttributes,
  onConvert,
  onEditMask,
}: VideoFrameBoxCardContentProps) {
  const geom = annotation.geometry;
  const frameIndex =
    geom.type === "video_bbox" ||
    geom.type === "video_polygon" ||
    geom.type === "video_polyline" ||
    geom.type === "video_rotated_bbox" ||
    geom.type === "video_keypoint" ||
    geom.type === "video_mask"
      ? geom.frame_index
      : null;
  const metrics = geometryMetrics(geom, imageWidth, imageHeight);
  const hasAttributes = !!attributeSchema && (attributeSchema.fields ?? []).length > 0;
  const timeLabel = frameIndex !== null && fps ? formatTimecode(frameIndex / fps) : null;

  const frameChip =
    frameIndex !== null ? (
      <span className={FRAME_CHIP_CLASS} title={`第 ${frameIndex} 帧`}>
        <Icon name="film" size={10} />F{frameIndex}
        {timeLabel && <span className={FRAME_TIME_CLASS}>· {timeLabel}</span>}
      </span>
    ) : undefined;

  return (
    <div className={BODY_CLASS}>
      <IdentityHeader
        className={annotation.class_name}
        source={annotationSourceKind(annotation)}
        trailing={frameChip}
      />

      <MetricGrid metrics={metrics} />

      {hasAttributes && (
        <div className={ATTR_BLOCK_CLASS} data-floating-panel-no-drag>
          <AttributeForm
            schema={attributeSchema}
            className={annotation.class_name}
            attributes={annotation.attributes ?? {}}
            onChange={(next) => onUpdateAttributes(annotation.id, next)}
            readOnly={readOnly}
            context="video"
            hideHeading
          />
        </div>
      )}

      <MetaFooter
        id={annotation.id}
        source={annotation.source}
        createdAt={annotation.created_at}
        updatedAt={annotation.updated_at}
        zOrder={annotation.z_order}
      />

      <ActionBar label="单帧标注操作">
        {geom.type === "video_mask" && onEditMask && (
          <Button
            variant="ghost"
            size="sm"
            title="编辑 Mask"
            aria-label="编辑 Mask"
            disabled={readOnly || annotation.is_locked}
            onClick={onEditMask}
          >
            <Icon name="edit" size={14} />
            编辑
          </Button>
        )}
        {geom.type === "video_polygon" && onConvert && (
          <Button
            variant="ghost"
            size="sm"
            title="转为 Mask"
            aria-label="转为 Mask"
            disabled={readOnly || annotation.is_locked}
            onClick={() => onConvert(annotation.id)}
          >
            转 Mask
          </Button>
        )}
        {frameIndex !== null && (
          <Button
            variant="ghost"
            size="sm"
            title="跳到该帧"
            onClick={() => onSeekFrame(frameIndex)}
          >
            <Icon name="crosshair" size={14} />
            跳到该帧
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          title="修改类别"
          aria-label="修改类别"
          disabled={readOnly}
          onClick={() => onChangeClass(annotation.id)}
        >
          <Icon name="tag" size={14} />
        </Button>
        <Button
          variant="danger"
          size="sm"
          title="删除标注"
          aria-label="删除标注"
          disabled={readOnly}
          onClick={() => onDelete(annotation.id)}
        >
          <Icon name="trash" size={14} />
        </Button>
      </ActionBar>
    </div>
  );
}
