import { CopyableError } from "@/components/ui/CopyableError";
// v0.18.25 · 交互工具上下文浮块 (前身 AIToolDrawer, 已退役)。
//
// 浮在 ImageStage container 顶部居中, 选中 AI 工具 (point/box/exemplar) 时渲染, 与 MaskToolbar
// 互斥 (mask 非 AI 工具)。主层保留工具提示、候选决策与恢复；高级区折叠引擎、模型、权重、
// 阈值和诊断。配置与本轮推理仍由现有上层 owner 持有，折叠仅改变显示。
// 引擎选择持久化由上层 useAiToolModelPref (服务端 User.preferences.ai.model_by_backend) 承载。

import { useEffect, useId, useState } from "react";
import { X } from "lucide-react";

import { ContextToolbar, type ContextToolbarAction } from "./ContextToolbar";
import { Button } from "@/components/ui/Button";
import { Button as IconButton } from "@/components/shadcn/ui/button";
import { Icon } from "@/components/ui/Icon";
import type {
  MLBackendCapability,
  MLBackendSupportedVariantGroup,
  MLModelCapability,
} from "@/api/ml-backends";
import type { AttributeField } from "@/api/projects";
import { VariantSelector } from "@/components/ml/VariantSelector";
import type { SamPolarity, Tool } from "../state/useWorkbenchState";
import type { TextOutputMode } from "../state/useInteractiveAI";
import type { CapabilityWarning } from "../state/useCapabilityValidation";
import { TOOL_REGISTRY, type ToolId } from "../stage/tools";
// v0.21.27 · U-pvs-1 · 共享的悬浮工具条 chrome（与视频 tracker 传播工具条共用同款外观）。
import { TOOLBAR_FIELD_LABEL_CLASS as FIELD_LABEL_CLASS } from "./workbenchToolbarChrome";

const SELECT_CLASS =
  "h-8 min-w-0 w-full rounded-lg border border-border bg-muted/40 px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";

export interface InteractiveToolBarProps {
  tool: Tool;
  presentationKey?: string;
  presentationHidden?: boolean;
  /** 解析到的交互后端名称 (来自 /setup.name); undefined → "未绑定". */
  backendName: string | undefined;
  capability: MLBackendCapability | undefined;
  samPolarity: SamPolarity;
  onSetSamPolarity: (p: SamPolarity) => void;
  isLoading: boolean;
  isError: boolean;
  capabilityRecoveryOnly?: boolean;
  capabilityError?: string;
  onRetryCapabilities?: () => void;
  isCapabilityRetrying?: boolean;
  isRunning?: boolean;
  inferenceError?: string | null;
  candidateCount?: number;
  /** Existing prompts can remain even when the backend returns no candidates. */
  hasPromptSession?: boolean;
  /** Zero-based index supplied by the current inference session. */
  activeCandidateIndex?: number;
  canAcceptCandidates?: boolean;
  candidateActionPending?: boolean;
  onCycleCandidate?: (dir: 1 | -1) => void;
  onAcceptCandidate?: () => void;
  onCancelCandidates?: () => void;
  canRetry?: boolean;
  onRetry?: () => void;
  // exemplar 工具输出形态 (box/mask/both); 会话级状态由 WorkbenchShell 持有.
  exemplarOutputMode?: TextOutputMode;
  onSetExemplarOutputMode?: (mode: TextOutputMode) => void;
  /** 单帧交互候选的持久几何；与 exemplar 的召回结果形态相互独立。 */
  singleFrameOutputGeometry?: "polygon" | "mask";
  onSetSingleFrameOutputGeometry?: (mode: "polygon" | "mask") => void;
  nativeMaskOutputDisabledReason?: string;
  /** 当前已鉴权的已存 Mask 提示摘要；不携带 RLE 正文。 */
  maskPromptSourceLabel?: string;
  // exemplar refine 会话控件 (多正负框 + text 组合 + 阈值重过滤).
  /** 叠加的 text 概念 (PCS text + 几何示例组合); 改动即重跑当前会话。 */
  exemplarText?: string;
  onSetExemplarText?: (text: string) => void;
  /** per-request 阈值; null=用 backend 默认。拖动即重过滤当前会话。 */
  exemplarThreshold?: number | null;
  onSetExemplarThreshold?: (thr: number | null) => void;
  /** backend 默认阈值 (slider 在未覆盖时的展示初值, 取自 /setup.params)。 */
  exemplarThresholdDefault?: number;
  /** 会话进行中 (已落 ≥1 框); 决定阈值/文本提示文案。 */
  exemplarSessionActive?: boolean;
  // 能力声明协议 v2 · 多模型选择. models 长度 <= 1 时**不渲染**选择器 (向后兼容).
  models?: MLModelCapability[];
  activeModelId?: string;
  onSetActiveModelId?: (id: string) => void;
  // active model 与项目配置的兼容性警告 (非阻断). 空数组时不渲染。
  capabilityWarnings?: CapabilityWarning[];
  // v0.20.2 · 「采纳后该属性将丢失」警告的一键补全回调: 把 model 自报字段补进项目所有启用工具单位。
  //   仅 warning.fillable 存在时渲染 CTA; 缺省 = 不渲染补全按钮。
  onFillAttribute?: (field: AttributeField) => void;
  // 交互后端选择器 (能力作用域化): 只列支持当前工具 prompt 的后端, 选中值 = 实际解析后端.
  //   <2 个候选时退化为只读显示 (无 UI 噪音), 行为 = 单后端现状.
  interactiveBackends?: Array<{ id: string; name: string }>;
  selectedInteractiveId?: string | null;
  onSelectInteractive?: (id: string) => void;
  // v0.18.26 · 模型权重(档位)选择: 交互后端 activeModel 的 variant 轴 (series/size 等);
  //   选择写回项目级 default_variants (与批量预标注同源)。无 variant 轴时不渲染入口。
  variantGroups?: MLBackendSupportedVariantGroup[];
  variantCombinations?: string[][];
  /** backend 自报 + 项目偏好合并后的默认 variant 组合 (axis_key → value)。 */
  variantDefaults?: Record<string, string>;
  /** 当前项目已选 variant slice (缺轴由 variantDefaults 兜底)。 */
  variantValue?: Record<string, string>;
  onVariantChange?: (next: Record<string, unknown>) => void;
}

