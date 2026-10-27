import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import {
  Brush,
  Check,
  ChevronDown,
  Eraser,
  Lasso,
  Redo2,
  RotateCcw,
  Undo2,
  X,
  PaintBucket,
  Scissors,
  Shapes,
} from "lucide-react";
import { Icon } from "@/components/ui/Icon";
import { Button, buttonVariants } from "@/components/shadcn/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/shadcn/ui/dropdown-menu";
import { ContextToolbar } from "./ContextToolbar";
import { Field, FieldLabel } from "@/components/shadcn/ui/field";
import { Input } from "@/components/shadcn/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/shadcn/ui/toggle-group";
import { cn } from "@/lib/utils";
import {
  MASK_BRUSH_MAX_PX,
  MASK_BRUSH_MIN_PX,
  type MaskEditorTool,
  type MaskOperationStatus,
  type MaskOperationPreview,
  type MaskInstanceOperationPreview,
} from "../state/useMaskEditor";
import type { MaskBrushShape } from "../stage/shared/geometry/maskBuffer";
import type {
  MaskConnectivity,
  MaskKernelShape,
  MaskOperationSpec,
} from "../stage/shared/geometry/maskOperations";
import type { MaskInstanceOperationSpec } from "../stage/shared/geometry/maskInstanceOperations";
import type { MaskEditBlockReason, MaskEditorPhase } from "../state/canEditMask";
import {
  MASK_EDIT_BLOCK_REASON_LABELS,
  type MaskPrimaryActions,
} from "../state/maskPrimaryActions";
import { TOOLBAR_FIELD_LABEL_CLASS } from "./workbenchToolbarChrome";

interface MaskToolbarProps {
  actions: MaskPrimaryActions;
  onPrimaryAction: () => void;
  onSecondaryAction: () => void;
  active: boolean;
  tool: MaskEditorTool;
  brushShape: MaskBrushShape;
  connectivity: MaskConnectivity;
  radius: number;
  dirty: boolean;
  phase: MaskEditorPhase;
  canUndo: boolean;
  canRedo: boolean;
  canEdit: boolean;
  canCommit?: boolean;
  interactionFrozen?: boolean;
  largeCanvas?: boolean;
  editBlockReason?: MaskEditBlockReason | null;
  operationPreview: MaskOperationPreview | null;
  instanceOperationPreview: MaskInstanceOperationPreview | null;
  operationStatus: MaskOperationStatus;
  operationError?: unknown;
  onSetTool: (tool: MaskEditorTool) => void;
  onSetBrushShape: (shape: MaskBrushShape) => void;
  onSetConnectivity: (connectivity: MaskConnectivity) => void;
  onSetRadius: (radius: number) => void;
  onRunOperation: (name: string, operation: MaskOperationSpec) => Promise<boolean>;
  onRunInstanceOperation: (name: string, operation: MaskInstanceOperationSpec) => Promise<boolean>;
  onPrepareJoin?: (mode: "replace_sources" | "preserve_sources") => void;
  onPrepareOverlap?: (policy: "erase_same_class" | "erase_all") => void;
  canPrepareJoin?: boolean;
  joinSupportsReplace?: boolean;
  sliceUnavailableReason?: string | null;
  instanceCommitting?: boolean;
  instanceRefreshing?: boolean;
  instanceCommitError?: string | null;
  instanceCanRetry?: boolean;
  instanceCanRefresh?: boolean;
  instancePreviewDetail?: string | null;
  instancePreviewRows?: Array<{
    annotationId: string;
    version: number | null;
    changedPixels: number | null;
    status: "update" | "delete" | "source" | "unresolved";
  }>;
  instanceCommitBlocked?: boolean;
  onCommitAndPropagate?: () => void;
  onOpenConversion?: () => void;
  onUndo: () => void;
  onRedo: () => void;
}

const phaseLabel: Record<MaskEditorPhase, string> = {
  idle: "未激活",
  loading: "加载中",
  ready: "就绪",
  dirty: "未保存",
  saving: "保存中",
  error: "操作失败",
};

