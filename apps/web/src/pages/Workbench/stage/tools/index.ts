import type { Viewport } from "../../state/useViewportTransform";
import type { SamPolarity } from "../../state/useWorkbenchState";
import type { UseMaskEditorReturn } from "../../state/useMaskEditor";
import { SelectTool } from "./SelectTool";
import { BboxTool } from "./BboxTool";
import { RotatedBboxTool } from "./RotatedBboxTool";
import { HandTool } from "./HandTool";
import { PolygonTool } from "./PolygonTool";
import { PolylineTool } from "./PolylineTool";
import { CanvasTool } from "./CanvasTool";
import { SmartPointTool } from "./SmartPointTool";
import { SmartBoxTool } from "./SmartBoxTool";
import { SmartScribbleTool } from "./SmartScribbleTool";
import { TextPromptTool } from "./TextPromptTool";
import { ExemplarTool } from "./ExemplarTool";
import { MaskTool } from "./MaskTool";
import { MagicBoxTool } from "./MagicBoxTool";
import { KeypointTool } from "./KeypointTool";
import type { IconName } from "@/components/ui/Icon";
import type { AnnotationResponse, Keypoint } from "@/types";
import type { BboxCreationMode } from "../ImageStage.helpers";

export type ToolId =
  | "select"
  | "box"
  | "rotated-box"
  | "hand"
  | "polygon"
  | "polyline"
  | "canvas"
  | "mask"
  | "smart-point"
  | "smart-box"
  | "smart-scribble"
  | "text-prompt"
  | "exemplar"
  | "magic-box"
  | "keypoint";

export type RequiredPrompt = "point" | "interactive_box" | "scribble" | "text" | "exemplar";

export interface ToolMeta {
  id: ToolId;
  hotkey: string;
  label: string;
  icon: IconName;
  cursor: "crosshair" | "grab" | "default";
}

export type DragInit =
  | { kind: "draw"; sx: number; sy: number; cx: number; cy: number; fromCenter?: boolean }
  | {
      kind: "samProbe";
      mode: "point" | "bbox" | "exemplar";
      sx: number;
      sy: number;
      cx: number;
      cy: number;
      alt: boolean;
    }
  | { kind: "samScribble"; points: [number, number][]; alt: boolean }
  | { kind: "pan"; sx: number; sy: number }
  | { kind: "canvasStroke"; points: number[] }
  | { kind: "maskBrush"; lastX: number; lastY: number }
  | { kind: "maskSlice"; start: [number, number]; end: [number, number] }
  | { kind: "maskLasso"; points: [number, number][] }
  | { kind: "rotateBox"; id: string; cx: number; cy: number; startAngle: number; cur: number };

export interface PolygonDraftHandle {
  points: [number, number][];
  addPoint: (pt: [number, number]) => void;
  close: () => void;
  cancel: () => void;
  closed?: boolean;
  beforeInput?: { current: ((event: KeyboardEvent) => boolean) | null };
  boundaryTrace?: {
    getPoints: () => [number, number][];
    append: (points: [number, number][], expected: [number, number][]) => boolean;
    readSource: (id: string, signal: AbortSignal) => Promise<AnnotationResponse | null>;
  };
  autoPoints?: {
    getPoints: () => [number, number][];
    append: (points: [number, number][], expected: [number, number][]) => boolean;
    beforeKey: { current: (() => void) | null };
  };
}

export interface KeypointDraftHandle {
  points: Keypoint[];
  nodeCount: number;
  addPoint: (kp: Keypoint) => void;
  cancel: () => void;
}

export interface ToolPointerContext {
  pt: { x: number; y: number };
  evt: MouseEvent;
  vp: Viewport;
  activeClass: string;
  imgW: number;
  imgH: number;
  spacePan: boolean;
  readOnly: boolean;
  pendingDrawing: boolean;
  bboxCreationMode?: BboxCreationMode;
  onClearSelection: () => void;
  preserveSelectionForPrompt?: boolean;
  snapPoint?: (pt: { x: number; y: number }, evt: MouseEvent) => { x: number; y: number };
  polygonDraft?: PolygonDraftHandle;
  startPolygonAutoPoints?: (point: [number, number], event: MouseEvent) => void;
  keypointDraft?: KeypointDraftHandle;
  samPolarity?: SamPolarity;
  maskEditor?: UseMaskEditorReturn;
  annotationLocked?: boolean;
}

export interface CanvasTool extends ToolMeta {
  requiredPrompt?: RequiredPrompt;
  onPointerDown?: (ctx: ToolPointerContext) => DragInit | null;
}

export const TOOL_REGISTRY: Record<ToolId, CanvasTool> = {
  select: SelectTool,
  box: BboxTool,
  "rotated-box": RotatedBboxTool,
  hand: HandTool,
  polygon: PolygonTool,
  polyline: PolylineTool,
  canvas: CanvasTool,
  mask: MaskTool,
  "smart-point": SmartPointTool,
  "smart-box": SmartBoxTool,
  "smart-scribble": SmartScribbleTool,
  "text-prompt": TextPromptTool,
  exemplar: ExemplarTool,
  "magic-box": MagicBoxTool,
  keypoint: KeypointTool,
};

export const ALL_TOOLS: CanvasTool[] = [
  SelectTool,
  BboxTool,
  RotatedBboxTool,
  PolygonTool,
  PolylineTool,
  KeypointTool,
  MaskTool,
  SmartPointTool,
  SmartBoxTool,
  SmartScribbleTool,
  ExemplarTool,
  MagicBoxTool,
];

export const AI_TOOLS: CanvasTool[] = ALL_TOOLS.filter((t) => !!t.requiredPrompt);
export const AI_TOOL_IDS: ToolId[] = AI_TOOLS.map((t) => t.id);

export function isAIToolId(id: ToolId): boolean {
  return AI_TOOL_IDS.includes(id);
}

export {
  SelectTool,
  BboxTool,
  RotatedBboxTool,
  HandTool,
  PolygonTool,
  PolylineTool,
  CanvasTool,
  MaskTool,
  SmartPointTool,
  SmartBoxTool,
  SmartScribbleTool,
  TextPromptTool,
  ExemplarTool,
  KeypointTool,
};
