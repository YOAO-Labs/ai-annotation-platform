import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ComponentPropsWithoutRef,
  type ReactNode,
  type Ref,
} from "react";
import { ConflictModal } from "@/components/workbench/ConflictModal";
import type { VideoStageControls } from "../stage/videoStageControls";
import { VideoTrackerPropagateDialog } from "../stage/VideoTrackerPropagateDialog";
import { AIInspectorPanel, AIPredictionPopover } from "./AIInspectorPanel";
import { DeleteConfirmModal } from "./DeleteConfirmModal";
import { DiscussionPanel } from "./DiscussionPanel";
import type { FloatingPanelRect } from "./FloatingPanelShell";
import { clampFloatingPosition, type FloatingPanelPoint } from "./useDragMove";
import { SelectedAnnotationCard, type SelectedAnnotationCardProps } from "./SelectedAnnotationCard";
import { HotkeyCheatSheet } from "./HotkeyCheatSheet";
import { OfflineQueueDrawer } from "./OfflineQueueDrawer";
import { StatusBar } from "./StatusBar";
import { TaskQueuePanel } from "./TaskQueuePanel";
import { ToolDock } from "./ToolDock";
import { Topbar } from "./Topbar";
import { WorkbenchBanners } from "./WorkbenchBanners";
import { WorkbenchSettingsDialog } from "./WorkbenchSettingsDialog";
import { WorkbenchStageHost } from "./WorkbenchStageHost";
import {
  WorkbenchPet,
  WORKBENCH_PET_SIZE,
  readWorkbenchPetPosition,
  writeWorkbenchPetPosition,
  type WorkbenchPetDock,
  type WorkbenchPetProps,
} from "./pet/WorkbenchPet";
import { GuidePanel, guidePanelScopeKey } from "../sidebar/GuidePanel";
import {
  WorkbenchDockWorkspace,
  type WorkbenchDockWorkspaceProps,
} from "../layout/WorkbenchDockWorkspace";

const PET_CARRY_VISIBLE_HEIGHT = WORKBENCH_PET_SIZE.h / 2;

interface WorkbenchLayoutProps {
  workspace: Omit<WorkbenchDockWorkspaceProps, "slots" | "renderTopbar">;
  taskQueue: ComponentProps<typeof TaskQueuePanel>;
  toolDock: ComponentProps<typeof ToolDock>;
  banners: ComponentProps<typeof WorkbenchBanners>;
  topbar: ComponentProps<typeof Topbar>;
  stageHost: ComponentPropsWithoutRef<typeof WorkbenchStageHost>;
  /** 保持锚定画布的审阅与 modal 层。 */
  stageOverlay?: ReactNode;
  videoControlsRef: Ref<VideoStageControls>;
  statusBar: ComponentProps<typeof StatusBar>;
  inspector: ComponentProps<typeof AIInspectorPanel>;
  aiPopover: ComponentProps<typeof AIPredictionPopover>;
  videoTracker: ComponentProps<typeof VideoTrackerPropagateDialog>;
  hotkeys: ComponentProps<typeof HotkeyCheatSheet>;
  offlineQueue: ComponentProps<typeof OfflineQueueDrawer>;
  /** v0.15.3 · 工作台设置窗口(齿轮菜单入口)。 */
  workbenchSettings: ComponentProps<typeof WorkbenchSettingsDialog>;
  conflict: ComponentProps<typeof ConflictModal>;
  deleteConfirm?: ComponentProps<typeof DeleteConfirmModal>;
  // v0.10.13 · E1 · 顶栏标注指引入口 (可选; 项目无 guide 时不渲染).
  guidePanel?: ComponentProps<typeof GuidePanel>;
  // v0.11.5 · B 组 · 右栏下段统一讨论面板 (转正; 上 AIInspectorPanel + 下 DiscussionPanel 两段固定).
  discussionPanel: ComponentProps<typeof DiscussionPanel>;
  // v0.16.8 · 选中标注浮动信息卡(图片 / 视频);null = 当前无选中 / 该端不显示。
  floatingSelection?: SelectedAnnotationCardProps | null;
  // v0.20.x · 工作台桌宠;enabled=false 时不挂载,折叠态回退为纯文字小条。
  pet?: ({ enabled: boolean } & WorkbenchPetProps) | null;
}

function carriedSelectionFromPet(
  selectionPosition: FloatingPanelRect,
  petPosition: FloatingPanelPoint,
): FloatingPanelRect {
  const x = petPosition.x + WORKBENCH_PET_SIZE.w / 2 - selectionPosition.w / 2;
  const y = petPosition.y + PET_CARRY_VISIBLE_HEIGHT - selectionPosition.h;
  const clamped = clampFloatingPosition(
    { x, y },
    { w: selectionPosition.w, h: selectionPosition.h },
  );
  return { ...selectionPosition, x: clamped.x, y: clamped.y };
}

function petPositionFromCarriedSelection(panelPosition: FloatingPanelRect): FloatingPanelPoint {
  return clampFloatingPosition(
    {
      x: panelPosition.x + panelPosition.w / 2 - WORKBENCH_PET_SIZE.w / 2,
      y: panelPosition.y + panelPosition.h - PET_CARRY_VISIBLE_HEIGHT,
    },
    WORKBENCH_PET_SIZE,
  );
}