// model.task → 中文分组标题. 受控 task 之外的归「其他」。
const MODEL_TASK_LABELS: Record<string, string> = {
  detect: "检测",
  obb: "旋转框检测",
  segment: "分割",
  pose: "关键点",
  classify: "分类",
  ocr: "文字识别",
  doc_layout: "版面分析",
  tracker: "视频追踪",
  interactive_seg: "交互式分割",
};

function modelTaskLabel(task: string | undefined): string {
  if (!task) return "其他";
  return MODEL_TASK_LABELS[task] ?? "其他";
}

// 按 task 把 models 分桶, 保持各 task 内的原始顺序; 返回 [task, models[]] 列表。
function groupModelsByTask(models: MLModelCapability[]): Array<[string, MLModelCapability[]]> {
  const order: string[] = [];
  const buckets = new Map<string, MLModelCapability[]>();
  for (const m of models) {
    const task = m.task ?? "";
    if (!buckets.has(task)) {
      buckets.set(task, []);
      order.push(task);
    }
    buckets.get(task)!.push(m);
  }
  return order.map((task) => [task, buckets.get(task)!]);
}

const TOOL_HINT: Record<ToolId, string | null> = {
  select: null,
  box: null,
  "rotated-box": null,
  hand: null,
  polygon: null,
  polyline: null,
  keypoint: null,
  canvas: null,
  mask: null,
  "smart-point": "在画布上添加提示点，逐步修正分割范围",
  "smart-box": "框住目标区域，生成分割候选",
  "smart-scribble": "在当前 Mask 上添加或排除区域",
  "text-prompt": null,
  exemplar: "框选示例，可叠加文本查找相似目标",
  "magic-box": "框住目标，自动贴合边缘生成矩形",
};

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

