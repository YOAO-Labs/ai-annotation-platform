import { apiClient } from "./client";
import type { AuthSessionOwner } from "@/stores/authStore";
import type {
  WorkspaceContext,
  WorkspaceEnvelope,
} from "@/pages/Workbench/layout/workbenchLayoutSnapshot";

export interface LoginPayload {
  email: string;
  password: string;
  captcha_token?: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface WorkbenchCommonPreferences {
  longTaskSampleRate: number;
  confirmDelete: "never" | "multi_only" | "always";
  recentClassesLimit: number;
  crossFrameOverlayEnabled: boolean;
  crossFrameOverlayK: number;
  crossFrameOverlayScope: "selected" | "all";
  performanceTier: "light" | "standard" | "aggressive";
  leftWidthPct: number;
  rightWidthPct: number;
  labelFontSize: number;
  labelVisibility: "always" | "selected" | "none";
  labelContent: LabelContentByType;
  strokeWidth: number;
  fillOpacity: number;
  fillOpacitySelected: number;
  petEnabled: boolean;
  focusSelectionEnabled: boolean;
  autoAdvanceOnDecide: boolean;
  showAnnotationComments: boolean;
}

export type LabelFieldToken = "id" | "score" | "attrs" | "source" | "state";

export interface LabelContentByType {
  single: Array<"id" | "attrs">;
  track: Array<"id" | "state" | "attrs">;
  ai: Array<"source" | "score" | "id" | "attrs">;
}

export const DEFAULT_LABEL_CONTENT: LabelContentByType = {
  single: [],
  track: ["id", "state"],
  ai: ["source", "score"],
};

function uniqFilter<T extends string>(val: unknown, allowed: readonly T[]): T[] {
  if (!Array.isArray(val)) return [];
  const out: T[] = [];
  for (const t of val) {
    if (
      typeof t === "string" &&
      (allowed as readonly string[]).includes(t) &&
      !out.includes(t as T)
    ) {
      out.push(t as T);
    }
  }
  return out;
}

export function migrateLabelContent(raw: unknown): LabelContentByType {
  if (Array.isArray(raw)) {
    return {
      single: uniqFilter(raw, ["id", "attrs"] as const),
      track: [...DEFAULT_LABEL_CONTENT.track],
      ai: uniqFilter(["source", ...raw], ["source", "score", "id", "attrs"] as const),
    };
  }
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    single: Array.isArray(obj.single)
      ? uniqFilter(obj.single, ["id", "attrs"] as const)
      : [...DEFAULT_LABEL_CONTENT.single],
    track: Array.isArray(obj.track)
      ? uniqFilter(obj.track, ["id", "state", "attrs"] as const)
      : [...DEFAULT_LABEL_CONTENT.track],
    ai: Array.isArray(obj.ai)
      ? uniqFilter(obj.ai, ["source", "score", "id", "attrs"] as const)
      : [...DEFAULT_LABEL_CONTENT.ai],
  };
}

export interface WorkbenchImagePreferences {
  smoothImage: boolean;
  cssImageFilter: string;
  controlPointsSize: number;
  autoFitOnResize: boolean;
  snapToGrid: boolean;
  afterBoxCreate: "pick_class" | "reuse_active";
  snapThresholdPx: number;
  zoomStepFactor: 1.05 | 1.1 | 1.15 | 1.2;
  fadedOpacity: number;
  maskOverlayOpacity: number;
}

export type VideoDefaultPlaybackRate = 0.25 | 0.5 | 1 | 2 | 4;
export type VideoLargeFrameStep = 5 | 10 | 30 | "grid";

export interface WorkbenchVideoPreferences {
  defaultPlaybackRate: VideoDefaultPlaybackRate;
  largeFrameStep: VideoLargeFrameStep;
  autoFitOnResize: boolean;
  trackContinueAutoAdvance: boolean;
}

export interface WorkbenchPointcloudPreferences {
  pointSize: number;
  persistCameraView: boolean;
  colorizeWithCamera: boolean;
  colorizeContrast: number;
  colorizeBrightness: number;
  colorizeGamma: number;
  showDepthHint: boolean;
  pointMaskSelectMode: "rect" | "lasso" | "polygon";
  showGrid: boolean;
  showAxisGizmo: boolean;
  cameraDamping: number;
  neighborPointOverlay: boolean;
  neighborPointOverlayK: 1 | 2 | 3;
  neighborPointCull: "keep" | "cull" | "align";
}

