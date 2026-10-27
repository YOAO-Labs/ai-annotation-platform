import { CopyableError } from "@/components/ui/CopyableError";
import { normalizeTask } from "@/api/generated/capabilityVocab.gen";
import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { ProgressBar } from "@/components/ui/ProgressBar";
import type { Annotation, AnnotationResponse } from "@/types";
import { filterBoxesByFrame, firstTrackFrame, type FrameFilter } from "./annotationFrameScope";
import type { AttributeField, AttributeSchema } from "@/api/projects";
import type { CapabilityWarning } from "../state/useCapabilityValidation";
import type { WorkbenchAiRequestPresentation } from "../state/useWorkbenchAiRequest";
import type { VideoSelectionCommand } from "../state/videoSelectionCommand";
import { isWorkbenchInputFocused } from "../state/useWorkbenchHotkeys";
import {
  PREDICTION_SOURCE_FILTERS,
  predictionSourceLabel,
  type AiBox,
  type PredictionSourceCounts,
  type PredictionSourceFilter,
  type PredictionSourceVisibility,
} from "../state/transforms";
import { BoxListItem } from "../stage/BoxListItem";
import { aiBoxOnFrame } from "../stage/aiBoxFrames";
import type { RasterMaskRecordStatus } from "../stage/shared/useRasterMaskRecords";
import { displayClassName } from "../stage/colors";
import { AttributeForm, getMissingRequired } from "./AttributeForm";
import { PreannotateConfigForm } from "@/pages/AIPreAnnotate/components/PreannotateConfigForm";
import { workbenchAiScopeLabel } from "../layout/workbenchPanelRegistry";
import { type PreannotateConfig } from "@/pages/AIPreAnnotate/components/usePreannotateConfig";
import {
  AI_PANEL_HEADER_CLASS,
  AI_PANEL_ICON_CLASS,
  AI_PANEL_SECTION_CLASS,
  AI_PANEL_SURFACE_CLASS,
} from "./workbenchAiPanelChrome";

// 卡片化分组头（AI 待审 / 人工 等列表分段头）与筛选卡共用的容器外观。
const SECTION_CARD_CLASS =
  "mb-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-foreground";

interface AIInspectorPanelProps {
  open: boolean;
  /** 受控宽度（仅 open=true 生效；列宽 handle 实际由 WorkbenchLayout 渲染）。 */
  width: number;
  onResize: (w: number) => void;
  /** Increment B · 视频工作台不启用属性快捷键区域（属性数字键仅图片流支持）。 */
  isVideoTask?: boolean;
  /** v0.20.19 · 属性区折叠态 (受控, 走 workbench.layout 持久); 缺省回落组件内会话态。 */
  attrCollapsed?: boolean;
  onToggleAttrCollapsed?: () => void;
  /** v0.20.22 · AI 待审 / 人工两大分组的折叠态 (受控, 走 workbench.layout 持久)。
   *  与 BoxesList 内部的 `collapsedGroups` (人工段内的子组/GroupCard 折叠) 命名区分:
   *  这里控制的是"两大分组头"整体折叠。缺省 = 展开。 */
  aiSectionCollapsed?: boolean;
  onToggleAiSection?: () => void;
  manualSectionCollapsed?: boolean;
  onToggleManualSection?: () => void;
  /** 列宽 handle 的像素边界与双击重置值(由 WorkbenchLayout 的 ResizeHandle 消费;随窗口宽度动态)。 */
  widthMin?: number;
  widthMax?: number;
  widthResetTo?: number;
  taskId?: string | null;
  aiBoxes: AiBox[];
  predictionSourceFilter?: PredictionSourceFilterState;
  userBoxes: Annotation[];
  rasterMaskStatusById?: ReadonlyMap<string, RasterMaskRecordStatus>;
  onRetryRasterMask?: (id: string) => void;
  orphanUserBoxIds?: Set<string>;
  selectedId: string | null;
  selectedIds?: string[];
  /** 与 user 框 IoU > 0.7 的同类 AI 框 id（视觉淡化）。 */
  dimmedAiIds?: Set<string>;
  imageWidth: number | null;
  imageHeight: number | null;
  /** 项目级属性 schema（v0.5.4）。空时不渲染表单。 */
  attributeSchema?: AttributeSchema;
  /** 选中的 AnnotationResponse（含 attributes / class_name），用于属性表单数据源。 */
  selectedAnnotation?: AnnotationResponse | null;
  /** 属性表单提交回调（防抖后触发）。 */
  onUpdateAttributes?: (annotationId: string, next: Record<string, unknown>) => void;
  /**
   * v0.10.20 · I12 多选批量编辑回调; 多选时 AttributeForm.onChange 改走此路径调用 useAnnotationBulkUpdate.
   * 不传时多选只显示 batch banner, 但 onChange 仍按单条 PATCH 走 (退化兼容).
   */
  onBulkUpdateAttributes?: (ids: string[], patch: { attributes?: Record<string, unknown> }) => void;
  hasMorePredictions?: boolean;
  isFetchingMorePredictions?: boolean;
  onFetchMorePredictions?: () => void;
  currentFrameIndex?: number;
  onSeekFrame?: (frameIndex: number) => void;
  /** Shift+click 进入多选；普通 click 单选。 */
  onSelect: (id: string, opts?: { shift?: boolean }) => void;
  onSelectVideoObject?: VideoSelectionCommand;
  onAcceptPrediction: (b: AiBox, attributeOverrides?: Record<string, unknown>) => void;
  onRejectPrediction?: (b: AiBox) => void;
  /** v0.10.8 · I11 · polygon 候选行展示「精修」按钮 → 启动 Mask 编辑器。 */
  onRefinePrediction?: (b: AiBox) => void;
  /** v0.10.9 · 已落库 user polygon 行展示「精修」按钮 → 启动 Mask 编辑器（update mutation）。 */
  onRefineUserPolygon?: (annotationId: string) => void;
  onEditRasterMask?: (annotationId: string) => void;
  onClearSelection: () => void;
  onDeleteUserBox: (id: string) => void;
  onChangeUserBoxClass?: (id: string) => void;
  /** v0.10.5 M4-β · I15 切换 shape 状态位（lock/hidden）。 */
  onToggleUserBoxFlag?: (id: string, flag: "is_locked" | "is_hidden") => void;
  /** v0.6.5 · 任务已锁定（review/completed），属性表单只读。 */
  readOnly?: boolean;
  videoTrackPanel?: React.ReactNode | ((frameFilter: FrameFilter) => React.ReactNode);
  /** v0.13.10 · 分离为同窗口浮窗，由 WorkbenchLayout 负责实际渲染分支。 */
  onDetach?: () => void;
  floating?: boolean;
  /** v0.14.9 · active model 与项目配置的兼容性警告 (非阻断)；空时不渲染。 */
  capabilityWarnings?: CapabilityWarning[];
  /**
   * v0.20.2 · 「采纳后该属性将丢失」警告的一键补全回调: 把 model 自报字段补进项目所有启用工具单位。
   * 仅 warning.fillable 存在时渲染 CTA; 缺省 = 不渲染补全按钮。
   */
  onFillAttribute?: (field: AttributeField) => void;
}

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

interface PredictionSourceFilterState {
  visibility: PredictionSourceVisibility;
  counts: PredictionSourceCounts;
  totalCount: number;
  /** Candidates remaining after source visibility, before the optional frame view. */
  visibleCount?: number;
  onToggle: (source: PredictionSourceFilter, visible: boolean) => void;
}

