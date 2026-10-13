import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  type WorkbenchCommonPreferences,
  type WorkbenchImagePreferences,
  type WorkbenchLayoutPreferences,
  type WorkbenchPointcloudPreferences,
  type WorkbenchPreferences,
  type WorkbenchVideoPreferences,
} from "@/api/auth";
import type { ProjectRenderingConfig } from "@/api/projects";
import { useToastStore } from "@/components/ui/Toast";
import { useAuthStore } from "@/stores/authStore";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

// v0.10.10 · I17.3 · 项目级覆盖的字段名集合（用于 SettingsPage badge 与控件 disabled）。
// 与 ProjectRenderingConfig 字段同集（平铺命名不变），不含 longTaskSampleRate / layout。
// v0.15.3 · 偏好四分树后,这些字段落在用户偏好的 image.* 子树。
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
  // cameraPanels 是按 role 分桶的全量 Record(由调用方合并好整份传入),非逐字段 patch。
  cameraPanels?: Record<string, CameraPanelState>;
  pointcloudCamera?: PointcloudCameraState | null;
};

/** v0.15.3 · 子树级 patch:每个子树内字段可单独提交,layout 同 update 既有语义(整树合并)。 */
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
  /** 设置窗口写路径:本地立即生效 + 300ms 防抖 PATCH(与 setLayout 共用卸载 flush)。 */
  setFields: (patch: WorkbenchConfigPatch) => void;
  setLayout: (patch: WorkbenchLayoutPatch) => void;
  /** v0.10.10 · I17.3 · 被项目级覆盖的字段名（用户级修改会立刻被合并覆盖）。 */
  lockedFields: LockableField[];
}

const clampNum = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

// v0.15.3 · 多实例同步:抽屉与画布(ImageStage)各自挂载本 hook,任一实例改动用户配置后
// 广播给其余实例 → 抽屉拖滑块画布实时预览。只广播内存态,不引入模块级缓存。
type ConfigListener = (config: WorkbenchPreferences, source: object) => void;
const configListeners = new Set<ConfigListener>();
function broadcastConfig(config: WorkbenchPreferences, source: object): void {
  for (const listener of configListeners) listener(config, source);
}

// 浮窗面板坐标 x/y/w/h 在前端来自 getBoundingClientRect / 指针位移，可能带小数；
// 后端 FloatingPanelState / FloatingSelectionState / TriViewFloatState 的这些字段是
// 整数（Pydantic int），直接 PATCH 小数会被 int_from_float 拒（422）。持久化前对这些
// 子树就地取整，保证契约一致。cameraPanels.x/y 与 pointcloudCamera 后端为 float，不动。
function roundPanelRect<T>(panel: T): T {
  if (!panel || typeof panel !== "object") return panel;
  const r = { ...(panel as Record<string, unknown>) };
  for (const k of ["x", "y", "w", "h"] as const) {
    if (typeof r[k] === "number") r[k] = Math.round(r[k] as number);
  }
  return r as T;
}

/** 持久化前对 layout 浮窗坐标取整，避免小数像素触发后端 int 校验 422。 */
export function sanitizeForPersist(wb: WorkbenchPreferences): Omit<
  WorkbenchPreferences,
  "layout" | "shortcuts"
