import { useState } from "react";
import { Icon, type IconName } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import type { ToolId } from "../stage/tools";
import type { ThreeDTool, VideoTool } from "../state/useWorkbenchState";

export interface ToolGestureHintPillProps {
  tool?: ToolId;
  videoTool?: VideoTool;
  threeDTool?: ThreeDTool;
  stageKind?: "image" | "video" | "3d";
  reviewMode?: boolean;
  className?: string;
}

interface ToolHintMeta {
  label: string;
  hotkey?: string;
  icon: IconName;
  gestures: string;
}

const IMAGE_TOOL_HINTS: Record<ToolId, ToolHintMeta> = {
  select: {
    label: "选择",
    hotkey: "V",
    icon: "cursor",
    gestures: "单击选中 · 拖拽移动 · Del 删除 · ESC 取消",
  },
  box: {
    label: "矩形框",
    hotkey: "B",
    icon: "rect",
    gestures: "拖拽画框 · 按住 Shift 约束正方形 · ESC 取消",
  },
  "rotated-box": {
    label: "旋转框",
    hotkey: "W",
    icon: "rotatedBox",
    gestures: "拖框确定范围 · 拖动顶部手柄旋转 (OBB)",
  },
  polygon: {
    label: "多边形",
    hotkey: "P",
    icon: "polygon",
    gestures: "单击依次落点 · Enter/双击闭合 · Backspace 撤销点",
  },
  polyline: {
    label: "折线",
    hotkey: "L",
    icon: "spline",
    gestures: "单击依次落点 · Enter/双击结束 · Backspace 撤销点",
  },
  keypoint: {
    label: "关键点",
    hotkey: "F",
    icon: "crosshair",
    gestures: "按骨骼依次落点 · Alt 标记遮挡 · 右键跳过点",
  },
  mask: {
    label: "Mask 笔刷",
    hotkey: "M",
    icon: "brush",
    gestures: "拖拽涂抹 · B 笔刷/E 橡皮 · 滚轮调粗细 · Enter 确认",
  },
  "smart-point": {
    label: "SAM 智能点",
    hotkey: "S",
    icon: "sparkle",
    gestures: "左键加正点 · Alt/右键加负点 · Enter 生成分割",
  },
  "smart-box": {
    label: "SAM 智能框",
    hotkey: "D",
    icon: "sparkle",
    gestures: "拖框框选目标范围 · SAM 自动提取轮廓",
  },
  "smart-scribble": {
    label: "SAM 涂抹",
    hotkey: "C",
    icon: "brush",
    gestures: "涂抹目标轮廓 · Alt 负样本涂抹 · Enter 确认",
  },
  "magic-box": {
    label: "Magic Box",
    hotkey: "Shift+B",
    icon: "box",
    gestures: "绘制粗糙外接框 · SAM 自动收紧为紧凑边界框",
  },
  "text-prompt": {
    label: "文本提示",
    hotkey: "T",
    icon: "type",
    gestures: "在右侧 AI 面板输入自然语言关键词召回目标",
  },
  exemplar: {
    label: "相似示例",
    hotkey: "X",
    icon: "copy",
    gestures: "框选目标样本 · 全图自动召回相似实例",
  },
  hand: {
    label: "抓手",
    hotkey: "H",
    icon: "move",
    gestures: "拖拽平移画布 · 滚轮缩放 · 双击重置视口",
  },
  canvas: {
    label: "批注",
    icon: "messageSquareText",
    gestures: "画布批注与协作讨论",
  },
};

