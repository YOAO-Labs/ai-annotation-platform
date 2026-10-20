import { CopyableError } from "@/components/ui/CopyableError";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, Clock3, Info, Loader2, MousePointer2, Type, X } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/shadcn/ui/badge";
import { Input } from "@/components/shadcn/ui/input";
import { Progress } from "@/components/shadcn/ui/progress";
import type { VideoTrackerDirection, VideoTrackerPropagatePayload } from "@/api/videoTracker";
import { cn } from "@/lib/utils";
import { readDialogMemory, writeDialogMemory } from "../state/videoDialogMemory";
import {
  AI_PANEL_HEADER_CLASS,
  AI_PANEL_ICON_CLASS,
  AI_PANEL_SECTION_CLASS,
  AI_PANEL_SURFACE_CLASS,
} from "../shell/workbenchAiPanelChrome";

const SPAN_PRESETS = ["10", "30", "60"] as const;
const RANGE_PRESETS = [...SPAN_PRESETS, "next-keyframe", "end"] as const;

const TRACKER_SELECT_CLASS =
  "h-8 w-full cursor-pointer appearance-none rounded-md border border-input bg-background px-2.5 text-xs text-foreground shadow-xs outline-none transition-[border-color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30";
const TRACKER_FIELD_LABEL_CLASS = "text-xs font-medium leading-none text-foreground";
const TRACKER_FIELD_HELP_CLASS = "text-2xs leading-relaxed text-muted-foreground";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

type RangePresetValue = (typeof RANGE_PRESETS)[number];
type TrackerDialogMemory = {
  rangePreset: RangePresetValue;
  direction: VideoTrackerDirection;
  modelKey: string;
  samVariant: string;
};

// 采样开启 (step>1) 时数字预设以网格格子为单位, 标签显示如「10 格 (≈100 帧)」;
// 关闭时维持「N 帧」。next-keyframe / end 与单位无关, 标签固定。
function presetLabel(value: RangePresetValue, step: number): string {
  if (value === "next-keyframe") return "到下一关键帧";
  if (value === "end") return "到结尾";
  const n = Number(value);
  return step > 1 ? `${n} 格 (≈${n * step} 帧)` : `${n} 帧`;
}

// v0.21.27 · U-pvs-3 · U3: label 用人类可读名 + 能力一句话 (原为裸 key, 对标注员无意义)。
// value 保持不变 (提交/记忆/测试均按 value)。
export const TRACKER_MODEL_OPTIONS: Array<{ value: string; label: string; note?: string }> = [
  { value: "mock_bbox", label: "mock · 测试框", note: "测试用 (不依赖 ML backend)" },
  { value: "sam2_video", label: "SAM2 · 框追踪", note: "种子框跨帧追踪" },
  { value: "sam3_video", label: "SAM3 · 文本检测追踪", note: "按文本每帧检测" },
  // v0.21.26 · 阶段 B-pvs · SAM3 交互追踪 (点/框 seed + memory 跨帧, 非文本驱动)。与
  // sam2_video 同为种子传播 (不显 text 框); 需绑定声明该 tracker 的 sam3 backend。
  {
    value: "sam3_video_interactive",
    label: "SAM3 · 点框交互追踪",
    note: "点/框种子 + memory 跨帧",
  },
  // v0.22.2 · B-combo · 发现追踪: multiplex 按文本发现目标 → 逐对象 PVS memory 追踪
  // (兼得自动发现与干净身份)。发现对象全新建, 需目标类别; 需 sam3 backend 同时声明
  // sam3_video 与 sam3_video_interactive。
  {
    value: "sam3_video_combo",
    label: "SAM3 · 发现追踪 (combo)",
    note: "按文本发现目标 + 逐对象 memory 追踪",
  },
];

// v0.10.36: SAM 模型尺寸 (tracker 不用 DINO, 只选 SAM 尺寸)。空 = 默认/tiny。
const SAM_VARIANTS: Array<{ value: string; label: string }> = [
  { value: "", label: "默认 (tiny)" },
  { value: "tiny", label: "tiny" },
  { value: "small", label: "small" },
  { value: "base_plus", label: "base_plus" },
  { value: "large", label: "large" },
];

// v0.21.27 · U-pvs-3 · U1: 方向标签消歧。forward/backward 用「更晚/更早帧」+ 箭头,
// 避免「向后追踪」被误读成倒放; title 补全语义。
const DIRECTION_META: Record<VideoTrackerDirection, { label: string; title: string }> = {
  forward: { label: "更晚帧 →", title: "向后追踪: 追踪到更晚 (时间轴更右) 的帧" },
  backward: { label: "← 更早帧", title: "向前追踪: 追踪到更早 (时间轴更左) 的帧" },
  bidirectional: { label: "⇆ 双向", title: "双向: 同时向更早与更晚帧追踪" },
};

// v0.21.27 · U-pvs-3 · U6: 分窗窗口数粗估。窗口尺寸镜像后端默认 (sam3 系 16 / 其余 300,
// 见 VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES / VIDEO_TRACKER_WINDOW_SIZE_FRAMES); 后端 env
// 可覆盖, 故仅作「会分多窗」的粗略提示, 不做精确耗时。
function estimateWindowCount(frameSpan: number, modelKey: string): number {
  const windowSize = modelKey.startsWith("sam3") ? 16 : 300;
  const frames = Math.max(1, frameSpan + 1);
  return Math.max(1, Math.ceil(frames / windowSize));
}

// v0.21.27 · 支持画布点/框种子 + 多目标 + 纠偏的 tracker: SAM3 PVS 交互, 及阶段 A 起的
// sam2_video(grounded-sam2 wrapper 解除单 obj 硬编码后同样吃 seeds[])。无种子则退选中轨迹框。
function supportsSeedCapture(modelKey: string): boolean {
  return modelKey === "sam3_video_interactive" || modelKey === "sam2_video";
}