> & {
  layout: Omit<WorkbenchPreferences["layout"], "workspace" | "triViewFloat">;
} {
  // Docking snapshots and shortcuts have their own writers; this legacy PATCH
  // must never carry stale data copied from another preferences response.
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
  // v0.16.7 · labelContent 规范化：旧扁平 list / 缺段补全（与后端 before validator 同款，前端兜底）。
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

/** v0.15.3 · 把子树级 patch 应用到完整配置(子树内字段级合并;layout 由调用方决定合并策略)。 */
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

// localStorage 布局缓存按账号分桶，避免同浏览器多账号布局串台 / 互相覆盖（首帧闪烁、
// 后登录账号被先前账号覆盖）。登出态用无账号前缀（此时无账号可串）。
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
  // v0.20.22 · 分组折叠 + 讨论区收起进 localStorage 白名单: 服务端仍是权威,
  // 但初始 render 直接从 localStorage 拿最近值, 避免"刷新一瞬间看到展开态再收起"闪。
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
    // 整份 Record(非逐字段 patch):readJsonObject 泛型回 Partial,JSON 解析出的值实为
    // 完整 CameraPanelState,断言回整份类型对齐 setter 语义。
    cameraPanels: readJsonObject<Record<string, CameraPanelState>>(K.cameraPanels) as
      | Record<string, CameraPanelState>
      | undefined,
    pointcloudCamera: readJsonObject<PointcloudCameraState>(K.pointcloudCamera) as
      | PointcloudCameraState
      | undefined,
    // v0.20.22 · 分组折叠 + 讨论区收起本地读回, 消除首屏闪。
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
    // v0.20.22 · 分组折叠 + 讨论区收起本地写入, 消除首屏闪。
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

// w/h 与后端 FloatingPanelState 约束一致(w 48–720 / h 120–900)，确保越界尺寸不会
// 让整棵 workbench PATCH 触发 422 → catch 只 console.warn → 偏好静默丢失且持续失败。
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

// 选中卡:无 detached,有 collapsed;w/h 界与 FloatingPanelState 一致(48–720 / 120–900)。
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
  // cameraPanels 是整份 Record(非逐字段 patch):优先方整份覆盖,缺省回另一方再回默认空。
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
    // v0.20.22 · 分组折叠 + 讨论区完全收起 (跨设备持久); 与 attrPanelCollapsed
    // 不同, 这三个进 LAYOUT_KEY_NAMES localStorage 双写以消首屏闪 (进任务立即渲染,
    // 属性区选中后才渲染, 闪的可见度差异催出这一分化)。
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

/**
 * v0.10.10 · 把项目级 rendering_config 合进用户级 preferences。
 * 仅 non-null/non-undefined 字段覆盖；其余字段沿用用户级。
 * 同时返回被覆盖的字段名列表，供 UI 渲染「项目锁定」badge。
 * v0.15.3 · ProjectRenderingConfig 保持平铺(项目侧不迁移),覆盖映射到 image.* 子树字段。
 */
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

/**
 * v0.9.41 · 工作台渲染配置 hook（I17）。
 * v0.10.10 · I17.3 · 支持项目级覆盖：合并优先级 = DEFAULTS → user prefs → project rendering_config。
 *
 * 多组件可同时挂载本 hook —— Settings 页面不传 project (= 纯用户视图);
 * 工作台传 currentProject.rendering_config，合并后驱动 KonvaImage 等。
 */
export function useWorkbenchConfig(
  projectRenderingConfig?: ProjectRenderingConfig | null,
): WorkbenchConfigState {
  const user = useAuthStore((s) => s.user);
  const userId = user?.id;
  const queryClient = useQueryClient();
  // v0.21.18 · 读路径接入共享 preferences query: 与 ai.* 四偏好 hook 复用同一
  // ["me","preferences",userId] GET, 消除首屏多个 useWorkbenchConfig 实例各自裸 fetch。
  const { prefs, loaded, error, refetch } = useUserPreferences();
  // A background refresh failure does not invalidate a previously loaded preference snapshot.
  const loadError = prefs === undefined ? error : null;
  const retryLoad = useCallback(() => {
    void refetch();
  }, [refetch]);
  const [userConfig, setUserConfig] = useState<WorkbenchPreferences>(() =>
    mergeUser(user?.preferences?.workbench, userId, { preferLocalLayout: true }),
  );
  const userConfigRef = useRef(userConfig);
  const saveRevisionRef = useRef(0);
  const layoutSaveTimerRef = useRef<number | null>(null);
  // 每个 userId 只用共享 query 数据 hydrate 一次本地态: 首屏 remote 优先同步, 之后共享 query
  // 的后续变化(ai.* 写触发的 refetch / 本 hook 写回灌)不再覆盖本地 userConfig, 避免盖掉
  // 「正在拖动但防抖 PATCH 未发」的 layout。切账号 userId 变则重新 hydrate。
  const hydratedUserIdRef = useRef<string | null | undefined>(null);
  const [saving, setSaving] = useState(false);
  // v0.15.3 · 多实例同步:本实例广播时携带 sourceRef 标识,收到自己发的广播不回灌。
  const sourceRef = useRef<object>({});

  useEffect(() => {
    userConfigRef.current = userConfig;
  }, [userConfig]);

  useEffect(() => {
    const listener: ConfigListener = (config, source) => {
      if (source === sourceRef.current) return;
      saveRevisionRef.current += 1;
      userConfigRef.current = config;
      setUserConfig(config);
    };
    configListeners.add(listener);
    return () => {
      configListeners.delete(listener);
    };
  }, []);

  useEffect(
    () => () => {
      if (layoutSaveTimerRef.current !== null) {
        window.clearTimeout(layoutSaveTimerRef.current);
        // 卸载(路由切换)时若仍有未发出的防抖 PATCH，立即 flush，避免拖完 300ms 内
        // 离开页面丢失跨设备同步。timer 仅在已登录时设置，故无需再判 userId。
        // v0.21.18 · flush 成功后把整份返回值回灌共享 query 缓存, 否则 staleTime 内再进
        // 工作台会读到被本次 flush 覆盖前的旧缓存。userId 从 store 取最新(本 effect deps 稳定)。
        const flushUserId = useAuthStore.getState().user?.id;
        void authApi
          .updatePreferences({ workbench: sanitizeForPersist(userConfigRef.current) })
          .then((res) => {
            cachePreferences(queryClient, flushUserId, res);
          })
          .catch(() => {
            useToastStore.getState().push({
              kind: "error",
              msg: "工作台设置未同步",
              sub: "离开页面前的修改保存失败，请返回工作台检查并重新设置。",
            });
          });
      }
    },
    [queryClient],
  );

  // v0.21.18 · 共享 query 数据到达时把 workbench 子树 hydrate 进本地态(每 userId 一次,
  // 见 hydratedUserIdRef)。首屏由 useUserPreferences 的单次 GET 供数; 各挂载实例各自在此
  // mergeUser + 写回本地 layout 缓存(幂等)。
  useEffect(() => {
    if (!userId || !prefs) return;
    if (hydratedUserIdRef.current === userId) return;
    hydratedUserIdRef.current = userId;
    const next = mergeUser(prefs.workbench, userId);
    userConfigRef.current = next;
    setUserConfig(next);
    writeLocalLayout(next.layout, userId);
  }, [prefs, userId]);

  const update = useCallback(
    async (patch: WorkbenchConfigPatch) => {
      const saveRevision = ++saveRevisionRef.current;
      const prev = userConfigRef.current;
      const next = applyConfigPatch(
        prev,
        patch,
        patch.layout ? mergeLayout(patch.layout, userId) : prev.layout,
      );
      userConfigRef.current = next;
      setUserConfig(next);
      broadcastConfig(next, sourceRef.current);
      setSaving(true);
      try {
        const res = await authApi.updatePreferences({
          workbench: sanitizeForPersist(next),
        });
        if (saveRevision !== saveRevisionRef.current) return;
        // v0.21.18 · 整份返回值回灌共享 query 缓存(PATCH 返回整份 preferences, 无子键覆盖风险)。
        cachePreferences(queryClient, userId, res);
        const saved = mergeUser(res.workbench, userId);
        userConfigRef.current = saved;
        setUserConfig(saved);
        broadcastConfig(saved, sourceRef.current);
        writeLocalLayout(saved.layout, userId);
      } catch {
        if (saveRevision !== saveRevisionRef.current) return;
        userConfigRef.current = prev;
        setUserConfig(prev);
        broadcastConfig(prev, sourceRef.current);
      } finally {
        if (saveRevision === saveRevisionRef.current) setSaving(false);
      }
    },
    [userId, queryClient],
  );

  // setLayout / setFields 共用的 300ms 防抖全量 PATCH(卸载时由上方 cleanup flush)。
  const scheduleDebouncedSave = useCallback(() => {
    if (layoutSaveTimerRef.current !== null) {
      window.clearTimeout(layoutSaveTimerRef.current);
    }
    if (!userId) return;

    layoutSaveTimerRef.current = window.setTimeout(() => {
      // 已触发，标记为「无待写」，卸载时不再冗余 flush(见上方 cleanup)。
      layoutSaveTimerRef.current = null;
      const payload = userConfigRef.current;
      const saveRevision = saveRevisionRef.current;
      setSaving(true);
      authApi
        .updatePreferences({ workbench: sanitizeForPersist(payload) })
        .then((res) => {
          if (saveRevision !== saveRevisionRef.current) return;
          cachePreferences(queryClient, userId, res);
          const saved = mergeUser(res.workbench, userId);
          userConfigRef.current = saved;
          setUserConfig(saved);
          broadcastConfig(saved, sourceRef.current);
          writeLocalLayout(saved.layout, userId);
        })
        .catch((err) => {
          if (saveRevision !== saveRevisionRef.current) return;
          console.warn("Failed to persist workbench preferences", err);
          useToastStore.getState().push({
            kind: "error",
            msg: "工作台设置未同步",
            sub: "当前工作台保留本次修改，再次调整设置可重试保存。",
          });
        })
        .finally(() => {
          if (saveRevision === saveRevisionRef.current) setSaving(false);
        });
    }, 300);
  }, [userId, queryClient]);

  const setLayout = useCallback(
    (patch: WorkbenchLayoutPatch) => {
      saveRevisionRef.current += 1;
      const prev = userConfigRef.current;
      const next = {
        ...prev,
        layout: applyLayoutPatch(prev.layout, patch),
      };
      userConfigRef.current = next;
      setUserConfig(next);
      broadcastConfig(next, sourceRef.current);
      writeLocalLayout(next.layout, userId);
      scheduleDebouncedSave();
    },
    [userId, scheduleDebouncedSave],
  );

  // 设置窗口写路径:成功读取后才允许编辑，避免默认值覆盖尚未加载的远端偏好。
  const setFields = useCallback(
    (patch: WorkbenchConfigPatch) => {
      if (!prefs) return;
      saveRevisionRef.current += 1;
      const prev = userConfigRef.current;
      const next = applyConfigPatch(
        prev,
        patch,
        patch.layout ? applyLayoutPatch(prev.layout, patch.layout) : prev.layout,
      );
      userConfigRef.current = next;
      setUserConfig(next);
      broadcastConfig(next, sourceRef.current);
      scheduleDebouncedSave();
    },
    [prefs, scheduleDebouncedSave],
  );

  const { merged, lockedFields } = useMemo(
    () => applyProjectOverride(userConfig, projectRenderingConfig),
    [userConfig, projectRenderingConfig],
  );

  return {
    config: merged,
    layout: userConfig.layout,
    loaded,
    loadError,
    retryLoad,
    saving,
    update,
    setFields,
    setLayout,
    lockedFields,
  };
}
