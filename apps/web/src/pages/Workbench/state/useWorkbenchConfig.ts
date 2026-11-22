import { useCallback, useLayoutEffect, useMemo, useReducer } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  authApi,
  DEFAULT_WORKBENCH_PREFERENCES,
  migrateLabelContent,
  type CameraPanelState,
  type FloatingPanelState,
  type FloatingSelectionState,
  type PointcloudCameraState,
  type TriViewFloatState,
  type UserPreferences,
  type UserPreferencesPatch,
  type WorkbenchCommonPreferences,
  type WorkbenchImagePreferences,
  type WorkbenchLayoutPreferences,
  type WorkbenchPointcloudPreferences,
  type WorkbenchPreferences,
  type WorkbenchVideoPreferences,
} from "@/api/auth";
import type { ProjectRenderingConfig } from "@/api/projects";
import { useToastStore } from "@/components/ui/Toast";
import { authSessionToken, type AuthSessionOwner, useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export type LockableField = "smoothImage" | "cssImageFilter" | "controlPointsSize" | "snapToGrid";

export type WorkbenchLayoutPatch = Omit<
  Partial<WorkbenchLayoutPreferences>,
  | "floatingTaskQueue"
  | "floatingClassPalette"
  | "floatingInspector"
  | "floatingDiscussion"
  | "floatingSelection"
  | "triViewFloat"
  | "cameraPanels"
  | "pointcloudCamera"
> & {
  floatingTaskQueue?: Partial<FloatingPanelState> | null;
  floatingClassPalette?: Partial<FloatingPanelState> | null;
  floatingInspector?: Partial<FloatingPanelState> | null;
  floatingDiscussion?: Partial<FloatingPanelState> | null;
  floatingSelection?: Partial<FloatingSelectionState> | null;
  triViewFloat?: Partial<TriViewFloatState> | null;
  cameraPanels?: Record<string, CameraPanelState>;
  pointcloudCamera?: PointcloudCameraState | null;
};

export interface WorkbenchConfigPatch {
  common?: Partial<WorkbenchCommonPreferences>;
  image?: Partial<WorkbenchImagePreferences>;
  video?: Partial<WorkbenchVideoPreferences>;
  pointcloud?: Partial<WorkbenchPointcloudPreferences>;
  layout?: Partial<WorkbenchLayoutPreferences>;
}

interface WorkbenchConfigState {
  config: WorkbenchPreferences;
  layout: WorkbenchLayoutPreferences;
  loaded: boolean;
  loadError: unknown | null;
  retryLoad: () => void;
  saving: boolean;
  update: (patch: WorkbenchConfigPatch) => Promise<void>;
  setFields: (patch: WorkbenchConfigPatch) => void;
  setLayout: (patch: WorkbenchLayoutPatch) => void;
  lockedFields: LockableField[];
}

const clampNum = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

interface ConfigSave {
  owner: AuthSessionOwner;
  revision: number;
  payload: UserPreferencesPatch;
}

interface ConfigSession {
  owner: AuthSessionOwner | null;
  config: WorkbenchPreferences;
  hydrated: boolean;
  active: boolean;
  saving: boolean;
  pending: { timer: number; save: ConfigSave } | null;
}

type ConfigListener = (
  config: WorkbenchPreferences,
  owner: AuthSessionOwner,
  source: ConfigSession,
) => void;
const configListeners = new Set<ConfigListener>();
let latestRevision = 0;

function cancelPending(session: ConfigSession): ConfigSave | undefined {
  if (!session.pending) return;
  const { timer, save } = session.pending;
  window.clearTimeout(timer);
  session.pending = null;
  return save;
}

function isCurrentSave(save: ConfigSave): boolean {
  return save.revision === latestRevision && !!authSessionToken(save.owner);
}

function broadcastConfig(
  config: WorkbenchPreferences,
  owner: AuthSessionOwner,
  source: ConfigSession,
): void {
  for (const listener of configListeners) listener(config, owner, source);
}

function roundPanelRect<T>(panel: T): T {
  if (!panel || typeof panel !== "object") return panel;
  const r = { ...(panel as Record<string, unknown>) };
  for (const k of ["x", "y", "w", "h"] as const) {
    if (typeof r[k] === "number") r[k] = Math.round(r[k] as number);
  }
  return r as T;
}

export function sanitizeForPersist(wb: WorkbenchPreferences): Omit<
  WorkbenchPreferences,
  "layout" | "shortcuts"
> & {
  layout: Omit<WorkbenchPreferences["layout"], "workspace" | "triViewFloat">;
} {
  const { shortcuts: _shortcuts, ...settings } = wb;
  const { workspace: _workspace, triViewFloat: _triViewFloat, ...l } = wb.layout;
  return {
    ...settings,
    layout: {
      ...l,
      floatingTaskQueue: roundPanelRect(l.floatingTaskQueue),
      floatingClassPalette: roundPanelRect(l.floatingClassPalette),
      floatingInspector: roundPanelRect(l.floatingInspector),
      floatingDiscussion: roundPanelRect(l.floatingDiscussion),
      floatingSelection: roundPanelRect(l.floatingSelection),
    },
  };
}

function cachePreferences(
  client: QueryClient,
  userId: string | null | undefined,
  response: UserPreferences,
): void {
  client.setQueryData<UserPreferences>(userPreferencesQueryKey(userId), (previous) => {
    const workspace = previous?.workbench?.layout?.workspace;
    const shortcuts = previous?.workbench?.shortcuts;
    if (workspace === undefined && shortcuts === undefined) return response;
    return {
      ...response,
      workbench: {
        ...response.workbench,
        ...(shortcuts === undefined ? {} : { shortcuts }),
        ...(workspace === undefined ? {} : { layout: { ...response.workbench.layout, workspace } }),
      },
    };
  });
}

function mergeUser(
  remote: Partial<WorkbenchPreferences> | undefined | null,
  userId: string | null | undefined,
  options?: { preferLocalLayout?: boolean },
): WorkbenchPreferences {
  const common = {
    ...DEFAULT_WORKBENCH_PREFERENCES.common,
    ...(remote?.common ?? {}),
  };
  const remoteCommon = remote?.common as Record<string, unknown> | undefined;
  common.labelContent = migrateLabelContent(remoteCommon?.labelContent);
  if (
    remoteCommon &&
    !("crossFrameOverlayEnabled" in remoteCommon) &&
    typeof remoteCommon.crossFrameOverlayK === "number"
  ) {
    common.crossFrameOverlayEnabled = remoteCommon.crossFrameOverlayK > 0;
  }
  return {
    common,
    image: { ...DEFAULT_WORKBENCH_PREFERENCES.image, ...(remote?.image ?? {}) },
    video: { ...DEFAULT_WORKBENCH_PREFERENCES.video, ...(remote?.video ?? {}) },
    pointcloud: {
      ...DEFAULT_WORKBENCH_PREFERENCES.pointcloud,
      ...(remote?.pointcloud ?? {}),
    },
    layout: mergeLayout(remote?.layout, userId, options),
  };
}

function applyConfigPatch(
  prev: WorkbenchPreferences,
  patch: WorkbenchConfigPatch,
  layout: WorkbenchLayoutPreferences,
): WorkbenchPreferences {
  return {
    common: { ...prev.common, ...(patch.common ?? {}) },
    image: { ...prev.image, ...(patch.image ?? {}) },
    video: { ...prev.video, ...(patch.video ?? {}) },
    pointcloud: { ...prev.pointcloud, ...(patch.pointcloud ?? {}) },
    layout,
  };
}

const LAYOUT_KEY_NAMES = [
  "leftOpen",
  "rightOpen",
  "floatingTaskQueue",
  "floatingClassPalette",
  "floatingInspector",
  "floatingDiscussion",
  "floatingSelection",
  "triViewFloat",
  "cameraPanels",
  "pointcloudCamera",
  "aiSectionCollapsed",
  "manualSectionCollapsed",
  "trackSectionCollapsed",
  "discussionCollapsed",
] as const;

type LayoutKeyName = (typeof LAYOUT_KEY_NAMES)[number];

function layoutStorageKeys(userId: string | null | undefined): Record<LayoutKeyName, string> {
  const prefix = userId ? `workbench.${userId}.` : "workbench.";
  return Object.fromEntries(LAYOUT_KEY_NAMES.map((n) => [n, `${prefix}${n}`])) as Record<
    LayoutKeyName,
    string
  >;
}

function readBool(key: string): boolean | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch {
    /* ignore localStorage failures */
  }
  return undefined;
}

