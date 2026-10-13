import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
import {
  DockviewReact,
  type DockviewApi,
  type IDockviewPanelProps,
  type IDockviewPanelHeaderProps,
  type IDockviewHeaderActionsProps,
  type DockviewWillShowOverlayLocationEvent,
  type DockviewWillDropEvent,
  type DroptargetOverlayModel,
} from "dockview-react";
import { toast } from "sonner";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import { Icon } from "@/components/ui/Icon";
import { Tooltip } from "@/components/ui/Tooltip";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { useAuthStore } from "@/stores/authStore";
import { cn } from "@/lib/utils";
import { useActiveIssueStore } from "../state/useActiveIssueStore";
import {
  useWorkbenchNamedPresets,
  type NamedPresetFailure,
  type WorkbenchNamedPreset,
} from "../state/useWorkbenchNamedPresets";
import { useWorkbenchWorkspaceLayout } from "../state/useWorkbenchWorkspaceLayout";
import { createWorkbenchLayoutExecutor } from "./workbenchLayoutExecutor";
import {
  createWorkspacePreset,
  getActiveWorkspacePreset,
  migrateLegacyWorkspace,
  presetSupportsContext,
  type WorkspacePresetId,
  type ActiveWorkspacePreset,
} from "./workbenchLayoutPresets";
import {
  WorkbenchLayoutQuickMenu,
  WorkbenchLayoutSettings,
  type NamedPresetControls,
} from "./WorkbenchLayoutSettings";
import type { PanelId, WorkspaceContext } from "./workbenchLayoutSnapshot";
import {
  PERIPHERAL_PANELS,
  WORKBENCH_PANEL_REGISTRY,
  defaultDockWidth,
  panelMinimumWidth,
  panelSupportsContext,
  type WorkbenchPanelSlots,
  type WorkbenchWorkspaceCommands,
  type WorkbenchWorkspaceState,
} from "./workbenchPanelRegistry";
import "dockview-react/dist/styles/dockview.css";
import styles from "./WorkbenchDockWorkspace.module.css";
import {
  Workbench3DLayoutContext,
  type ThreeDLayoutActions,
  type ThreeDPanelId,
  useWorkbench3DLayout,
} from "./Workbench3DLayoutContext";
import { createWorkbenchViewportRegions } from "./workbenchViewportRegions";
import { THREE_D_LAYOUT_PRESETS } from "../stages/three-d/ThreeDWorkbench.helpers";

export interface WorkbenchDockWorkspaceProps {
  context: WorkspaceContext;
  legacy: Parameters<typeof migrateLegacyWorkspace>[0];
  commandsRef?: Ref<WorkbenchWorkspaceCommands>;
  onStateChange?: (state: WorkbenchWorkspaceState) => void;
  slots: WorkbenchPanelSlots;
  onOpenLayoutSettings?: () => void;
  renderTopbar: (
    menu: ReactNode,
    state: WorkbenchWorkspaceState,
    layoutSettings: ReactNode,
  ) => ReactNode;
}

type Executor = ReturnType<typeof createWorkbenchLayoutExecutor>;
const SlotsContext = createContext<WorkbenchPanelSlots | null>(null);
const PanelCloseContext = createContext<{ hide: (id: PanelId) => void; disabled: boolean }>({
  hide: () => {},
  disabled: true,
});

function isPanelContentVisible(api: IDockviewPanelProps["api"] | undefined): boolean {
  return Boolean(
    api &&
    api.group.id !== "parking" &&
    api.group.activePanel?.id === api.id &&
    api.isVisible &&
    api.group.api.isVisible,
  );
}

// Dockview owns these stable React portals. Reparenting a panel does not recreate its content.
function PanelContent({ api, containerApi }: IDockviewPanelProps) {
  const slots = useContext(SlotsContext)!;
  const layout = useWorkbench3DLayout();
  const setTarget = layout?.setTarget;
  const setPanelVisible = layout?.setPanelVisible;
  const [visible, setVisible] = useState(api.isVisible);
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (element.current) element.current.inert = !visible;
    if (api.id === "tri-view" || api.id === "camera-view") setPanelVisible?.(api.id, visible);
  }, [api.id, setPanelVisible, visible]);
  useLayoutEffect(() => {
    if (api.id !== "tri-view" && api.id !== "camera-view") return;
    setTarget?.(api.id, element.current);
    return () => setTarget?.(api.id as ThreeDPanelId, null);
  }, [api.id, setTarget]);
  useEffect(() => {
    const update = () => setVisible(isPanelContentVisible(api));
    let groupVisibility = api.group.api.onDidVisibilityChange(update);
    const subscriptions = [
      api.onDidVisibilityChange(update),
      api.onDidActiveChange(update),
      api.onDidGroupChange(() => {
        groupVisibility.dispose();
        groupVisibility = api.group.api.onDidVisibilityChange(update);
        update();
      }),
      containerApi.onDidLayoutChange(update),
    ];
    update();
    return () => {
      groupVisibility.dispose();
      subscriptions.forEach((subscription) => subscription.dispose());
    };
  }, [api, containerApi]);
  return (
    <div
      ref={element}
      className={styles.panel}
      data-workbench-panel={api.id}
      aria-hidden={!visible}
    >
      {slots[api.id as PanelId]}
    </div>
  );
}