// v0.22.2 · SAM 尺寸档位 (tiny/small/base_plus/large) 是 SAM2 checkpoint 概念, 只有
// sam2_video 消费; sam3 系 (multiplex / PVS / combo) 用各自 sam3 权重、忽略该变体, 故不显
// 尺寸选择器 (显示会误导用户以为能调 sam3 模型大小)。
function usesSamVariant(modelKey: string): boolean {
  return modelKey === "sam2_video";
}

// v0.22.2 · mock_bbox「测试框」只是无后端时验证流程的开发/测试兜底, 不是给真实用户的模型。
// 绑了真实后端 (preferNonMockModel) 时一直会过滤掉它; 现进一步在生产构建 (非 import.meta.env.DEV)
// 里彻底不出现 —— 即便项目没绑后端, 生产 UI 也不再露出 mock。dev 构建保留它便于本地无 GPU 验证。
export function visibleTrackerModelOptions(
  preferNonMockModel: boolean,
  isDev: boolean,
  supportedTrackers?: string[],
): typeof TRACKER_MODEL_OPTIONS {
  const visible =
    preferNonMockModel || !isDev
      ? TRACKER_MODEL_OPTIONS.filter((m) => m.value !== "mock_bbox")
      : TRACKER_MODEL_OPTIONS;
  if (supportedTrackers === undefined) return visible;
  const supported = new Set(supportedTrackers);
  return visible.filter(
    (model) =>
      model.value === "mock_bbox" ||
      supported.has(model.value) ||
      (model.value === "sam3_video_combo" &&
        supported.has("sam3_video") &&
        supported.has("sam3_video_interactive")),
  );
}

const DEFAULT_TRACKER_MEMORY: TrackerDialogMemory = {
  rangePreset: "30",
  direction: "forward",
  modelKey: "mock_bbox",
  samVariant: "",
};

export function resolveTrackerDefaultModel({
  projectDefaultModel,
  rememberedModel,
  preferNonMockModel,
  options = TRACKER_MODEL_OPTIONS,
}: {
  projectDefaultModel?: string | null;
  rememberedModel?: string | null;
  preferNonMockModel: boolean;
  options?: Array<{ value: string }>;
}): string {
  const values = new Set(options.map((model) => model.value));
  if (projectDefaultModel && values.has(projectDefaultModel)) return projectDefaultModel;
  if (rememberedModel && values.has(rememberedModel)) return rememberedModel;
  if (preferNonMockModel) {
    const realModel = options.find((model) => model.value !== "mock_bbox");
    if (realModel) return realModel.value;
  }
  return values.has("mock_bbox") ? "mock_bbox" : (options[0]?.value ?? "");
}

function hasOption<T extends string>(options: readonly T[], value: unknown): value is T {
  return typeof value === "string" && options.includes(value as T);
}

function validateTrackerMemory(value: unknown): TrackerDialogMemory | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<TrackerDialogMemory>;
  const modelValues = TRACKER_MODEL_OPTIONS.map((m) => m.value);
  const variantValues = SAM_VARIANTS.map((v) => v.value);
  return {
    rangePreset: hasOption(RANGE_PRESETS, raw.rangePreset)
      ? raw.rangePreset
      : DEFAULT_TRACKER_MEMORY.rangePreset,
    direction:
      raw.direction === "forward" ||
      raw.direction === "backward" ||
      raw.direction === "bidirectional"
        ? raw.direction
        : DEFAULT_TRACKER_MEMORY.direction,
    modelKey:
      typeof raw.modelKey === "string" && modelValues.includes(raw.modelKey)
        ? raw.modelKey
        : DEFAULT_TRACKER_MEMORY.modelKey,
    samVariant:
      typeof raw.samVariant === "string" && variantValues.includes(raw.samVariant)
        ? raw.samVariant
        : DEFAULT_TRACKER_MEMORY.samVariant,
  };
}

export interface TrackerSeedTargetSummary {
  targetId: number;
  pointCount: number;
  boxCount: number;
  frames: number[];
}