function readJsonObject<T extends object>(key: string): Partial<T> | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function readLocalLayout(userId: string | null | undefined): WorkbenchLayoutPatch {
  const K = layoutStorageKeys(userId);
  return {
    leftOpen: readBool(K.leftOpen),
    rightOpen: readBool(K.rightOpen),
    floatingTaskQueue: readJsonObject<FloatingPanelState>(K.floatingTaskQueue),
    floatingClassPalette: readJsonObject<FloatingPanelState>(K.floatingClassPalette),
    floatingInspector: readJsonObject<FloatingPanelState>(K.floatingInspector),
    floatingDiscussion: readJsonObject<FloatingPanelState>(K.floatingDiscussion),
    floatingSelection: readJsonObject<FloatingSelectionState>(K.floatingSelection),
    triViewFloat: readJsonObject<TriViewFloatState>(K.triViewFloat),
    cameraPanels: readJsonObject<Record<string, CameraPanelState>>(K.cameraPanels) as
      | Record<string, CameraPanelState>
      | undefined,
    pointcloudCamera: readJsonObject<PointcloudCameraState>(K.pointcloudCamera) as
      | PointcloudCameraState
      | undefined,
    aiSectionCollapsed: readBool(K.aiSectionCollapsed),
    manualSectionCollapsed: readBool(K.manualSectionCollapsed),
    trackSectionCollapsed: readBool(K.trackSectionCollapsed),
    discussionCollapsed: readBool(K.discussionCollapsed),
  };
}