const VIDEO_TOOL_HINTS: Partial<Record<VideoTool, ToolHintMeta>> = {
  select: {
    label: "选择",
    hotkey: "V",
    icon: "cursor",
    gestures: "单击选中视频对象 · 拖拽调整位置",
  },
  box: {
    label: "单帧矩形框",
    hotkey: "B",
    icon: "rect",
    gestures: "拖拽绘制当前帧独立矩形框",
  },
  "rotated-box": {
    label: "单帧旋转框",
    hotkey: "W",
    icon: "rotatedBox",
    gestures: "绘制当前帧旋转框 · 拖动手柄调整角度",
  },
  keypoint: {
    label: "单帧关键点",
    hotkey: "F",
    icon: "crosshair",
    gestures: "依次落点 · Alt 标记遮挡 · 右键跳过",
  },
  polygon: {
    label: "单帧多边形",
    hotkey: "P",
    icon: "polygon",
    gestures: "单击落点 · Enter/双击闭合多边形",
  },
  polyline: {
    label: "单帧折线",
    hotkey: "L",
    icon: "spline",
    gestures: "单击落点 · Enter/双击结束折线",
  },
  "polygon-track": {
    label: "多边形轨迹",
    icon: "polygon",
    gestures: "关键帧绘制多边形 · 自动跨帧空间传播追踪",
  },
  "polyline-track": {
    label: "折线轨迹",
    icon: "spline",
    gestures: "关键帧绘制折线 · 自动跨帧空间传播追踪",
  },
  mask: {
    label: "单帧笔刷",
    hotkey: "M",
    icon: "brush",
    gestures: "涂抹当前帧像素 Mask · 滚轮调粗细",
  },
  "mask-track": {
    label: "Mask 轨迹",
    icon: "brush",
    gestures: "当前帧涂抹 Mask · 自动跨帧空间传播",
  },
  "smart-point": {
    label: "SAM 智能点",
    hotkey: "S",
    icon: "sparkle",
    gestures: "点选目标 · SAM 分割当前帧 · Alt 负点",
  },
  "smart-box": {
    label: "SAM 智能框",
    hotkey: "D",
    icon: "sparkle",
    gestures: "框选目标 · SAM 自动提取边界",
  },
  "magic-box": {
    label: "Magic Box",
    hotkey: "Shift+B",
    icon: "box",
    gestures: "绘制粗糙外接框 · SAM 自动收紧为紧凑边界框",
  },
  exemplar: {
    label: "相似示例",
    hotkey: "X",
    icon: "copy",
    gestures: "框选目标样本 · 视频帧自动召回相似实例",
  },
  track: {
    label: "目标轨迹",
    icon: "play",
    gestures: "首帧标记目标 · 自动向前/向后空间传播追踪",
  },
};

const THREE_D_TOOL_HINTS: Record<ThreeDTool, ToolHintMeta> = {
  select: {
    label: "3D 选择",
    icon: "cursor",
    gestures: "点击点云对象 · 轴向手柄调整位姿",
  },
  box: {
    label: "3D 框",
    icon: "box",
    gestures: "底面两点确定平面 · 拖拽确定高度与朝向",
  },
  "point-mask": {
    label: "点云笔刷",
    icon: "brush",
    gestures: "框选/笔刷圈选点云聚类 · 自动分割实例",
  },
  measure: {
    label: "3D 测量",
    icon: "ruler",
    gestures: "点选两点或表面 · 实时计算空间距离与包络",
  },
};

export function ToolGestureHintPill({
  tool = "select",
  videoTool,
  threeDTool,
  stageKind = "image",
  reviewMode,
  className,
}: ToolGestureHintPillProps) {
  const [collapsed, setCollapsed] = useState(false);

  let meta: ToolHintMeta;
  if (reviewMode) {
    meta = {
      label: "审阅模式",
      icon: "clipboardCheck",
      gestures: "检查标注质量 · A 键采纳 / R 键驳回",
    };
  } else if (stageKind === "video" && videoTool) {
    meta = VIDEO_TOOL_HINTS[videoTool] ?? IMAGE_TOOL_HINTS[tool] ?? IMAGE_TOOL_HINTS.select;
  } else if (stageKind === "3d" && threeDTool) {
    meta = THREE_D_TOOL_HINTS[threeDTool] ?? THREE_D_TOOL_HINTS.select;
  } else {
    meta = IMAGE_TOOL_HINTS[tool] ?? IMAGE_TOOL_HINTS.select;
  }

  return (
    <div
      aria-label="当前工具操作提示"
      data-testid="tool-gesture-hint-pill"
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border/80 bg-muted/60 py-0.5 pl-2 pr-2 text-2xs select-none transition-all duration-150",
        className,
      )}
    >
      <span className="flex items-center gap-1 font-semibold text-foreground">
        <Icon name={meta.icon} size={11} className="text-brand shrink-0" />
        <span>{meta.label}</span>
        {meta.hotkey && (
          <kbd className="rounded bg-background px-1 py-px font-mono text-3xs text-muted-foreground shadow-2xs">
            {meta.hotkey}
          </kbd>
        )}
      </span>
      {!collapsed ? (
        <>
          <span className="text-border">|</span>
          <span
            className="text-muted-foreground truncate max-w-[200px] sm:max-w-xs md:max-w-md lg:max-w-xl"
            title={meta.gestures}
          >
            {meta.gestures}
          </span>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            title="收起操作提示"
            aria-label="收起操作提示"
            className="ml-0.5 inline-flex size-3.5 cursor-pointer appearance-none items-center justify-center rounded-full text-muted-foreground/70 hover:bg-background hover:text-foreground"
          >
            <Icon name="x" size={9} />
          </button>
        </>
      ) : (
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          title={`展开 ${meta.label} 手势操作提示`}
          aria-label={`展开 ${meta.label} 手势操作提示`}
          className="ml-0.5 inline-flex size-3.5 cursor-pointer appearance-none items-center justify-center rounded-full text-muted-foreground/70 hover:bg-background hover:text-foreground"
        >
          <Icon name="chevRight" size={9} />
        </button>
      )}
    </div>
  );
}
