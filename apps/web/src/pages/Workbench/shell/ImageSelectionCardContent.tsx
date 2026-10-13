import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AttributeSchema } from "@/api/projects";
import type { AnnotationResponse } from "@/types";
import { AttributeForm } from "./AttributeForm";
import { IdentityHeader, annotationSourceKind } from "./selectionCard/IdentityHeader";
import { MetricGrid } from "./selectionCard/MetricGrid";
import { MetaFooter } from "./selectionCard/MetaFooter";
import { ActionBar } from "./selectionCard/ActionBar";
import { geometryMetrics } from "./selectionCard/geometryMetrics";
import { isComplexPolygonGeometry } from "../stage/shared/geometry/geometryEditPolicy";
import type { RasterMaskRecordStatus } from "../stage/shared/useRasterMaskRecords";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";
const ATTR_BLOCK_CLASS = "border-t border-border pt-2";

export interface ImageSelectionCardContentProps {
  annotation: AnnotationResponse;
  imageWidth: number | null;
  imageHeight: number | null;
  attributeSchema: AttributeSchema | undefined;
  /** 任务级锁定(review/completed)→ 全部操作只读。 */
  readOnly: boolean;
  onChangeClass: (id: string) => void;
  onToggleFlag: (id: string, flag: "is_locked" | "is_hidden", value: boolean) => void;
  onDelete: (id: string) => void;
  onUpdateAttributes: (id: string, next: Record<string, unknown>) => void;
  rasterMaskStatus?: RasterMaskRecordStatus;
  onRetryRasterMask?: (id: string) => void;
  onEditRasterMask?: (id: string) => void;
  onConvertRegionToRaster?: (id: string) => void;
  onConvertRasterToRegion?: (id: string) => void;
}

/**
 * v0.16.14 · 选中标注浮动信息卡的图片端内容。
 * 由设计系统积木组合:IdentityHeader(类别 + 来源 + 置信度)/ MetricGrid(结构化几何指标)/
 * AttributeForm(属性)/ MetaFooter(折叠次要信息)/ ActionBar(改类 / 隐藏 / 锁定 / 删除)。
 * 复用模型既有回调,不重写逻辑;与右栏 attrDock 共享同一批回调。
 */