function definedPatch<T extends object>(patch: Partial<T> | null | undefined): Partial<T> {
  if (!patch) return {};
  const out: Partial<T> = {};
  for (const [key, value] of Object.entries(patch) as Array<[keyof T, T[keyof T] | undefined]>) {
    if (value !== undefined) out[key] = value as T[keyof T];
  }
  return out;
}

function mergeLayoutPatch<T extends object>(
  local: Partial<T> | null | undefined,
  remote: Partial<T> | null | undefined,
  preferLocal: boolean,
): Partial<T> {
  const localDefined = definedPatch(local);
  const remoteDefined = definedPatch(remote);
  return preferLocal
    ? { ...remoteDefined, ...localDefined }
    : { ...localDefined, ...remoteDefined };
}

function writeLocalLayout(
  layout: WorkbenchLayoutPreferences,
  userId: string | null | undefined,
): void {
  if (typeof window === "undefined") return;
  const K = layoutStorageKeys(userId);
  try {
    window.localStorage.setItem(K.leftOpen, layout.leftOpen ? "1" : "0");
    window.localStorage.setItem(K.rightOpen, layout.rightOpen ? "1" : "0");
    window.localStorage.setItem(K.floatingTaskQueue, JSON.stringify(layout.floatingTaskQueue));
    window.localStorage.setItem(
      K.floatingClassPalette,
      JSON.stringify(layout.floatingClassPalette),
    );
    window.localStorage.setItem(K.floatingInspector, JSON.stringify(layout.floatingInspector));
    window.localStorage.setItem(K.floatingDiscussion, JSON.stringify(layout.floatingDiscussion));
    window.localStorage.setItem(K.floatingSelection, JSON.stringify(layout.floatingSelection));
    window.localStorage.setItem(K.cameraPanels, JSON.stringify(layout.cameraPanels));
    window.localStorage.setItem(K.pointcloudCamera, JSON.stringify(layout.pointcloudCamera));
    window.localStorage.setItem(K.aiSectionCollapsed, layout.aiSectionCollapsed ? "1" : "0");
    window.localStorage.setItem(
      K.manualSectionCollapsed,
      layout.manualSectionCollapsed ? "1" : "0",
    );
    window.localStorage.setItem(K.trackSectionCollapsed, layout.trackSectionCollapsed ? "1" : "0");
    window.localStorage.setItem(K.discussionCollapsed, layout.discussionCollapsed ? "1" : "0");
  } catch {
    /* local fallback is best-effort */
  }
}