interface VideoTrackerPropagateDialogProps {
  open: boolean;
  visible?: boolean;
  frameIndex: number;
  minFrame?: number;
  maxFrame: number;
  nextKeyframeAfter: number | null;
  /** 向前(backward)传播的「到上一关键帧」目标; 缺省 null 时回退到固定跨度。 */
  prevKeyframeBefore?: number | null;
  userId?: string | null;
  /** 采样网格步长 (源帧). >1 时数字预设以网格格子为单位; 缺省 1 = 现状 (按源帧). */
  samplingStep?: number;
  projectDefaultModel?: string | null;
  preferNonMockModel?: boolean;
  /** 当前项目已启用、已连接且能力可达的 tracker。 */
  supportedTrackers?: string[];
  /** v0.21.19 · text-driven tracker 子集; 选中其中之一时显 text 框。缺省时 sam3_video 静态视为 text-driven。 */
  textDrivenTrackers?: string[];
  /** 可执行模型 → 提供该能力的已连接项目后端名称。 */
  trackerModelProviders?: Record<string, string[]>;
  /** polyline 轨迹追踪暂不支持 (后端只识别 polygon/bbox track); 为真时灰置传播动作。 */
  isPolylineTrack?: boolean;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (payload: VideoTrackerPropagatePayload) => Promise<void>;
  /** Publish the preview only while visible; retain range settings when hidden. */
  onRangeChange?: (range: { startFrame: number; endFrame: number } | null) => void;
  /** v0.21.14 · 时间轴 Shift+拖刷选回填的范围 (覆盖预设/方向派生的范围); 每次刷选传新对象。 */
  brushedRange?: { startFrame: number; endFrame: number } | null;
  /** v0.21.27 · U-pvs-1 · PVS 点种子采集 (仅 sam3_video_interactive)。开启后画布点击落归一化种子点。 */
  seedCollecting?: boolean;
  /** 已落种子点数。 */
  seedPointCount?: number;
  /** 切换「落点选目标」采集态 (进入时画布切 smart-point、退出复原)。 */
  onToggleSeedCollecting?: () => void;
  /** 清空已落种子点。 */
  onClearSeeds?: () => void;
  /** 新目标: 当前目标已落点后, 后续点归入下一目标 (各成一条轨迹)。 */
  onNewSeedTarget?: () => void;
  /** 按目标聚合的种子摘要，用于展示每个目标的点、框和涉及帧。 */
  seedTargets?: TrackerSeedTargetSummary[];
  /** 当前继续接收新种子的目标编号。 */
  activeSeedTargetId?: number;
  /** v0.21.27 · 框修正 · 采集模式: point 落点 / box 画修正框。 */
  seedMode?: "point" | "box";
  /** 切换采集模式 (采集中即时切 smart-point ↔ smart-box)。 */
  onChangeSeedMode?: (mode: "point" | "box") => void;
  /** 已落框种子数 (框修正)。 */
  seedBoxCount?: number;
  /** v0.22.1 · A2/A3 · 选中源轨迹类别 (用于「本次影响」摘要 + 文本检测类别继承警示); 无源时 null。 */
  sourceTrackClassName?: string | null;
  /** v0.22.1 · B · 无源检测模式 (画布级入口, 无选中轨迹): 显示目标类别选择器, 提交带 target_class_name。 */
  sourceless?: boolean;
  /** v0.22.1 · B · 无源时可选的目标类别 (项目 classes)。 */
  availableClasses?: string[];
  /** v0.22.2 · M2 · 多选批量: 一次延展的源轨迹条数 (≥2 时摘要转「延展 N 条轨迹」)。缺省 = 单源。 */
  sourceCount?: number;
  /** v0.22.2 · M2 · 多选批量源的去重类别 (单类展示「XX」, 混类展示「N 类」)。 */
  sourceClassNames?: string[];
  /**
   * v0.22.2 · U8 · 提交后就地进行态。true 时对话框不再显表单, 而是在原位转成
   * 「追踪中…」轻量进度视图 (让位审阅条前给即时反馈); 由上层在追踪 job 建成后置真、
   * 结果就绪 / 失败时关闭对话框复位。
   */
  tracking?: boolean;
  /** v0.22.2 · U8 · 追踪分窗进度 (WS 回报; 未开始为 null)。tracking 为真时显示「第 c/t 窗」。 */
  trackingWindow?: { current: number; total: number } | null;
}