const operationLabel: Record<string, string> = {
  lasso_add: "套索添加",
  lasso_subtract: "套索扣除",
  fill_add: "区域填充",
  fill_subtract: "区域擦除",
  component_keep: "保留命中组件",
  component_delete: "删除命中组件",
  hole_fill: "填充命中孔洞",
  dilate: "膨胀",
  erode: "腐蚀",
  open: "开运算",
  close: "闭运算",
  smooth: "边界平滑",
  deburr: "去除小组件",
  fill_holes_all: "填充全部孔洞",
  fill_holes_small: "填充小孔洞",
  component_copy: "复制命中组件",
  split_components: "拆分组件",
  slice_mask: "直线切割",
};

export function MaskToolbar({
  actions,
  onPrimaryAction,
  onSecondaryAction,
  tool,
  brushShape,
  connectivity,
  radius,
  dirty,
  phase,
  canUndo,
  canRedo,
  canEdit,
  largeCanvas = false,
  editBlockReason,
  operationPreview,
  instanceOperationPreview,
  operationStatus,
  operationError,
  onSetTool,
  onSetBrushShape,
  onSetConnectivity,
  onSetRadius,
  onRunOperation,
  onRunInstanceOperation,
  onPrepareJoin,
  onPrepareOverlap,
  canPrepareJoin = false,
  joinSupportsReplace = true,
  sliceUnavailableReason,
  instanceCommitError,
  instancePreviewDetail,
  instancePreviewRows = [],
  onCommitAndPropagate,
  onOpenConversion,
  onUndo,
  onRedo,
}: MaskToolbarProps) {
  const currentToolLabel =
    tool === "brush" ? "笔刷" : tool === "erase" ? "橡皮" : (operationLabel[tool] ?? "Mask 工具");
  const CurrentToolIcon =
    tool === "brush"
      ? Brush
      : tool === "erase"
        ? Eraser
        : tool.startsWith("lasso")
          ? Lasso
          : tool.startsWith("fill") || tool === "hole_fill"
            ? PaintBucket
            : tool === "slice_mask"
              ? Scissors
              : Shapes;
  const [componentThreshold, setComponentThreshold] = useState(16);
  const [morphologyRadius, setMorphologyRadius] = useState(1);
  const [kernelShape, setKernelShape] = useState<MaskKernelShape>("disk");
  const runMorphology = (operation: "dilate" | "erode" | "open" | "close") => {
    void onRunOperation(operation, {
      type: "morphology",
      operation,
      kernelShape,
      radius: morphologyRadius,
    });
  };
  const renderSettings = (close: () => void) => (
    <>
      <div className="flex items-center gap-2" data-testid="mask-toolbar-header">
        <span className="flex size-6 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon name="brush" size={13} />
        </span>
        <b className="text-xs font-semibold tracking-wide">Mask 编辑</b>
        <span
          className={cn(
            "rounded-full bg-muted px-2 py-0.5 text-2xs font-medium",
            dirty ? "text-status-caution" : "text-muted-foreground",
          )}
        >
          {phaseLabel[phase]}
        </span>
        <Button
          variant="ghost"
          size="icon-xs"
          className="ml-auto rounded-full text-muted-foreground"
          aria-label="收起 Mask 设置"
          title="收起设置，继续绘制"
          onClick={() => close()}
        >
          <X />
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2" data-testid="mask-toolbar-controls">
        <div className="flex shrink-0 items-center gap-2">
          <ToggleGroup
            type="single"
            value={tool}
            variant="outline"
            size="sm"
            disabled={!canEdit}
            aria-label="Mask pointer 工具"
            className="[&_button]:h-6 [&_button]:gap-1 [&_button]:px-2 [&_button]:text-xs [&_svg]:size-3"
            onValueChange={(value) => value && onSetTool(value as MaskEditorTool)}
          >
            <ToggleGroupItem value="brush" title="笔刷 (B)" aria-label="笔刷">
              <Brush />
              笔刷
            </ToggleGroupItem>
            <ToggleGroupItem value="erase" title="橡皮 (E)" aria-label="橡皮">
              <Eraser />
              橡皮
            </ToggleGroupItem>
            <ToggleGroupItem value="lasso_add" title="套索添加" aria-label="套索添加">
              <Lasso />
              添加
            </ToggleGroupItem>
            <ToggleGroupItem value="lasso_subtract" title="套索扣除" aria-label="套索扣除">
              <Lasso />
              扣除
            </ToggleGroupItem>
          </ToggleGroup>

          <DropdownMenu>
            <DropdownMenuTrigger
              data-workbench-tool-menu-trigger
              className={buttonVariants({ variant: "outline", size: "xs" })}
              disabled={!canEdit}
              title="Mask 高级工具"
            >
              {operationLabel[tool] ?? "高级"} <ChevronDown />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              data-workbench-tool-menu
              align="start"
              className="z-workbench-top max-h-[min(32rem,var(--radix-dropdown-menu-content-available-height))] w-72 overflow-y-auto"
            >
              <DropdownMenuLabel>区域工具</DropdownMenuLabel>
              <DropdownMenuItem disabled={largeCanvas} onSelect={() => onSetTool("fill_add")}>
                填充命中区域
              </DropdownMenuItem>
              <DropdownMenuItem disabled={largeCanvas} onSelect={() => onSetTool("fill_subtract")}>
                擦除命中区域
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>组件与孔洞</DropdownMenuLabel>
              <DropdownMenuItem disabled={largeCanvas} onSelect={() => onSetTool("component_keep")}>
                保留命中组件
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() => onSetTool("component_delete")}
              >
                删除命中组件
              </DropdownMenuItem>
              <DropdownMenuItem disabled={largeCanvas} onSelect={() => onSetTool("component_copy")}>
                复制命中组件为新实例
              </DropdownMenuItem>
              <DropdownMenuItem disabled={largeCanvas} onSelect={() => onSetTool("hole_fill")}>
                填充命中孔洞
              </DropdownMenuItem>
              <DropdownMenuLabel className="flex items-center gap-2 font-normal">
                <span className="text-xs text-muted-foreground">面积阈值</span>
                <Input
                  type="number"
                  min={1}
                  step={1}
                  value={componentThreshold}
                  aria-label="组件与孔洞面积阈值"
                  onChange={(event) =>
                    setComponentThreshold(Math.max(1, Number.parseInt(event.target.value, 10) || 1))
                  }
                  className="h-7 w-20"
                />
                <span className="text-xs text-muted-foreground">px</span>
              </DropdownMenuLabel>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() =>
                  void onRunOperation("deburr", {
                    type: "remove_small_components",
                    maxArea: componentThreshold,
                    connectivity,
                  })
                }
              >
                去除小组件（≤ {componentThreshold}px）
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() =>
                  void onRunOperation("fill_holes_small", {
                    type: "fill_holes",
                    mode: "max_area",
                    maxArea: componentThreshold,
                  })
                }
              >
                填充小孔洞（≤ {componentThreshold}px）
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() =>
                  void onRunOperation("fill_holes_all", { type: "fill_holes", mode: "all" })
                }
              >
                填充全部孔洞
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() =>
                  void onRunInstanceOperation("split_components", {
                    type: "split_components",
                    keep: "largest",
                    connectivity,
                  })
                }
              >
                拆分全部组件（保留最大）
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>派生与转换</DropdownMenuLabel>
              {sliceUnavailableReason !== undefined && (
                <DropdownMenuItem
                  disabled={sliceUnavailableReason !== null || dirty || largeCanvas}
                  title={
                    sliceUnavailableReason ??
                    (dirty ? "请先保存当前 Mask 草稿" : "拖动两点定义切线，预览后确认")
                  }
                  onSelect={() => onSetTool("slice_mask")}
                >
                  直线切割为两个实例
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                disabled={!onOpenConversion || dirty || largeCanvas}
                title={
                  largeCanvas
                    ? "大画布全图转换超出同步预算"
                    : dirty
                      ? "请先保存当前 Mask 草稿"
                      : "打开标注转换中心"
                }
                onSelect={() => onOpenConversion?.()}
              >
                转为紧致 BBox / Polygon
              </DropdownMenuItem>
              {onPrepareJoin && (
                <>
                  {joinSupportsReplace && (
                    <DropdownMenuItem
                      disabled={!canPrepareJoin || largeCanvas}
                      onSelect={() => onPrepareJoin("replace_sources")}
                    >
                      合并已选 Mask（替换来源）
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem
                    disabled={!canPrepareJoin || largeCanvas}
                    onSelect={() => onPrepareJoin("preserve_sources")}
                  >
                    合并为副本（保留来源）
                  </DropdownMenuItem>
                </>
              )}
              {onPrepareOverlap && (
                <>
                  <DropdownMenuItem
                    disabled={largeCanvas}
                    onSelect={() => onPrepareOverlap("erase_same_class")}
                  >
                    预览同类严格非重叠
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={largeCanvas}
                    onSelect={() => onPrepareOverlap("erase_all")}
                  >
                    预览全类严格非重叠
                  </DropdownMenuItem>
                </>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuLabel>
                {largeCanvas ? "形态学（当前视口 ROI）" : "形态学"}
              </DropdownMenuLabel>
              <DropdownMenuLabel className="flex items-center gap-2 font-normal">
                <span className="text-xs text-muted-foreground">半径</span>
                <Input
                  type="number"
                  min={1}
                  max={32}
                  step={1}
                  value={morphologyRadius}
                  aria-label="形态学半径"
                  onChange={(event) => {
                    const value = Number.parseInt(event.target.value, 10) || 1;
                    setMorphologyRadius(Math.max(1, Math.min(32, value)));
                  }}
                  className="h-7 w-16"
                />
              </DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={kernelShape}
                onValueChange={(value) => setKernelShape(value as MaskKernelShape)}
              >
                <DropdownMenuRadioItem value="disk">圆盘 kernel</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="square">方形 kernel</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
              <DropdownMenuItem onSelect={() => runMorphology("dilate")}>膨胀</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => runMorphology("erode")}>腐蚀</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => runMorphology("open")}>开运算</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => runMorphology("close")}>闭运算</DropdownMenuItem>
              <DropdownMenuItem
                disabled={largeCanvas}
                onSelect={() =>
                  void onRunOperation("smooth", {
                    type: "smooth",
                    kernelShape,
                    radius: morphologyRadius,
                  })
                }
              >
                边界平滑（闭→开）
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>笔刷形状</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={brushShape}
                onValueChange={(value) => onSetBrushShape(value as MaskBrushShape)}
              >
                <DropdownMenuRadioItem value="circle">圆形硬边</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="square">方形硬边</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>连通邻域</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={String(connectivity)}
                onValueChange={(value) => onSetConnectivity(value === "8" ? 8 : 4)}
              >
                <DropdownMenuRadioItem value="4">4 邻域</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="8">8 邻域</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="flex min-w-0 basis-56 grow items-center justify-end gap-2">
          {(tool === "brush" || tool === "erase") && (
            <Field
              orientation="horizontal"
              className="min-w-0 flex-1 gap-2 [&>[data-slot=field-label]]:flex-none"
            >
              <FieldLabel htmlFor="mask-radius" className={TOOLBAR_FIELD_LABEL_CLASS}>
                半径
              </FieldLabel>
              <Input
                id="mask-radius"
                type="range"
                min={MASK_BRUSH_MIN_PX}
                max={MASK_BRUSH_MAX_PX}
                step={1}
                value={radius}
                disabled={!canEdit}
                onChange={(event) => onSetRadius(Number.parseInt(event.target.value, 10))}
                className="h-5 min-w-12 flex-1 border-0 px-0 py-0 shadow-none"
                data-testid="mask-radius-slider"
              />
              <span className="mono min-w-8 text-right text-2xs tabular-nums text-foreground">
                {radius}px
              </span>
            </Field>
          )}

          <div
            className="flex shrink-0 items-center gap-0.5 border-l border-border pl-2"
            role="group"
            aria-label="笔画历史"
          >
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              onClick={onUndo}
              disabled={!canEdit || !canUndo}
              title="撤销笔画 (Ctrl+Z)"
            >
              <Undo2 />
              <span className="sr-only">撤销</span>
            </Button>
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              onClick={onRedo}
              disabled={!canEdit || !canRedo}
              title="重做笔画 (Ctrl+Y)"
            >
              <Redo2 />
              <span className="sr-only">重做</span>
            </Button>
          </div>
        </div>
      </div>
      {!canEdit && editBlockReason && (
        <span className="basis-full text-2xs text-status-caution" role="status">
          不可编辑：{MASK_EDIT_BLOCK_REASON_LABELS[editBlockReason]}
        </span>
      )}
      {largeCanvas && (
        <span className="basis-full text-2xs text-muted-foreground" role="status">
          大画布分块模式：形态学仅作用当前视口，全图扫描工具已禁用
        </span>
      )}
      {tool === "slice_mask" && !instanceOperationPreview && (
        <span className="basis-full text-2xs text-muted-foreground" role="status">
          拖动两点定义直线，松手预览两块；较大块保留来源，像素相等时沿切线左侧保留。
        </span>
      )}

      {operationPreview && (
        <div className="flex basis-full items-center gap-2 rounded-md border border-border bg-muted px-2 py-1">
          <span className="text-2xs font-medium text-foreground">
            {operationLabel[operationPreview.name] ?? operationPreview.name}
          </span>
          <span className="text-2xs text-muted-foreground">
            变化 {operationPreview.report.changedPixels} px
          </span>
          <span className="text-2xs text-muted-foreground">
            面积 {operationPreview.report.beforeArea}→{operationPreview.report.afterArea}· 组件{" "}
            {operationPreview.report.beforeComponents}→{operationPreview.report.afterComponents}·
            孔洞 {operationPreview.report.beforeHoles}→{operationPreview.report.afterHoles}
          </span>
        </div>
      )}
      {instanceOperationPreview && (
        <div className="flex max-w-3xl basis-full flex-wrap items-center gap-2 rounded-md border border-border bg-muted px-2 py-1">
          <span className="text-2xs font-medium text-foreground">
            {instanceOperationPreview.plan.kind === "copy_component"
              ? "复制组件"
              : instanceOperationPreview.plan.kind === "copy_keyframe"
                ? "粘贴为新轨迹"
                : instanceOperationPreview.plan.kind === "join_masks"
                  ? "合并 Mask"
                  : instanceOperationPreview.plan.kind === "overlap"
                    ? "严格非重叠"
                    : instanceOperationPreview.plan.kind === "slice_mask"
                      ? "直线切割"
                      : "拆分组件"}
          </span>
          <span className="text-2xs text-muted-foreground">
            {instanceOperationPreview.plan.sourceCount} 个来源 →{" "}
            {instanceOperationPreview.plan.resultCount} 个结果
          </span>
          {instancePreviewDetail && (
            <span className="text-2xs text-muted-foreground">{instancePreviewDetail}</span>
          )}
          {instancePreviewRows.length > 0 && (
            <div
              className="flex max-h-20 basis-full flex-wrap gap-x-3 gap-y-1 overflow-y-auto border-t border-border pt-1"
              aria-label="受影响 Mask 对象"
            >
              {instancePreviewRows.map((row) => (
                <span key={row.annotationId} className="text-2xs text-muted-foreground">
                  {row.annotationId.slice(0, 8)}·v{row.version ?? "?"}
                  {row.changedPixels === null ? "" : `·${row.changedPixels}px`}·
                  {row.status === "delete"
                    ? "删除"
                    : row.status === "unresolved"
                      ? "未解决"
                      : row.status === "update"
                        ? "更新"
                        : "来源"}
                </span>
              ))}
            </div>
          )}
          <span className="text-2xs text-status-caution">待原子提交</span>
        </div>
      )}
      {instanceCommitError && (
        <div
          className="flex basis-full items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1"
          role="alert"
        >
          <span className="text-2xs text-destructive">
            <CopyableError message={instanceCommitError} />
          </span>
        </div>
      )}
      {operationStatus === "error" && (
        <div
          className="flex basis-full items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1"
          role="alert"
        >
          <span className="text-2xs text-destructive">
            高级操作失败：
            <CopyableError
              message={
                operationError instanceof Error
                  ? operationError.message
                  : String(operationError ?? "未知错误")
              }
            />
          </span>
        </div>
      )}

      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border/70 pt-2"
        data-testid="mask-toolbar-footer"
      >
        <div
          role="status"
          aria-live="polite"
          data-testid="mask-action-hint"
          className="min-w-0 flex-1 text-2xs leading-relaxed text-muted-foreground"
        >
          {actions.hint}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <Button
            data-testid="mask-secondary-action"
            type="button"
            size="xs"
            variant="outline"
            onClick={onSecondaryAction}
            disabled={actions.secondary.disabled}
            title={`${actions.secondary.label} (Esc)`}
          >
            {actions.secondary.label}
          </Button>
          <Button
            data-testid="mask-primary-action"
            type="button"
            size="xs"
            variant={actions.primary.kind === "none" ? "ghost" : "default"}
            onClick={onPrimaryAction}
            disabled={actions.primary.disabled}
            title={`${actions.primary.label} (Enter) · ${actions.primary.description}`}
          >
            {["save", "apply_region", "commit_instances"].includes(actions.primary.kind) ? (
              <Check />
            ) : actions.primary.kind !== "none" ? (
              <RotateCcw />
            ) : null}
            {actions.primary.label}
          </Button>
          {onCommitAndPropagate && (
            <Button
              type="button"
              size="xs"
              variant="ghost"
              onClick={onCommitAndPropagate}
              disabled={!dirty || actions.primary.kind !== "save" || actions.primary.disabled}
              title="保存人工纠错帧并选择定向重传播"
            >
              保存并传播
            </Button>
          )}
        </div>
      </div>
    </>
  );
  return (
    <ContextToolbar
      id="mask"
      label="Mask"
      summaryLabel={`Mask 常用工具：${currentToolLabel}`}
      summaryTitle={`${currentToolLabel} · ${phaseLabel[phase]} · 悬停展开常用工具`}
      summary={
        <>
          <CurrentToolIcon className="size-3.5 text-primary" />
          {(tool === "brush" || tool === "erase") && (
            <span className="font-mono text-2xs tabular-nums text-muted-foreground">
              {radius}
              <span className="ml-0.5">px</span>
            </span>
          )}
          {(dirty || phase === "saving" || phase === "error") && (
            <span
              role="status"
              aria-label={phaseLabel[phase]}
              className={cn(
                "size-1.5 rounded-full",
                phase === "error" ? "bg-status-danger" : "bg-status-caution",
              )}
            />
          )}
        </>
      }
      quickActions={[
        ...(
          [
            ["brush", "笔刷", Brush],
            ["erase", "橡皮", Eraser],
            ["lasso_add", "套索添加", Lasso],
            ["lasso_subtract", "套索扣除", Lasso],
          ] as const
        ).map(([value, label, ToolIcon]) => ({
          id: value,
          label,
          active: tool === value,
          disabled: !canEdit,
          onSelect: () => onSetTool(value),
          icon: (
            <>
              <ToolIcon className="size-3.5" />
              {value.startsWith("lasso") && (
                <span aria-hidden className="absolute bottom-0 right-0.5 text-2xs leading-none">
                  {value === "lasso_add" ? "+" : "−"}
                </span>
              )}
            </>
          ),
        })),
        {
          id: "undo",
          label: "撤销笔画",
          icon: <Undo2 className="size-3.5" />,
          disabled: !canEdit || !canUndo,
          onSelect: onUndo,
        },
      ]}
    >
      {renderSettings}
    </ContextToolbar>
  );
}
