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

// Prompt-first ToolDock 重构:
//   SAM 单工具拆为 4 个独立工具, 每个声明 requiredPrompt (point/bbox/text/exemplar) 由
//   useMLCapabilities 决定可用性. 旧 "sam" id 移除.
export type ToolId =
  // 选择工具：点选 / 移动已有标注与预标注；默认工具，ESC 回退到它。
  | "select"
  | "box"
  // 旋转框 (OBB).
  | "rotated-box"
  | "hand"
  | "polygon"
  // 折线（开放、不闭合）。
  | "polyline"
  | "canvas"
  | "mask"
  | "smart-point"
  | "smart-box"
  | "smart-scribble"
  | "text-prompt"
  | "exemplar"
  // 复用 SAM bbox prompt → polygon → 紧凑外接矩形 → bbox 标注.
  | "magic-box"
  // 关键点 (COCO 范式: 命名节点 + 骨骼连线).
  | "keypoint";

/** 后端 /setup.supported_prompts 字段对应的 prompt 类型集合.
 *  "bbox" 改名 "interactive_box" (单框单 mask, 统一双 backend); 与后端 supported_prompts 对齐. */
export type RequiredPrompt = "point" | "interactive_box" | "scribble" | "text" | "exemplar";

export interface ToolMeta {
  id: ToolId;
  hotkey: string;
  label: string;
  icon: IconName;
  cursor: "crosshair" | "grab" | "default";
}

/** Drag 初始化负载：仅 stage 空白处按下能产生的几种。 */
export type DragInit =
  | { kind: "draw"; sx: number; sy: number; cx: number; cy: number; fromCenter?: boolean }
  /**
   * SAM 工具拖动负载.
   * mode 由子工具决定, 不再按几何尺寸隐式分流.
   * 松手时 ImageStage 按 mode 将 exemplar 派发给 onSamPrompt。
   */
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
  /**
   * Mask 笔刷拖动负载。lastX/Y 为像素坐标，ImageStage 在 pointermove 中对
   * 相邻两点做线段插值 (步长 = radius/2) 调 paintAt，松手不 commit；commit 由
   * Enter / MaskToolbar 显式触发。
   */
  | { kind: "maskBrush"; lastX: number; lastY: number }
  | { kind: "maskSlice"; start: [number, number]; end: [number, number] }
  /** Mask lasso 草稿使用原图像素坐标，松手后才计算 preview。 */
  | { kind: "maskLasso"; points: [number, number][] }
  /**
   * 旋转框 (OBB) 旋转手柄拖拽。cx/cy 为框中心 (归一化), startAngle 为按下时角度,
   * cur 为拖拽中实时角度 (度数 [0,360) 顺时针)。ImageStage 松手时 commit angle 变更。
   */
  | { kind: "rotateBox"; id: string; cx: number; cy: number; startAngle: number; cur: number };

export interface PolygonDraftHandle {
  points: [number, number][];
  addPoint: (pt: [number, number]) => void;
  /** 结束草稿并提交：polygon 闭合提交 / polyline 直接提交（不闭合）。 */
  close: () => void;
  cancel: () => void;
  /** false 表示折线（不闭合）；缺省 / true 表示多边形。 */
  closed?: boolean;
  beforeInput?: { current: ((event: KeyboardEvent) => boolean) | null };
  boundaryTrace?: {
    getPoints: () => [number, number][];
    append: (points: [number, number][], expected: [number, number][]) => boolean;
    readSource: (id: string, signal: AbortSignal) => Promise<AnnotationResponse | null>;
  };
  /** Image Polygon drag batches share the existing draft and reject a retired snapshot. */
  autoPoints?: {
    getPoints: () => [number, number][];
    append: (points: [number, number][], expected: [number, number][]) => boolean;
    beforeKey: { current: (() => void) | null };
  };
}

/**
 * 关键点草稿句柄。KeypointTool 依次 addPoint，放满 nodeCount 个后 Shell 自动 commit。
 *   nodeCount: 当前类别 keypoint_schema.nodes 的长度 (0 = 未配置 schema → 工具不可落点)。
 */
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
  /** 在已存 Mask 上追加 AI 提示时保留选中，便于服务端按 id + version 解析种子。 */
  preserveSelectionForPrompt?: boolean;
  /** 仅 polygon/polyline 落点消费；返回吸附后的归一化图像坐标。 */
  snapPoint?: (pt: { x: number; y: number }, evt: MouseEvent) => { x: number; y: number };
  /** 仅 PolygonTool 用. */
  polygonDraft?: PolygonDraftHandle;
  startPolygonAutoPoints?: (point: [number, number], event: MouseEvent) => void;
  /** 仅 KeypointTool 用. */
  keypointDraft?: KeypointDraftHandle;
  /** 仅 SmartPointTool 消费, "+/-" 极性 (与 Alt 修饰键合并). */
  samPolarity?: SamPolarity;
  /**
   * 仅 MaskTool 消费. WorkbenchShell 注入 useMaskEditor 返回; MaskTool 在
   * pointerdown 时 beginBlank / paintAt 一次, 返回 maskBrush DragInit.
   */
  maskEditor?: UseMaskEditorReturn;
  /**
   * 当前选中 annotation 的 is_locked; MaskTool 据此 (与 readOnly 一起)
   * 经 canEditMask 判定是否允许落笔, 关闭锁定对象经 pointer 路径修改的绕过。
   */
  annotationLocked?: boolean;
}

/** 画布工具接口。新增 polygon / keypoint 等类型时，实现此接口并注册到 TOOL_REGISTRY。 */
export interface CanvasTool extends ToolMeta {
  /**
   * 声明该工具需要的 backend prompt 能力 key.
   * 缺省 = 非 AI 工具, 永远可用; 否则按 useMLCapabilities.isPromptSupported 决定可用性.
   */
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

/** ToolDock 渲染顺序:
 *   选择工具 → 分隔 → 普通绘制 → 分隔 → AI 工具组 (按 prompt 范式排).
 *   ToolDock 在分组切换处插入分隔; canvas 仅评论批注, 不入栏;
 *   hand 仅供 spacePan / 右键拖拽内部复用 (TOOL_REGISTRY.hand), 不在工具栏展示.
 * text-prompt 文本召回本质属批量能力, 从工具栏 AI 工具组摘除 (只留批量 AI 面板);
 *   TextPromptTool / TOOL_REGISTRY 的 "text-prompt" 条目保留 (类型与历史兼容), 仅不再出现在工具栏。
 */
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

/** 仅 AI 工具子集 (requiredPrompt 非空), 供 hotkey 循环和 InteractiveToolBar 判定. */
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