function mergeFloatingPanel(
  fallback: FloatingPanelState,
  remote: Partial<FloatingPanelState> | null | undefined,
): FloatingPanelState {
  const m = { ...fallback, ...(remote ?? {}) };
  return {
    ...m,
    w: m.w == null ? m.w : clampNum(m.w, 48, 720),
    h: m.h == null ? m.h : clampNum(m.h, 120, 900),
  };
}

function mergeFloatingSelection(
  remote: Partial<FloatingSelectionState> | null | undefined,
): FloatingSelectionState {
  const m = {
    ...DEFAULT_WORKBENCH_PREFERENCES.layout.floatingSelection,
    ...(remote ?? {}),
  };
  return {
    ...m,
    w: m.w == null ? m.w : clampNum(m.w, 48, 720),
    h: m.h == null ? m.h : clampNum(m.h, 120, 900),
  };
}

function mergeTriViewFloat(
  remote: Partial<TriViewFloatState> | null | undefined,
): TriViewFloatState {
  const m = {
    ...DEFAULT_WORKBENCH_PREFERENCES.layout.triViewFloat,
    ...(remote ?? {}),
  };
  return {
    ...m,
    w: m.w == null ? m.w : clampNum(m.w, 200, 480),
    h: m.h == null ? m.h : clampNum(m.h, 240, 720),
  };
}