export function InteractiveToolBar({
  tool,
  presentationKey,
  presentationHidden = false,
  backendName,
  capability,
  samPolarity,
  onSetSamPolarity,
  isLoading,
  isError,
  capabilityRecoveryOnly = false,
  capabilityError,
  onRetryCapabilities,
  isCapabilityRetrying = false,
  isRunning = false,
  inferenceError,
  candidateCount = 0,
  hasPromptSession = false,
  activeCandidateIndex = 0,
  canAcceptCandidates = false,
  candidateActionPending = false,
  onCycleCandidate,
  onAcceptCandidate,
  onCancelCandidates,
  canRetry,
  onRetry,
  exemplarOutputMode,
  onSetExemplarOutputMode,
  singleFrameOutputGeometry,
  onSetSingleFrameOutputGeometry,
  nativeMaskOutputDisabledReason,
  maskPromptSourceLabel,
  exemplarText,
  onSetExemplarText,
  exemplarThreshold,
  onSetExemplarThreshold,
  exemplarThresholdDefault,
  exemplarSessionActive,
  models,
  activeModelId,
  onSetActiveModelId,
  capabilityWarnings,
  onFillAttribute,
  interactiveBackends,
  selectedInteractiveId,
  onSelectInteractive,
  variantGroups,
  variantCombinations,
  variantDefaults,
  variantValue,
  onVariantChange,
}: InteractiveToolBarProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const advancedId = useId();
  const meta = TOOL_REGISTRY[tool];
  const hint = TOOL_HINT[tool];
  const hasVariants = !!variantGroups && variantGroups.length > 0 && !!onVariantChange;
  const supportsOutputGeometry = [
    "smart-point",
    "smart-box",
    "smart-scribble",
    "exemplar",
  ].includes(tool);

  // 模型选择器按当前工具 prompt 过滤: 只列声明支持该交互 prompt 的图像交互 model
  // (point → 仅 interactive_seg; tracker 属视频, 排除)。过滤后通常剩 1 个 → 选择器自动隐藏。
  const toolPrompt = TOOL_REGISTRY[tool]?.requiredPrompt;
  const modelList = models ?? [];
  const filteredModels =
    toolPrompt && toolPrompt !== "text"
      ? modelList.filter(
          (m) =>
            m.is_interactive === true &&
            m.task !== "tracker" &&
            (m.supported_prompts ?? []).includes(toolPrompt),
        )
      : modelList;
  const showModelSelector = filteredModels.length > 1 && !!onSetActiveModelId;

  // 当前 exemplar 工具对应的交互模型能力 (隐藏后端不支持的控件)。
  // 优先取选中 activeModel, 回退到首个支持 exemplar 的交互模型 (单后端常态)。
  const exemplarModel =
    tool === "exemplar"
      ? (filteredModels.find((m) => m.id === activeModelId) ?? filteredModels[0])
      : undefined;
  const exCaps = exemplarModel?.exemplar_capabilities;
  // 缺省 = 全支持 (向后兼容 sam3 旧 setup 不带本字段时不改行为)。
  const exemplarNegative = exCaps?.negative_box !== false;
  const exemplarTextCombo = exCaps?.text_combination !== false;

  // 后端无负框 (YOLOE) 时强制正极性: 隐藏负极性按钮后, 防止从 smart-point 残留的负极性
  // 让 exemplar 拖框误发 label=False (被后端剔除 → 0 结果)。
  useEffect(() => {
    if (
      !capabilityRecoveryOnly &&
      tool === "exemplar" &&
      !exemplarNegative &&
      samPolarity === "negative"
    ) {
      onSetSamPolarity("positive");
    }
  }, [tool, exemplarNegative, samPolarity, onSetSamPolarity, capabilityRecoveryOnly]);

  // 交互后端选择器: ≥2 个候选 (支持当前工具 prompt 的后端) 时可切, 否则只读显示。
  const backendCands = interactiveBackends ?? [];
  const canSwitchBackend = backendCands.length >= 2 && !!onSelectInteractive;
  const warnings = capabilityWarnings ?? [];
  const totalCandidates = Number.isFinite(candidateCount)
    ? Math.max(0, Math.floor(candidateCount))
    : 0;
  const hasActiveCandidate =
    Number.isInteger(activeCandidateIndex) &&
    activeCandidateIndex >= 0 &&
    activeCandidateIndex < totalCandidates;
  const capabilityFailed = isError || !!capabilityError;
  const capabilityBusy = isLoading || isCapabilityRetrying;
  const inferenceStatus = candidateActionPending
    ? "候选处理中…"
    : isRunning
      ? "本轮推理中…"
      : inferenceError
        ? "本轮推理失败"
        : totalCandidates > 0
          ? "候选已就绪"
          : "等待提示";

  const supportsPolarity =
    tool === "smart-point" ||
    tool === "smart-scribble" ||
    (tool === "exemplar" && exemplarNegative);
  const canChoosePersistence =
    supportsOutputGeometry && (tool !== "exemplar" || exemplarOutputMode !== "box");
  const refinementSummary = ["smart-point", "smart-box", "smart-scribble"].includes(tool)
    ? maskPromptSourceLabel
    : undefined;
  const effectivePolarity = tool === "exemplar" && !exemplarNegative ? "positive" : samPolarity;
  const polarityLabel = `${effectivePolarity === "positive" ? "正" : "负"}${tool === "exemplar" ? "例框" : tool === "smart-scribble" ? "向笔迹" : "向点"}`;
  const outputLabel =
    tool === "magic-box"
      ? "矩形"
      : tool === "exemplar"
        ? { box: "框", mask: "Mask", both: "框 + Mask" }[exemplarOutputMode ?? "box"]
        : singleFrameOutputGeometry === "polygon"
          ? "多边形"
          : singleFrameOutputGeometry === "mask"
            ? "Mask"
            : undefined;
  const summaryText = tool === "exemplar" && exemplarTextCombo ? exemplarText?.trim() : undefined;
  const showSession = totalCandidates > 0 || isRunning || candidateActionPending;
  const canCancelSession =
    !!onCancelCandidates &&
    (showSession || hasPromptSession || exemplarSessionActive || canRetry || !!inferenceError);
  const capabilityStatusLabel = capabilityBusy
    ? "正在加载能力…"
    : capabilityFailed
      ? "能力协商失败"
      : capability
        ? "能力就绪"
        : "未获取能力";
  const polarityDot = (
    <span
      data-testid="interactive-summary-polarity"
      aria-label={polarityLabel}
      title={polarityLabel}
      className={cn(
        "flex size-3 shrink-0 items-center justify-center rounded-full",
        effectivePolarity === "positive"
          ? "bg-status-positive text-primary-foreground"
          : "bg-status-danger text-primary-foreground",
      )}
    >
      <Icon name={effectivePolarity === "positive" ? "plus" : "minus"} size={9} />
    </span>
  );
  const exemplarConfidenceControl = !capabilityRecoveryOnly &&
    tool === "exemplar" &&
    onSetExemplarThreshold && (
      <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="exemplar-threshold">
        <div className="flex h-5 items-center gap-1.5">
          <span className={FIELD_LABEL_CLASS}>置信度</span>

          <span className="ml-auto text-2xs tabular-nums text-muted-foreground">
            {(exemplarThreshold ?? exemplarThresholdDefault ?? 0.5).toFixed(2)}
            {exemplarThreshold == null && "*"}
          </span>
          {exemplarThreshold != null && (
            <button
              type="button"
              data-testid="exemplar-threshold-reset"
              aria-label="重置为后端默认阈值"
              onClick={() => onSetExemplarThreshold(null)}
              className="flex size-5 cursor-pointer items-center justify-center rounded-sm border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"
              title="重置为后端默认阈值 (*)"
            >
              <Icon name="rotate-ccw" size={11} />
            </button>
          )}
        </div>
        <input
          type="range"
          aria-label="示例召回阈值"
          min={0}
          max={1}
          step={0.05}
          value={exemplarThreshold ?? exemplarThresholdDefault ?? 0.5}
          onChange={(event) => onSetExemplarThreshold(Number(event.target.value))}
          className="w-full min-w-0 cursor-pointer accent-brand"
          title={
            exemplarSessionActive
              ? "拖动实时增减结果 (调高更准 / 调低更全)"
              : "拖框后开始 refine; 加正框扩召回 / Alt 或负极性加负框去误检"
          }
        />
      </div>
    );
  const renderPrimary = (compact: boolean) => (
    <div
      data-testid="interactive-toolbar-primary"
      className={cn("flex w-full min-w-0 flex-col", compact ? "gap-2" : "gap-3")}
    >
      {!capabilityRecoveryOnly && (
        <>
          {!compact && (supportsPolarity || canChoosePersistence || tool === "exemplar") && (
            <div
              className={cn(
                "grid gap-3",
                tool === "exemplar" && supportsPolarity && canChoosePersistence
                  ? "grid-cols-3"
                  : "grid-cols-2",
              )}
            >
              {supportsPolarity && (
                <div className="flex min-w-0 flex-col gap-1.5">
                  <span className={FIELD_LABEL_CLASS}>提示方式</span>
                  <button
                    type="button"
                    data-testid="ai-tool-polarity"
                    aria-label={samPolarity === "positive" ? "切换到负向提示" : "切换到正向提示"}
                    title={
                      tool === "exemplar"
                        ? samPolarity === "positive"
                          ? "正框 (+)，扩召回；按 - 切负框"
                          : "负框 (−)，排误检；按 + 切正框"
                        : samPolarity === "positive"
                          ? "正向 (+)，按 - 切负向"
                          : "负向 (−)，按 + 切正向"
                    }
                    onClick={() =>
                      onSetSamPolarity(samPolarity === "positive" ? "negative" : "positive")
                    }
                    className={cn(SELECT_CLASS, "flex items-center gap-2 text-left hover:bg-muted")}
                  >
                    <span
                      className={cn(
                        "size-2 rounded-full",
                        samPolarity === "positive" ? "bg-status-positive" : "bg-status-danger",
                      )}
                    />
                    {polarityLabel}
                    <span className="ml-auto text-2xs text-muted-foreground">切换</span>
                  </button>
                </div>
              )}
              {canChoosePersistence &&
                singleFrameOutputGeometry &&
                onSetSingleFrameOutputGeometry && (
                  <label
                    className="flex min-w-0 flex-col gap-1.5"
                    data-testid="single-frame-output-geometry"
                  >
                    <span className={FIELD_LABEL_CLASS}>
                      {tool === "exemplar" ? "分割保存为" : "保存为"}
                    </span>
                    <select
                      data-testid="single-frame-output-geometry-select"
                      aria-label="单帧提交几何"
                      value={singleFrameOutputGeometry}
                      onChange={(event) =>
                        onSetSingleFrameOutputGeometry(event.target.value as "polygon" | "mask")
                      }
                      className={SELECT_CLASS}
                      title={nativeMaskOutputDisabledReason ?? "单帧候选持久化几何"}
                    >
                      <option value="polygon">多边形</option>
                      <option value="mask" disabled={nativeMaskOutputDisabledReason != null}>
                        原生 Mask
                      </option>
                    </select>
                  </label>
                )}
              {tool === "exemplar" && exemplarOutputMode && onSetExemplarOutputMode && (
                <label className="flex min-w-0 flex-col gap-1.5" data-testid="exemplar-output-mode">
                  <span className={FIELD_LABEL_CLASS}>召回形态</span>
                  <select
                    data-testid="exemplar-output-mode-select"
                    aria-label="示例召回形态"
                    value={exemplarOutputMode}
                    onChange={(event) =>
                      onSetExemplarOutputMode(event.target.value as TextOutputMode)
                    }
                    className={SELECT_CLASS}
                    title="输出形态"
                  >
                    <option value="box">框</option>
                    <option value="mask">掩膜</option>
                    <option value="both">框与掩膜</option>
                  </select>
                </label>
              )}
            </div>
          )}
          {maskPromptSourceLabel &&
            (tool === "smart-point" || tool === "smart-box" || tool === "smart-scribble") && (
              <div
                data-testid="mask-prompt-source"
                className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground"
                title="接纳后原位更新当前 Mask"
              >
                <Icon name="layers" size={13} />
                <span className="min-w-0 break-words">{maskPromptSourceLabel}</span>
              </div>
            )}
          {tool === "exemplar" && onSetExemplarText && exemplarTextCombo && (
            <label className="flex min-w-0 flex-col gap-1.5" data-testid="exemplar-text">
              <span className={FIELD_LABEL_CLASS}>
                目标文本 <span className="text-muted-foreground/70">可选</span>
              </span>
              <input
                type="text"
                aria-label="示例叠加文本"
                value={exemplarText ?? ""}
                onChange={(event) => onSetExemplarText(event.target.value)}
                placeholder="如 car . person"
                className={SELECT_CLASS}
                title="叠加文本概念，与示例框组合"
              />
            </label>
          )}
          {compact && exemplarConfidenceControl}
          <div
            className={
              showSession || canCancelSession
                ? compact
                  ? "flex min-w-0 flex-col gap-2"
                  : "flex min-w-0 flex-wrap items-center justify-between gap-2"
                : "sr-only"
            }
          >
            <div className="flex items-center justify-between gap-2">
              <span
                data-testid="interactive-candidate-count"
                className={cn(
                  "whitespace-nowrap text-xs tabular-nums text-muted-foreground",
                  !showSession && "sr-only",
                )}
                aria-live="polite"
              >
                候选 {hasActiveCandidate ? activeCandidateIndex + 1 : 0} / {totalCandidates}
              </span>
              {showSession && (
                <div className="flex items-center gap-0.5">
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    data-testid="interactive-candidate-previous"
                    aria-label="上一个候选"
                    title="上一个候选"
                    disabled={
                      !hasActiveCandidate ||
                      totalCandidates < 2 ||
                      !onCycleCandidate ||
                      isRunning ||
                      candidateActionPending
                    }
                    onClick={() => onCycleCandidate?.(-1)}
                  >
                    <Icon name="chevron-left" size={13} />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    data-testid="interactive-candidate-next"
                    aria-label="下一个候选"
                    title="下一个候选"
                    disabled={
                      !hasActiveCandidate ||
                      totalCandidates < 2 ||
                      !onCycleCandidate ||
                      isRunning ||
                      candidateActionPending
                    }
                    onClick={() => onCycleCandidate?.(1)}
                  >
                    <Icon name="chevron-right" size={13} />
                  </Button>
                </div>
              )}
            </div>
            {(showSession || canCancelSession) && (
              <div className="flex flex-wrap items-center gap-2">
                {showSession && (
                  <Button
                    type="button"
                    variant="primary"
                    size={compact ? "xs" : "sm"}
                    className="min-w-0 flex-1"
                    data-testid="interactive-candidate-accept"
                    disabled={
                      !hasActiveCandidate ||
                      !canAcceptCandidates ||
                      !onAcceptCandidate ||
                      isRunning ||
                      candidateActionPending
                    }
                    onClick={onAcceptCandidate}
                  >
                    <Icon name={candidateActionPending ? "loader2" : "check"} size={13} />
                    接受
                  </Button>
                )}
                {canCancelSession && (
                  <Button
                    type="button"
                    variant="ghost"
                    size={compact ? "xs" : "sm"}
                    data-testid="interactive-candidate-cancel"
                    disabled={!onCancelCandidates || candidateActionPending}
                    onClick={onCancelCandidates}
                  >
                    取消本轮
                  </Button>
                )}
              </div>
            )}
          </div>
          <div
            className={cn(
              "flex flex-wrap items-center gap-2",
              !isRunning && !candidateActionPending && !inferenceError && !canRetry && "sr-only",
            )}
          >
            <span
              data-testid="interactive-inference-status"
              role="status"
              className={cn(
                "flex items-center gap-1.5 text-2xs",
                inferenceError ? "text-status-danger" : "text-muted-foreground",
              )}
            >
              {(isRunning || candidateActionPending) && <Icon name="loader2" size={12} />}
              {inferenceStatus}
            </span>
            {canRetry && onRetry && (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                disabled={isRunning || candidateActionPending}
                onClick={onRetry}
                data-testid="interactive-prompt-retry"
              >
                <Icon name="rotate-ccw" size={12} />
                重试本轮
              </Button>
            )}
          </div>
        </>
      )}
      <span
        data-testid="interactive-capability-status"
        role="status"
        className={cn(
          "flex items-center gap-1.5 text-2xs",
          capabilityFailed ? "text-status-danger" : "text-muted-foreground",
          !capabilityBusy && !capabilityFailed && "sr-only",
        )}
      >
        {capabilityBusy && <Icon name="loader2" size={12} />}
        {capabilityStatusLabel}
      </span>
      {capabilityFailed && (
        <div
          data-testid="interactive-capability-error"
          className="flex min-w-0 flex-wrap items-center gap-2 rounded-lg bg-status-danger-soft p-2 text-xs text-status-danger"
        >
          <span role="alert" className="min-w-0 flex-1 break-words">
            能力协商：
            <CopyableError message={capabilityError ?? "无法获取后端能力"} />
          </span>
          {onRetryCapabilities && (
            <Button
              type="button"
              variant="ghost"
              size="xs"
              data-testid="interactive-capability-retry"
              disabled={capabilityBusy}
              onClick={onRetryCapabilities}
            >
              <Icon name={capabilityBusy ? "loader2" : "rotate-ccw"} size={12} />
              {capabilityBusy ? "重试中…" : "重试能力协商"}
            </Button>
          )}
        </div>
      )}
      {!capabilityRecoveryOnly && inferenceError && (
        <div
          data-testid="interactive-inference-error"
          role="alert"
          className="min-w-0 break-words rounded-lg bg-status-danger-soft p-2 text-xs text-status-danger"
        >
          本轮推理：
          <CopyableError message={inferenceError} />
        </div>
      )}
    </div>
  );
  const quickActions: ContextToolbarAction[] = [];
  if (
    !capabilityRecoveryOnly &&
    (tool === "smart-point" ||
      tool === "smart-scribble" ||
      (tool === "exemplar" && exemplarNegative))
  ) {
    quickActions.push({
      id: "polarity",
      label: samPolarity === "positive" ? "切换到负向提示" : "切换到正向提示",
      shortLabel: samPolarity === "positive" ? "负向提示" : "正向提示",
      icon: <Icon name={samPolarity === "positive" ? "minus" : "plus"} size={14} />,
      onSelect: () => onSetSamPolarity(samPolarity === "positive" ? "negative" : "positive"),
    });
  }
  if (
    !capabilityRecoveryOnly &&
    canChoosePersistence &&
    singleFrameOutputGeometry &&
    onSetSingleFrameOutputGeometry
  ) {
    quickActions.push({
      id: "geometry",
      label: singleFrameOutputGeometry === "mask" ? "提交为多边形" : "提交为原生 Mask",
      shortLabel: singleFrameOutputGeometry === "mask" ? "多边形输出" : "Mask 输出",
      icon: <Icon name="layers" size={14} />,
      disabled: singleFrameOutputGeometry !== "mask" && nativeMaskOutputDisabledReason != null,
      onSelect: () =>
        onSetSingleFrameOutputGeometry(singleFrameOutputGeometry === "mask" ? "polygon" : "mask"),
    });
  }
  if (presentationHidden) return null;
  return (
    <ContextToolbar
      key={presentationKey ?? tool}
      id="interactive"
      label="AI"
      summaryLabel={capabilityRecoveryOnly ? "AI 连接常用工具" : `${meta.label}常用工具`}
      summaryTitle={capabilityRecoveryOnly ? "恢复 AI 连接" : `${meta.label} · ${hint ?? ""}`}
      panelSize="compact"
      summary={
        <>
          <Icon name={capabilityRecoveryOnly ? "info" : meta.icon} size={14} />
          {!capabilityRecoveryOnly && (
            <>
              {supportsPolarity && polarityDot}
              {refinementSummary ? (
                <span className="max-w-28 truncate text-xs" title={refinementSummary}>
                  {refinementSummary}
                </span>
              ) : (
                <>
                  {summaryText && (
                    <span className="max-w-24 truncate text-xs" title={summaryText}>
                      {summaryText}
                    </span>
                  )}
                  {outputLabel && (
                    <span className="text-2xs text-muted-foreground">{outputLabel}</span>
                  )}
                </>
              )}
              {totalCandidates > 0 && (
                <span
                  className="text-2xs tabular-nums text-muted-foreground"
                  aria-label={`候选 ${hasActiveCandidate ? activeCandidateIndex + 1 : 0} / ${totalCandidates}`}
                >
                  {hasActiveCandidate ? activeCandidateIndex + 1 : 0}/{totalCandidates}
                </span>
              )}
            </>
          )}
          {capabilityBusy || (!capabilityRecoveryOnly && (isRunning || candidateActionPending)) ? (
            <span
              aria-label={capabilityBusy ? capabilityStatusLabel : inferenceStatus}
              title={capabilityBusy ? capabilityStatusLabel : inferenceStatus}
            >
              <Icon name="loader2" size={12} className="animate-spin motion-reduce:animate-none" />
            </span>
          ) : capabilityFailed || (!capabilityRecoveryOnly && inferenceError) ? (
            <span
              aria-label={capabilityFailed ? "能力协商失败" : "本轮推理失败"}
              title={capabilityError ?? inferenceError ?? "能力协商失败"}
              className="text-status-caution"
            >
              <Icon name="warning" size={13} />
            </span>
          ) : null}
        </>
      }
      quickActions={quickActions}
      primaryContent={renderPrimary(true)}
    >
      {(close) => (
        <div className="flex min-w-0 flex-col gap-3" data-workbench-ai-toolbar>
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-2.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground">
                <Icon name={capabilityRecoveryOnly ? "info" : meta.icon} size={16} />
              </span>
              <div className="min-w-0">
                <h2 className="m-0 text-sm font-semibold">
                  {capabilityRecoveryOnly ? "AI 连接" : meta.label}
                </h2>
                <p className="mb-0 mt-1 text-2xs leading-relaxed text-muted-foreground">
                  {capabilityRecoveryOnly ? "恢复模型连接后继续标注" : hint}
                </p>
              </div>
            </div>
            <IconButton
              size="icon-xs"
              variant="ghost"
              className="shrink-0 rounded-full text-muted-foreground"
              aria-label="收起 AI 设置"
              title="收起设置，继续标注"
              onClick={close}
            >
              <X />
            </IconButton>
          </div>
          {renderPrimary(false)}
          <button
            type="button"
            data-testid="interactive-toolbar-advanced-toggle"
            aria-expanded={advancedOpen}
            aria-controls={advancedId}
            onClick={() => setAdvancedOpen((value) => !value)}
            className="flex w-full items-center gap-2 border-t border-border/60 pt-3 text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Icon name="settings" size={13} />
            <span className="font-medium">
              模型与参数{warnings.length > 0 ? ` · ${warnings.length}` : ""}
            </span>
            <span className="ml-auto max-w-36 truncate text-2xs text-muted-foreground">
              {backendName ?? "未连接"}
            </span>
            <Icon name={advancedOpen ? "chevUp" : "chevDown"} size={12} />
          </button>
          {/* Keep configuration controls mounted so folding never resets their state or requests. */}
          <section
            id={advancedId}
            data-testid="interactive-toolbar-advanced"
            aria-label="AI 高级设置"
            hidden={!advancedOpen}
            className="flex min-w-0 flex-col gap-3"
          >
            <div className="flex min-w-0 flex-col gap-3">
              <div className="grid min-w-0 grid-cols-2 gap-3">
                <label className="flex min-w-0 flex-col gap-1.5">
                  <span className={FIELD_LABEL_CLASS}>推理引擎</span>
                  <select
                    data-testid="ai-tool-backend-select"
                    aria-label="交互后端"
                    value={canSwitchBackend ? (selectedInteractiveId ?? "") : (backendName ?? "")}
                    disabled={!canSwitchBackend}
                    onChange={(event) => onSelectInteractive?.(event.target.value)}
                    className={cn(SELECT_CLASS, "min-w-0 max-w-full opacity-[0.85]")}
                    title="交互后端"
                  >
                    {canSwitchBackend ? (
                      backendCands.map((backend) => (
                        <option key={backend.id} value={backend.id}>
                          {backend.name}
                        </option>
                      ))
                    ) : (
                      <option
                        value={backendName ?? ""}
                        disabled={!backendName}
                        hidden={!backendName}
                      >
                        {backendName ?? "未绑定 ML 后端"}
                      </option>
                    )}
                  </select>
                </label>
                {showModelSelector && (
                  <label className="flex min-w-0 flex-col gap-1.5">
                    <span className={FIELD_LABEL_CLASS}>模型</span>
                    <select
                      data-testid="ai-tool-model-select"
                      aria-label="模型"
                      value={activeModelId ?? ""}
                      onChange={(event) => onSetActiveModelId?.(event.target.value)}
                      className={cn(SELECT_CLASS, "min-w-0 max-w-full cursor-pointer")}
                      title="模型"
                    >
                      {groupModelsByTask(filteredModels).map(([task, group]) => (
                        <optgroup key={task} label={modelTaskLabel(task)}>
                          {group.map((model) => (
                            <option key={model.id} value={model.id}>
                              {model.display_name || model.id}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  </label>
                )}
                {!showModelSelector && (
                  <div className="flex min-w-0 flex-col gap-1.5">
                    <span className={FIELD_LABEL_CLASS}>模型版本</span>
                    <span className="flex h-8 items-center text-xs">
                      {filteredModels.find((model) => model.id === activeModelId)?.display_name ??
                        capability?.version ??
                        "默认模型"}
                    </span>
                  </div>
                )}
                {hasVariants && (
                  <div className="col-span-2 flex flex-wrap gap-2 [&_select]:h-8 [&_select]:rounded-lg">
                    <VariantSelector
                      compact
                      supportedVariants={variantGroups}
                      variantCombinations={variantCombinations}
                      defaults={variantDefaults}
                      value={variantValue ?? {}}
                      onChange={(next) => onVariantChange?.(next)}
                    />
                  </div>
                )}
              </div>

              {exemplarConfidenceControl}
            </div>

            <p
              className="m-0 border-t border-border/50 pt-2 text-2xs text-muted-foreground"
              data-testid="interactive-capability-diagnostics"
            >
              {capability
                ? `能力：${capability.name}${capability.version ? ` · ${capability.version}` : ""}${capability.protocol_version ? ` · 协议 ${capability.protocol_version}` : ""}`
                : "尚无后端能力信息"}
            </p>

            {warnings.length > 0 && (
              <div
                className="flex flex-wrap items-center gap-2"
                data-testid="ai-tool-capability-warnings"
              >
                {warnings.map((warning) => (
                  <div
                    key={warning.key}
                    className="flex min-w-0 flex-wrap items-start gap-1 rounded-sm bg-status-caution-soft px-1.5 py-0.5 text-2xs leading-[1.4] text-status-caution"
                  >
                    <Icon name="warning" size={11} />
                    <span className="min-w-0 break-words">{warning.message}</span>
                    {warning.fillable && onFillAttribute && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        onClick={() => onFillAttribute(warning.fillable!)}
                        title="把该属性字段补进项目所有启用工具单位"
                      >
                        一键补全
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </ContextToolbar>
  );
}