function PanelHeaderActions({ group }: IDockviewHeaderActionsProps) {
  const [id, setId] = useState(group.activePanel?.id as PanelId | undefined);
  useEffect(() => {
    const update = () => setId(group.activePanel?.id as PanelId | undefined);
    const subscription = group.api.onDidActivePanelChange(update);
    update();
    return () => subscription.dispose();
  }, [group]);
  const layout = useWorkbench3DLayout();
  if (id !== "camera-view") return null;
  return (
    <div className="flex h-full items-center gap-1 pr-1" data-workbench-layout-control>
      {id === "camera-view" && (
        <button
          type="button"
          className="rounded-sm px-1 text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
          disabled={layout?.disabled}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            layout?.commands.setCameraPresentation("floating");
          }}
        >
          悬浮显示
        </button>
      )}
    </div>
  );
}

function PanelTab({ api }: IDockviewPanelHeaderProps) {
  const id = api.id as PanelId;
  const { hide, disabled } = useContext(PanelCloseContext);
  return (
    <div className="flex h-full items-center gap-1 pl-2 text-xs" data-workbench-layout-control>
      <span>{WORKBENCH_PANEL_REGISTRY[api.id as PanelId].title}</span>
      {WORKBENCH_PANEL_REGISTRY[id].capabilities.hide && (
        <Tooltip name={`隐藏${WORKBENCH_PANEL_REGISTRY[id].title}`} side="bottom">
          <button
            type="button"
            aria-label={`隐藏${WORKBENCH_PANEL_REGISTRY[id].title}`}
            disabled={disabled}
            className="inline-flex size-6 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              hide(id);
            }}
          >
            <Icon name="x" size={14} />
          </button>
        </Tooltip>
      )}
    </div>
  );
}
const components = { "workbench-panel": PanelContent };
const theme = { name: "workbench", className: "workbench-dock-theme" };
const EMPTY_STATE: WorkbenchWorkspaceState = {
  sides: { left: "empty", right: "empty" },
  taskQueueVisible: true,
  inspectorVisible: true,
  aiTaskVisible: false,
  videoTrackerVisible: false,
  videoTrackerContentVisible: false,
  triViewVisible: false,
  cameraViewVisible: false,
  cameraPresentation: "floating",
  canvasMaximized: false,
  taskQueueWidth: 220,
  inspectorWidth: 260,
  disabled: true,
};
const PRESET_LABELS: Record<WorkspacePresetId, string> = {
  standard: "标准标注",
  focus: "专注画布",
  review: "审核协作",
  "ai-review": "图片 AI 审阅",
  "video-tracking": "视频追踪",
};
const NAMED_PRESET_ERRORS: Record<NamedPresetFailure, string> = {
  "invalid-name": "预设名称不能为空，且不超过 40 个字。",
  "duplicate-name": "已有同名预设，请换一个名称。",
  limit: "最多保存 5 组布局预设，请先删除一组。",
  conflict: "预设已在其他设备更新，已刷新最新内容，请重试。",
  request: "预设暂未保存，请稍后重试。",
};

