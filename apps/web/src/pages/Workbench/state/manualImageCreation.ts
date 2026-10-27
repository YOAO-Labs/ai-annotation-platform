import { useLayoutEffect, useRef } from "react";
import type { AttributeSchema, ToolBindings } from "@/api/projects";
import type { ToolUnitId } from "@/constants/toolUnits";
import type { AnnotationPayload } from "@/api/tasks";
import type { Tool, PendingDrawing } from "./useWorkbenchState";
import { bboxGeom, keypointGeom, polygonGeom, polylineGeom } from "./transforms";
import { classesForUnit } from "./useToolBindings";

export const MANUAL_IMAGE_TOOLS = [
  { tool: "box", unit: "bbox", label: "矩形框" },
  { tool: "rotated-box", unit: "rotated_bbox", label: "旋转框" },
  { tool: "polygon", unit: "region", label: "多边形" },
  { tool: "polyline", unit: "polyline", label: "折线" },
  { tool: "keypoint", unit: "keypoint", label: "关键点" },
] as const;

export type ManualImageTool = (typeof MANUAL_IMAGE_TOOLS)[number]["tool"];
export type ContinuousImageCreation = {
  projectId: string;
  tool: ManualImageTool;
  toolUnitId: ToolUnitId;
  className: string;
};
export type ManualCreationDraft = {
  id: string;
  taskId: string;
  toolUnitId: ToolUnitId;
  className: string;
  attributes: Record<string, unknown>;
  requiredKeys: string[];
  phase: "class" | "attributes" | "saving" | "error";
  error?: string;
};

export function manualImageTool(tool: Tool) {
  return MANUAL_IMAGE_TOOLS.find((item) => item.tool === tool);
}

export function continuousIntentError(
  intent: ContinuousImageCreation,
  bindings: ToolBindings | undefined,
): string | null {
  const definition = manualImageTool(intent.tool);
  if (!definition || definition.unit !== intent.toolUnitId || !bindings?.[definition.unit]?.enabled)
    return "当前工具已停用";
  if (definition.unit === "keypoint" && !bindings.keypoint?.keypoint_schema?.nodes?.length)
    return "关键点工具尚未配置模板";
  if (intent.className && !classesForUnit(bindings, intent.toolUnitId).includes(intent.className))
    return "当前类别已不属于该工具";
  return null;
}

interface ContinuousCreationGuardOptions {
  intent: ContinuousImageCreation | null;
  setIntent: (intent: ContinuousImageCreation | null) => void;
  ownerKey: string | null;
  projectId: string | undefined;
  contextAllowsIntent: boolean;
  businessLocked: boolean;
  mediaBlocked: boolean;
  tool: Tool;
  bindings: ToolBindings | undefined;
  onInvalidBinding: (error: string) => void;
}

/** Media admission pauses writes without retiring a project-scoped creation intent. */
export function useContinuousCreationGuard({
  intent,
  setIntent,
  ownerKey,
  projectId,
  contextAllowsIntent,
  businessLocked,
  mediaBlocked,
  tool,
  bindings,
  onInvalidBinding,
}: ContinuousCreationGuardOptions): boolean {
  const previousOwner = useRef(ownerKey);
  const ownerChanged = previousOwner.current !== ownerKey;
  useLayoutEffect(() => {
    previousOwner.current = ownerKey;
    if (!intent) return;
    if (
      ownerChanged ||
      !ownerKey ||
      !contextAllowsIntent ||
      businessLocked ||
      intent.projectId !== projectId
    ) {
      setIntent(null);
      return;
    }
    const error = bindings ? continuousIntentError(intent, bindings) : null;
    if (error) {
      setIntent(null);
      onInvalidBinding(error);
      return;
    }
    const next = manualImageTool(tool);
    if (!next) setIntent(null);
    else if (next.unit !== intent.toolUnitId)
      setIntent({
        projectId: intent.projectId,
        tool: next.tool,
        toolUnitId: next.unit,
        className: "",
      });
  }, [
    intent,
    setIntent,
    ownerKey,
    ownerChanged,
    projectId,
    contextAllowsIntent,
    businessLocked,
    tool,
    bindings,
    onInvalidBinding,
  ]);
  // Fence the changing owner's render before the layout effect clears its old intent.
  return businessLocked || mediaBlocked || (ownerChanged && !!intent) || !ownerKey;
}

/** Every object starts with its own defaults, including false and zero. */
export function creationAttributeDefaults(schema: AttributeSchema, className: string) {
  const values: Record<string, unknown> = {};
  for (const field of schema.fields ?? []) {
    if (field.applies_to && field.applies_to !== "*" && !field.applies_to.includes(className))
      continue;
    if (field.default !== undefined) values[field.key] = structuredClone(field.default);
  }
  return values;
}

export function manualDrawingPayload(
  drawing: NonNullable<PendingDrawing>,
  draft: ManualCreationDraft,
): AnnotationPayload {
  const { geom } = drawing;
  const base = {
    tool_unit_id: draft.toolUnitId,
    class_name: draft.className,
    attributes: draft.attributes,
    confidence: 1,
  };
  switch (drawing.kind) {
    case "polygon":
      return { ...base, annotation_type: "polygon", geometry: polygonGeom(drawing.points) };
    case "polyline":
      return { ...base, annotation_type: "polyline", geometry: polylineGeom(drawing.points) };
    case "keypoint":
      return { ...base, annotation_type: "keypoint", geometry: keypointGeom(drawing.points) };
    case "rotated_bbox":
      return {
        ...base,
        annotation_type: "rotated_bbox",
        geometry: {
          type: "rotated_bbox",
          cx: geom.x + geom.w / 2,
          cy: geom.y + geom.h / 2,
          w: geom.w,
          h: geom.h,
          angle: 0,
        },
      };
    default:
      return { ...base, annotation_type: "bbox", geometry: bboxGeom(geom) };
  }
}