export function AIInspectorPanel({
  // v0.11.5+ · width/onResize 仍在 props 接口里，但列宽拖拽 handle 已上移到
  // WorkbenchLayout 的 .rightSplit（全高），故此处不再渲染/解构它们。
  open,
  taskId,
  aiBoxes,
  predictionSourceFilter,
  userBoxes,
  orphanUserBoxIds,
  rasterMaskStatusById,
  onRetryRasterMask,
  selectedId,
  selectedIds,
  dimmedAiIds,
  imageWidth,
  imageHeight,
  attributeSchema,
  selectedAnnotation,
  onUpdateAttributes,
  onBulkUpdateAttributes,
  hasMorePredictions,
  isFetchingMorePredictions,
  onFetchMorePredictions,
  currentFrameIndex,
  onSeekFrame,
  onSelect,
  onSelectVideoObject,
  onAcceptPrediction,
  onRejectPrediction,
  onRefinePrediction,
  onRefineUserPolygon,
  onEditRasterMask,
  onClearSelection,
  onDeleteUserBox,
  onChangeUserBoxClass,
  onToggleUserBoxFlag,
  readOnly = false,
  videoTrackPanel,
  onDetach,
  floating = false,
  capabilityWarnings,
  onFillAttribute,
  attrCollapsed: attrCollapsedProp,
  onToggleAttrCollapsed,
  isVideoTask,
  aiSectionCollapsed = false,
  onToggleAiSection,
  manualSectionCollapsed = false,
  onToggleManualSection,
}: AIInspectorPanelProps) {
  const selSet =
    selectedIds && selectedIds.length > 0
      ? new Set(selectedIds)
      : selectedId
        ? new Set([selectedId])
        : new Set<string>();
  const multiCount = selSet.size > 1 ? selSet.size : 0;
  // 底部属性区折叠态（v0.11.28 上下分栏：属性区固定在列表下方，可折叠让出列表空间）。
  // v0.20.19 · 受控优先(走 workbench.layout 持久), 缺省回落组件内会话态(测试/独立使用)。
  const [attrCollapsedLocal, setAttrCollapsedLocal] = useState(false);
  const attrCollapsed = attrCollapsedProp ?? attrCollapsedLocal;
  const toggleAttrCollapsed = onToggleAttrCollapsed ?? (() => setAttrCollapsedLocal((v) => !v));
  const attrMissing =
    selectedAnnotation && attributeSchema
      ? getMissingRequired(
          attributeSchema,
          selectedAnnotation.class_name,
          selectedAnnotation.attributes ?? {},
        )
      : [];
  // Selected AI candidates keep editable attributes separate from persisted annotations.
  // An empty attribute object may still need required project fields before acceptance.
  const selectedAiBox =
    !selectedAnnotation && selSet.size === 1
      ? (aiBoxes.find((b) => selSet.has(b.id)) ?? null)
      : null;
  // v0.18.3 · 候选属性审阅 + 分步采纳: 预览改为可编辑, 改动存本地 state (按 box id 重置),
  // 采纳时经 onAcceptPrediction 的 attributeOverrides 把改后值原子落库 (而非一步全采纳原值)。
  const [editedAiBoxAttrs, setEditedAiBoxAttrs] = useState<Record<string, unknown> | null>(null);
  const editedBoxIdRef = useRef<string | null>(null);
  if (selectedAiBox && selectedAiBox.id !== editedBoxIdRef.current) {
    editedBoxIdRef.current = selectedAiBox.id;
    // A hidden or temporarily absent candidate keeps its draft. A different candidate owns a new one.
    if (editedAiBoxAttrs !== null) setEditedAiBoxAttrs(null);
  }
  const aiBoxAttrs = selectedAiBox
    ? { ...(selectedAiBox.attributes ?? {}), ...(editedAiBoxAttrs ?? {}) }
    : null;
  const aiAttrMissing = selectedAiBox
    ? getMissingRequired(attributeSchema, selectedAiBox.cls, aiBoxAttrs ?? {})
    : [];

  // The selected candidate action and list rows share the same attribute override path.
  // Unedited candidates keep the original undefined override contract.
  // 画布贴框采纳 (SelectionOverlay/BoxRenderer 走 ImageStage.onAcceptPrediction 1-arg 版本)
  // 拿不到该本地 state, 维持采纳原值 (计划内不上提 state)。
  const acceptWithReviewEdits = (box: AiBox, overrides?: Record<string, unknown>) => {
    if (readOnly || (box.id === selectedAiBox?.id && aiAttrMissing.length > 0)) return;
    const shouldAttachReviewEdits =
      box.id === selectedAiBox?.id &&
      editedAiBoxAttrs !== null &&
      Object.keys(editedAiBoxAttrs).length > 0;
    if (shouldAttachReviewEdits) {
      onAcceptPrediction(box, { ...editedAiBoxAttrs, ...(overrides ?? {}) });
    } else {
      onAcceptPrediction(box, overrides);
    }
  };
  const handleCandidateKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (
      event.defaultPrevented ||
      readOnly ||
      !selectedAiBox ||
      isWorkbenchInputFocused(event.target) ||
      event.repeat ||
      event.nativeEvent.isComposing ||
      event.nativeEvent.keyCode === 229 ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      event.shiftKey ||
      (currentFrameIndex !== undefined && !aiBoxOnFrame(selectedAiBox, currentFrameIndex))
    ) {
      return;
    }
    const key = event.key.toLowerCase();
    if (key !== "a" && key !== "d") return;
    if (key === "d" && !onRejectPrediction) return;
    // The surrounding toolbar marker yields background listeners to this draft-aware owner.
    event.preventDefault();
    event.stopPropagation();
    if (key === "a") acceptWithReviewEdits(selectedAiBox);
    else onRejectPrediction?.(selectedAiBox);
  };

  if (!open) {
    return null;
  }

  return (
    <div
      data-workbench-ai-toolbar={selectedAiBox ? "" : undefined}
      data-testid="ai-inspector-panel"
      onKeyDown={handleCandidateKeyDown}
      className={cn(
        "relative flex h-full min-h-0 flex-col overflow-hidden bg-card",
        floating ? "border-l-0" : "border-l border-border",
      )}
    >
      <div className="border-b border-border bg-card px-3.5 py-3">
        <div className="flex items-center justify-between">
          <b className="text-sm">标注详情</b>
          {onDetach && (
            <button
              type="button"
              className="inline-flex size-6 cursor-pointer appearance-none items-center justify-center rounded-sm border border-border bg-background p-0 text-muted-foreground hover:border-brand hover:text-brand"
              onClick={onDetach}
              aria-label="分离为浮窗"
              title="分离为浮窗"
            >
              <Icon name="pictureInPicture2" size={14} />
            </button>
          )}
        </div>
      </div>

      {capabilityWarnings && capabilityWarnings.length > 0 && (
        <div
          className="flex flex-col gap-1 border-b border-border bg-status-caution-soft px-3 py-2"
          data-testid="ai-inspector-capability-warnings"
        >
          {capabilityWarnings.map((w) => (
            <div
              key={w.key}
              className="flex items-start gap-1.5 text-xs leading-[1.4] text-status-caution"
            >
              <Icon name="warning" size={11} />
              <span>{w.message}</span>
              {w.fillable && onFillAttribute && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => onFillAttribute(w.fillable!)}
                  title="把该属性字段补进项目所有启用工具单位"
                >
                  一键补全
                </Button>
              )}
            </div>
          ))}
        </div>
      )}

      {multiCount > 0 && (
        <div className="flex items-center justify-between border-b border-border bg-brand/10 px-3.5 py-1.5 text-xs text-brand">
          <span>
            已选 <b>{multiCount}</b> 个 user 框
          </span>
          <button
            data-workbench-video-tool-command={onSelectVideoObject ? "" : undefined}
            onClick={onSelectVideoObject ? () => onSelectVideoObject(null) : onClearSelection}
            className="cursor-pointer appearance-none rounded-[3px] border border-border bg-transparent px-1.5 py-px text-2xs text-muted-foreground"
          >
            清除
          </button>
        </div>
      )}

      <BoxesList
        aiBoxes={aiBoxes}
        predictionSourceFilter={predictionSourceFilter}
        userBoxes={userBoxes}
        orphanUserBoxIds={orphanUserBoxIds}
        rasterMaskStatusById={rasterMaskStatusById}
        onRetryRasterMask={onRetryRasterMask}
        selSet={selSet}
        dimmedAiIds={dimmedAiIds}
        imageWidth={imageWidth}
        imageHeight={imageHeight}
        hasMore={hasMorePredictions}
        isFetchingMore={isFetchingMorePredictions}
        onFetchMore={onFetchMorePredictions}
        taskId={taskId}
        currentFrameIndex={currentFrameIndex}
        onSeekFrame={onSeekFrame}
        onSelect={onSelect}
        onSelectVideoObject={onSelectVideoObject}
        onAcceptPrediction={acceptWithReviewEdits}
        onRejectPrediction={onRejectPrediction}
        onRefinePrediction={onRefinePrediction}
        onRefineUserPolygon={onRefineUserPolygon}
        onEditRasterMask={onEditRasterMask}
        readOnly={readOnly}
        onDeleteUserBox={onDeleteUserBox}
        onChangeUserBoxClass={onChangeUserBoxClass}
        onToggleUserBoxFlag={onToggleUserBoxFlag}
        videoTrackPanel={videoTrackPanel}
        aiSectionCollapsed={aiSectionCollapsed}
        onToggleAiSection={onToggleAiSection}
        manualSectionCollapsed={manualSectionCollapsed}
        onToggleManualSection={onToggleManualSection}
      />
      {/* 视频任务的属性由 VideoTrackPanel 内的两层 VideoAttributesEditor 承载，此处仅图片任务渲染。 */}
      {!videoTrackPanel && selectedAnnotation && attributeSchema && onUpdateAttributes && (
        <div className="flex max-h-[45%] flex-[0_0_auto] flex-col border-t border-border bg-card">
          <button
            type="button"
            className="flex w-full cursor-pointer appearance-none items-center gap-1.5 border-0 bg-transparent px-3.5 py-2 text-left text-xs font-semibold text-foreground hover:bg-muted"
            onClick={toggleAttrCollapsed}
            aria-expanded={!attrCollapsed}
            title={attrCollapsed ? "展开属性" : "折叠属性"}
          >
            <Icon name={attrCollapsed ? "chevRight" : "chevDown"} size={13} />
            <span>属性</span>
            {attrMissing.length > 0 && (
              <span className="text-xs font-normal text-status-caution">
                · {attrMissing.length} 项必填未填
              </span>
            )}
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              {displayClassName(selectedAnnotation.class_name)}
            </span>
          </button>
          {!attrCollapsed && (
            <div className="overflow-y-auto pb-1">
              <AttributeForm
                schema={attributeSchema}
                className={selectedAnnotation.class_name}
                attributes={selectedAnnotation.attributes ?? {}}
                attributesMeta={selectedAnnotation.attributes_meta}
                // v0.10.20 · I12 多选批量: 有 onBulkUpdateAttributes 且选中 >1 时 fan-out, 否则单条 PATCH.
                onChange={(next) => {
                  if (multiCount > 1 && onBulkUpdateAttributes) {
                    onBulkUpdateAttributes(Array.from(selSet), { attributes: next });
                  } else {
                    onUpdateAttributes(selectedAnnotation.id, next);
                  }
                }}
                batchCount={multiCount > 1 ? multiCount : undefined}
                readOnly={readOnly}
                hideHeading
                // Increment B · 图片选中标注的数字属性键改为显式区域 opt-in。
                shortcutRegion={!isVideoTask}
              />
            </div>
          )}
        </div>
      )}

      {/* v0.18.0 · 候选属性预览; v0.18.3 · 可编辑 + 分步采纳: 审阅多阶段预标产出的属性, 改后再采纳。 */}
      {aiBoxAttrs && selectedAiBox && attributeSchema && (
        <div className="flex max-h-[45%] flex-[0_0_auto] flex-col border-t border-border bg-card">
          <button
            type="button"
            className="flex w-full cursor-pointer appearance-none items-center gap-1.5 border-0 bg-transparent px-3.5 py-2 text-left text-xs font-semibold text-foreground hover:bg-muted"
            onClick={toggleAttrCollapsed}
            aria-expanded={!attrCollapsed}
            title={attrCollapsed ? "展开属性审阅" : "折叠属性审阅"}
          >
            <Icon name={attrCollapsed ? "chevRight" : "chevDown"} size={13} />
            <span>属性审阅</span>
            <span className="text-xs font-normal text-status-info">· 候选</span>
            {editedAiBoxAttrs && Object.keys(editedAiBoxAttrs).length > 0 && (
              <span className="text-xs font-normal text-status-caution">· 已改动</span>
            )}
            {aiAttrMissing.length > 0 && (
              <span className="text-xs font-normal text-status-caution">
                · {aiAttrMissing.length} 项必填未填
              </span>
            )}
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              {displayClassName(selectedAiBox.cls)}
            </span>
          </button>
          {!attrCollapsed && (
            <div className="overflow-y-auto pb-1">
              <AttributeForm
                key={selectedAiBox.id}
                schema={attributeSchema}
                className={selectedAiBox.cls}
                attributes={aiBoxAttrs}
                onChange={(next) => setEditedAiBoxAttrs(next)}
                readOnly={readOnly}
                immediate
                hideHeading
              />
              {!readOnly && (
                <div className="px-3.5 pb-2 pt-1 text-2xs leading-normal text-muted-foreground">
                  改动将随采纳一并落库。
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {selectedAiBox && (
        <div
          data-testid="ai-candidate-review-actions"
          className="shrink-0 border-t border-border bg-card px-3.5 py-2.5"
        >
          <div className="mb-2 text-xs text-muted-foreground">
            当前候选 ·{" "}
            <span className="text-foreground">{displayClassName(selectedAiBox.cls)}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button
              type="button"
              variant="primary"
              size="sm"
              data-testid="ai-candidate-accept"
              disabled={readOnly || aiAttrMissing.length > 0}
              title={aiAttrMissing.length > 0 ? "请先补齐候选必填属性" : "接受当前候选及属性改动"}
              onClick={() => acceptWithReviewEdits(selectedAiBox)}
            >
              <Icon name="check" size={12} />
              接受候选
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="ai-candidate-reject"
              disabled={readOnly || !onRejectPrediction}
              onClick={() => onRejectPrediction?.(selectedAiBox)}
            >
              <Icon name="x" size={12} />
              拒绝候选
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

interface AIPredictionPopoverProps {
  aiModel: string;
  aiRunning: boolean;
  aiBoxCount: number;
  request?: WorkbenchAiRequestPresentation;
  onRetryRequest?: () => void;
  onCancelRequest?: () => void;
  onReviewCandidates?: () => void;
  // v0.21.10 · 视频任务时面板做单帧检测 (方案 a); 留一句指引告诉找整段追踪的用户去哪跑。
  isVideoTask?: boolean;
  confThreshold: number;
  aiTakeoverRate: number;
  onClose: () => void;
  onRunAi: () => void;
  // v0.18.28 · 项目存了编排 (v0.18.27) 时多出「运行当前题（按项目编排）」入口。
  // popover 仍是执行器、非编排编辑器: 编排在 /ai-pre 定义, 这里只把它跑当前一图。
  hasProjectPipeline?: boolean;
  projectPipelineStageCount?: number;
  // claude[bot] P1 #5 · 编排可执行 (引用的 backend 都还在); false 时按钮禁用 + 提示。
  projectPipelineRunnable?: boolean;
  pipelineMissingBackendCount?: number;
  onRunPipeline?: () => void;
  onAcceptAll: () => void;
  /** Count of loaded candidates that remain eligible for the current-task batch action. */
  batchEligibleCount?: number;
  onSetConfThreshold: (v: number) => void;
  // v0.10.23 · 设计 B · 文本输入段下沉到 InteractiveToolBar; popover 不再承载 SAM 文本提示控件.
  taskAiCost?: number;
  taskAiAvgMs?: number | null;
  taskAiPredictionCount?: number;
  // 配置区共享状态 (任务类型 / 模型任务 / 类别白名单 / variant / 参数 / prompt); 与批量页同一 hook.
  cfg: PreannotateConfig;
  runUnavailableReason?: string;
  // 当前选中 variant 是否已 warm (源自 isVariantHot: 单一 hot map, 持久化到 sessionStorage).
  // false → 按钮显示"加载模型中…"给用户冷启动心理预期.
  isVariantWarm?: boolean;
  // 多 backend: 项目绑了 >1 个后端时, 配置区顶部出 backend 选择器 (≤1 项时自动隐藏).
  backends?: Array<{ id: string; name: string }>;
  selectedBackendId?: string | null;
  onSelectBackend?: (id: string | null) => void;
  projectMlBackendId?: string | null;
}

function requestInputSummary(input: Record<string, unknown>): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  if (typeof input.prompt === "string" && input.prompt.trim()) {
    rows.push(["提示词", input.prompt]);
  }
  const taskLabels: Record<string, string> = {
    detect: "检测",
    segment: "分割",
    pose: "关键点",
    obb: "旋转框检测",
    ocr: "文字识别",
    doc_layout: "文档版面",
  };
  const task = input.task ?? input.task_type;
  if (typeof task === "string") {
    const canonicalTask = normalizeTask(task);
    rows.push(["任务类型", taskLabels[canonicalTask] ?? canonicalTask]);
  }
  const variants = input.model_variants;
  if (variants && typeof variants === "object" && !Array.isArray(variants)) {
    const names: Record<string, string> = {
      size: "尺寸",
      series: "系列",
      version: "版本",
      lang: "语言",
      precision: "精度",
    };
    const values = Object.entries(variants)
      .filter(([, value]) => typeof value === "string" || typeof value === "number")
      .map(([key, value]) => `${names[key] ?? key}：${String(value)}`);
    if (values.length > 0) rows.push(["权重", values.join(" · ")]);
  }
  const outputLabels: Record<string, string> = {
    box: "框",
    mask: "掩膜",
    both: "框和掩膜",
  };
  if (typeof input.output_mode === "string" && outputLabels[input.output_mode]) {
    rows.push(["输出", outputLabels[input.output_mode]]);
  }
  if (Array.isArray(input.class_filter) && input.class_filter.length > 0) {
    rows.push(["类别筛选", `${input.class_filter.length} 个模型类别`]);
  }
  if (Array.isArray(input.pipeline_stages)) {
    rows.push(["项目编排", `${input.pipeline_stages.length} 个阶段`]);
  }
  return rows;
}

export function AIPredictionPopover({
  aiModel,
  aiRunning,
  aiBoxCount,
  request,
  onRetryRequest,
  onCancelRequest,
  onReviewCandidates,
  isVideoTask,
  confThreshold,
  aiTakeoverRate,
  onClose,
  onRunAi,
  hasProjectPipeline,
  projectPipelineStageCount,
  projectPipelineRunnable = true,
  pipelineMissingBackendCount = 0,
  onRunPipeline,
  onAcceptAll,
  batchEligibleCount: batchEligibleCountProp,
  onSetConfThreshold,
  taskAiCost,
  taskAiAvgMs,
  taskAiPredictionCount,
  cfg,
  runUnavailableReason,
  isVariantWarm: isVariantWarmProp,
  backends,
  selectedBackendId,
  onSelectBackend,
  projectMlBackendId,
}: AIPredictionPopoverProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const aiPanelTitle = `${workbenchAiScopeLabel(!!isVideoTask)} AI`;
  const advancedId = useId();
  const running = request ? request.status === "running" : aiRunning;
  const batchEligibleCount = batchEligibleCountProp ?? aiBoxCount;
  const phase = running
    ? "running"
    : request?.status === "error"
      ? "error"
      : aiBoxCount > 0 || batchEligibleCount > 0
        ? "review"
        : "idle";
  const phaseLabels = {
    idle: "准备运行",
    running: "推理中",
    review: "待审阅",
    error: "请求失败",
  };
  const summary = request?.summary;
  const progressPct =
    request?.progressPct != null && Number.isFinite(request.progressPct)
      ? Math.min(100, Math.max(0, request.progressPct))
      : null;
  const canCancel = !!request?.canCancel && !!onCancelRequest;
  const canRetry = !!request?.canRetry && !!onRetryRequest;
  const needsPrompt = !!cfg.backendId && !cfg.isDocMode && !cfg.isGeometricBackend;

  // Only legacy callers infer a cold start from the current configuration. Owned requests use
  // their immutable summary and reported progress, even when the next configuration is edited.
  const coldStarting = !request && running && isVariantWarmProp === false;
  const [coldElapsedSec, setColdElapsedSec] = useState(0);
  useEffect(() => {
    if (!coldStarting) {
      setColdElapsedSec(0);
      return;
    }
    const t0 = Date.now();
    const id = window.setInterval(() => {
      setColdElapsedSec(Math.floor((Date.now() - t0) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, [coldStarting]);

  return (
    <div
      data-testid="ai-prediction-popover"
      data-workbench-ai-toolbar
      className={cn(AI_PANEL_SURFACE_CLASS, "flex h-full min-h-0 flex-col rounded-none border-0")}
    >
      <div className={AI_PANEL_HEADER_CLASS}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className={AI_PANEL_ICON_CLASS}>
              <Icon name="bot" size={14} />
            </span>
            <b className="whitespace-nowrap text-sm">{aiPanelTitle}</b>
          </div>
          <div className="flex items-center gap-1.5">
            <Badge variant={phase === "error" ? "danger" : "ai"} className="gap-1 text-2xs">
              {running && <Icon name="loader2" size={10} />}
              {phaseLabels[phase]}
            </Badge>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
              aria-label={`关闭${aiPanelTitle}`}
              title={`关闭${aiPanelTitle}`}
              className="px-1.5 py-0.5"
            >
              <Icon name="x" size={12} />
            </Button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <section
          className={AI_PANEL_SECTION_CLASS}
          data-testid="ai-prediction-phase"
          data-phase={phase}
        >
          {phase === "idle" ? (
            <div className="mb-2.5">
              <div className="mb-1 text-xs text-muted-foreground">
                待运行模型：<span className="font-medium text-foreground">{aiModel}</span>
              </div>
              {request?.status === "cancelled" && (
                <p role="status" className="mb-2 text-2xs text-muted-foreground">
                  本次请求已取消，可以调整输入后重新运行。
                </p>
              )}
              {request?.status === "completed" && aiBoxCount === 0 && (
                <p role="status" className="mb-2 text-2xs text-muted-foreground">
                  本次运行已完成，当前没有待审候选。
                </p>
              )}
              <div hidden={advancedOpen}>
                {needsPrompt ? (
                  <label className="mb-2 flex flex-col gap-1 text-xs text-foreground">
                    提示词
                    <textarea
                      rows={2}
                      data-testid="ai-prediction-prompt"
                      value={cfg.prompt}
                      onChange={(event) => cfg.setPrompt(event.target.value)}
                      placeholder="例如 car, person"
                      className="w-full resize-y rounded-sm border border-border bg-card px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground"
                    />
                  </label>
                ) : (
                  <p className="my-1 text-2xs leading-normal text-muted-foreground">
                    {cfg.backendId
                      ? "使用当前模型与输入配置，可在高级配置中调整。"
                      : "请在高级配置中选择已接入的 ML 后端。"}
                  </p>
                )}
              </div>
              {cfg.sourceBatchableWarning && (
                <p role="alert" className="my-1 text-2xs text-status-caution">
                  {cfg.sourceBatchableWarning}
                </p>
              )}
            </div>
          ) : (
            <>
              {summary ? (
                <div className="mb-2.5" data-testid="ai-request-summary">
                  <div className="mb-1.5 text-xs font-semibold text-foreground">本次请求</div>
                  <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-1 text-2xs leading-normal">
                    <dt className="text-muted-foreground">模型</dt>
                    <dd className="m-0 break-words text-foreground">{summary.modelName}</dd>
                    <dt className="text-muted-foreground">后端</dt>
                    <dd className="m-0 break-words text-foreground">{summary.backendName}</dd>
                    <dt className="text-muted-foreground">任务</dt>
                    <dd className="m-0 break-all text-foreground">
                      {summary.taskLabel ?? summary.taskId}
                    </dd>
                    <dt className="text-muted-foreground">范围</dt>
                    <dd className="m-0 text-foreground">
                      {summary.frameIndex == null ? "当前图片" : `当前帧 F${summary.frameIndex}`}
                    </dd>
                    {requestInputSummary(summary.input).map(([label, value]) => (
                      <div key={label} className="contents">
                        <dt className="text-muted-foreground">{label}</dt>
                        <dd className="m-0 break-words text-foreground">{value}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ) : (
                <p className="mb-2.5 text-2xs text-muted-foreground">
                  {phase === "review"
                    ? "请选择候选，核对类别和属性后决定是否接受。"
                    : "本次请求摘要不可用。"}
                </p>
              )}
              {phase === "running" && (
                <div className="mb-2.5" data-testid="ai-request-progress">
                  {progressPct == null ? (
                    <p role="status" className="my-1 text-2xs text-muted-foreground">
                      {coldStarting
                        ? `正在加载模型，已等待 ${coldElapsedSec} 秒`
                        : "推理正在进行，尚无可用进度。"}
                    </p>
                  ) : (
                    <div
                      role="progressbar"
                      aria-label="本次请求进度"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={progressPct}
                    >
                      <div className="mb-1 text-right text-2xs tabular-nums text-muted-foreground">
                        {progressPct.toFixed(0)}%
                      </div>
                      <ProgressBar value={progressPct} />
                    </div>
                  )}
                  <p className="my-1 text-2xs leading-normal text-muted-foreground">
                    隐藏或移动面板不影响本次运行。
                  </p>
                </div>
              )}
              {request?.error && (
                <div className="mb-2.5" data-testid="ai-request-error">
                  <p role="alert" className="my-1 break-words text-xs text-status-danger">
                    <CopyableError message={request.error} />
                  </p>
                  {phase === "running" && canRetry && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      data-testid="ai-request-retry-poll"
                      onClick={onRetryRequest}
                    >
                      <Icon name="rotate-ccw" size={11} />
                      重试状态查询
                    </Button>
                  )}
                </div>
              )}
              {phase === "review" && (
                <p className="mb-2 text-xs text-foreground">
                  <span className="font-semibold tabular-nums">{aiBoxCount}</span> 个候选待审阅
                  {batchEligibleCount !== aiBoxCount && (
                    <span className="text-muted-foreground">
                      ，{batchEligibleCount} 个已加载候选可批量采纳
                    </span>
                  )}
                </p>
              )}
            </>
          )}

          {phase === "running" ? (
            <Button
              type="button"
              variant={canCancel ? "default" : "ghost"}
              size="sm"
              data-testid="ai-prediction-primary-action"
              disabled={!canCancel || request?.cancelling}
              onClick={onCancelRequest}
              className="w-full"
            >
              <Icon name={canCancel && !request?.cancelling ? "x" : "loader2"} size={12} />
              {request?.cancelling ? "正在取消本次请求" : canCancel ? "取消本次请求" : "推理进行中"}
            </Button>
          ) : (
            <Button
              type="button"
              variant="primary"
              size="sm"
              data-testid="ai-prediction-primary-action"
              disabled={
                phase === "idle"
                  ? !!runUnavailableReason || !cfg.configReady
                  : phase === "error"
                    ? !canRetry
                    : !onReviewCandidates
              }
              title={phase === "idle" ? runUnavailableReason : undefined}
              onClick={
                phase === "idle" ? onRunAi : phase === "error" ? onRetryRequest : onReviewCandidates
              }
              className="w-full"
            >
              <Icon
                name={phase === "idle" ? "wandSparkles" : phase === "error" ? "rotate-ccw" : "list"}
                size={12}
              />
              {phase === "idle"
                ? isVideoTask
                  ? "运行当前帧"
                  : "运行当前图片"
                : phase === "error"
                  ? request?.retryCreatesNewRequest
                    ? "按原配置重新运行"
                    : "重试本次请求"
                  : "审阅候选"}
            </Button>
          )}

          {isVideoTask && (
            <p className="mb-0 mt-2 text-2xs leading-normal text-muted-foreground">
              仅对当前帧做单帧检测。整段目标可用 Ctrl+B 种子追踪，或到「AI
              预标」批量页按整段序列运行。
            </p>
          )}
        </section>

        <div className="border-b border-border px-3.5 py-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="ai-prediction-advanced-toggle"
            aria-expanded={advancedOpen}
            aria-controls={advancedId}
            onClick={() => setAdvancedOpen((open) => !open)}
            className="w-full justify-between"
          >
            <span>{phase === "idle" ? "高级配置" : "高级配置 · 下一轮"}</span>
            <Icon name={advancedOpen ? "chevUp" : "chevDown"} size={12} />
          </Button>
        </div>

        {/* One persistent form instance retains presets and drafts across display phases. */}
        <div id={advancedId} data-testid="ai-prediction-advanced" hidden={!advancedOpen}>
          <div className={AI_PANEL_SECTION_CLASS}>
            <div className="mb-2 text-xs font-semibold text-foreground">
              {phase === "idle" ? "运行输入" : "下一轮配置"}
            </div>
            {phase !== "idle" && (
              <p className="mb-2 text-2xs leading-normal text-muted-foreground">
                这里的改动用于下一轮，不会改变上方本次请求或重试输入。
              </p>
            )}
            <PreannotateConfigForm
              cfg={cfg}
              backends={backends}
              selectedBackendId={selectedBackendId}
              onSelectBackend={onSelectBackend}
              projectMlBackendId={projectMlBackendId}
              backendSelectorLabel={phase === "idle" ? "本次后端" : "下一轮后端"}
            />
            {phase !== "idle" && (
              <Button
                type="button"
                size="sm"
                data-testid="ai-prediction-run-next"
                onClick={onRunAi}
                disabled={running || !!runUnavailableReason || !cfg.configReady}
                title={runUnavailableReason}
                className="mt-2 w-full"
              >
                <Icon name="wandSparkles" size={12} />
                运行下一轮
              </Button>
            )}
            {!isVideoTask && hasProjectPipeline && onRunPipeline && (
              <Button
                type="button"
                size="sm"
                data-testid="ai-prediction-run-pipeline"
                onClick={onRunPipeline}
                disabled={running || !projectPipelineRunnable}
                className="mt-2 w-full"
                title={
                  projectPipelineRunnable
                    ? "按项目已保存的多阶段编排，对当前图片运行完整流程"
                    : `编排引用的 ${pipelineMissingBackendCount} 个后端不可用，请检查后端启用、连接和能力状态`
                }
              >
                <Icon name="layers" size={12} />
                {projectPipelineRunnable
                  ? `运行当前图片（项目编排 · ${projectPipelineStageCount} 阶段）`
                  : `编排引用 ${pipelineMissingBackendCount} 个后端不可用`}
              </Button>
            )}
          </div>

          <div className={AI_PANEL_SECTION_CLASS}>
            <FilterGroup
              label="候选筛选与批量采纳"
              role="heading"
              aria-level={3}
              compact
              className="mb-1.5"
            />
            <div className="mb-1 flex items-baseline justify-between text-xs">
              <span className="text-muted-foreground">置信度阈值</span>
              <span className="rounded-sm bg-status-info-soft px-1.5 text-xs font-semibold tabular-nums text-status-info">
                {(confThreshold * 100).toFixed(0)}%
              </span>
            </div>
            <p className="mb-1.5 text-2xs leading-normal text-muted-foreground">
              低于阈值的候选会隐藏，不纳入批量采纳。可拖动滑块，或使用工具栏 <kbd>[</kbd> /{" "}
              <kbd>]</kbd>； 滚轮每次 5%，按住 Shift 为 10%。
            </p>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={confThreshold}
              onChange={(event) =>
                onSetConfThreshold(Number(parseFloat(event.target.value).toFixed(2)))
              }
              onWheel={(event) => {
                event.preventDefault();
                const step = event.shiftKey ? 0.1 : 0.05;
                const next = Math.min(
                  1,
                  Math.max(0, confThreshold + (event.deltaY < 0 ? step : -step)),
                );
                onSetConfThreshold(Number(next.toFixed(2)));
              }}
              className="my-0.5 w-full cursor-pointer accent-brand"
              aria-label="置信度阈值"
              data-testid="ai-threshold-display"
            />
            <p
              data-testid="ai-prediction-bulk-scope"
              className="mb-2 text-2xs leading-normal text-muted-foreground"
            >
              批量采纳沿用当前题已加载、符合筛选的候选范围（{batchEligibleCount}{" "}
              个），跳过与人工标注重复的候选。
              {isVideoTask && "视频范围可能包括其他帧。"}
            </p>
            <Button
              type="button"
              size="sm"
              data-testid="ai-prediction-accept-all"
              onClick={onAcceptAll}
              disabled={batchEligibleCount === 0}
              className="w-full"
              title={`采纳当前题已加载的 ${batchEligibleCount} 个候选`}
            >
              <Icon name="check" size={12} />
              采纳已加载候选（{batchEligibleCount}）
            </Button>
          </div>

          <div className={AI_PANEL_SECTION_CLASS}>
            <div className="mb-1.5 text-xs text-muted-foreground">本题效率</div>
            <div className="mb-1 flex justify-between text-xs">
              <span>AI 接管率</span>
              <span className="font-semibold tabular-nums text-status-info">{aiTakeoverRate}%</span>
            </div>
            <ProgressBar value={aiTakeoverRate} color="var(--sc-chart-4)" />
            {taskAiPredictionCount != null && taskAiPredictionCount > 0 && (
              <div
                data-testid="task-ai-cost"
                className="mt-1.5 flex flex-wrap justify-between gap-2.5 text-xs text-muted-foreground"
              >
                <span>本题</span>
                <span className="tabular-nums text-foreground">
                  {taskAiCost != null && taskAiCost > 0 ? `¥${taskAiCost.toFixed(4)}` : "¥0"}
                  {taskAiAvgMs != null && (
                    <>
                      <span className="mx-1 text-muted-foreground">·</span>
                      {taskAiAvgMs}ms
                    </>
                  )}
                  <span className="ml-1 text-muted-foreground">({taskAiPredictionCount} 次)</span>
                </span>
              </div>
            )}
          </div>

          {summary && (
            <details className={AI_PANEL_SECTION_CLASS} data-testid="ai-request-diagnostics">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                本次请求诊断
              </summary>
              <p className="my-1 break-all text-2xs text-muted-foreground">
                项目 {summary.projectId} · 任务 {summary.taskId}
              </p>
              <pre className="m-0 max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-card p-2 font-mono text-2xs text-muted-foreground">
                {JSON.stringify(summary.input, null, 2)}
              </pre>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

// ── 虚拟化合并列表 ─────────────────────────────────────────────────────────
type Row =
  | { kind: "ai"; box: AiBox; key: string }
  | {
      kind: "frameFilter";
      filter: FrameFilter;
      key: string;
      onFilterChange: (filter: FrameFilter) => void;
    }
  | { kind: "sourceFilter"; filter: PredictionSourceFilterState; key: string }
  | {
      kind: "header";
      count: number;
      totalCount: number;
      key: string;
      label: string;
      /** v0.20.22 · header 带上分组身份 + 折叠态 + 点击回调, 存在 onToggle 时 header 渲染为可点 button。 */
      sectionKey?: "ai" | "manual";
      collapsed?: boolean;
      onToggle?: () => void;
    }
  | { kind: "videoTracks"; key: string }
  /** v0.20.9 · 父子标注: depth=1 的行是子框, 在其父框行下方缩进渲染。 */
  | { kind: "user"; box: Annotation; key: string; depth?: number };

function FrameFilterTabs({
  value,
  onChange,
}: {
  value: FrameFilter;
  onChange: (filter: FrameFilter) => void;
}) {
  const options: Array<{ value: FrameFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "current", label: "当前帧" },
  ];
  return (
    <div
      aria-label="帧过滤"
      className="grid grid-cols-2 overflow-hidden rounded-md border border-border bg-background"
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <FilterToggle
            key={option.value}
            compact
            active={active}
            onClick={() => onChange(option.value)}
            className="h-6 rounded-none border-0 py-0 text-xs"
          >
            {option.label}
          </FilterToggle>
        );
      })}
    </div>
  );
}

function PredictionSourceFilterCard({ filter }: { filter: PredictionSourceFilterState }) {
  return (
    <FilterGroup
      label="来源"
      compact
      className="mb-1.5 grid grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5"
      aria-label="预测来源筛选"
    >
      <div className="flex min-w-0 items-center justify-end gap-1.5">
        {PREDICTION_SOURCE_FILTERS.map((source) => {
          const checked = filter.visibility[source];
          const count = filter.counts[source];
          const label = predictionSourceLabel(source);
          const isImport = source === "external_import";
          return (
            <label
              key={source}
              className={cn(
                "flex min-w-0 cursor-pointer items-center gap-1 whitespace-nowrap rounded-md border border-border bg-muted px-2 py-1 text-xs text-muted-foreground",
                checked &&
                  !isImport &&
                  "border-status-info/40 bg-status-info-soft text-status-info",
                checked &&
                  isImport &&
                  "border-status-caution/40 bg-status-caution-soft text-status-caution",
              )}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={count === 0}
                onChange={(e) => filter.onToggle(source, e.currentTarget.checked)}
                className="size-3 accent-brand"
              />
              <Icon name={source === "ml_backend" ? "sparkle" : "upload"} size={11} />
              <span>{label}</span>
              <span className="mono text-2xs text-inherit">{count}</span>
            </label>
          );
        })}
      </div>
    </FilterGroup>
  );
}

interface BoxesListProps {
  aiBoxes: AiBox[];
  predictionSourceFilter?: PredictionSourceFilterState;
  userBoxes: Annotation[];
  orphanUserBoxIds?: Set<string>;
  rasterMaskStatusById?: ReadonlyMap<string, RasterMaskRecordStatus>;
  onRetryRasterMask?: (id: string) => void;
  selSet: Set<string>;
  dimmedAiIds?: Set<string>;
  imageWidth: number | null;
  imageHeight: number | null;
  hasMore?: boolean;
  isFetchingMore?: boolean;
  onFetchMore?: () => void;
  taskId?: string | null;
  currentFrameIndex?: number;
  onSelect: (id: string, opts?: { shift?: boolean }) => void;
  onSelectVideoObject?: VideoSelectionCommand;
  onAcceptPrediction: (b: AiBox, attributeOverrides?: Record<string, unknown>) => void;
  onRejectPrediction?: (b: AiBox) => void;
  /** v0.10.8 · I11 · 仅 polygon 候选会展示「精修」按钮，由 WorkbenchShell 注入 handleRefinePrediction。 */
  onRefinePrediction?: (b: AiBox) => void;
  /** v0.10.9 · 已落库 user polygon 行的「精修」按钮，update mutation 替换 geometry。 */
  onRefineUserPolygon?: (annotationId: string) => void;
  onEditRasterMask?: (annotationId: string) => void;
  readOnly?: boolean;
  onDeleteUserBox: (id: string) => void;
  onChangeUserBoxClass?: (id: string) => void;
  /** v0.10.5 M4-β · I15 切换 shape 状态位（lock/hidden）。 */
  onToggleUserBoxFlag?: (id: string, flag: "is_locked" | "is_hidden") => void;
  onSeekFrame?: (frameIndex: number) => void;
  videoTrackPanel?: React.ReactNode | ((frameFilter: FrameFilter) => React.ReactNode);
  /** v0.20.22 · AI 待审 / 人工两大分组头折叠 (透传自 AIInspectorPanel;
   *  与内部 `collapsedGroups` 子组折叠区分, 独立跨设备持久)。 */
  aiSectionCollapsed?: boolean;
  onToggleAiSection?: () => void;
  manualSectionCollapsed?: boolean;
  onToggleManualSection?: () => void;
}

function BoxesList({
  aiBoxes,
  predictionSourceFilter,
  userBoxes,
  orphanUserBoxIds,
  rasterMaskStatusById,
  onRetryRasterMask,
  selSet,
  dimmedAiIds,
  imageWidth,
  imageHeight,
  hasMore,
  isFetchingMore,
  onFetchMore,
  taskId,
  currentFrameIndex,
  onSeekFrame,
  onSelect,
  onSelectVideoObject,
  onAcceptPrediction,
  onRejectPrediction,
  onRefinePrediction,
  onRefineUserPolygon,
  onEditRasterMask,
  readOnly = false,
  onDeleteUserBox,
  onChangeUserBoxClass,
  onToggleUserBoxFlag,
  videoTrackPanel,
  aiSectionCollapsed = false,
  onToggleAiSection,
  manualSectionCollapsed = false,
  onToggleManualSection,
}: BoxesListProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  // 视频默认聚焦「当前帧」,避免一上来在「全部」视图里跨帧误操作;图片端 frameFilter 不显示
  // 且 filterBoxesByFrame 在 currentFrameIndex 为 undefined 时回落全部,故对图片无影响。
  const [frameFilter, setFrameFilter] = useState<FrameFilter>("current");
  useEffect(() => {
    setFrameFilter("current");
  }, [taskId]);
  const showFrameFilter = typeof currentFrameIndex === "number";
  const resolvedVideoTrackPanel = useMemo(
    () => (typeof videoTrackPanel === "function" ? videoTrackPanel(frameFilter) : videoTrackPanel),
    [frameFilter, videoTrackPanel],
  );

  const filteredAiBoxes = useMemo(
    () => filterBoxesByFrame(aiBoxes, currentFrameIndex, frameFilter),
    [aiBoxes, currentFrameIndex, frameFilter],
  );
  const filteredUserBoxes = useMemo(
    () => filterBoxesByFrame(userBoxes, currentFrameIndex, frameFilter),
    [userBoxes, currentFrameIndex, frameFilter],
  );

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    const aiReviewableCount = predictionSourceFilter?.totalCount ?? aiBoxes.length;
    const aiVisibleCount = predictionSourceFilter?.visibleCount ?? aiBoxes.length;
    if (
      showFrameFilter &&
      (aiReviewableCount > 0 || userBoxes.length > 0 || resolvedVideoTrackPanel)
    ) {
      out.push({
        kind: "frameFilter",
        key: "frame-filter",
        filter: frameFilter,
        onFilterChange: setFrameFilter,
      });
    }
    const hasKnownPredictionSources = predictionSourceFilter
      ? PREDICTION_SOURCE_FILTERS.some((source) => predictionSourceFilter.counts[source] > 0)
      : false;
    out.push({
      kind: "header",
      label: "AI 待审",
      count: filteredAiBoxes.length,
      totalCount: aiVisibleCount,
      key: "ai-header",
      sectionKey: "ai",
      collapsed: aiSectionCollapsed,
      onToggle: onToggleAiSection,
    });
    // 分组标题常驻；空分组仍显示 0，成员行与来源筛选只在有数据且展开时渲染。
    if (!aiSectionCollapsed && aiReviewableCount > 0) {
      if (predictionSourceFilter && hasKnownPredictionSources) {
        out.push({
          kind: "sourceFilter",
          key: "prediction-source-filter",
          filter: predictionSourceFilter,
        });
      }
      filteredAiBoxes.forEach((b) => out.push({ kind: "ai", box: b, key: `ai-${b.id}` }));
    }
    out.push({
      kind: "header",
      label: "人工",
      count: filteredUserBoxes.length,
      totalCount: userBoxes.length,
      key: "user-header",
      sectionKey: "manual",
      collapsed: manualSectionCollapsed,
      onToggle: onToggleManualSection,
    });
    // v0.20.22 · 人工分组整体收起时跳过所有成员行 (含 GroupCard/子组), 头 + 计数仍显示。
    if (!manualSectionCollapsed) {
      // v0.20.9 · 父子标注: 建 parent → children 映射。子框从顶层迭代中剔除, 改在父框行下方
      // 缩进渲染 (depth=1)。parent 缩进是唯一主结构 (v0.21.3 已移除 group 分桶 legacy)。
      const userIdSet = new Set(filteredUserBoxes.map((b) => b.id));
      const childrenByParent = new Map<string, Annotation[]>();
      for (const b of filteredUserBoxes) {
        const pid = b.parent_annotation_id;
        if (pid && userIdSet.has(pid)) {
          if (!childrenByParent.has(pid)) childrenByParent.set(pid, []);
          childrenByParent.get(pid)!.push(b);
        }
      }
      // 顶层框: 无 parent, 或 parent 不在当前过滤集内 (跨帧过滤等 → 回落顶层展示, 不隐藏)。
      const topLevelBoxes = filteredUserBoxes.filter(
        (b) => !(b.parent_annotation_id && userIdSet.has(b.parent_annotation_id)),
      );
      const emitBoxWithChildren = (b: Annotation) => {
        out.push({ kind: "user", box: b, key: `user-${b.id}` });
        const kids = childrenByParent.get(b.id);
        if (kids)
          kids.forEach((c) => out.push({ kind: "user", box: c, key: `user-${c.id}`, depth: 1 }));
      };

      // v0.21.3 · 标注编组已删除: 顶层框平铺 (parent 缩进保留)。
      topLevelBoxes.forEach(emitBoxWithChildren);
    }
    if (resolvedVideoTrackPanel) out.push({ kind: "videoTracks", key: "video-track-panel" });
    return out;
  }, [
    aiBoxes.length,
    filteredAiBoxes,
    filteredUserBoxes,
    frameFilter,
    predictionSourceFilter,
    showFrameFilter,
    userBoxes.length,
    resolvedVideoTrackPanel,
    aiSectionCollapsed,
    manualSectionCollapsed,
    onToggleAiSection,
    onToggleManualSection,
  ]);

  const selectBox = (box: Annotation | AiBox, shift: boolean | undefined) => {
    const frame = shift ? null : firstTrackFrame(box);
    if (onSelectVideoObject) {
      onSelectVideoObject(box.id, { shift: !!shift, frameIndex: frame ?? undefined });
      return;
    }
    if (!shift) {
      if (frame !== null) onSeekFrame?.(frame);
    }
    onSelect(box.id, { shift: !!shift });
  };

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: (i) => {
      const row = rows[i];
      if (row?.kind === "frameFilter") return 38;
      if (row?.kind === "sourceFilter") return 48;
      if (row?.kind === "header") return 36;
      if (row?.kind === "videoTracks") return 420;
      return 68;
    },
    overscan: 8,
  });

  // 滚到接近末尾时自动加载下一页（仅 AI 段尚有未加载）
  const items = virtualizer.getVirtualItems();
  useEffect(() => {
    if (!items.length || !hasMore || isFetchingMore || !onFetchMore) return;
    const aiEndIndex = aiBoxes.length;
    const visibleAiNearEnd =
      aiBoxes.length > 0 &&
      items.some(
        (item) => item.index <= aiEndIndex && item.index >= Math.max(1, aiBoxes.length - 4),
      );
    if (visibleAiNearEnd) onFetchMore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, hasMore, isFetchingMore, aiBoxes.length]);
  return (
    <div className="flex flex-1 flex-col min-h-0 overflow-hidden">
      <div
        data-testid="boxes-list-sticky-header"
        className="sticky top-0 z-base flex shrink-0 items-center justify-between border-b border-border bg-card/95 px-3 py-1.5 backdrop-blur-sm"
      >
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => {
              const aiIdx = rows.findIndex((r) => r.kind === "header" && r.sectionKey === "ai");
              if (aiIdx >= 0) virtualizer.scrollToIndex(aiIdx, { align: "start" });
            }}
            title="跳转到 AI 待审"
            className={cn(
              "inline-flex cursor-pointer appearance-none items-center gap-1 rounded-full border px-2 py-0.5 text-2xs font-medium transition-colors",
              filteredAiBoxes.length > 0
                ? "border-status-info/30 bg-status-info-soft text-status-info hover:bg-status-info/20"
                : "border-border bg-muted/40 text-muted-foreground",
            )}
          >
            <Icon name="sparkle" size={10} />
            <span>待审候选</span>
            <span className="font-semibold tabular-nums">{filteredAiBoxes.length}</span>
          </button>
          <button
            type="button"
            onClick={() => {
              const userIdx = rows.findIndex(
                (r) => r.kind === "header" && r.sectionKey === "manual",
              );
              if (userIdx >= 0) virtualizer.scrollToIndex(userIdx, { align: "start" });
            }}
            title="跳转到人工标注"
            className={cn(
              "inline-flex cursor-pointer appearance-none items-center gap-1 rounded-full border px-2 py-0.5 text-2xs font-medium transition-colors",
              filteredUserBoxes.length > 0
                ? "border-brand/30 bg-brand/10 text-brand hover:bg-brand/20"
                : "border-border bg-muted/40 text-muted-foreground",
            )}
          >
            <Icon name="check" size={10} />
            <span>人工标注</span>
            <span className="font-semibold tabular-nums">{filteredUserBoxes.length}</span>
          </button>
        </div>
        <div className="flex items-center gap-1.5 text-2xs text-muted-foreground">
          <span>共 {filteredAiBoxes.length + filteredUserBoxes.length} 个对象</span>
        </div>
      </div>
      <div ref={parentRef} className="flex-1 overflow-y-auto px-2 py-1">
        <div
          className="relative h-[var(--ai-inspector-list-height)]"
          ref={(node) => {
            node?.style.setProperty(
              "--ai-inspector-list-height",
              `${virtualizer.getTotalSize()}px`,
            );
          }}
        >
          {items.map((vItem) => {
            const r = rows[vItem.index];
            if (!r) return null;
            return (
              <div
                key={r.key}
                data-index={vItem.index}
                data-workbench-video-tool-command={
                  onSelectVideoObject && (r.kind === "ai" || r.kind === "user") ? "" : undefined
                }
                ref={(node) => {
                  virtualizer.measureElement(node);
                  node?.style.setProperty("--ai-inspector-row-y", `${vItem.start}px`);
                }}
                className="absolute left-0 top-0 w-full translate-y-[var(--ai-inspector-row-y)]"
              >
                {r.kind === "ai" && (
                  <BoxListItem
                    b={r.box}
                    isAi
                    rasterMaskStatus={rasterMaskStatusById?.get(r.box.id)}
                    onRetryRasterMask={
                      onRetryRasterMask ? () => onRetryRasterMask(r.box.id) : undefined
                    }
                    selected={selSet.has(r.box.id)}
                    dimmed={dimmedAiIds?.has(r.box.id) ?? false}
                    imageWidth={imageWidth}
                    imageHeight={imageHeight}
                    onSelect={(e) => selectBox(r.box, e?.shiftKey)}
                    onAccept={() => onAcceptPrediction(r.box)}
                    onReject={() => onRejectPrediction?.(r.box)}
                    onRefine={
                      onRefinePrediction && r.box.geometry?.type === "polygon"
                        ? () => onRefinePrediction(r.box)
                        : undefined
                    }
                  />
                )}
                {r.kind === "header" &&
                  (r.onToggle ? (
                    // v0.20.22 · 分组头可点折叠 (AI 待审 / 人工)。计数常驻显示; 收起时下方成员行整体跳过。
                    <button
                      type="button"
                      onClick={r.onToggle}
                      aria-expanded={!r.collapsed}
                      title={r.collapsed ? `展开${r.label}` : `折叠${r.label}`}
                      data-testid={`section-header-${r.sectionKey ?? "unknown"}`}
                      className={cn(
                        SECTION_CARD_CLASS,
                        "flex w-full cursor-pointer appearance-none items-center justify-between gap-2 text-left hover:bg-muted",
                      )}
                    >
                      <span className="flex items-center gap-1.5 text-sm font-semibold">
                        <Icon name={r.collapsed ? "chevRight" : "chevDown"} size={13} />
                        {r.label}
                      </span>
                      <span className="mono text-xs font-medium text-muted-foreground">
                        {showFrameFilter && frameFilter === "current"
                          ? `${r.count}/${r.totalCount}`
                          : r.count}
                      </span>
                    </button>
                  ) : (
                    <div className={SECTION_CARD_CLASS}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">{r.label}</span>
                        <span className="mono text-xs font-medium text-muted-foreground">
                          {showFrameFilter && frameFilter === "current"
                            ? `${r.count}/${r.totalCount}`
                            : r.count}
                        </span>
                      </div>
                    </div>
                  ))}
                {r.kind === "frameFilter" && (
                  <FilterGroup
                    label="显示范围"
                    compact
                    className="mb-1.5 grid grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5"
                  >
                    <FrameFilterTabs value={r.filter} onChange={r.onFilterChange} />
                  </FilterGroup>
                )}
                {r.kind === "sourceFilter" && <PredictionSourceFilterCard filter={r.filter} />}
                {r.kind === "videoTracks" && (
                  <div data-testid="video-track-panel-row">{resolvedVideoTrackPanel}</div>
                )}
                {r.kind === "user" && (
                  <div className={r.depth ? "ml-3 border-l-2 border-border pl-2" : undefined}>
                    <BoxListItem
                      b={r.box}
                      rasterMaskStatus={rasterMaskStatusById?.get(r.box.id)}
                      onRetryRasterMask={
                        onRetryRasterMask ? () => onRetryRasterMask(r.box.id) : undefined
                      }
                      orphan={orphanUserBoxIds?.has(r.box.id) ?? false}
                      selected={selSet.has(r.box.id)}
                      imageWidth={imageWidth}
                      imageHeight={imageHeight}
                      onSelect={(e) => selectBox(r.box, e?.shiftKey)}
                      onDelete={
                        !readOnly && !r.box.is_locked ? () => onDeleteUserBox(r.box.id) : undefined
                      }
                      onChangeClass={
                        !readOnly && !r.box.is_locked && onChangeUserBoxClass
                          ? () => onChangeUserBoxClass(r.box.id)
                          : undefined
                      }
                      onToggleFlag={
                        onToggleUserBoxFlag
                          ? (flag) => onToggleUserBoxFlag(r.box.id, flag)
                          : undefined
                      }
                      onRefine={
                        r.box.geometry?.type === "raster_mask"
                          ? !readOnly && !r.box.is_locked && onEditRasterMask
                            ? () => onEditRasterMask(r.box.id)
                            : undefined
                          : onRefineUserPolygon && r.box.geometry?.type === "polygon"
                            ? () => onRefineUserPolygon(r.box.id)
                            : undefined
                      }
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {(hasMore || isFetchingMore) && (
          <div className="px-2 py-1.5 text-center text-xs text-muted-foreground">
            {isFetchingMore ? (
              "加载更多预测..."
            ) : (
              <button
                onClick={onFetchMore}
                className="cursor-pointer appearance-none rounded border border-border bg-transparent px-3 py-1 text-xs text-muted-foreground"
              >
                加载更多
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