export function ImageSelectionCardContent({
  annotation,
  imageWidth,
  imageHeight,
  attributeSchema,
  readOnly,
  onChangeClass,
  onToggleFlag,
  onDelete,
  onUpdateAttributes,
  rasterMaskStatus,
  onRetryRasterMask,
  onEditRasterMask,
  onConvertRegionToRaster,
  onConvertRasterToRegion,
}: ImageSelectionCardContentProps) {
  const locked = !!annotation.is_locked;
  const hidden = !!annotation.is_hidden;
  const metrics = geometryMetrics(annotation.geometry, imageWidth, imageHeight);
  const complexPolygon = isComplexPolygonGeometry(annotation.geometry);
  const rasterMask = annotation.geometry.type === "raster_mask";
  const hasAttributes = !!attributeSchema && (attributeSchema.fields ?? []).length > 0;
  const source = annotationSourceKind(annotation);
  // 置信度仅对 AI 来源(采纳 / 导入)有意义;手动框即便后端落了 conf=1 也不展示 pill。
  const confidence = source === "manual" ? null : annotation.confidence;

  return (
    <div className={BODY_CLASS}>
      <IdentityHeader className={annotation.class_name} source={source} confidence={confidence} />

      <MetricGrid metrics={metrics} />

      {complexPolygon && (
        <p
          role="status"
          className="m-0 rounded-md border border-border bg-muted px-2.5 py-2 text-xs leading-5 text-muted-foreground"
        >
          此标注含内环或多个外环。为避免丢失几何，画布已禁用顶点编辑和整体拖动；仍可选择、改类、编辑属性或删除。
        </p>
      )}

      {rasterMask && (
        <div
          role="status"
          className="m-0 rounded-md border border-border bg-muted px-2.5 py-2 text-xs leading-5 text-muted-foreground"
        >
          {!rasterMaskStatus || rasterMaskStatus.state === "loading" ? (
            <span>Mask 内容加载中…</span>
          ) : rasterMaskStatus.state === "ready" ? (
            <span>
              {rasterMaskStatus.preview ? "受限预览、按原始 RLE 精确命中" : "已按真实像素渲染"} ·{" "}
              {rasterMaskStatus.area} px · {rasterMaskStatus.componentCount} 个组件 ·{" "}
              {rasterMaskStatus.holeCount} 个孔洞 · {rasterMaskStatus.boundaryPixelCount} 边界像素
              {` · AABB (${rasterMaskStatus.bounds.x.toFixed(3)}, ${rasterMaskStatus.bounds.y.toFixed(3)}, ${rasterMaskStatus.bounds.w.toFixed(3)}, ${rasterMaskStatus.bounds.h.toFixed(3)})`}
            </span>
          ) : (
            <span className="flex items-center gap-2">
              <span className="min-w-0 flex-1">
                {rasterMaskStatus.backendReason ?? rasterMaskStatus.reason}：
                {rasterMaskStatus.message}
              </span>
              {rasterMaskStatus.retryable && onRetryRasterMask && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onRetryRasterMask(annotation.id)}
                  aria-label="重试 Mask 内容"
                >
                  重试
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  void navigator.clipboard?.writeText(
                    JSON.stringify({
                      annotationId: annotation.id,
                      reason: rasterMaskStatus.backendReason ?? rasterMaskStatus.reason,
                      message: rasterMaskStatus.message,
                      httpStatus: rasterMaskStatus.httpStatus,
                    }),
                  )
                }
                aria-label="复制 Mask 诊断"
              >
                复制诊断
              </Button>
            </span>
          )}
        </div>
      )}

      {hasAttributes && (
        <div className={ATTR_BLOCK_CLASS} data-floating-panel-no-drag>
          <AttributeForm
            schema={attributeSchema}
            className={annotation.class_name}
            attributes={annotation.attributes ?? {}}
            onChange={(next) => onUpdateAttributes(annotation.id, next)}
            readOnly={readOnly || locked}
            context="image"
            hideHeading
            shortcutRegion
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

      <ActionBar label="标注操作">
        {(annotation.geometry.type === "polygon" || annotation.geometry.type === "multi_polygon") &&
          onConvertRegionToRaster && (
            <Button
              variant="ghost"
              size="sm"
              title="转为 Mask"
              aria-label="转为 Mask"
              disabled={readOnly || locked}
              onClick={() => onConvertRegionToRaster(annotation.id)}
              className="!h-[24px] !rounded-md !px-2 !text-xs"
            >
              转 Mask
            </Button>
          )}
        {rasterMask && onConvertRasterToRegion && (
          <Button
            variant="ghost"
            size="sm"
            title="转为矢量几何"
            aria-label="转为矢量几何"
            disabled={readOnly || locked || rasterMaskStatus?.state !== "ready"}
            onClick={() => onConvertRasterToRegion(annotation.id)}
            className="!h-[24px] !rounded-md !px-2 !text-xs"
          >
            转矢量
          </Button>
        )}
        {rasterMask && onEditRasterMask && (
          <Button
            variant="ghost"
            size="sm"
            title="编辑 Mask"
            aria-label="编辑 Mask"
            disabled={readOnly || locked}
            onClick={() => onEditRasterMask(annotation.id)}
            className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
          >
            <Icon name="edit" size={12} />
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          title="修改类别"
          aria-label="修改类别"
          disabled={readOnly || locked}
          onClick={() => onChangeClass(annotation.id)}
          className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
        >
          <Icon name="tag" size={12} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={hidden ? "显示" : "隐藏"}
          aria-label={hidden ? "显示" : "隐藏"}
          aria-pressed={hidden}
          onClick={() => onToggleFlag(annotation.id, "is_hidden", !hidden)}
          className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
        >
          <Icon name={hidden ? "eyeOff" : "eye"} size={12} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={locked ? "解锁" : "锁定"}
          aria-label={locked ? "解锁" : "锁定"}
          aria-pressed={locked}
          disabled={readOnly}
          onClick={() => onToggleFlag(annotation.id, "is_locked", !locked)}
          className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
        >
          <Icon name={locked ? "lock" : "unlock"} size={12} />
        </Button>
        <Button
          variant="danger"
          size="sm"
          title="删除标注"
          aria-label="删除标注"
          disabled={readOnly || locked}
          onClick={() => onDelete(annotation.id)}
          className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
        >
          <Icon name="trash" size={12} />
        </Button>
      </ActionBar>
    </div>
  );
}