function mergeLayout(
  remote: Partial<WorkbenchLayoutPreferences> | null | undefined,
  userId: string | null | undefined,
  options?: { preferLocalLayout?: boolean },
): WorkbenchLayoutPreferences {
  const local = readLocalLayout(userId);
  const preferLocal = options?.preferLocalLayout === true;
  const merged = {
    ...DEFAULT_WORKBENCH_PREFERENCES.layout,
    ...mergeLayoutPatch<WorkbenchLayoutPatch>(local, remote, preferLocal),
  };
  const floatingTaskQueue = mergeLayoutPatch(
    local.floatingTaskQueue,
    remote?.floatingTaskQueue,
    preferLocal,
  );
  const floatingClassPalette = mergeLayoutPatch(
    local.floatingClassPalette,
    remote?.floatingClassPalette,
    preferLocal,
  );
  const floatingInspector = mergeLayoutPatch(
    local.floatingInspector,
    remote?.floatingInspector,
    preferLocal,
  );
  const floatingDiscussion = mergeLayoutPatch(
    local.floatingDiscussion,
    remote?.floatingDiscussion,
    preferLocal,
  );
  const floatingSelection = mergeLayoutPatch(
    local.floatingSelection,
    remote?.floatingSelection,
    preferLocal,
  );
  const triViewFloat = mergeLayoutPatch(local.triViewFloat, remote?.triViewFloat, preferLocal);
  const cameraPanels = preferLocal
    ? (local.cameraPanels ?? remote?.cameraPanels)
    : (remote?.cameraPanels ?? local.cameraPanels);
  const pointcloudCamera = preferLocal
    ? (local.pointcloudCamera ?? remote?.pointcloudCamera)
    : (remote?.pointcloudCamera ?? local.pointcloudCamera);
  return {
    workspace: merged.workspace,
    leftOpen: merged.leftOpen ?? DEFAULT_WORKBENCH_PREFERENCES.layout.leftOpen,
    rightOpen: merged.rightOpen ?? DEFAULT_WORKBENCH_PREFERENCES.layout.rightOpen,
    attrPanelCollapsed:
      merged.attrPanelCollapsed ?? DEFAULT_WORKBENCH_PREFERENCES.layout.attrPanelCollapsed,
    aiSectionCollapsed:
      merged.aiSectionCollapsed ?? DEFAULT_WORKBENCH_PREFERENCES.layout.aiSectionCollapsed,
    manualSectionCollapsed:
      merged.manualSectionCollapsed ?? DEFAULT_WORKBENCH_PREFERENCES.layout.manualSectionCollapsed,
    trackSectionCollapsed:
      merged.trackSectionCollapsed ?? DEFAULT_WORKBENCH_PREFERENCES.layout.trackSectionCollapsed,
    discussionCollapsed:
      merged.discussionCollapsed ?? DEFAULT_WORKBENCH_PREFERENCES.layout.discussionCollapsed,
    floatingTaskQueue: mergeFloatingPanel(
      DEFAULT_WORKBENCH_PREFERENCES.layout.floatingTaskQueue,
      floatingTaskQueue,
    ),
    floatingClassPalette: mergeFloatingPanel(
      DEFAULT_WORKBENCH_PREFERENCES.layout.floatingClassPalette,
      floatingClassPalette,
    ),
    floatingInspector: mergeFloatingPanel(
      DEFAULT_WORKBENCH_PREFERENCES.layout.floatingInspector,
      floatingInspector,
    ),
    floatingDiscussion: mergeFloatingPanel(
      DEFAULT_WORKBENCH_PREFERENCES.layout.floatingDiscussion,
      floatingDiscussion,
    ),
    floatingSelection: mergeFloatingSelection(floatingSelection),
    triViewFloat: mergeTriViewFloat(triViewFloat),
    cameraPanels: cameraPanels ?? DEFAULT_WORKBENCH_PREFERENCES.layout.cameraPanels,
    pointcloudCamera: pointcloudCamera ?? DEFAULT_WORKBENCH_PREFERENCES.layout.pointcloudCamera,
  };
}

function applyLayoutPatch(
  current: WorkbenchLayoutPreferences,
  patch: WorkbenchLayoutPatch,
): WorkbenchLayoutPreferences {
  return {
    ...current,
    ...patch,
    floatingTaskQueue:
      patch.floatingTaskQueue === undefined
        ? current.floatingTaskQueue
        : mergeFloatingPanel(DEFAULT_WORKBENCH_PREFERENCES.layout.floatingTaskQueue, {
            ...current.floatingTaskQueue,
            ...(patch.floatingTaskQueue ?? {}),
          }),
    floatingClassPalette:
      patch.floatingClassPalette === undefined
        ? current.floatingClassPalette
        : mergeFloatingPanel(DEFAULT_WORKBENCH_PREFERENCES.layout.floatingClassPalette, {
            ...current.floatingClassPalette,
            ...(patch.floatingClassPalette ?? {}),
          }),
    floatingInspector:
      patch.floatingInspector === undefined
        ? current.floatingInspector
        : mergeFloatingPanel(DEFAULT_WORKBENCH_PREFERENCES.layout.floatingInspector, {
            ...current.floatingInspector,
            ...(patch.floatingInspector ?? {}),
          }),
    floatingDiscussion:
      patch.floatingDiscussion === undefined
        ? current.floatingDiscussion
        : mergeFloatingPanel(DEFAULT_WORKBENCH_PREFERENCES.layout.floatingDiscussion, {
            ...current.floatingDiscussion,
            ...(patch.floatingDiscussion ?? {}),
          }),
    floatingSelection:
      patch.floatingSelection === undefined
        ? current.floatingSelection
        : mergeFloatingSelection({
            ...current.floatingSelection,
            ...(patch.floatingSelection ?? {}),
          }),
    triViewFloat:
      patch.triViewFloat === undefined
        ? current.triViewFloat
        : mergeTriViewFloat({
            ...current.triViewFloat,
            ...(patch.triViewFloat ?? {}),
          }),
    cameraPanels: patch.cameraPanels === undefined ? current.cameraPanels : patch.cameraPanels,
    pointcloudCamera:
      patch.pointcloudCamera === undefined ? current.pointcloudCamera : patch.pointcloudCamera,
  };
}