export interface WorkbenchPreferences {
  common: WorkbenchCommonPreferences;
  image: WorkbenchImagePreferences;
  video: WorkbenchVideoPreferences;
  pointcloud: WorkbenchPointcloudPreferences;
  layout: WorkbenchLayoutPreferences;
  shortcuts?: WorkbenchShortcutPreferences;
}

export interface ShortcutBindingPayload {
  key: string;
  modifiers: Array<"mod" | "alt" | "shift">;
}

export type ShortcutOverrideBucket = Record<string, ShortcutBindingPayload[] | null>;

export interface WorkbenchShortcutPreferences {
  schemaVersion: 1;
  common?: ShortcutOverrideBucket;
  image?: ShortcutOverrideBucket;
  video?: ShortcutOverrideBucket;
}

export interface FloatingPanelState {
  detached: boolean;
  x: number | null;
  y: number | null;
  w: number | null;
  h: number | null;
}

export type FloatingInspectorState = FloatingPanelState;

export interface FloatingSelectionState {
  collapsed: boolean;
  x: number | null;
  y: number | null;
  w: number | null;
  h: number | null;
}

export interface TriViewFloatState {
  collapsed: boolean;
  x: number | null;
  y: number | null;
  w: number | null;
  h: number | null;
}

export interface CameraPanelState {
  x: number | null;
  y: number | null;
  collapsed?: boolean;
}

export interface PointcloudCameraState {
  position: [number, number, number];
  target: [number, number, number];
  up: [number, number, number];
  mode: "orbit" | "bev";
}

export interface NamedWorkspacePreset extends WorkspaceEnvelope {
  name: string;
  context: WorkspaceContext;
}

export type StoredNamedWorkspacePresets = Record<string, unknown>;

export const MAX_NAMED_WORKSPACE_PRESETS = 5;
export const MAX_WORKSPACE_PRESET_NAME_LENGTH = 40;

export interface WorkspacePreferences {
  engine: string;
  contexts?: Partial<Record<WorkspaceContext, WorkspaceEnvelope>>;
  namedPresets?: StoredNamedWorkspacePresets;
}

export interface WorkspacePreferencesPatch {
  engine: "dockview@8";
  contexts?: Partial<Record<WorkspaceContext, WorkspaceEnvelope>>;
  namedPresets?: never;
}

export interface NamedWorkspacePresetsPatch {
  namedPresetsRevision: string;
  workbench: {
    layout: {
      workspace: {
        engine?: never;
        contexts?: never;
        namedPresets: StoredNamedWorkspacePresets;
      };
    };
  };
}

export interface WorkbenchLayoutPreferences {
  workspace?: WorkspacePreferences | null;
  leftOpen: boolean;
  rightOpen: boolean;
  attrPanelCollapsed: boolean;
  aiSectionCollapsed: boolean;
  manualSectionCollapsed: boolean;
  trackSectionCollapsed: boolean;
  discussionCollapsed: boolean;
  floatingTaskQueue: FloatingPanelState;
  floatingClassPalette: FloatingPanelState;
  floatingInspector: FloatingPanelState;
  floatingDiscussion: FloatingPanelState;
  floatingSelection: FloatingSelectionState;
  triViewFloat: TriViewFloatState;
  cameraPanels: Record<string, CameraPanelState>;
  pointcloudCamera: PointcloudCameraState | null;
}

export interface AIToolPreferences {
  params_by_backend?: Record<string, Record<string, unknown>>;
  model_by_backend?: Record<string, string>;
  interactive_backend_by_project?: Record<string, string>;
  secondary_by_model?: Record<
    string,
    { params?: Record<string, unknown>; variants?: Record<string, unknown> }
  >;
}

export type ThemePref = "light" | "dark" | "system";

export interface UIPreferences {
  theme?: ThemePref;
  secondary_bar_hidden?: boolean;
  changelog_seen_version?: string;
}

export interface OnboardingProjectState {
  guide_version: string;
  dismissed: boolean;
  guide_read: boolean;
}

export interface OnboardingPreferences {
  projects: Record<string, OnboardingProjectState>;
}

export interface OnboardingPreferencesPatch {
  projects?: Record<string, Partial<OnboardingProjectState>>;
}

export interface UserPreferences {
  namedPresetsRevision?: string;
  workbench: WorkbenchPreferences;
  ai: AIToolPreferences;
  ui: UIPreferences;
  onboarding?: OnboardingPreferences;
}

