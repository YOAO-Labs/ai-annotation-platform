import { apiClient } from "./client";
import type {
  WorkspaceContext,
  WorkspaceEnvelope,
} from "@/pages/Workbench/layout/workbenchLayoutSnapshot";

export interface LoginPayload {
  email: string;
  password: string;
  // v0.9.3 · progressive CAPTCHA：达到失败阈值后必填
  captcha_token?: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

/** v0.15.3 · 偏好四分树:跨模态通用项。 */
export interface WorkbenchCommonPreferences {
  longTaskSampleRate: number;
  confirmDelete: "never" | "multi_only" | "always";
  recentClassesLimit: number;
  /** v0.15.19 · 邻帧框叠加独立开关;历史 crossFrameOverlayK=0 仍按关闭兼容。 */
  crossFrameOverlayEnabled: boolean;
  /** v0.15.6 · 邻帧框叠加帧数档位(1/3/5/7;0 仅兼容旧关闭值)。 */
  crossFrameOverlayK: number;
  /** v0.15.17 · 邻帧框叠加范围:selected=仅选中对象的 group(现状);all=不选对象也叠邻帧全部框。 */
  crossFrameOverlayScope: "selected" | "all";
  performanceTier: "light" | "standard" | "aggressive";
  /** 左右边栏宽度,占工作台宽度百分比;拖拽与设置面板双向同步,默认 15。 */
  leftWidthPct: number;
  rightWidthPct: number;
  /** v0.15.27 · 标注视觉样式(图片 + 视频共享,annotationVisual.ts 消费)。 */
  /** 标签字号基准 px(图片按画布缩放 /scale,视频固定 CSS px)。 */
  labelFontSize: number;
  /** 标签显隐:always 恒显 / selected 仅选中 / none 不显示(取代旧 image.showBoxLabels)。 */
  labelVisibility: "always" | "selected" | "none";
  /** v0.16.7 · 标签内容按标注类型分段(single/track/ai);class 三段恒显。 */
  labelContent: LabelContentByType;
  /** 描边线宽基准(screen px;选中态 = 基值 + 0.5)。 */
  strokeWidth: number;
  /** 闭合形状填充透明度(非选中)。 */
  fillOpacity: number;
  /** 选中对象填充加重透明度。 */
  fillOpacitySelected: number;
  /** v0.20.x · 工作台桌宠(常驻像素小精灵);关闭后选中信息卡折叠态回退为纯文字小条。 */
  petEnabled: boolean;
  /**
   * v0.21.11 · 焦点联动:选中对象(键盘两级循环 / 点选)时, 若对象出视口或过小则自动平移居中 + 适度缩放。
   * 视频 + 图片 2D 工作台共享。默认关(不改选中前不移动视口的现状)。
   */
  focusSelectionEnabled: boolean;
  /**
   * v0.21.11 · 审阅流水线:采纳/拒绝(A/D)AI 候选后, 自动把选中推进到下一个待决 AI(仅移动选中,
   * 不缩放视口;视口聚焦另由 focusSelectionEnabled 控制)。视频 + 图片 2D 共享。默认开(流水线手感)。
   */
  autoAdvanceOnDecide: boolean;
  /** Show per-annotation comment counts on the image canvas. */
  showAnnotationComments: boolean;
}

/** v0.16.7 · 标签字段 token 全集;class 三段恒显,不入表。 */
export type LabelFieldToken = "id" | "score" | "attrs" | "source" | "state";

/** v0.16.7 · 标签内容按标注类型分段;每段只含该类型有意义的字段。 */
export interface LabelContentByType {
  /** 单帧(图片手工框):分组号 #id / 属性。 */
  single: Array<"id" | "attrs">;
  /** 轨迹(视频 track 框):轨迹号 #num / 状态(插值·遮挡) / 属性。 */
  track: Array<"id" | "state" | "attrs">;
  /** AI(图片预测框):✦来源前缀 / 置信度 / 分组号 / 属性。 */
  ai: Array<"source" | "score" | "id" | "attrs">;
}

/** v0.16.7 · per-type 默认值(对齐旧观感:单帧只类别名 / 轨迹 #号+状态 / AI 来源+置信度)。 */
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

/** v0.16.7 · 旧扁平 labelContent(string[]) / 部分对象 → 规范 LabelContentByType。 */
export function migrateLabelContent(raw: unknown): LabelContentByType {
  // 旧扁平 list 只作用过图片:single/ai 按老值分发(ai 补 source 保 ✦ 前缀),track 用默认。
  if (Array.isArray(raw)) {
    return {
      single: uniqFilter(raw, ["id", "attrs"] as const),
      track: [...DEFAULT_LABEL_CONTENT.track],
      ai: uniqFilter(["source", ...raw], ["source", "score", "id", "attrs"] as const),
    };
  }
  // 对象:逐段去重过滤;缺段 / 非数组补默认。
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

/** v0.15.3 · 图像工作台渲染偏好(原顶层平铺字段归位)。 */
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
  /** 仅用于读取旧偏好；Mask 渲染已统一使用 common.fillOpacity / fillOpacitySelected。 */
  maskOverlayOpacity: number;
}

export type VideoDefaultPlaybackRate = 0.25 | 0.5 | 1 | 2 | 4;
export type VideoLargeFrameStep = 5 | 10 | 30 | "grid";

/** v0.15.5 · 视频工作台播放 / 步进偏好。 */
export interface WorkbenchVideoPreferences {
  defaultPlaybackRate: VideoDefaultPlaybackRate;
  largeFrameStep: VideoLargeFrameStep;
  autoFitOnResize: boolean;
  /**
   * v0.21.12 · 轨迹「续写后自动前进」:跨网格帧续写完一条轨迹后, 自动选中同帧下一条待续轨迹
   * (上一网格帧有关键帧、当前帧未画者)。默认关(保持逐条手动 Tab / 点选的现状)。
   */
  trackContinueAutoAdvance: boolean;
}

/** v0.15.6 · 点云工作台渲染 / 导航偏好。 */
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
  /** v0.15.18 · 邻帧点云叠加(ego 补偿对齐前后帧点云,静止背景加密/动态拖影)。需 ego 轨迹。 */
  neighborPointOverlay: boolean;
  /** v0.15.19 · 邻帧点云叠加帧数,独立于邻帧框叠加;点云较重,限制为 1-3。 */
  neighborPointOverlayK: 1 | 2 | 3;
  /**
   * v0.15.22 · §C.8-B / v0.15.23 · §C.8-A 邻帧点云动态点处理:
   * keep=保留拖影 / cull=剔除落在当前帧 box 内的邻帧点 / align=逐目标把邻帧点搬到当前位置。
   */
  neighborPointCull: "keep" | "cull" | "align";
}