function applyProjectOverride(
  user: WorkbenchPreferences,
  project: ProjectRenderingConfig | null | undefined,
): { merged: WorkbenchPreferences; lockedFields: LockableField[] } {
  if (!project) return { merged: user, lockedFields: [] };
  const image = { ...user.image };
  const locked: LockableField[] = [];
  const fields: LockableField[] = [
    "smoothImage",
    "cssImageFilter",
    "controlPointsSize",
    "snapToGrid",
  ];
  for (const key of fields) {
    const v = project[key];
    if (v !== null && v !== undefined) {
      (image[key] as WorkbenchImagePreferences[typeof key]) =
        v as WorkbenchImagePreferences[typeof key];
      locked.push(key);
    }
  }
  return { merged: { ...user, image }, lockedFields: locked };
}

export function useWorkbenchConfig(
  projectRenderingConfig?: ProjectRenderingConfig | null,
): WorkbenchConfigState {
  const userId = useAuthStore((s) => s.user?.id);
  const sessionId = useAuthStore((s) => s.sessionId);
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const { prefs, error, refetch } = useUserPreferences();
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const session = useMemo<ConfigSession>(() => {
    const user = useAuthStore.getState().user;
    return {
      owner: userId && sessionId ? { userId, sessionId } : null,
      config: mergeUser(user?.id === userId ? user?.preferences?.workbench : undefined, userId, {
        preferLocalLayout: true,
      }),
      hydrated: false,
      active: false,
      saving: false,
      pending: null,
    };
  }, [userId, sessionId]);

  const retryLoad = useCallback(() => {
    if (session.owner && authSessionToken(session.owner)) void refetch();
  }, [session, refetch]);

  const publish = useCallback(
    (config: WorkbenchPreferences, owner: AuthSessionOwner) => {
      if (session.active) {
        session.config = config;
        refresh();
      }
      broadcastConfig(config, owner, session);
    },
    [session],
  );

  const persist = useCallback(
    async (
      save: ConfigSave,
      mode: "immediate" | "debounce" | "unmount",
      previous?: WorkbenchPreferences,
    ) => {
      if (!isCurrentSave(save)) return;
      if (session.active) {
        session.saving = true;
        refresh();
      }
      try {
        const result = await authApi.updatePreferences(save.payload, save.owner);
        if (!isCurrentSave(save)) return;
        const { userId } = save.owner;
        await queryClient.cancelQueries(
          { queryKey: userPreferencesQueryKey(userId), exact: true },
          { revert: false },
        );
        if (!isCurrentSave(save)) return;
        cachePreferences(queryClient, userId, result);
        const config = mergeUser(result.workbench, userId);
        writeLocalLayout(config.layout, userId);
        publish(config, save.owner);
        return true;
      } catch (err) {
        if (!isCurrentSave(save)) return;
        if (previous) publish(previous, save.owner);
        if (mode === "debounce") console.warn("Failed to persist workbench preferences", err);
        useToastStore.getState().push({
          kind: "error",
          msg: "工作台设置未同步",
          sub:
            mode === "unmount"
              ? "离开页面前的修改保存失败，请返回工作台检查并重新设置。"
              : mode === "immediate"
                ? "设置已恢复为保存前的值，请重试。"
                : "当前工作台保留本次修改，再次调整设置可重试保存。",
        });
        if (previous) throw err;
      } finally {
        if (session.active && isCurrentSave(save)) {
          session.saving = false;
          refresh();
        }
      }
    },
    [session, queryClient, publish],
  );

  useLayoutEffect(() => {
    session.active = true;
    const listener: ConfigListener = (config, owner, source) => {
      const current = session.owner;
      if (
        source === session ||
        !current ||
        owner.userId !== current.userId ||
        owner.sessionId !== current.sessionId ||
        !session.hydrated
      ) {
        return;
      }
      cancelPending(session);
      session.config = config;
      session.saving = false;
      refresh();
    };
    configListeners.add(listener);
    return () => {
      configListeners.delete(listener);
      session.active = false;
      const save = cancelPending(session);
      if (save) void persist(save, "unmount");
    };
  }, [persist, session]);

  useLayoutEffect(() => {
    const owner = session.owner;
    if (!owner || !token || !prefs || session.hydrated || !authSessionToken(owner)) return;
    session.config = mergeUser(prefs.workbench, owner.userId);
    session.hydrated = true;
    writeLocalLayout(session.config.layout, owner.userId);
    refresh();
  }, [prefs, session, token]);

  const prepareSave = useCallback(
    (config: WorkbenchPreferences, owner: AuthSessionOwner): ConfigSave => {
      cancelPending(session);
      session.saving = false;
      const save = {
        owner,
        revision: ++latestRevision,
        payload: { workbench: sanitizeForPersist(config) },
      };
      publish(config, owner);
      return save;
    },
    [publish, session],
  );

  const update = useCallback(
    async (patch: WorkbenchConfigPatch) => {
      const owner = session.owner;
      if (session.active && session.hydrated && owner && authSessionToken(owner)) {
        const previous = session.config;
        const config = applyConfigPatch(
          previous,
          patch,
          patch.layout ? mergeLayout(patch.layout, owner.userId) : previous.layout,
        );
        const save = prepareSave(config, owner);
        if ((await persist(save, "immediate", previous)) && isCurrentSave(save)) return;
      }
      throw new DOMException("工作台设置保存已取消", "AbortError");
    },
    [persist, prepareSave, session],
  );

  const scheduleSave = useCallback(
    (config: WorkbenchPreferences, owner: AuthSessionOwner) => {
      const save = prepareSave(config, owner);
      const timer = window.setTimeout(() => {
        session.pending = null;
        void persist(save, "debounce");
      }, 300);
      session.pending = { timer, save };
    },
    [persist, prepareSave, session],
  );

  const setLayout = useCallback(
    (patch: WorkbenchLayoutPatch) => {
      const owner = session.owner;
      if (!session.active || !session.hydrated || !owner || !authSessionToken(owner)) return;
      const config = { ...session.config, layout: applyLayoutPatch(session.config.layout, patch) };
      writeLocalLayout(config.layout, owner.userId);
      scheduleSave(config, owner);
    },
    [scheduleSave, session],
  );

  const setFields = useCallback(
    (patch: WorkbenchConfigPatch) => {
      const owner = session.owner;
      if (!session.active || !session.hydrated || !owner || !authSessionToken(owner)) return;
      const previous = session.config;
      const config = applyConfigPatch(
        previous,
        patch,
        patch.layout ? applyLayoutPatch(previous.layout, patch.layout) : previous.layout,
      );
      scheduleSave(config, owner);
    },
    [scheduleSave, session],
  );

  const userConfig = session.config;
  const { merged, lockedFields } = useMemo(
    () => applyProjectOverride(userConfig, projectRenderingConfig),
    [userConfig, projectRenderingConfig],
  );

  return {
    config: merged,
    layout: userConfig.layout,
    loaded: session.hydrated,
    loadError: session.hydrated ? null : error,
    retryLoad,
    saving: session.saving,
    update,
    setFields,
    setLayout,
    lockedFields,
  };
}
