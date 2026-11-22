import { Fragment, useLayoutEffect, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/shadcn/ui/dropdown-menu";
import { ALL_TOOLS, type CanvasTool, type ToolId } from "../stage/tools";
import { toolUnitForTool } from "../stage/tools/toolUnits";
import type { ThreeDTool, VideoTool } from "../state/useWorkbenchState";
import { videoToolScopeForTool, type VideoToolScope } from "../stage/videoToolUnits";
import { splitToolDock, type ToolDockEntry, type ToolDockMetrics } from "./toolDockOverflow";

const ROOT_CLASS =
  "relative flex flex-col items-center gap-1.5 border-r border-border bg-card px-1 py-2.5";
const TOOL_BTN_CLASS =
  "relative flex size-[38px] shrink-0 cursor-pointer appearance-none items-center justify-center rounded-md border transition-colors";
const TOOL_BTN_IDLE = "border-transparent bg-transparent text-muted-foreground";
const TOOL_BTN_HOVER = "hover:bg-muted hover:text-foreground";
const TOOL_BTN_ACTIVE = "border-brand bg-brand text-brand-foreground shadow-sm";
const TOOL_BTN_DISABLED = "cursor-not-allowed opacity-40";
const HOTKEY_BADGE_CLASS =
  "pointer-events-none absolute bottom-px right-[3px] text-3xs font-bold leading-none text-muted-foreground/60";
const HOTKEY_BADGE_ACTIVE = "text-brand-foreground/80";
const DIVIDER_CLASS = "my-1.5 h-px w-[26px] shrink-0 bg-border";
const VIDEO_SECTION_CLASS = "flex shrink-0 flex-col items-center gap-1.5";
const VIDEO_SECTION_LABEL_CLASS =
  "mb-0.5 text-2xs font-semibold leading-none text-muted-foreground";
const VIDEO_SUBSECTION_LABEL_CLASS =
  "mt-1 text-3xs font-medium leading-none tracking-wide text-muted-foreground/70";

interface ToolDockProps {
  tool: ToolId;
  onSetTool: (t: ToolId) => void;
  videoTool?: VideoTool;
  onSetVideoTool?: (t: VideoTool) => void;
  videoToolScope?: VideoToolScope;
  onSetVideoToolScope?: (scope: VideoToolScope) => void;
  isPromptSupported?: (type: string) => boolean;
  toolDisabledReasons?: Partial<Record<ToolId, string>>;
  capabilitiesLoading?: boolean;
  reviewMode?: boolean;
  videoMode?: boolean;
  enabledToolUnits?: Set<string> | null;
  aiInteractiveEnabled?: boolean;
  isVideoToolEnabled?: (t: VideoTool) => boolean;
  videoKeypointNodeCount?: number;
  threeDMode?: boolean;
  threeDTool?: ThreeDTool;
  onSetThreeDTool?: (t: ThreeDTool) => void;
  toolHotkeys?: Record<string, { label: string; alt?: string } | null>;
}

interface ToolDescriptor {
  desc: string;
  altDigit?: number;
}

function imageToolIcon(id: ToolId): IconName {
  return ALL_TOOLS.find((tool) => tool.id === id)?.icon ?? "cursor";
}

const TOOL_DESCRIPTORS: Record<ToolId, ToolDescriptor> = {
  select: { desc: "点选 / 移动已有标注与预标注 · ESC 回退到它", altDigit: 4 },
  box: { desc: "拖鼠标画矩形框", altDigit: 1 },
  "rotated-box": { desc: "拖框 → 顶部手柄旋转 (OBB)" },
  polygon: { desc: "逐点画多边形 (Enter 闭合)", altDigit: 2 },
  polyline: { desc: "逐点画折线 (Enter 结束, 不闭合)" },
  keypoint: { desc: "依次落关键点 · Alt=遮挡, 右键=跳过" },
  mask: { desc: "Mask 笔刷 · B/E 切模式, 滚轮调半径, Enter 执行当前阶段主动作" },
  "smart-point": { desc: "单击 = 正向点；Alt+点 = 负向点", altDigit: 3 },
  "smart-box": { desc: "拖框作为 SAM 提示" },
  "smart-scribble": { desc: "在选中 Mask 上画正向笔迹；Alt = 负向笔迹" },
  "text-prompt": { desc: "文本召回 (右侧 AI 面板输入)" },
  exemplar: { desc: "拖框示例 → 全图相似实例 (SAM 3)" },
  "magic-box": { desc: "Magic Box · 粗框 → SAM 收紧 → 落 bbox" },
  hand: { desc: "拖拽平移画布", altDigit: 4 },
  canvas: { desc: "评论批注 (内部, 不展示)" },
};

const VIDEO_TOOLS: Array<{
  id: VideoTool;
  label: string;
  icon: IconName;
  desc: string;
  hotkey?: string;
  altDigit?: number;
  group: "select" | "frame" | "sam" | "track";
  requiredPrompt?: string;
}> = [
  {
    id: "select",
    hotkey: "V",
    label: "选择",
    icon: "cursor",
    desc: "点选 / 移动已有视频标注",
    altDigit: 3,
    group: "select",
  },
  {
    id: "box",
    hotkey: "B",
    label: "矩形框",
    icon: "rect",
    desc: "当前帧独立矩形框",
    altDigit: 1,
    group: "frame",
  },
  {
    id: "rotated-box",
    hotkey: "W",
    label: "旋转框",
    icon: imageToolIcon("rotated-box"),
    desc: "当前帧旋转框 · 拖框后用顶部手柄旋转",
    group: "frame",
  },
  {
    id: "keypoint",
    hotkey: "F",
    label: "关键点",
    icon: imageToolIcon("keypoint"),
    desc: "按骨骼模板依次落点 · Alt 遮挡 · 右键跳过",
    group: "frame",
  },
  {
    id: "polygon",
    hotkey: "P",
    label: "多边形",
    icon: imageToolIcon("polygon"),
    desc: "点击落点画当前帧多边形 · Enter/双击闭合",
    group: "frame",
  },
  {
    id: "polyline",
    label: "折线",
    icon: imageToolIcon("polyline"),
    desc: "点击落点画当前帧折线 · Enter/双击结束",
    group: "frame",
  },
  {
    id: "smart-point",
    hotkey: "S",
    label: "智能点",
    icon: imageToolIcon("smart-point"),
    desc: "点选目标 · SAM 分割当前帧 · Alt 负点",
    group: "sam",
    requiredPrompt: "point",
  },
  {
    id: "smart-box",
    hotkey: "D",
    label: "智能框",
    icon: imageToolIcon("smart-box"),
    desc: "框选目标 · SAM 分割当前帧；选中当前帧待决 AI 候选时 D 为忽略",
    group: "sam",
    requiredPrompt: "interactive_box",
  },
  {
    id: "exemplar",
    hotkey: "E",
    label: "示例框",
    icon: imageToolIcon("exemplar"),
    desc: "框一个例子 · 找出画面里所有同类 · Alt 框排误检",
    group: "sam",
    requiredPrompt: "exemplar",
  },
  {
    id: "magic-box",
    hotkey: "G",
    label: "Magic Box",
    icon: imageToolIcon("magic-box"),
    desc: "粗框 → SAM 收紧 → 落矩形框",
    group: "sam",
    requiredPrompt: "interactive_box",
  },
  {
    id: "track",
    hotkey: "T",
    label: "矩形框轨迹",
    icon: "galleryHorizontalEnd",
    desc: "跨帧矩形框轨迹",
    altDigit: 2,
    group: "track",
  },
  {
    id: "polygon-track",
    label: "多边形轨迹",
    icon: "polygonTrack",
    desc: "点击落点画多边形轨迹 · Enter/双击闭合",
    group: "track",
  },
  {
    id: "polyline-track",
    label: "折线轨迹",
    icon: "polylineTrack",
    desc: "点击落点画折线轨迹 · Enter/双击结束",
    group: "track",
  },
  {
    id: "mask-track",
    label: "Mask 轨迹",
    icon: "maskTrack",
    desc: "绘制或编辑跨帧 Mask 轨迹关键帧",
    group: "track",
  },
  {
    id: "mask",
    hotkey: "M",
    label: "单帧 Mask",
    icon: imageToolIcon("mask"),
    desc: "在当前帧绘制或编辑逐像素 Mask",
    group: "frame",
  },
];

const THREE_D_TOOLS: Array<{
  id: ThreeDTool;
  hotkey: string;
  label: string;
  icon: IconName;
  desc: string;
}> = [
  { id: "select", hotkey: "V", label: "选择", icon: "move", desc: "拾取 / 选中 3D 框" },
  {
    id: "box",
    hotkey: "B",
    label: "连续建框",
    icon: "cube",
    desc: "点击放置或拖框自动拟合；完成后继续待命",
  },
  { id: "point-mask", hotkey: "P", label: "分割", icon: "scissors", desc: "框选点云生成 3D 分割" },
  {
    id: "measure",
    hotkey: "M",
    label: "测量",
    icon: "ruler",
    desc: "吸附点云测量三维距离、水平距离与高差",
  },
];

function unitForThreeDTool(tool: ThreeDTool): string | null {
  if (tool === "box") return "lidar_box_3d";
  if (tool === "point-mask") return "point_mask_3d";
  return null;
}

const cn = (...parts: Array<string | false | null | undefined>) => parts.filter(Boolean).join(" ");

interface DockTool extends ToolDockEntry {
  label: string;
  icon: IconName;
  description: string;
  hotkey?: string;
  disabledReason?: string;
  testId: string;
  onSelect: () => void;
}

const GROUP_LABELS: Record<ToolDockEntry["group"], string> = {
  select: "选择",
  draw: "绘制",
  ai: "AI 工具",
  view: "视图",
  frame: "单帧工具",
  sam: "SAM 工具",
  track: "轨迹工具",
};

const IMAGE_TOOL_COMMAND: Partial<Record<ToolId, string>> = {
  select: "image.tool.select",
  box: "image.tool.box",
  "rotated-box": "image.tool.rotatedBox",
  polygon: "image.tool.polygon",
  polyline: "image.tool.polyline",
  keypoint: "image.tool.keypoint",
  mask: "image.tool.mask",
  "magic-box": "image.tool.magicBox",
};

const VIDEO_TOOL_COMMAND: Partial<Record<VideoTool, string>> = {
  select: "video.tool.select",
  box: "video.tool.box",
  "rotated-box": "video.tool.rotatedBox",
  keypoint: "video.tool.keypoint",
  polygon: "video.tool.polygon",
  "smart-point": "video.tool.smartPoint",
  "smart-box": "video.tool.smartBox",
  exemplar: "video.tool.exemplar",
  "magic-box": "video.tool.magicBox",
  track: "video.tool.track",
  mask: "video.tool.mask",
};

function AdaptiveToolDock({
  tools,
  activeId,
  video = false,
}: {
  tools: DockTool[];
  activeId: string;
  video?: boolean;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const lastFocusedId = useRef<string | null>(null);
  const [open, setOpen] = useState(false);
  const [geometry, setGeometry] = useState<{ height: number; metrics: ToolDockMetrics } | null>(
    null,
  );
  const allocation = geometry
    ? splitToolDock(tools, activeId, geometry.height, video, geometry.metrics)
    : { visibleIds: tools.map((tool) => tool.id), overflowIds: [], scroll: false };
  const visibleTools = tools.filter((tool) => allocation.visibleIds.includes(tool.id));
  const overflowTools = tools.filter((tool) => allocation.overflowIds.includes(tool.id));
  const signature = `${tools.map((tool) => tool.id).join("|")}:${allocation.visibleIds.join("|")}`;
  const previousSignature = useRef(signature);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const measure = measureRef.current;
    if (!root || !measure) return;
    const outerHeight = (selector: string) => {
      const node = measure.querySelector<HTMLElement>(selector)!;
      const style = getComputedStyle(node);
      return (
        node.getBoundingClientRect().height +
        (parseFloat(style.marginTop) || 0) +
        (parseFloat(style.marginBottom) || 0)
      );
    };
    const update = () => {
      const style = getComputedStyle(root);
      const next = {
        height: root.getBoundingClientRect().height,
        metrics: {
          button: outerHeight('[data-dock-measure="button"]'),
          divider: outerHeight('[data-dock-measure="divider"]'),
          sectionLabel: outerHeight('[data-dock-measure="section"]'),
          subsectionLabel: outerHeight('[data-dock-measure="subsection"]'),
          gap: parseFloat(style.rowGap) || 0,
          padding: (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0),
        },
      };
      setGeometry((previous) =>
        JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(root);
    [...measure.children].forEach((child) => observer.observe(child));
    return () => observer.disconnect();
  }, []);

  const restoreFocus = () => {
    const fallback =
      rootRef.current?.querySelector<HTMLButtonElement>(`[data-tool-dock-entry="${activeId}"]`) ??
      rootRef.current?.querySelector<HTMLButtonElement>('[data-tool-dock-entry="select"]');
    (moreRef.current ?? fallback)?.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    if (signature === previousSignature.current) return;
    previousSignature.current = signature;
    if (open) {
      setOpen(false);
    } else if (
      lastFocusedId.current &&
      !(lastFocusedId.current === "more"
        ? overflowTools.length > 0
        : allocation.visibleIds.includes(lastFocusedId.current))
    ) {
      restoreFocus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const renderTool = (tool: DockTool) => {
    const active = tool.id === activeId;
    return (
      <Tooltip
        key={tool.id}
        name={tool.label}
        desc={tool.disabledReason ?? tool.description}
        hotkey={tool.hotkey}
        side="right"
        delay={250}
      >
        <button
          type="button"
          onClick={() => {
            if (!tool.disabledReason) tool.onSelect();
          }}
          aria-label={tool.label}
          aria-pressed={active}
          aria-disabled={!!tool.disabledReason || undefined}
          disabled={!!tool.disabledReason}
          data-testid={tool.testId}
          data-tool-dock-entry={tool.id}
          data-workbench-video-tool-command={video ? "" : undefined}
          className={cn(
            TOOL_BTN_CLASS,
            active ? TOOL_BTN_ACTIVE : cn(TOOL_BTN_IDLE, !tool.disabledReason && TOOL_BTN_HOVER),
            tool.disabledReason && TOOL_BTN_DISABLED,
          )}
        >
          <Icon name={tool.icon} size={17} />
          <span aria-hidden className={cn(HOTKEY_BADGE_CLASS, active && HOTKEY_BADGE_ACTIVE)}>
            {tool.hotkey}
          </span>
        </button>
      </Tooltip>
    );
  };
  const frames = visibleTools.filter((tool) => tool.group === "frame");
  const sam = visibleTools.filter((tool) => tool.group === "sam");
  const tracks = visibleTools.filter((tool) => tool.group === "track");

  return (
    <div
      ref={rootRef}
      data-testid="tool-dock"
      data-tool-dock-scroll={allocation.scroll || undefined}
      className={cn(
        ROOT_CLASS,
        "h-full min-h-0 shrink-0 overflow-x-hidden",
        allocation.scroll ? "overflow-y-auto [scrollbar-width:none]" : "overflow-y-hidden",
      )}
      onFocusCapture={(event) => {
        lastFocusedId.current =
          (event.target as HTMLElement).closest<HTMLElement>("[data-tool-dock-entry]")?.dataset
            .toolDockEntry ?? null;
      }}
      onBlurCapture={(event) => {
        if (!event.relatedTarget || !event.currentTarget.contains(event.relatedTarget))
          lastFocusedId.current = null;
      }}
    >
      <div
        ref={measureRef}
        aria-hidden
        className="pointer-events-none invisible absolute left-0 top-0 flex flex-col items-center"
      >
        <span data-dock-measure="button" className={TOOL_BTN_CLASS} />
        <span data-dock-measure="divider" className={DIVIDER_CLASS} />
        <span data-dock-measure="section" className={VIDEO_SECTION_LABEL_CLASS}>
          单帧
        </span>
        <span data-dock-measure="subsection" className={VIDEO_SUBSECTION_LABEL_CLASS}>
          SAM
        </span>
      </div>
      {video ? (
        <>
          {visibleTools.filter((tool) => tool.group === "select").map(renderTool)}
          {(frames.length > 0 || sam.length > 0) && (
            <>
              <div aria-hidden className={DIVIDER_CLASS} />
              <div role="group" aria-label="单帧工具" className={VIDEO_SECTION_CLASS}>
                <span aria-hidden className={VIDEO_SECTION_LABEL_CLASS}>
                  单帧
                </span>
                {frames.map(renderTool)}
                {sam.length > 0 && (
                  <div role="group" aria-label="SAM 工具" className={VIDEO_SECTION_CLASS}>
                    <span aria-hidden className={VIDEO_SUBSECTION_LABEL_CLASS}>
                      SAM
                    </span>
                    {sam.map(renderTool)}
                  </div>
                )}
              </div>
            </>
          )}
          {tracks.length > 0 && (
            <>
              <div aria-hidden className={DIVIDER_CLASS} />
              <div role="group" aria-label="轨迹工具" className={VIDEO_SECTION_CLASS}>
                <span aria-hidden className={VIDEO_SECTION_LABEL_CLASS}>
                  轨迹
                </span>
                {tracks.map(renderTool)}
              </div>
            </>
          )}
        </>
      ) : (
        visibleTools.map((tool, index) => (
          <Fragment key={tool.id}>
            {index > 0 && visibleTools[index - 1].group !== tool.group && (
              <div aria-hidden className={DIVIDER_CLASS} />
            )}
            {renderTool(tool)}
          </Fragment>
        ))
      )}
      {overflowTools.length > 0 && (
        <DropdownMenu open={open} onOpenChange={setOpen}>
          <DropdownMenuTrigger asChild>
            <button
              ref={moreRef}
              type="button"
              aria-label="更多工具"
              title="更多工具"
              data-testid="tool-dock-more"
              data-workbench-tool-menu-trigger
              data-workbench-video-tool-command={video ? "" : undefined}
              data-tool-dock-entry="more"
              className={cn(
                TOOL_BTN_CLASS,
                TOOL_BTN_IDLE,
                TOOL_BTN_HOVER,
                "focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring",
              )}
            >
              <Icon name="more" size={17} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side="right"
            align="start"
            collisionPadding={8}
            data-workbench-tool-menu
            data-workbench-video-tool-command={video ? "" : undefined}
            aria-label="更多工具"
            data-testid="tool-dock-menu"
            className="z-overlay-high w-64 data-[state=open]:animate-none data-[state=closed]:animate-none"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              restoreFocus();
            }}
          >
            <DropdownMenuRadioGroup value={activeId}>
              {overflowTools.map((tool, index) => (
                <Fragment key={tool.id}>
                  {(index === 0 || overflowTools[index - 1].group !== tool.group) && (
                    <>
                      {index > 0 && <DropdownMenuSeparator />}
                      <DropdownMenuLabel className="text-xs text-muted-foreground">
                        {GROUP_LABELS[tool.group]}
                      </DropdownMenuLabel>
                    </>
                  )}
                  <DropdownMenuRadioItem
                    value={tool.id}
                    disabled={!!tool.disabledReason}
                    aria-label={tool.label}
                    aria-describedby={
                      tool.disabledReason ? `dock-tool-reason-${tool.id}` : undefined
                    }
                    data-testid={`tool-overflow-item-${tool.id}`}
                    data-workbench-video-tool-command={video ? "" : undefined}
                    onSelect={tool.onSelect}
                  >
                    <Icon name={tool.icon} size={16} />
                    <span className="min-w-0 flex-1">
                      <span className="block">{tool.label}</span>
                      {tool.disabledReason && (
                        <span
                          id={`dock-tool-reason-${tool.id}`}
                          className="block text-xs text-muted-foreground"
                        >
                          {tool.disabledReason}
                        </span>
                      )}
                    </span>
                    {tool.hotkey && <DropdownMenuShortcut>{tool.hotkey}</DropdownMenuShortcut>}
                  </DropdownMenuRadioItem>
                </Fragment>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

export function ToolDock({
  tool,
  onSetTool,
  videoTool = "select",
  onSetVideoTool,
  videoToolScope,
  onSetVideoToolScope,
  isPromptSupported,
  toolDisabledReasons,
  capabilitiesLoading = false,
  reviewMode = false,
  videoMode = false,
  enabledToolUnits = null,
  aiInteractiveEnabled,
  isVideoToolEnabled,
  videoKeypointNodeCount = 0,
  threeDMode = false,
  threeDTool = "select",
  onSetThreeDTool,
  toolHotkeys,
}: ToolDockProps) {
  const effectiveHotkey = (
    commandId: string | undefined,
  ): { label: string; alt?: string } | null => {
    if (!commandId) return null;
    if (!toolHotkeys || !Object.prototype.hasOwnProperty.call(toolHotkeys, commandId)) return null;
    return toolHotkeys[commandId] ?? { label: "" };
  };
  if (threeDMode) {
    const visibleThreeDTools = THREE_D_TOOLS.filter((t) => {
      const unit = unitForThreeDTool(t.id);
      if (!unit || !enabledToolUnits) return true;
      return enabledToolUnits.has(unit);
    });
    return (
      <div className={ROOT_CLASS}>
        {visibleThreeDTools.map((t) => {
          const active = threeDTool === t.id;
          return (
            <Tooltip
              key={t.id}
              name={t.label}
              desc={t.desc}
              hotkey={t.hotkey}
              side="right"
              delay={250}
            >
              <button
                type="button"
                onClick={() => onSetThreeDTool?.(t.id)}
                aria-label={t.label}
                aria-pressed={active}
                data-testid={`three-d-tool-btn-${t.id}`}
                className={cn(
                  TOOL_BTN_CLASS,
                  active ? TOOL_BTN_ACTIVE : cn(TOOL_BTN_IDLE, TOOL_BTN_HOVER),
                )}
              >
                <Icon name={t.icon} size={17} />
                <span aria-hidden className={cn(HOTKEY_BADGE_CLASS, active && HOTKEY_BADGE_ACTIVE)}>
                  {t.hotkey}
                </span>
              </button>
            </Tooltip>
          );
        })}
      </div>
    );
  }
  if (videoMode) {
    const scope = videoToolScope ?? videoToolScopeForTool(videoTool) ?? "frame";
    const visibleVideoTools = VIDEO_TOOLS.filter((t) => {
      if (t.id === "select") return true;
      if (t.requiredPrompt && aiInteractiveEnabled === false) return false;
      if (!isVideoToolEnabled) return true;
      return isVideoToolEnabled(t.id);
    });
    const orderedTools = (["select", "frame", "sam", "track"] as const).flatMap((group) =>
      visibleVideoTools.filter((tool) => tool.group === group),
    );
    const tools: DockTool[] = orderedTools.map((t) => {
      const supported =
        !t.requiredPrompt || !isPromptSupported || isPromptSupported(t.requiredPrompt);
      const disabledReason =
        t.id === "keypoint" && videoKeypointNodeCount <= 0
          ? "请先在项目设置中配置关键点骨骼"
          : t.requiredPrompt && capabilitiesLoading
            ? "正在协商后端能力…"
            : !supported
              ? "当前后端不支持此交互模式"
              : undefined;
      return {
        id: t.id,
        group: t.group,
        label: t.label,
        icon: t.icon,
        hotkey: effectiveHotkey(VIDEO_TOOL_COMMAND[t.id])?.label ?? t.hotkey,
        description: (() => {
          const h = effectiveHotkey(VIDEO_TOOL_COMMAND[t.id]);
          if (h?.alt) return `${t.desc} · 备用 ${h.alt}`;
          return !h && t.altDigit ? `${t.desc} · 备用 Alt+${t.altDigit}` : t.desc;
        })(),
        disabledReason,
        testId: `video-tool-btn-${t.id}`,
        onSelect: () => onSetVideoTool?.(t.id),
      };
    });
    const scopedTools = tools.filter(
      (tool) => tool.id === "select" || videoToolScopeForTool(tool.id as VideoTool) === scope,
    );
    return (
      <div className="flex h-full min-h-0 shrink-0 flex-col">
        <div
          role="group"
          aria-label="视频作用范围"
          data-testid="video-tool-scope"
          data-scope={scope}
          className="flex shrink-0 flex-col items-center gap-1 border-b border-r border-border bg-card px-1 py-1.5"
        >
          {(["frame", "track"] as const).map((targetScope) => (
            <button
              key={targetScope}
              type="button"
              aria-label={targetScope === "frame" ? "单帧范围" : "轨迹范围"}
              aria-pressed={scope === targetScope}
              data-workbench-video-tool-command
              disabled={!onSetVideoToolScope}
              onClick={() => onSetVideoToolScope?.(targetScope)}
              className={cn(
                "h-7 w-[38px] shrink-0 cursor-pointer appearance-none rounded-md border text-2xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default",
                scope === targetScope
                  ? "border-brand bg-brand/10 text-brand"
                  : "border-transparent bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {targetScope === "frame" ? "单帧" : "轨迹"}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1">
          <AdaptiveToolDock tools={scopedTools} activeId={videoTool} video />
        </div>
      </div>
    );
  }

  const visibleTools = reviewMode
    ? ALL_TOOLS.filter((t) => t.id === "select")
    : ALL_TOOLS.filter((t) => {
        if (t.id === "select") return true;
        if (t.requiredPrompt && aiInteractiveEnabled === false) return false;
        if (!enabledToolUnits) return true;
        return enabledToolUnits.has(toolUnitForTool(t.id));
      });

  const isAITool = (t: CanvasTool) => !!t.requiredPrompt;
  const groupOf = (t: CanvasTool): "select" | "draw" | "ai" | "view" =>
    t.id === "select" ? "select" : t.id === "hand" ? "view" : isAITool(t) ? "ai" : "draw";

  const tools: DockTool[] = visibleTools.map((t) => {
    const descriptor = TOOL_DESCRIPTORS[t.id];
    const desc = descriptor?.desc ?? "";
    const supported =
      !t.requiredPrompt || !isPromptSupported || isPromptSupported(t.requiredPrompt);
    const disabledReason =
      toolDisabledReasons?.[t.id] ??
      (t.requiredPrompt && capabilitiesLoading
        ? "正在协商后端能力…"
        : !supported
          ? "当前后端不支持此交互模式"
          : undefined);
    const h = effectiveHotkey(IMAGE_TOOL_COMMAND[t.id]);
    return {
      id: t.id,
      group: groupOf(t),
      label: t.label,
      icon: t.icon as IconName,
      hotkey: h ? h.label : t.hotkey.toUpperCase(),
      description: h?.alt
        ? `${desc} · 备用 ${h.alt}`
        : !h && descriptor?.altDigit
          ? `${desc} · 备用 Alt+${descriptor.altDigit}`
          : desc,
      disabledReason,
      testId: `tool-btn-${t.id}`,
      onSelect: () => onSetTool(t.id),
    };
  });
  return <AdaptiveToolDock tools={tools} activeId={tool} />;
}