/** v0.15.3 · common/image/video/pointcloud 四子树;layout 保持顶层(壳层/设备维度)。 */
export interface WorkbenchPreferences {
  common: WorkbenchCommonPreferences;
  image: WorkbenchImagePreferences;
  video: WorkbenchVideoPreferences;
  pointcloud: WorkbenchPointcloudPreferences;
  layout: WorkbenchLayoutPreferences;
  /**
   * Increment B · 快捷键覆盖（账号级，跟随账号跨项目）。
   * 只由 useWorkbenchShortcutPreferences 的定向 PATCH 维护；其余 workbench 写路径
   * （布局 flush 等）不得携带该子树，后端深合并保持存量值不动。
   */
  shortcuts?: WorkbenchShortcutPreferences;
}

/** Increment B · 单条结构化绑定：规范化 key + 显式修饰键（Mod = 平台主修饰键）。 */
export interface ShortcutBindingPayload {
  key: string;
  modifiers: Array<"mod" | "alt" | "shift">;
}

/** commandId → 组合列表；null = 恢复默认，[] = 停用，缺失 = 用默认。 */
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

/**
 * v0.16.8 · 选中标注浮动信息卡的位置 / 尺寸 / 折叠态(跨设备)。
 * 与边栏浮窗不同:无「合并回边栏」语义,故只有 collapsed(无 detached);显隐由选中状态驱动。
 */
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

/** v0.15.x · 3D 悬浮相机面板位置 + 折叠态,按相机 role 分桶。x/y 为 null = 未拖动,用默认贴边位。 */
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

/**
 * 用户显式另存的布局。与 `contexts` 的直播快照分开:后者是"当前布局自动记住",
 * 本表是账号级最多 5 份可反复套用的命名副本,由布局设置里的 CRUD 整表提交。
 */
export interface NamedWorkspacePreset extends WorkspaceEnvelope {
  name: string;
  context: WorkspaceContext;
}

/** GET 保留无法由当前客户端解释的存量条目，写入时需原样带回。 */
export type StoredNamedWorkspacePresets = Record<string, unknown>;

export const MAX_NAMED_WORKSPACE_PRESETS = 5;
export const MAX_WORKSPACE_PRESET_NAME_LENGTH = 40;

export interface WorkspacePreferences {
  /** GET 可能透传更新客户端写入的引擎标记。 */
  engine: string;
  /** 只存过命名预设的账号还没有任何直播快照,此时整个键缺席。 */
  contexts?: Partial<Record<WorkspaceContext, WorkspaceEnvelope>>;
  namedPresets?: StoredNamedWorkspacePresets;
}

export interface WorkspacePreferencesPatch {
  engine: "dockview@8";
  contexts?: Partial<Record<WorkspaceContext, WorkspaceEnvelope>>;
  namedPresets?: never;
}

