import { createContext, useContext } from "react";
import type { ClientRectSnapshot } from "../stages/three-d/PointCloudTriViewPass";
import type { WorkbenchWorkspaceCommands } from "./workbenchPanelRegistry";

export type ThreeDPanelId = "tri-view" | "camera-view";
export interface ThreeDLayoutActions {
  resetCameras(): void;
}

export const Workbench3DLayoutContext = createContext<{
  renderSurface: HTMLDivElement | null;
  targets: Record<ThreeDPanelId, HTMLDivElement | null>;
  setTarget(id: ThreeDPanelId, element: HTMLDivElement | null): void;
  panelVisible: Record<ThreeDPanelId, boolean>;
  setPanelVisible(id: ThreeDPanelId, visible: boolean): void;
  layoutKey: number;
  getVisibleRegions(element: HTMLElement): readonly ClientRectSnapshot[];
  cameraPresentation: "floating" | "docked";
  cameraVisible: boolean;
  disabled: boolean;
  commands: WorkbenchWorkspaceCommands;
  registerActions(actions: ThreeDLayoutActions | null): void;
} | null>(null);

export const useWorkbench3DLayout = () => useContext(Workbench3DLayoutContext);