export type UserPreferencesPatch = Omit<
  Partial<UserPreferences>,
  "workbench" | "onboarding" | "namedPresetsRevision"
> & {
  workbench?: Omit<Partial<WorkbenchPreferences>, "layout" | "shortcuts"> & {
    layout?: Omit<Partial<WorkbenchLayoutPreferences>, "workspace"> & {
      workspace?: WorkspacePreferencesPatch;
    };
    shortcuts?: Partial<WorkbenchShortcutPreferences>;
  };
  onboarding?: OnboardingPreferencesPatch;
};

export const DEFAULT_WORKBENCH_PREFERENCES: WorkbenchPreferences = {
  common: {
    longTaskSampleRate: 0.05,
    confirmDelete: "never",
    recentClassesLimit: 5,
    crossFrameOverlayEnabled: false,
    crossFrameOverlayK: 1,
    crossFrameOverlayScope: "selected",
    performanceTier: "standard",
    leftWidthPct: 15,
    rightWidthPct: 15,
    labelFontSize: 12,
    labelVisibility: "always",
    labelContent: { single: [], track: ["id", "state"], ai: ["source", "score"] },
    strokeWidth: 1.5,
    fillOpacity: 0.07,
    fillOpacitySelected: 0.12,
    petEnabled: true,
    focusSelectionEnabled: false,
    autoAdvanceOnDecide: true,
    showAnnotationComments: true,
  },
  image: {
    smoothImage: true,
    cssImageFilter: "",
    controlPointsSize: 6,
    autoFitOnResize: true,
    snapToGrid: false,
    afterBoxCreate: "pick_class",
    snapThresholdPx: 8,
    zoomStepFactor: 1.1,
    fadedOpacity: 0.35,
    maskOverlayOpacity: 0.45,
  },
  video: {
    defaultPlaybackRate: 1,
    largeFrameStep: 10,
    autoFitOnResize: true,
    trackContinueAutoAdvance: false,
  },
  pointcloud: {
    pointSize: 0.06,
    persistCameraView: false,
    colorizeWithCamera: false,
    colorizeContrast: 1,
    colorizeBrightness: 0,
    colorizeGamma: 1,
    showDepthHint: false,
    pointMaskSelectMode: "rect",
    showGrid: true,
    showAxisGizmo: true,
    cameraDamping: 0.1,
    neighborPointOverlay: false,
    neighborPointOverlayK: 1,
    neighborPointCull: "keep",
  },
  layout: {
    leftOpen: true,
    rightOpen: true,
    attrPanelCollapsed: false,
    aiSectionCollapsed: false,
    manualSectionCollapsed: false,
    trackSectionCollapsed: false,
    discussionCollapsed: false,
    floatingTaskQueue: {
      detached: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    floatingClassPalette: {
      detached: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    floatingInspector: {
      detached: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    floatingDiscussion: {
      detached: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    floatingSelection: {
      collapsed: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    triViewFloat: {
      collapsed: false,
      x: null,
      y: null,
      w: null,
      h: null,
    },
    cameraPanels: {},
    pointcloudCamera: null,
  },
};

export interface MeResponse {
  id: string;
  email: string;
  name: string;
  role: string;
  group_name: string | null;
  status: string;
  created_at: string;
  password_admin_reset_at?: string | null;
  deactivation_requested_at?: string | null;
  deactivation_scheduled_at?: string | null;
  preferences?: Partial<UserPreferences>;
  avatar_ref?: string | null;
}

export const authApi = {
  login: (payload: LoginPayload) => apiClient.post<TokenResponse>("/auth/login", payload),
  me: () => apiClient.get<MeResponse>("/auth/me"),
  logout: () => apiClient.post<void>("/auth/logout", {}),
  logoutAll: () => apiClient.post<TokenResponse>("/auth/logout-all", {}),
  refresh: () => apiClient.post<TokenResponse>("/auth/refresh", {}),
  getPreferences: () => apiClient.get<UserPreferences>("/auth/me/preferences"),
  updatePreferences: (payload: UserPreferencesPatch, session?: AuthSessionOwner) =>
    session
      ? apiClient.patchForSession<UserPreferences>("/auth/me/preferences", payload, session)
      : apiClient.patch<UserPreferences>("/auth/me/preferences", payload),
  updateNamedPresets: (payload: NamedWorkspacePresetsPatch) =>
    apiClient.patch<UserPreferences>("/auth/me/preferences", payload),
};
