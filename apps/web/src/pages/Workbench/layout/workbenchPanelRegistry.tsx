import type { ReactNode } from "react";
import type { CameraPresentation, PanelId, WorkspaceContext } from "./workbenchLayoutSnapshot";
import type { WorkspaceSide, WorkspaceSideState } from "./workbenchLayoutExecutor";
import type { ThreeDWorkspacePresetId } from "./workbenchLayoutPresets";

export type WorkbenchPanelSlots = Record<PanelId, ReactNode>;
export interface WorkbenchWorkspaceCommands {
  show(id: PanelId): void;
  hide(id: PanelId): void;
  toggle(id: PanelId): void;
  toggleSide(side: WorkspaceSide): void;
  setCameraPresentation(mode: CameraPresentation): void;
  applyThreeDPreset(preset: ThreeDWorkspacePresetId): void;
}
export interface WorkbenchWorkspaceState {
  sides: Record<WorkspaceSide, WorkspaceSideState>;
  taskQueueVisible: boolean;
  inspectorVisible: boolean;
  aiTaskVisible: boolean;
  videoTrackerVisible: boolean;
  /** True only while the tracking panel content is the displayed tab of a visible group. */
  videoTrackerContentVisible: boolean;
  triViewVisible: boolean;
  cameraViewVisible: boolean;
  cameraPresentation: CameraPresentation;
  canvasMaximized: boolean;
  taskQueueWidth: number;
  inspectorWidth: number;
  disabled: boolean;
}

const PANEL_SCOPE = { modes: ["annotate", "review"], stages: ["image", "video", "3d"] } as const;
const SIDE_CAPABILITIES = { dock: true, tab: true, float: true, hide: true } as const;

export const WORKBENCH_PANEL_REGISTRY = {
  canvas: {
    ...PANEL_SCOPE,
    capabilities: { dock: true, tab: false, float: false, hide: false },
    id: "canvas",
    title: "画布",
    renderer: "always",
    defaultPosition: "center",
    minWidth: 480,
    minHeight: 320,
    width: 800,
    height: 600,
    closable: false,
  },
  "task-queue": {
    ...PANEL_SCOPE,
    capabilities: SIDE_CAPABILITIES,
    id: "task-queue",
    title: "任务队列",
    renderer: "onlyWhenVisible",
    defaultPosition: "left",
    minWidth: 180,
    minHeight: 120,
    width: 220,
    height: 360,
    closable: false,
  },
  "class-palette": {
    ...PANEL_SCOPE,
    capabilities: SIDE_CAPABILITIES,
    id: "class-palette",
    title: "类别面板",
    renderer: "onlyWhenVisible",
    defaultPosition: "left",
    minWidth: 180,
    minHeight: 120,
    width: 220,
    height: 240,
    closable: false,
  },
  inspector: {
    ...PANEL_SCOPE,
    capabilities: SIDE_CAPABILITIES,
    id: "inspector",
    title: "标注详情",
    renderer: "always",
    defaultPosition: "right",
    minWidth: 220,
    minHeight: 160,
    width: 260,
    height: 360,
    closable: false,
  },
  discussion: {
    ...PANEL_SCOPE,
    capabilities: SIDE_CAPABILITIES,
    id: "discussion",
    title: "讨论",
    renderer: "always",
    defaultPosition: "below",
    minWidth: 220,
    minHeight: 160,
    width: 260,
    height: 240,
    closable: false,
  },
  "ai-task": {
    modes: ["annotate"],
    stages: ["image", "video"],
    capabilities: SIDE_CAPABILITIES,
    id: "ai-task",
    title: "单图 AI",
    renderer: "always",
    defaultPosition: "right",
    minWidth: 320,
    minHeight: 320,
    width: 360,
    height: 520,
    closable: false,
  },
  "video-tracker": {
    modes: ["annotate"],
    stages: ["video"],
    capabilities: SIDE_CAPABILITIES,
    id: "video-tracker",
    title: "视频追踪",
    renderer: "always",
    defaultPosition: "right",
    minWidth: 320,
    minHeight: 320,
    width: 360,
    height: 520,
    closable: false,
  },
  "tri-view": {
    modes: ["annotate", "review"],
    stages: ["3d"],
    capabilities: SIDE_CAPABILITIES,
    id: "tri-view",
    title: "三视图精修",
    renderer: "always",
    defaultPosition: "right",
    minWidth: 220,
    minHeight: 320,
    width: 240,
    height: 600,
    closable: false,
  },
  "camera-view": {
    modes: ["annotate", "review"],
    stages: ["3d"],
    capabilities: { ...SIDE_CAPABILITIES, float: false },
    id: "camera-view",
    title: "相机视图",
    renderer: "always",
    defaultPosition: "right",
    minWidth: 220,
    minHeight: 160,
    width: 240,
    height: 600,
    closable: false,
  },
} as const satisfies Record<
  PanelId,
  {
    id: PanelId;
    title: string;
    renderer: "always" | "onlyWhenVisible";
    defaultPosition: string;
    minWidth: number;
    minHeight: number;
    width: number;
    height: number;
    closable: false;
    modes: readonly string[];
    stages: readonly string[];
    capabilities: { dock: boolean; tab: boolean; float: boolean; hide: boolean };
  }
>;

export const PERIPHERAL_PANELS = [
  "task-queue",
  "class-palette",
  "inspector",
  "discussion",
  "ai-task",
  "video-tracker",
  "tri-view",
  "camera-view",
] as const;

export const defaultDockWidth = (width: number): number => Math.round(width * 0.15);

export const workbenchAiScopeLabel = (isVideoTask: boolean): "单图" | "单帧" =>
  isVideoTask ? "单帧" : "单图";

export function workbenchPanelTitle(id: PanelId, context: WorkspaceContext): string {
  return id === "ai-task"
    ? `${workbenchAiScopeLabel(context.endsWith(":video"))} AI`
    : WORKBENCH_PANEL_REGISTRY[id].title;
}

/** A docked panel must fit the preset sidebar; floating groups keep their own limits. */
export function panelMinimumWidth(id: PanelId, workspaceWidth: number): number {
  const minimum = WORKBENCH_PANEL_REGISTRY[id].minWidth;
  return id === "canvas" ? minimum : Math.min(minimum, defaultDockWidth(workspaceWidth));
}

export function panelSupportsContext(id: PanelId, context: WorkspaceContext): boolean {
  const [mode, stage] = context.split(":");
  const panel = WORKBENCH_PANEL_REGISTRY[id];
  return (
    panel.modes.some((value) => value === mode) && panel.stages.some((value) => value === stage)
  );
}