export function WorkbenchDockWorkspace(props: WorkbenchDockWorkspaceProps) {
  const { context, commandsRef, slots, renderTopbar } = props;
  const user = useAuthStore((state) => state.user);
  const userId = user?.id;
  const legacyAccount = useRef(userId);
  const compact = useMediaQuery("(max-width: 1024px)");
  const availablePanels = useMemo(
    () => PERIPHERAL_PANELS.filter((id) => panelSupportsContext(id, context)),
    [context],
  );
  const host = useRef<HTMLDivElement>(null);
  const [renderSurface, setRenderSurface] = useState<HTMLDivElement | null>(null);
  const [targets, setTargets] = useState<Record<ThreeDPanelId, HTMLDivElement | null>>({
    "tri-view": null,
    "camera-view": null,
  });
  const setTarget = useCallback((id: ThreeDPanelId, element: HTMLDivElement | null) => {
    setTargets((previous) =>
      previous[id] === element ? previous : { ...previous, [id]: element },
    );
  }, []);
  const [panelVisible, setPanelVisibility] = useState<Record<ThreeDPanelId, boolean>>({
    "tri-view": false,
    "camera-view": false,
  });
  const setPanelVisible = useCallback((id: ThreeDPanelId, visible: boolean) => {
    setPanelVisibility((previous) =>
      previous[id] === visible ? previous : { ...previous, [id]: visible },
    );
  }, []);
  const [layoutKey, setLayoutKey] = useState(0);
  const regions = useRef<ReturnType<typeof createWorkbenchViewportRegions> | null>(null);
  const getVisibleRegions = useCallback(
    (element: HTMLElement) =>
      regions.current?.getVisibleRegions(element) ?? [element.getBoundingClientRect()],
    [],
  );
  const [threeDActions, registerActions] = useState<ThreeDLayoutActions | null>(null);
  const bounds = useCallback(
    () => ({
      width: host.current?.clientWidth || window.innerWidth,
      height: host.current?.clientHeight || Math.max(320, window.innerHeight - 100),
    }),
    [],
  );
  // Legacy preferences are a one-time seed, never a second layout owner.
  const fallback = useMemo(() => {
    const seed =
      legacyAccount.current === userId ? props.legacy : (user?.preferences?.workbench ?? {});
    legacyAccount.current = userId;
    let rightSplitTop: number | undefined;
    try {
      const stored = localStorage.getItem("workbench.rightSplit.topHeight");
      if (stored !== null && Number.isFinite(Number(stored))) rightSplitTop = Number(stored);
    } catch {
      /* Account preferences still work when local storage is unavailable. */
    }
    return migrateLegacyWorkspace({ ...seed, rightSplitTop }, bounds());
  }, [context, userId]); // eslint-disable-line react-hooks/exhaustive-deps
  const standard = useMemo(
    () => createWorkspacePreset("standard", bounds(), context),
    [bounds, context],
  );
  const owner = useWorkbenchWorkspaceLayout(context, fallback, standard, compact);
  const [api, setApi] = useState<DockviewApi | null>(null);
  const dropOverlays = useMemo(
    () => ({
      content: { smallWidthBoundary: 0 } as DroptargetOverlayModel,
      edge: {
        smallWidthBoundary: 0,
        activationSize: { type: "pixels", value: 10 },
      } as DroptargetOverlayModel,
    }),
    [],
  );
  const dropOverlayModel = useCallback(
    ({ location }: { location: string }) =>
      location === "content" ? dropOverlays.content : undefined,
    [dropOverlays],
  );
  const executor = useRef<Executor | null>(null);
  const latest = useRef({ owner, compact, onStateChange: props.onStateChange });
  latest.current = { owner, compact, onStateChange: props.onStateChange };
  const [view, setView] = useState(EMPTY_STATE);
  const [activePreset, setActivePreset] = useState<ActiveWorkspacePreset>("custom");
  const published = useRef(EMPTY_STATE);
  const [visiblePanels, setVisiblePanels] = useState<PanelId[]>([...PERIPHERAL_PANELS]);
  const restoring = useRef(false);
  const pointerDown = useRef(false);
  const interaction = useRef(false);
  const pendingFrame = useRef<number | null>(null);
  const undo = useRef<string | number | null>(null);
  const hydration = useRef<{ api: DockviewApi; session: string; revision: number } | null>(null);

  const publish = useCallback(() => {
    const engine = executor.current;
    if (!api || !engine) return;
    engine.syncConstraints();
    // Capturing a maximized layout exits/reenters maximization and emits layout changes.
    setActivePreset(
      engine.isCanvasMaximized() ? "focus" : getActiveWorkspacePreset(engine.capture(), context),
    );
    const opened = PERIPHERAL_PANELS.filter((id) => engine.isVisible(id));
    const next = {
      sides: engine.getSides(),
      taskQueueVisible: opened.includes("task-queue"),
      inspectorVisible: opened.includes("inspector"),
      aiTaskVisible: opened.includes("ai-task"),
      videoTrackerVisible: opened.includes("video-tracker"),
      videoTrackerContentVisible: isPanelContentVisible(api.getPanel("video-tracker")?.api),
      triViewVisible: opened.includes("tri-view"),
      cameraViewVisible: opened.includes("camera-view"),
      cameraPresentation: engine.getCameraPresentation(),
      canvasMaximized: engine.isCanvasMaximized(),
      taskQueueWidth: api.getPanel("task-queue")?.api.width ?? 220,
      inspectorWidth: api.getPanel("inspector")?.api.width ?? 260,
      disabled: latest.current.owner.readOnly,
    };
    if (JSON.stringify(published.current) !== JSON.stringify(next)) {
      published.current = next;
      setView(next);
      latest.current.onStateChange?.(next);
    }
    setVisiblePanels((previous) => (previous.join() === opened.join() ? previous : opened));
  }, [api, context]);

  const persist = useCallback(() => {
    if (pendingFrame.current !== null) cancelAnimationFrame(pendingFrame.current);
    pendingFrame.current = requestAnimationFrame(() => {
      pendingFrame.current = null;
      if (restoring.current || !executor.current) return;
      publish();
      if (
        !interaction.current ||
        pointerDown.current ||
        executor.current.isCompact() ||
        latest.current.owner.readOnly
      )
        return;
      interaction.current = false;
      try {
        latest.current.owner.save(executor.current.capture());
      } catch {
        latest.current.owner.failRestore();
      }
    });
  }, [publish]);

  const run = useCallback(
    (action: (engine: Executor) => void, allowCompact = false) => {
      const engine = executor.current;
      if (!engine || latest.current.owner.readOnly || (!allowCompact && engine.isCompact())) return;
      restoring.current = true;
      try {
        action(engine);
        publish();
        if (!engine.isCompact()) latest.current.owner.save(engine.capture());
      } catch {
        latest.current.owner.failRestore();
      } finally {
        restoring.current = false;
      }
    },
    [publish],
  );

  const previousWidths = useRef(props.legacy.common);
  useEffect(() => {
    const previous = previousWidths.current;
    const next = props.legacy.common;
    previousWidths.current = next;
    if (!previous || !next) return;
    for (const side of ["left", "right"] as const) {
      const key = side === "left" ? "leftWidthPct" : "rightWidthPct";
      if (next[key] !== undefined && next[key] !== previous[key])
        run((engine) => engine.resizeSide(side, next[key]!));
    }
  }, [props.legacy.common, run]);

  const commands = useMemo<WorkbenchWorkspaceCommands>(
    () => ({
      toggleSide: (side) => run((engine) => engine.toggleSide(side)),
      setCameraPresentation: (mode) => run((engine) => engine.setCameraPresentation(mode)),
      applyThreeDPreset: (id) => run((engine) => engine.applyThreeDPreset(id)),
      show: (id) => {
        if (panelSupportsContext(id, context)) run((engine) => engine.show(id), true);
      },
      hide: (id) => run((engine) => engine.hide(id), true),
      toggle: (id) => {
        if (panelSupportsContext(id, context))
          run((engine) => (engine.isVisible(id) ? engine.hide(id) : engine.show(id)), true);
      },
    }),
    [context, run],
  );
  useImperativeHandle(commandsRef, () => commands, [commands]);

  const dismissUndo = useCallback(() => {
    if (undo.current !== null) toast.dismiss(undo.current);
    undo.current = null;
  }, []);
  const preset = (id: WorkspacePresetId) =>
    run((engine) => {
      const before = engine.capture();
      engine.applyPreset(id);
      undo.current = toast(`已切换到${PRESET_LABELS[id]}布局`, {
        id: undo.current ?? undefined,
        action: { label: "撤销", onClick: () => run((current) => current.restore(before)) },
        duration: 8000,
        position: "bottom-center",
      });
    });
  const reset = () => {
    if (
      compact ||
      !owner.initialized ||
      owner.readOnlyReason === "newer-schema" ||
      !executor.current
    )
      return;
    restoring.current = true;
    try {
      dismissUndo();
      executor.current.applyPreset("standard");
      owner.reset(executor.current.capture());
      publish();
    } catch {
      owner.failRestore();
    } finally {
      restoring.current = false;
    }
  };

  const named = useWorkbenchNamedPresets();
  const report = (failure: NamedPresetFailure | null, done: string) => {
    if (failure === null) toast.success(done, { position: "bottom-center" });
    else toast.error(NAMED_PRESET_ERRORS[failure], { position: "bottom-center" });
  };
  const applyNamedPreset = (entry: WorkbenchNamedPreset) => {
    const snapshot = entry.snapshot;
    if (!snapshot || entry.context !== context) return;
    run((engine) => {
      const before = engine.capture();
      engine.restore(snapshot);
      undo.current = toast(`已应用「${entry.name}」布局`, {
        id: undo.current ?? undefined,
        action: { label: "撤销", onClick: () => run((current) => current.restore(before)) },
        duration: 8000,
        position: "bottom-center",
      });
    });
  };
  const namedPresetControls: NamedPresetControls = {
    presets: named.presets,
    context,
    count: named.count,
    full: named.full,
    busy: named.saving,
    disabled: owner.readOnly || compact || !named.loaded,
    onSave: (name) => {
      const engine = executor.current;
      if (!engine || owner.readOnly || compact) return;
      void named
        .save(name, context, engine.capture())
        .then((failure) => report(failure, `已保存布局预设「${name.trim()}」`));
    },
    onApply: applyNamedPreset,
    onRename: (id, name) =>
      void named.rename(id, name).then((failure) => report(failure, "已重命名布局预设")),
    onRemove: (entry) =>
      void named.remove(entry.id).then((failure) => report(failure, `已删除「${entry.name}」`)),
  };
  useLayoutEffect(() => {
    if (!api) return;
    const session = `${userId ?? "anonymous"}:${context}`;
    const previous = hydration.current;
    if (
      previous?.api === api &&
      previous.session === session &&
      previous.revision === owner.restoreRevision
    )
      return;
    restoring.current = true;
    interaction.current = false;
    dismissUndo();
    const engine =
      previous?.api === api && previous.session === session
        ? executor.current!
        : createWorkbenchLayoutExecutor(api, bounds);
    executor.current = engine;
    try {
      let maximizeAfterHydration = false;
      // The only whole-tree restore seam: cold start and the settled initial authority.
      if (
        !previous ||
        previous.api !== api ||
        previous.session !== session ||
        !owner.readOnlyReason
      ) {
        engine.setPresentation(owner.snapshot);
        if (engine.isCanvasMaximized()) engine.toggleCanvasMaximized();
        const layout = structuredClone(owner.snapshot.layout);
        maximizeAfterHydration = !!layout.grid.maximizedNode;
        delete layout.grid.maximizedNode;
        api.fromJSON(layout, { reuseExistingPanels: true });
        engine.setReturns(owner.snapshot.returns);
      } else {
        engine.setPresentation(owner.snapshot);
        engine.recover(owner.snapshot);
      }
      hydration.current = { api, session, revision: owner.restoreRevision };
      for (const panel of api.panels) {
        const spec = WORKBENCH_PANEL_REGISTRY[panel.id as PanelId];
        panel.api.setRenderer(spec.renderer);
        panel.api.setConstraints({
          minimumWidth: panelMinimumWidth(panel.id as PanelId, bounds().width),
          minimumHeight: spec.minHeight,
        });
      }
      const canvas = api.groups.find((group) => group.id === "canvas");
      if (canvas) {
        canvas.api.locked = true;
        canvas.header.hidden = true;
      }
      const parking = api.groups.find((group) => group.id === "parking");
      if (parking) {
        parking.api.locked = "no-drop-target";
        parking.header.hidden = true;
        parking.api.setVisible(false);
      }
      for (const id of PERIPHERAL_PANELS) if (!availablePanels.includes(id)) engine.hide(id);
      if (maximizeAfterHydration) {
        engine.syncConstraints();
        engine.toggleCanvasMaximized();
      }
      if (compact && owner.initialized) engine.enterCompact();
      publish();
    } catch {
      hydration.current = { api, session, revision: owner.restoreRevision };
      owner.failRestore();
    } finally {
      restoring.current = false;
    }
  }, [api, availablePanels, bounds, compact, context, dismissUndo, owner, publish, userId]);

  useLayoutEffect(() => {
    const engine = executor.current;
    if (!api || !engine || !owner.initialized || compact === engine.isCompact()) return;
    restoring.current = true;
    interaction.current = false;
    try {
      dismissUndo();
      if (compact && !engine.isCompact()) engine.enterCompact();
      if (!compact && engine.isCompact()) {
        if (engine.exitCompact()) owner.save(engine.capture());
      }
      publish();
    } catch {
      owner.failRestore();
    } finally {
      restoring.current = false;
    }
  }, [api, compact, owner, publish, dismissUndo]);

  useEffect(() => {
    if (!api) return;
    api.updateOptions({ locked: owner.readOnly || compact, disableDnd: owner.readOnly || compact });
    publish();
  }, [api, owner.readOnly, compact, publish]);

  useEffect(() => {
    if (!api) return;
    const blocked = () => latest.current.owner.readOnly || latest.current.compact;
    // Overlay-rendered panel content (e.g. the canvas) sits above its Dockview
    // group. Dockview forwards overlay DnD to the group, but in this layout the
    // forwarded HTML5 target did not activate, so the group's drop target never
    // received dragover/drop and the dock was cancelled. While an HTML5 dock
    // drag is active, make the overlay layer transparent so the drag hit-tests
    // the owning group. Scoped to HTML5 drags only: Dockview also arms a pointer
    // drag source, and Shift+pointer is the float gesture, which must keep the
    // canvas overlay interactive.
    const setOverlayPassthrough = (active: boolean) => {
      host.current?.classList.toggle(styles.dragPassthrough, active);
    };
    // Dockview arms both an HTML5 and a pointer drag source. Only the HTML5
    // backend needs the overlay passthrough; Shift+pointer is Dockview's float
    // gesture and must keep the canvas overlay interactive.
    const armOverlayPassthrough = (nativeEvent: DragEvent | PointerEvent) => {
      if (!(nativeEvent instanceof DragEvent) || nativeEvent.shiftKey) return;
      setOverlayPassthrough(true);
    };
    const guardShiftFloat = (event: PointerEvent) => {
      if (!event.shiftKey || !(event.target instanceof HTMLElement)) return;
      const tab = event.target.closest<HTMLElement>(".dv-tab");
      const groupSpace = event.target.closest(".dv-void-container");
      const cameraGroup =
        groupSpace &&
        api.groups.some(
          (group) =>
            group.element.contains(groupSpace) &&
            group.panels.some((panel) => panel.id === "camera-view"),
        );
      if (tab?.dataset.tabPanelId === "camera-view" || cameraGroup) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    const workspaceHost = host.current;
    workspaceHost?.addEventListener("pointerdown", guardShiftFloat, true);
    let restoreDragSizes: ReturnType<Executor["preserveGridSizes"]> | undefined;
    let movedPanel: string | undefined;
    let movedGroup: string | undefined;
    const guardDrop = (event: DockviewWillDropEvent | DockviewWillShowOverlayLocationEvent) => {
      const source = event.getData();
      const target = event.group;
      if (
        blocked() ||
        source?.panelId === "canvas" ||
        source?.groupId === "canvas" ||
        target?.id === "parking" ||
        (target?.id === "canvas" && event.position === "center") ||
        target?.api.location.type === "popout" ||
        (target?.api.location.type === "floating" &&
          (source?.panelId === "camera-view" ||
            api.groups
              .find((group) => group.id === source?.groupId)
              ?.panels.some((panel) => panel.id === "camera-view"))) ||
        (target?.api.location.type === "floating" && event.position !== "center")
      )
        event.preventDefault();
    };
    const subscriptions = [
      api.onWillMutateLayout((event) => {
        if (
          !blocked() &&
          event.origin === "user" &&
          ["move", "float", "remove"].includes(event.kind)
        )
          restoreDragSizes ??= executor.current?.preserveGridSizes();
      }),
      api.onDidMutateLayout(() => {
        const restore = restoreDragSizes;
        restoreDragSizes = undefined;
        if (!restore) return;
        try {
          // A root-edge restructure can re-expose the reserved parking group
          // into the grid; re-assert the owner invariant before restoring sizes.
          executor.current?.reassertReservedGroups();
          restore(movedPanel ? api.getPanel(movedPanel)?.group.id : movedGroup);
        } catch {
          latest.current.owner.failRestore();
        }
        movedPanel = movedGroup = undefined;
      }),
      api.onWillDragPanel((event) => {
        if (blocked() || event.panel.id === "canvas") {
          event.nativeEvent.preventDefault();
          return;
        }
        armOverlayPassthrough(event.nativeEvent);
      }),
      api.onWillDragGroup((event) => {
        if (blocked() || event.group.id === "canvas" || event.group.id === "parking") {
          event.nativeEvent.preventDefault();
          return;
        }
        armOverlayPassthrough(event.nativeEvent);
      }),
      api.onWillShowOverlay((event) => {
        guardDrop(event);
        if (event.defaultPrevented) return;
        const horizontal = event.position === "left" || event.position === "right";
        const width = defaultDockWidth(api.width);
        // Dockview reads these models immediately after this event, before painting.
        dropOverlays.content.size = horizontal
          ? { type: "pixels", value: width }
          : { type: "percentage", value: 50 };
        dropOverlays.edge.size = horizontal
          ? { type: "pixels", value: width }
          : { type: "pixels", value: 20 };
      }),
      api.onWillDrop((event) => {
        guardDrop(event);
        if (!event.defaultPrevented) {
          // Root-edge drops create a grid branch before Dockview emits its mutation event.
          restoreDragSizes ??= executor.current?.preserveGridSizes();
          const source = event.position !== "center" ? event.getData() : undefined;
          movedPanel = source?.panelId ?? undefined;
          movedGroup = source?.groupId;
        }
      }),
      api.onDidLayoutChange(persist),
      api.onDidActivePanelChange(persist),
      api.onDidMovePanel(persist),
    ];
    const release = () => {
      restoreDragSizes = undefined;
      movedPanel = movedGroup = undefined;
      pointerDown.current = false;
      setOverlayPassthrough(false);
      persist();
    };
    window.addEventListener("pointerup", release);
    window.addEventListener("dragend", release);
    // Moving a tab can remove the drag source before dragend reaches window.
    window.addEventListener("drop", release, true);
    return () => {
      subscriptions.forEach((subscription) => subscription.dispose());
      window.removeEventListener("pointerup", release);
      window.removeEventListener("dragend", release);
      window.removeEventListener("drop", release, true);
      workspaceHost?.removeEventListener("pointerdown", guardShiftFloat, true);
      setOverlayPassthrough(false);
      if (pendingFrame.current !== null) cancelAnimationFrame(pendingFrame.current);
    };
  }, [api, persist, dropOverlays]);

  useEffect(() => {
    if (!host.current || !api) return;
    const observer = new ResizeObserver(() => {
      if (executor.current?.isCompact()) executor.current.resizeCompact();
    });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, [api]);
  useEffect(() => dismissUndo, [dismissUndo]);
  useLayoutEffect(() => {
    if (!api || !host.current || !context.endsWith(":3d")) return;
    const workspaceHost = host.current;
    const geometry = createWorkbenchViewportRegions(api, workspaceHost);
    regions.current = geometry;
    let frame: number | null = null;
    let disposed = false;
    let signature = "";
    const update = () => {
      frame = null;
      if (disposed) return;
      const next = JSON.stringify([
        api.groups.map((group) => {
          const rect = group.element.getBoundingClientRect();
          const floating = group.element.closest<HTMLElement>(".dv-resize-container");
          return [
            group.id,
            group.activePanel?.id,
            group.api.isVisible,
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            floating?.style.zIndex,
          ];
        }),
        [...workspaceHost.querySelectorAll<HTMLElement>(".dv-render-overlay")].map((overlay) => {
          const rect = overlay.getBoundingClientRect();
          return [
            rect.x,
            rect.y,
            rect.width,
            rect.height,
            overlay.style.display,
            overlay.style.zIndex,
          ];
        }),
      ]);
      if (signature === next) return;
      signature = next;
      geometry.update();
      setLayoutKey((value) => value + 1);
    };
    const schedule = () => {
      if (!disposed && frame === null) frame = requestAnimationFrame(update);
    };
    const subscriptions = [
      api.onDidLayoutChange(schedule),
      api.onDidActivePanelChange(schedule),
      api.onDidActiveGroupChange(schedule),
    ];
    const observer = new ResizeObserver(schedule);
    observer.observe(host.current);
    const movement = new MutationObserver((records) => {
      if (
        records.some(
          (record) =>
            record.type === "childList" ||
            (record.target instanceof HTMLElement &&
              record.target.matches(".dv-resize-container, .dv-render-overlay")),
        )
      )
        schedule();
    });
    movement.observe(host.current, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["style", "aria-level"],
    });
    window.addEventListener("resize", schedule);
    update();
    return () => {
      disposed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      subscriptions.forEach((entry) => entry.dispose());
      observer.disconnect();
      movement.disconnect();
      window.removeEventListener("resize", schedule);
      geometry.dispose();
      regions.current = null;
    };
  }, [api, context]);
  useEffect(
    () =>
      useActiveIssueStore.subscribe((next, previous) => {
        if (next.tabRequestTick !== previous.tabRequestTick) commands.show("discussion");
      }),
    [commands],
  );

  const layoutItems: DropdownItem[] = [
    ...(Object.keys(PRESET_LABELS) as WorkspacePresetId[])
      .filter((id) => presetSupportsContext(id, context))
      .map((id) => ({
        id,
        label: id === "focus" && view.canvasMaximized ? "恢复画布布局" : `${PRESET_LABELS[id]}布局`,
        disabled: owner.readOnly || compact,
        onSelect: () => preset(id),
      })),
    { id: "separator", label: "", divider: true },
    ...(context.endsWith(":3d")
      ? [
          ...THREE_D_LAYOUT_PRESETS.map((entry) => ({
            id: `3d-${entry.id}`,
            label: entry.label,
            disabled: owner.readOnly || compact,
            onSelect: () =>
              run((engine) => {
                const before = engine.capture();
                engine.applyThreeDPreset(entry.id);
                undo.current = toast(`已切换到${entry.label}`, {
                  id: undo.current ?? undefined,
                  duration: 8000,
                  position: "bottom-center",
                  action: {
                    label: "撤销",
                    onClick: () => run((current) => current.restore(before)),
                  },
                });
              }),
          })),
          {
            id: "camera-presentation",
            label: view.cameraPresentation === "floating" ? "全部相机停靠" : "全部相机悬浮",
            disabled: owner.readOnly || compact,
            onSelect: () =>
              commands.setCameraPresentation(
                view.cameraPresentation === "floating" ? "docked" : "floating",
              ),
          },
          { id: "separator-3d", label: "", divider: true },
        ]
      : []),
    ...availablePanels.map((id) => ({
      id,
      label: WORKBENCH_PANEL_REGISTRY[id].title,
      active: visiblePanels.includes(id),
      disabled: owner.readOnly,
      onSelect: () => commands.toggle(id),
    })),
    { id: "separator-reset", label: "", divider: true },
    ...(context.endsWith(":3d")
      ? [
          {
            id: "reset-cameras",
            label: "恢复相机排列",
            disabled: owner.readOnly || !threeDActions,
            onSelect: () => threeDActions?.resetCameras(),
          },
        ]
      : []),
    {
      id: "reset",
      label: "重置为标准布局",
      disabled: !owner.initialized || compact || owner.readOnlyReason === "newer-schema",
      onSelect: reset,
    },
  ];
  const menu = (
    <DropdownMenu
      content={({ close }) => (
        <WorkbenchLayoutQuickMenu
          items={layoutItems.filter((item) => ["standard", "focus"].includes(item.id))}
          activePreset={activePreset}
          namedPresets={namedPresetControls}
          close={close}
          onOpenSettings={props.onOpenLayoutSettings}
        />
      )}
      panelAriaLabel="布局快捷设置"
      minWidth={296}
      disablePanelPadding
      trigger={({ ref, toggle, open }) => (
        <button
          ref={ref}
          type="button"
          data-workbench-layout-control
          aria-label="布局"
          title="布局"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={toggle}
          className="inline-flex h-8 shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-2 text-xs text-muted-foreground hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring @max-[1100px]:w-7 @max-[1100px]:justify-center @max-[1100px]:p-0"
        >
          <span className="@max-[1100px]:hidden">布局</span>
          <Icon name="grid" size={14} className="hidden @max-[1100px]:block" />
          <Icon name="chevDown" size={12} className="@max-[1100px]:hidden" />
        </button>
      )}
    />
  );
  return (
    <Workbench3DLayoutContext.Provider
      value={{
        renderSurface,
        targets,
        setTarget,
        panelVisible,
        setPanelVisible,
        layoutKey,
        getVisibleRegions,
        cameraPresentation: view.cameraPresentation,
        cameraVisible: view.cameraViewVisible,
        disabled: view.disabled || compact,
        commands,
        registerActions,
      }}
    >
      <SlotsContext.Provider value={slots}>
        <PanelCloseContext.Provider value={{ hide: commands.hide, disabled: owner.readOnly }}>
          {renderTopbar(
            menu,
            view,
            <WorkbenchLayoutSettings
              items={layoutItems}
              activePreset={activePreset}
              namedPresets={namedPresetControls}
            />,
          )}
          {(owner.error || owner.readOnlyReason) && (
            <div
              role="status"
              className="flex shrink-0 items-center gap-2 border-b border-border bg-muted px-3 py-1 text-xs text-muted-foreground"
            >
              {owner.error ??
                (owner.readOnlyReason === "newer-schema"
                  ? "此布局来自新版，当前使用只读标准布局。"
                  : "保存的布局无法恢复，请从布局菜单重置。")}
            </div>
          )}
          <div
            ref={host}
            className={cn(
              "min-h-0 min-w-0 flex-1 overflow-clip",
              styles.workspace,
              owner.readOnly && styles.locked,
              compact && styles.compact,
            )}
            data-workbench-workspace
            data-shared-3d={context.endsWith(":3d")}
            data-compact={compact}
            onPointerDownCapture={(event) => {
              if (
                (event.target as HTMLElement).closest(
                  '.dv-tab, .dv-tabs-and-actions-container, .dv-sash, [class*="dv-resize-handle-"]',
                )
              ) {
                pointerDown.current = true;
                interaction.current = true;
              }
            }}
            onKeyDown={(event) => {
              if (
                (event.target as HTMLElement).closest(
                  "[data-workbench-tool-menu], [data-workbench-tool-menu-trigger]",
                )
              )
                return;
              if (
                (event.target as HTMLElement).closest(
                  '[role="tab"], [role="menu"], [role="menuitem"], [data-workbench-layout-control]',
                )
              ) {
                interaction.current = true;
                persist();
                event.stopPropagation();
              }
            }}
            onDoubleClick={(event) => {
              if (!(event.target as HTMLElement).closest("[data-workbench-canvas]"))
                event.stopPropagation();
            }}
          >
            {context.endsWith(":3d") && (
              <div
                ref={setRenderSurface}
                className={styles.renderSurface}
                aria-hidden="true"
                data-workbench-render-surface
              />
            )}
            <DockviewReact
              components={components}
              defaultTabComponent={PanelTab}
              rightHeaderActionsComponent={PanelHeaderActions}
              theme={theme}
              onReady={(event) => setApi(event.api)}
              floatingGroupBounds="boundedWithinViewport"
              floatingGroupDragHandle="tabbar"
              dndEdges={dropOverlays.edge}
              dropOverlayModel={dropOverlayModel}
            />
          </div>
        </PanelCloseContext.Provider>
      </SlotsContext.Provider>
    </Workbench3DLayoutContext.Provider>
  );
}