/** Compare-and-swap write owned exclusively by useWorkbenchNamedPresets. */
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
  /** GET may preserve an explicitly null historical/corrupt stored value. */
  workspace?: WorkspacePreferences | null;
  leftOpen: boolean;
  rightOpen: boolean;
  /** v0.20.19 · 右栏「标注详情」属性区折叠态(随账号持久)。 */
  attrPanelCollapsed: boolean;
  /** v0.20.22 · 右栏「AI 待审」分组折叠态(随账号持久)。 */
  aiSectionCollapsed: boolean;
  /** v0.20.22 · 右栏「人工」分组折叠态(随账号持久)。 */
  manualSectionCollapsed: boolean;
  /** 右栏「轨迹」分组折叠态(视频任务;随账号持久)。 */
  trackSectionCollapsed: boolean;
  /** v0.20.22 · 右栏下段「讨论」(评论/历史/Issue)完全收起态(随账号持久)。 */
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

/**
 * 每用户的 AI 工具偏好。子键各自独立保存（后端 `ai` 子树深一层合并）:
 * - params_by_backend: 推理参数（按 backend；不同后端参数 schema 不同）。
 * - model_by_backend (v0.18.25): 交互工具的引擎(模型)选择（按 backend）。
 * - interactive_backend_by_project (v0.18.31): 交互后端(引擎)选择（按 project）。
 * - secondary_by_model (v0.20.17): 单框二次推理的参数 + 模型变体（按 `backendId:modelId`）。
 * 各 writer 只提交自己那一子键，故皆可选。
 */
export interface AIToolPreferences {
  params_by_backend?: Record<string, Record<string, unknown>>;
  model_by_backend?: Record<string, string>;
  interactive_backend_by_project?: Record<string, string>;
  secondary_by_model?: Record<
    string,
    { params?: Record<string, unknown>; variants?: Record<string, unknown> }
  >;
}

/** v0.15.25 · 主题偏好:light/dark 固定，system 跟随 OS prefers-color-scheme。 */
export type ThemePref = "light" | "dark" | "system";

/** v0.15.25 · 全局 UI 偏好(工作台之外);主题从 localStorage 升到服务端,跟随账号跨设备。 */
export interface UIPreferences {
  /** 缺省(partial PATCH 只提交别的 ui 子键时)由后端回落 "system"。 */
  theme?: ThemePref;
  /** v0.20.19 · 二次推理面板显隐(跨设备);true=隐藏。缺省 false=显示。 */
  secondary_bar_hidden?: boolean;
  /** 登录后「版本更新提醒」已确认的版本(跨设备)。空/缺省 = 从未确认;前端按 semver
   *  比较,仅当构建版本更新时弹一次「本次更新」,确认后写回本键。 */
  changelog_seen_version?: string;
}

/** v0.10.14 · 员工开工清单按账号、项目和指南版本持久化。 */
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
  /** Opaque compare-and-swap token for the complete namedPresets map. */
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
    /**
     * 快捷键覆盖的定向 PATCH：只携带变更的命令条目，且刻意省略 schemaVersion
     * （显式写 1 会经深合并把更新版本的存量树降级；缺省键由后端 exclude_unset 丢弃）。
     */
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
  // v0.8.1
  password_admin_reset_at?: string | null;
  deactivation_requested_at?: string | null;
  deactivation_scheduled_at?: string | null;
  // v0.9.41 · 标注偏好（workbench 渲染配置等）。空对象表示未设置，按客户端默认。
  preferences?: Partial<UserPreferences>;
  /** 头像引用(`preset:<slug>` / `upload:<token>`);空 = 首字母回退。 */
  avatar_ref?: string | null;
}

export const authApi = {
  login: (payload: LoginPayload) => apiClient.post<TokenResponse>("/auth/login", payload),
  me: () => apiClient.get<MeResponse>("/auth/me"),
  logout: () => apiClient.post<void>("/auth/logout", {}),
  logoutAll: () => apiClient.post<TokenResponse>("/auth/logout-all", {}),
  // v0.8.8 · 用现有（即将 / 已过期）token 换新 token，7 天 grace 内有效。
  // useNotificationSocket onclose 1008/4001 时触发，长会话标注员永不被踢。
  refresh: () => apiClient.post<TokenResponse>("/auth/refresh", {}),
  // v0.9.41 · 工作台偏好读写
  getPreferences: () => apiClient.get<UserPreferences>("/auth/me/preferences"),
  // 后端按顶层子树合并（exclude_unset），故可只提交单个子树（workbench 或 ai）。
  updatePreferences: (payload: UserPreferencesPatch) =>
    apiClient.patch<UserPreferences>("/auth/me/preferences", payload),
  updateNamedPresets: (payload: NamedWorkspacePresetsPatch) =>
    apiClient.patch<UserPreferences>("/auth/me/preferences", payload),
};