export function VideoTrackerPropagateDialog({
  open,
  visible = true,
  frameIndex,
  minFrame = 0,
  maxFrame,
  nextKeyframeAfter,
  prevKeyframeBefore = null,
  userId,
  samplingStep = 1,
  projectDefaultModel = null,
  preferNonMockModel = false,
  supportedTrackers,
  textDrivenTrackers,
  trackerModelProviders = {},
  isPolylineTrack = false,
  submitting,
  onCancel,
  onSubmit,
  onRangeChange,
  brushedRange = null,
  seedCollecting = false,
  seedPointCount = 0,
  onToggleSeedCollecting,
  onClearSeeds,
  onNewSeedTarget,
  seedTargets = [],
  activeSeedTargetId = 1,
  seedMode = "point",
  onChangeSeedMode,
  seedBoxCount = 0,
  sourceTrackClassName = null,
  sourceless = false,
  availableClasses = [],
  sourceCount = 1,
  sourceClassNames = [],
  tracking = false,
  trackingWindow = null,
}: VideoTrackerPropagateDialogProps) {
  const initializedOpen = useRef(false);
  const [direction, setDirection] = useState<VideoTrackerDirection>("forward");
  const [rangePreset, setRangePreset] = useState<RangePresetValue>("30");
  const [modelKey, setModelKey] = useState<string>("mock_bbox");
  // v0.10.36: SAM 模型尺寸; 空 = 默认 (tiny)。
  const [samVariant, setSamVariant] = useState<string>("");
  const [outputGeometry, setOutputGeometry] = useState<"" | "bbox" | "polygon" | "mask">("");
  // v0.21.19: text-driven 追踪 (sam3_video) 的文本 query; 每次打开重置 (非持久化)。
  const [text, setText] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  // v0.21.14 · 时间轴 Shift+拖刷选回填的自定义范围; 非 null 时覆盖预设/方向派生的范围。
  // 改预设 / 方向即清空 (回到派生范围)。
  const [customRange, setCustomRange] = useState<{ from: number; to: number } | null>(null);
  // v0.22.1 · B · 无源检测的目标类别 (新建轨迹用); 每次打开重置为首个可选类别。
  const [targetClass, setTargetClass] = useState<string>("");

  // v0.21.14 · 绑真实后端时过滤掉测试用 mock_bbox (避免误选跑出假框); v0.22.2 · 生产构建
  // 里进一步彻底隐藏 mock (即便没绑后端)。过滤后的表也用于默认模型解析, 使记忆里残留的
  // mock_bbox 不再复现 (不在候选 → 回退到首个真实模型)。
  const modelOptions = useMemo(() => {
    const available = visibleTrackerModelOptions(
      preferNonMockModel,
      import.meta.env.DEV,
      supportedTrackers,
    );
    if (sourceless) return available;
    return available.filter((model) => model.value !== "sam3_video_combo");
  }, [preferNonMockModel, sourceless, supportedTrackers]);

  // text-driven 判定用于显示目标描述；可执行性已由 modelOptions 按项目后端能力过滤。
  const isTextDrivenModel = useMemo(() => {
    const set = new Set(textDrivenTrackers ?? []);
    // combo 也文本驱动 (发现趟按 text 检测), 显 text 框。
    return (value: string) =>
      set.has(value) || value === "sam3_video" || value === "sam3_video_combo";
  }, [textDrivenTrackers]);
  const textDrivenActive = isTextDrivenModel(modelKey);
  const selectedModelDisabled = !modelOptions.some((model) => model.value === modelKey);
  // v0.22.2 · B-combo · combo 发现对象全新建, 与无源检测同样需目标类别 —— 无论从画布级
  // 无源入口还是选中源入口打开, 选 combo 都按无源处理 (显类别选择器 + payload 带 target)。
  const sourcelessLike = sourceless || modelKey === "sam3_video_combo";
  const operationScope = sourceCount >= 2 ? "batch" : sourceless ? "canvas" : "single";
  const scopeTitle =
    operationScope === "canvas"
      ? "发现新目标"
      : operationScope === "batch"
        ? `批量延展 ${sourceCount} 条轨迹`
        : "延展当前轨迹";
  const scopeDescription =
    operationScope === "canvas"
      ? "画布级操作：发现或播种多个新目标，不修改当前选中轨迹。"
      : operationScope === "batch"
        ? "只延展已选轨迹，结果分别回填原轨迹。"
        : `只延展当前选中轨迹${sourceTrackClassName ? `「${sourceTrackClassName}」` : ""}。`;
  const submitLabel =
    operationScope === "canvas"
      ? "开始发现"
      : operationScope === "batch"
        ? "开始批量延展"
        : "开始延展";
  const progressLabel =
    operationScope === "canvas"
      ? "正在发现目标…"
      : operationScope === "batch"
        ? "正在批量延展…"
        : "正在延展轨迹…";

  useEffect(() => {
    if (!open) {
      initializedOpen.current = false;
      return;
    }
    if (initializedOpen.current || modelOptions.length === 0) return;
    initializedOpen.current = true;
    const remembered =
      readDialogMemory(userId, "trackerPropagate", validateTrackerMemory) ?? DEFAULT_TRACKER_MEMORY;
    setDirection(remembered.direction);
    setRangePreset(remembered.rangePreset);
    setModelKey(
      resolveTrackerDefaultModel({
        projectDefaultModel,
        rememberedModel: remembered.modelKey,
        preferNonMockModel,
        options: modelOptions,
      }),
    );
    setSamVariant(remembered.samVariant);
    setText("");
    setError(null);
    setCustomRange(null);
  }, [open, preferNonMockModel, projectDefaultModel, userId, modelOptions]);

  // v0.22.1 · B · 无源目标类别重置独立成 effect (仅依赖 open); 不把不稳定的 availableClasses
  // 引用混进主 open effect 依赖, 否则每次 render 重跑主 effect 会重置方向 / 范围。
  useEffect(() => {
    if (open) setTargetClass(availableClasses[0] ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 时间轴 Shift+拖刷选 → 覆盖为自定义范围 (每次刷选传新对象, 故按引用触发)。
  useEffect(() => {
    if (!open || !brushedRange) return;
    setCustomRange({ from: brushedRange.startFrame, to: brushedRange.endFrame });
  }, [open, brushedRange]);

  const grid = Math.max(1, Math.round(samplingStep));

  const derivedRange = useMemo(() => {
    if (rangePreset === "next-keyframe") {
      // 向前(backward): 传播到上一关键帧; 无上一关键帧时回退固定跨度。其余方向: 到下一关键帧。
      if (direction === "backward") {
        if (prevKeyframeBefore !== null && prevKeyframeBefore < frameIndex) {
          return { from: prevKeyframeBefore, to: frameIndex };
        }
        return { from: Math.max(minFrame, frameIndex - 30), to: frameIndex };
      }
      if (nextKeyframeAfter !== null && nextKeyframeAfter > frameIndex) {
        return { from: frameIndex, to: nextKeyframeAfter };
      }
      return { from: frameIndex, to: Math.min(maxFrame, frameIndex + 30) };
    }
    if (rangePreset === "end") {
      return direction === "backward"
        ? { from: minFrame, to: frameIndex }
        : { from: frameIndex, to: maxFrame };
    }
    // 数字预设是网格格子数; 采样开启时换算成源帧跨度 (span * grid)。
    const span = Number(rangePreset) * grid;
    if (direction === "backward") {
      return { from: Math.max(minFrame, frameIndex - span), to: frameIndex };
    }
    if (direction === "bidirectional") {
      return {
        from: Math.max(minFrame, frameIndex - span),
        to: Math.min(maxFrame, frameIndex + span),
      };
    }
    return { from: frameIndex, to: Math.min(maxFrame, frameIndex + span) };
  }, [
    direction,
    frameIndex,
    grid,
    maxFrame,
    minFrame,
    nextKeyframeAfter,
    prevKeyframeBefore,
    rangePreset,
  ]);

  // 自定义范围 (来自时间轴刷选) 优先; 否则用预设/方向派生的范围。
  const range = customRange
    ? {
        from: clamp(customRange.from, minFrame, maxFrame),
        to: clamp(customRange.to, minFrame, maxFrame),
      }
    : derivedRange;

  // v0.21.27 · U-pvs-3 · U6: 当前范围粗估窗口数 (>1 时提示大范围将分多窗处理)。
  const estimatedWindows = estimateWindowCount(range.to - range.from, modelKey);

  // 「+新目标」后的目标在首个种子落下前还不在聚合数据中；先显示一行空态，
  // 让用户始终知道接下来的点 / 框会归到哪个目标。
  const displayedSeedTargets = useMemo(() => {
    const targets = [...seedTargets].sort((a, b) => a.targetId - b.targetId);
    if (
      seedPointCount + seedBoxCount > 0 &&
      !targets.some((target) => target.targetId === activeSeedTargetId)
    ) {
      targets.push({
        targetId: activeSeedTargetId,
        pointCount: 0,
        boxCount: 0,
        frames: [],
      });
      targets.sort((a, b) => a.targetId - b.targetId);
    }
    return targets;
  }, [activeSeedTargetId, seedBoxCount, seedPointCount, seedTargets]);

  // A retained background panel owns its settings, but must not paint a timeline preview.
  useEffect(() => {
    if (!open || !visible) {
      onRangeChange?.(null);
      return;
    }
    onRangeChange?.({ startFrame: range.from, endFrame: range.to });
    return () => onRangeChange?.(null);
  }, [open, visible, range.from, range.to, onRangeChange]);

  // v0.21.14 · 浮层化后无遮罩, 用 Esc 关闭 (替代原点击遮罩关闭)。
  useEffect(() => {
    if (!open || !visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel, visible]);

  if (!open) return null;

  const handleSubmit = async () => {
    if (isPolylineTrack) {
      setError("polyline 轨迹追踪暂不支持");
      return;
    }
    if (range.from > range.to) {
      setError("起止帧无效");
      return;
    }
    if (selectedModelDisabled) {
      setError("当前项目没有可执行的追踪模型");
      return;
    }
    // v0.21.19 · text-driven tracker 必须有文本描述 (否则每帧无检测依据)。
    const trimmedText = text.trim();
    if (textDrivenActive && !trimmedText) {
      setError("文本驱动追踪需填写文本描述");
      return;
    }
    if (sourceless && supportsSeedCapture(modelKey) && seedPointCount + seedBoxCount === 0) {
      setError("画布级点框追踪需先在画布添加点或框种子");
      return;
    }
    // v0.22.1 · B · 无源检测必须选目标类别 (新建轨迹归属); combo 发现同理。
    if (sourcelessLike && !availableClasses.includes(targetClass)) {
      setError("请选择新目标的类别");
      return;
    }
    try {
      await onSubmit({
        from_frame: range.from,
        to_frame: range.to,
        model_key: modelKey,
        direction,
        // sam3 系忽略 SAM2 尺寸档位 —— 只对 sam2_video 透传, 避免把残留档位发给 sam3。
        sam_variant: usesSamVariant(modelKey) ? samVariant || undefined : undefined,
        text: textDrivenActive ? trimmedText : undefined,
        output_geometry: outputGeometry || undefined,
        target_class_name: sourcelessLike ? targetClass : undefined,
        target_tool_unit_id: sourcelessLike ? "bbox" : undefined,
      });
      writeDialogMemory(userId, "trackerPropagate", {
        rangePreset,
        direction,
        modelKey,
        samVariant,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "提交失败");
    }
  };

  const activeModelOption = modelOptions.find((option) => option.value === modelKey);
  const trackingProgressValue =
    trackingWindow && trackingWindow.total > 0
      ? Math.min(100, Math.round((trackingWindow.current / trackingWindow.total) * 100))
      : null;

  return (
    <div
      role="dialog"
      aria-label={scopeTitle}
      data-testid="video-tracker-propagate-dialog"
      className={cn(
        AI_PANEL_SURFACE_CLASS,
        "@container/tracker-panel flex h-full min-h-0 flex-col rounded-none border-0 text-card-foreground",
      )}
    >
      {tracking ? (
        <div className="flex flex-col" role="status" aria-live="polite">
          <div
            data-testid="tracker-panel-header"
            className={cn(AI_PANEL_HEADER_CLASS, "flex items-start justify-between gap-3")}
          >
            <div className="flex min-w-0 items-start gap-2">
              <span className={AI_PANEL_ICON_CLASS}>
                <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" />
              </span>
              <div className="flex min-w-0 flex-col gap-1">
                <div
                  data-testid="tracker-progress"
                  className="flex flex-wrap items-baseline gap-x-2 gap-y-1"
                >
                  <h2 className="text-sm font-semibold tracking-tight">{progressLabel}</h2>
                  {trackingWindow && trackingWindow.total > 1 && (
                    <span
                      data-testid="tracker-progress-window"
                      className="mono text-xs text-muted-foreground"
                    >
                      第 {trackingWindow.current}/{trackingWindow.total} 窗
                    </span>
                  )}
                </div>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  正在处理 F{range.from} 到 F{range.to}，结果将先进入候选审阅。
                </p>
              </div>
            </div>
            <Button
              variant="ghost"
              size="xs"
              onClick={onCancel}
              aria-label={`关闭${scopeTitle}进度`}
            >
              <X data-icon="inline-start" />
            </Button>
          </div>

          {trackingProgressValue !== null && trackingWindow && trackingWindow.total > 1 && (
            <div className={cn(AI_PANEL_SECTION_CLASS, "flex flex-col gap-2")}>
              <Progress
                value={trackingProgressValue}
                aria-label={`${scopeTitle}进度 ${trackingProgressValue}%`}
                className="h-1.5"
              />
            </div>
          )}

          <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
            <span className="text-2xs leading-relaxed text-muted-foreground">
              关闭面板不会中止后台任务。
            </span>
            <Button variant="ghost" size="sm" onClick={onCancel}>
              后台继续
            </Button>
          </div>
        </div>
      ) : (
        <>
          <header
            data-testid="tracker-panel-header"
            className={cn(
              AI_PANEL_HEADER_CLASS,
              "flex flex-wrap items-start justify-between gap-3",
            )}
          >
            <div className="flex min-w-0 flex-1 items-start gap-2">
              <span className={AI_PANEL_ICON_CLASS}>
                <Bot className="size-3.5" />
              </span>
              <div className="flex min-w-0 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold tracking-tight">{scopeTitle}</h2>
                  <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-2xs text-muted-foreground">
                    Ctrl+B
                  </kbd>
                </div>
              </div>
            </div>
            <Button variant="ghost" size="xs" onClick={onCancel} aria-label={`关闭${scopeTitle}`}>
              <X data-icon="inline-start" />
            </Button>
            <p className="w-full text-xs leading-relaxed text-muted-foreground">
              {scopeDescription}结果先进入候选审阅。
            </p>
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <section
              data-testid="tracker-scope-summary"
              className={cn(AI_PANEL_SECTION_CLASS, "flex items-start gap-2.5 bg-muted/40")}
            >
              <Info className="mt-0.5 size-4 shrink-0 text-status-info" />
              <div className="flex min-w-0 flex-col gap-1">
                <span className="w-fit rounded-full border border-border bg-background px-2 py-0.5 text-2xs font-medium text-foreground">
                  {operationScope === "canvas"
                    ? "画布级 · 新建"
                    : operationScope === "batch"
                      ? "多选 · 延展"
                      : "单轨 · 延展"}
                </span>
                <span
                  data-testid="tracker-impact-summary"
                  className="text-xs leading-relaxed text-foreground"
                >
                  {operationScope === "batch"
                    ? `批量延展 ${sourceCount} 条轨迹 · ${sourceClassNames.length === 1 ? `「${sourceClassNames[0]}」` : `${sourceClassNames.length} 类`}`
                    : operationScope === "canvas"
                      ? `画布新建多目标轨迹${targetClass ? ` · 类别「${targetClass}」` : ""}`
                      : `延展当前轨迹${sourceTrackClassName ? `「${sourceTrackClassName}」` : ""}`}
                </span>
              </div>
            </section>
            <section
              aria-labelledby="tracker-settings-heading"
              data-testid="tracker-settings-section"
              className={cn(AI_PANEL_SECTION_CLASS, "flex flex-col gap-2.5")}
            >
              <h3 id="tracker-settings-heading" className="text-xs font-semibold text-foreground">
                本次追踪
              </h3>
              <div className="grid grid-cols-1 gap-2.5 @min-[280px]/tracker-panel:grid-cols-2">
                <fieldset className="col-span-full m-0 min-w-0 border-0 p-0">
                  <legend
                    className={cn(
                      TRACKER_FIELD_LABEL_CLASS,
                      "mb-1.5 block w-full p-0 leading-normal",
                    )}
                  >
                    追踪方向
                  </legend>
                  <div className="grid grid-cols-1 gap-1 rounded-md bg-muted p-1 ring-1 ring-border @min-[240px]/tracker-panel:grid-cols-3">
                    {(["forward", "backward", "bidirectional"] as VideoTrackerDirection[]).map(
                      (d) => (
                        <button
                          key={d}
                          type="button"
                          data-testid={`tracker-direction-${d}`}
                          title={DIRECTION_META[d].title}
                          aria-pressed={direction === d}
                          onClick={() => {
                            setDirection(d);
                            setCustomRange(null);
                          }}
                          className={cn(
                            "min-h-6 cursor-pointer whitespace-nowrap rounded-sm px-2 text-xs font-medium outline-none transition-[background-color,color,box-shadow,transform] duration-200 focus-visible:ring-[3px] focus-visible:ring-ring/50 active:scale-[0.98]",
                            direction === d
                              ? "bg-background text-foreground shadow-sm"
                              : "text-muted-foreground hover:bg-background/70 hover:text-foreground",
                          )}
                        >
                          {DIRECTION_META[d].label}
                        </button>
                      ),
                    )}
                  </div>
                </fieldset>

                <div className="flex min-w-0 flex-col gap-1.5">
                  <label htmlFor="tracker-range-preset" className={TRACKER_FIELD_LABEL_CLASS}>
                    帧范围
                  </label>
                  <select
                    id="tracker-range-preset"
                    value={rangePreset}
                    onChange={(e) => {
                      setRangePreset(e.target.value as RangePresetValue);
                      setCustomRange(null);
                    }}
                    className={TRACKER_SELECT_CLASS}
                  >
                    {RANGE_PRESETS.map((preset) => (
                      <option key={preset} value={preset}>
                        {presetLabel(preset, grid)}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="flex min-w-0 flex-col gap-1.5">
                  <label htmlFor="tracker-model" className={TRACKER_FIELD_LABEL_CLASS}>
                    追踪模型
                  </label>
                  <select
                    id="tracker-model"
                    value={modelKey}
                    onChange={(e) => setModelKey(e.target.value)}
                    disabled={modelOptions.length === 0}
                    className={TRACKER_SELECT_CLASS}
                  >
                    {modelKey && !modelOptions.some((model) => model.value === modelKey) && (
                      <option value={modelKey} disabled hidden>
                        已选追踪模型（暂不可用）
                      </option>
                    )}
                    {modelOptions.length === 0 && (
                      <option value="" disabled hidden>
                        无可用追踪模型
                      </option>
                    )}
                    {modelOptions.map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}
                        {trackerModelProviders[m.value]?.length
                          ? ` · ${trackerModelProviders[m.value].join(" / ")}`
                          : ""}
                      </option>
                    ))}
                  </select>
                  {activeModelOption?.note && (
                    <p className={TRACKER_FIELD_HELP_CLASS}>{activeModelOption.note}</p>
                  )}
                </div>
              </div>

              {(sourcelessLike || usesSamVariant(modelKey) || modelKey !== "mock_bbox") && (
                <div className="grid grid-cols-1 gap-2.5 @min-[280px]/tracker-panel:grid-cols-2">
                  {sourcelessLike && (
                    <div className="flex min-w-0 flex-col gap-1.5">
                      <label htmlFor="tracker-target-class" className={TRACKER_FIELD_LABEL_CLASS}>
                        目标类别
                      </label>
                      <select
                        id="tracker-target-class"
                        value={targetClass}
                        onChange={(e) => setTargetClass(e.target.value)}
                        data-testid="tracker-target-class"
                        disabled={availableClasses.length === 0}
                        className={TRACKER_SELECT_CLASS}
                      >
                        {targetClass && !availableClasses.includes(targetClass) && (
                          <option value={targetClass} disabled hidden>
                            已选类别（暂不可用）
                          </option>
                        )}
                        {availableClasses.length === 0 && (
                          <option value="" disabled hidden>
                            暂无类别
                          </option>
                        )}
                        {availableClasses.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  {usesSamVariant(modelKey) && (
                    <div className="flex min-w-0 flex-col gap-1.5">
                      <label htmlFor="tracker-sam-variant" className={TRACKER_FIELD_LABEL_CLASS}>
                        尺寸
                      </label>
                      <select
                        id="tracker-sam-variant"
                        value={samVariant}
                        onChange={(e) => setSamVariant(e.target.value)}
                        className={TRACKER_SELECT_CLASS}
                      >
                        {SAM_VARIANTS.map((v) => (
                          <option key={v.value} value={v.value}>
                            {v.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  {modelKey !== "mock_bbox" && (
                    <div className="flex min-w-0 flex-col gap-1.5">
                      <label
                        htmlFor="tracker-output-geometry"
                        className={TRACKER_FIELD_LABEL_CLASS}
                      >
                        输出几何
                      </label>
                      <select
                        id="tracker-output-geometry"
                        data-testid="tracker-output-geometry"
                        value={outputGeometry}
                        onChange={(event) =>
                          setOutputGeometry(event.target.value as typeof outputGeometry)
                        }
                        className={TRACKER_SELECT_CLASS}
                      >
                        <option value="">跟随当前轨迹</option>
                        <option value="mask">栅格 mask</option>
                        <option value="polygon">多边形</option>
                        <option value="bbox">矩形框</option>
                      </select>
                    </div>
                  )}
                </div>
              )}
            </section>

            {(supportsSeedCapture(modelKey) || textDrivenActive) && (
              <section
                aria-labelledby="tracker-prompt-heading"
                className={cn(AI_PANEL_SECTION_CLASS, "flex flex-col gap-2.5")}
              >
                <div className="flex items-start gap-2.5">
                  <span className="flex size-6 shrink-0 items-center justify-center text-muted-foreground">
                    {textDrivenActive ? (
                      <Type className="size-3.5" />
                    ) : (
                      <MousePointer2 className="size-3.5" />
                    )}
                  </span>
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <h3 id="tracker-prompt-heading" className="text-xs font-medium text-foreground">
                      {textDrivenActive ? "文本目标" : "目标种子"}
                    </h3>
                    <p className={TRACKER_FIELD_HELP_CLASS}>
                      {textDrivenActive
                        ? "模型会按描述在每帧发现并追踪目标。"
                        : "可在不同帧添加点或框，用于首帧定位和后续纠偏。"}
                    </p>
                  </div>
                </div>

                {supportsSeedCapture(modelKey) && (
                  <>
                    <div className="flex flex-wrap items-center gap-2">
                      <div
                        className="grid h-8 grid-cols-2 gap-1 rounded-md bg-background p-1 ring-1 ring-border"
                        role="group"
                        aria-label="种子类型"
                      >
                        {(["point", "box"] as const).map((m) => (
                          <button
                            key={m}
                            type="button"
                            data-testid={`tracker-seed-mode-${m}`}
                            onClick={() => onChangeSeedMode?.(m)}
                            disabled={submitting}
                            aria-pressed={seedMode === m}
                            className={cn(
                              "cursor-pointer rounded-sm px-3 text-xs font-medium outline-none transition-[background-color,color,box-shadow,transform] duration-200 focus-visible:ring-[3px] focus-visible:ring-ring/50 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50",
                              seedMode === m
                                ? "bg-primary text-primary-foreground shadow-sm"
                                : "text-muted-foreground hover:bg-muted hover:text-foreground",
                            )}
                          >
                            {m === "point" ? "点" : "框"}
                          </button>
                        ))}
                      </div>
                      <Button
                        type="button"
                        variant={seedCollecting ? "ai" : "default"}
                        size="sm"
                        onClick={onToggleSeedCollecting}
                        disabled={submitting}
                        data-testid="tracker-seed-toggle"
                        data-workbench-video-tool-command
                        aria-pressed={seedCollecting}
                      >
                        <MousePointer2 data-icon="inline-start" />
                        {seedCollecting
                          ? seedMode === "box"
                            ? "画框中…"
                            : "落点中…"
                          : seedMode === "box"
                            ? "画框选目标"
                            : "落点选目标"}
                      </Button>
                      {(seedPointCount > 0 || seedBoxCount > 0) && sourceless && (
                        <Button
                          type="button"
                          size="sm"
                          onClick={onNewSeedTarget}
                          disabled={submitting}
                          data-testid="tracker-seed-new-target"
                          title="后续落点或框归入新目标，各成一条轨迹"
                        >
                          + 新目标
                        </Button>
                      )}
                      {(seedPointCount > 0 || seedBoxCount > 0) && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="xs"
                          onClick={onClearSeeds}
                          disabled={submitting}
                        >
                          清空
                        </Button>
                      )}
                    </div>

                    {displayedSeedTargets.length > 0 && (
                      <div
                        data-testid="tracker-seed-count"
                        className="flex flex-col overflow-hidden rounded-md border border-border bg-background"
                        aria-label="目标种子摘要"
                      >
                        {displayedSeedTargets.map((target) => {
                          const isActive = target.targetId === activeSeedTargetId;
                          const frames = [...new Set(target.frames)].sort((a, b) => a - b);
                          return (
                            <div
                              key={target.targetId}
                              data-testid={`tracker-seed-target-${target.targetId}`}
                              aria-current={isActive ? "true" : undefined}
                              className={cn(
                                "flex min-w-0 items-start justify-between gap-3 border-b border-border px-3 py-2.5 last:border-b-0",
                                isActive && "bg-primary/5",
                              )}
                            >
                              <div className="flex min-w-0 items-start gap-2.5">
                                <span
                                  className={cn(
                                    "flex size-6 shrink-0 items-center justify-center rounded-md border border-border bg-muted text-xs font-semibold tabular-nums text-muted-foreground",
                                    isActive &&
                                      "border-primary/30 bg-primary text-primary-foreground",
                                  )}
                                >
                                  {target.targetId}
                                </span>
                                <div className="flex min-w-0 flex-col gap-1">
                                  <div className="flex flex-wrap items-center gap-1.5">
                                    <span className="text-xs font-medium text-foreground">
                                      目标 {target.targetId}
                                    </span>
                                    {isActive && <Badge variant="secondary">当前</Badge>}
                                  </div>
                                  <span className="mono text-2xs leading-relaxed text-muted-foreground">
                                    {frames.length > 0
                                      ? `帧 ${frames.map((frame) => `F${frame}`).join("、")}`
                                      : "等待在画布添加种子"}
                                  </span>
                                </div>
                              </div>
                              <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                                <Badge variant="outline" className="tabular-nums">
                                  {target.pointCount} 点
                                </Badge>
                                <Badge variant="outline" className="tabular-nums">
                                  {target.boxCount} 框
                                </Badge>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </>
                )}

                {textDrivenActive && (
                  <div className="flex min-w-0 flex-col gap-1.5">
                    <label htmlFor="tracker-text-input" className={TRACKER_FIELD_LABEL_CLASS}>
                      目标描述
                    </label>
                    <Input
                      id="tracker-text-input"
                      type="text"
                      value={text}
                      onChange={(e) => setText(e.target.value)}
                      placeholder="例如：the red car"
                      data-testid="tracker-text-input"
                    />
                  </div>
                )}
              </section>
            )}

            <section
              aria-labelledby="tracker-impact-heading"
              className={cn(AI_PANEL_SECTION_CLASS, "flex items-start justify-between gap-3")}
            >
              <div className="flex items-start gap-2">
                <Clock3 className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                <div className="flex flex-col gap-1">
                  <h3 id="tracker-impact-heading" className="text-xs font-medium text-foreground">
                    处理范围
                  </h3>
                  <span className="mono text-xs font-medium text-foreground">
                    {grid > 1 ? (
                      <>
                        G{Math.round(range.from / grid)} → G{Math.round(range.to / grid)} (F
                        {range.from} → F{range.to})
                      </>
                    ) : (
                      <>
                        F{range.from} → F{range.to}
                      </>
                    )}
                    {customRange && (
                      <span data-testid="tracker-range-custom" className="ml-1.5 text-brand">
                        · 自定义
                      </span>
                    )}
                  </span>
                  {estimatedWindows > 1 && (
                    <span
                      data-testid="tracker-window-estimate"
                      className={TRACKER_FIELD_HELP_CLASS}
                    >
                      ≈{estimatedWindows} 窗，将分段处理
                    </span>
                  )}
                </div>
              </div>
            </section>

            {(selectedModelDisabled || isPolylineTrack || error) && (
              <div className={cn(AI_PANEL_SECTION_CLASS, "flex flex-col gap-2")} aria-live="polite">
                {selectedModelDisabled && (
                  <p className="rounded-md bg-status-caution-soft px-3 py-2 text-xs leading-relaxed text-status-caution">
                    当前项目没有可执行的追踪模型。请在项目设置中启用并连接支持视频追踪的 ML
                    Backend。
                  </p>
                )}
                {isPolylineTrack && (
                  <p
                    data-testid="tracker-polyline-unsupported"
                    className="rounded-md bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
                  >
                    polyline 轨迹追踪暂不支持
                  </p>
                )}
                {error && (
                  <p
                    role="alert"
                    className="rounded-md bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
                  >
                    <CopyableError message={error} />
                  </p>
                )}
              </div>
            )}
          </div>

          <footer className="flex flex-col gap-2 bg-card px-3.5 py-2.5 @min-[320px]/tracker-panel:flex-row @min-[320px]/tracker-panel:items-center @min-[320px]/tracker-panel:justify-between">
            <span className="text-2xs leading-relaxed text-muted-foreground">
              Shift + 时间轴拖选范围
            </span>
            <div className="flex shrink-0 flex-col gap-2 @min-[240px]/tracker-panel:flex-row @min-[240px]/tracker-panel:items-center @min-[240px]/tracker-panel:justify-end">
              <Button variant="ghost" size="sm" onClick={onCancel} disabled={submitting}>
                取消
              </Button>
              <Button
                variant="ai"
                size="sm"
                className="h-auto min-h-8 min-w-0 whitespace-normal"
                onClick={handleSubmit}
                disabled={
                  submitting ||
                  selectedModelDisabled ||
                  isPolylineTrack ||
                  (sourcelessLike && !availableClasses.includes(targetClass))
                }
                title={isPolylineTrack ? "polyline 轨迹追踪暂不支持" : undefined}
              >
                {submitting ? (
                  <Loader2
                    data-icon="inline-start"
                    className="animate-spin motion-reduce:animate-none"
                  />
                ) : (
                  <Bot data-icon="inline-start" />
                )}
                {submitting ? progressLabel : submitLabel}
              </Button>
            </div>
          </footer>
        </>
      )}
    </div>
  );
}