export function WorkbenchLayout({
  workspace,
  taskQueue,
  toolDock,
  banners,
  topbar,
  stageHost,
  stageOverlay,
  videoControlsRef,
  statusBar,
  inspector,
  aiPopover,
  videoTracker,
  hotkeys,
  offlineQueue,
  workbenchSettings,
  conflict,
  deleteConfirm,
  guidePanel,
  discussionPanel,
  floatingSelection,
  pet,
}: WorkbenchLayoutProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [petPosition, setPetPositionState] = useState(readWorkbenchPetPosition);
  const [threeDDetailsVisible, setThreeDDetailsVisible] = useState(false);
  const linkedFloatingSelection =
    pet?.enabled && floatingSelection && !floatingSelection.collapsed
      ? carriedSelectionFromPet(floatingSelection.position, petPosition)
      : null;

  const setPetPosition = useCallback((next: FloatingPanelPoint) => {
    const clamped = clampFloatingPosition(next, WORKBENCH_PET_SIZE);
    setPetPositionState(clamped);
    writeWorkbenchPetPosition(clamped);
  }, []);

  const petDock = useMemo<WorkbenchPetDock>(
    () => ({
      enabled: Boolean(pet?.enabled),
      position: petPosition,
      onPositionChange: setPetPosition,
      onDetailsVisibleChange: setThreeDDetailsVisible,
    }),
    [pet?.enabled, petPosition, setPetPosition],
  );

  const linkedSelectionPositionChange = useCallback(
    (patch: Partial<FloatingPanelRect>) => {
      if (!floatingSelection || !linkedFloatingSelection) return;
      floatingSelection.onPositionChange(patch);
      if (typeof patch.x === "number" || typeof patch.y === "number") {
        const nextPanelPosition = {
          ...linkedFloatingSelection,
          ...patch,
        };
        setPetPosition(petPositionFromCarriedSelection(nextPanelPosition));
      }
    },
    [floatingSelection, linkedFloatingSelection, setPetPosition],
  );

  // 3D / 点云画布在软件渲染下用 backdrop-filter 会拖慢合成清理，这类工作台只压暗。
  const dialogBackdropBlur = stageHost.common.stageKind !== "3d";

  return (
    <div ref={rootRef} className="relative flex h-full flex-col overflow-hidden bg-muted">
      <WorkbenchDockWorkspace
        {...workspace}
        onOpenLayoutSettings={topbar.onOpenWorkbenchSettings}
        renderTopbar={(menu, state, layoutSettings) => (
          <>
            <Topbar
              {...topbar}
              guideSlot={
                guidePanel ? (
                  <GuidePanel
                    key={guidePanelScopeKey(guidePanel)}
                    {...guidePanel}
                    projectName={topbar.projectName}
                    backdropBlur={dialogBackdropBlur}
                  />
                ) : undefined
              }
              layoutMenuSlot={menu}
              layoutDisabled={state.disabled}
              sides={state.sides}
            />
            <WorkbenchSettingsDialog
              {...workbenchSettings}
              layoutSettings={layoutSettings}
              backdropBlur={dialogBackdropBlur}
            />
          </>
        )}
        slots={{
          canvas: (
            <div className="flex h-full min-h-0 min-w-0 overflow-hidden" data-workbench-canvas>
              <ToolDock {...toolDock} />
              <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
                <WorkbenchBanners {...banners} />
                <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
                  <WorkbenchStageHost
                    ref={videoControlsRef}
                    {...stageHost}
                    petDock={petDock}
                    common={
                      stageHost.common.stageKind === "video"
                        ? {
                            ...stageHost.common,
                            overlays: (
                              <>
                                {stageHost.common.overlays}
                                {stageOverlay}
                              </>
                            ),
                          }
                        : stageHost.common
                    }
                  />
                  {stageHost.common.stageKind !== "video" && stageOverlay}
                </div>
                <StatusBar
                  {...statusBar}
                  toolHint={
                    statusBar.toolHint ?? {
                      tool: toolDock.tool,
                      videoTool: toolDock.videoTool,
                      threeDTool: toolDock.threeDTool,
                      stageKind: stageHost.common.stageKind,
                      reviewMode: toolDock.reviewMode,
                    }
                  }
                />
              </div>
            </div>
          ),
          "task-queue": (
            <TaskQueuePanel {...taskQueue} open floatingSection="queue" onDetachQueue={undefined} />
          ),
          "class-palette": (
            <TaskQueuePanel
              {...taskQueue}
              open
              floatingSection="palette"
              onDetachPalette={undefined}
            />
          ),
          inspector: <AIInspectorPanel {...inspector} open floating onDetach={undefined} />,
          discussion: <DiscussionPanel {...discussionPanel} floating onDetach={undefined} />,
          "ai-task": <AIPredictionPopover {...aiPopover} />,
          "video-tracker": <VideoTrackerPropagateDialog {...videoTracker} />,
          "tri-view": null,
          "camera-view": null,
        }}
      />

      {/* 桌宠开启时吃掉折叠态:折叠小条不渲染(由像素小人举牌代替),仅展开态渲染完整卡。 */}
      {floatingSelection && (!pet?.enabled || !floatingSelection.collapsed) && (
        <SelectedAnnotationCard
          {...floatingSelection}
          position={linkedFloatingSelection ?? floatingSelection.position}
          onPositionChange={
            linkedFloatingSelection
              ? linkedSelectionPositionChange
              : floatingSelection.onPositionChange
          }
          linkedToPet={Boolean(linkedFloatingSelection)}
        />
      )}
      {pet?.enabled && (
        <WorkbenchPet
          context={pet.context}
          position={petPosition}
          onPositionChange={setPetPosition}
          detailsVisible={
            Boolean(linkedFloatingSelection) ||
            (stageHost.common.stageKind === "3d" && threeDDetailsVisible)
          }
          onExpand={pet.onExpand}
        />
      )}

      <HotkeyCheatSheet {...hotkeys} />
      <OfflineQueueDrawer {...offlineQueue} />
      <ConflictModal {...conflict} />
      {deleteConfirm && <DeleteConfirmModal {...deleteConfirm} />}
    </div>
  );
}
