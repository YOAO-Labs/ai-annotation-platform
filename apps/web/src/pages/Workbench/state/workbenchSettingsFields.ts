// v0.15.3 · 工作台设置字段注册表(单一来源):工作台设置窗口与 Settings 页「标注偏好」
// 共用本数组渲染,杜绝两处 UI 漂移。新增字段流程:后端子树加字段 → auth.ts 类型同步 →
// 这里加一行 → 消费点读配置。
import type { LabelContentByType, WorkbenchPreferences } from "@/api/auth";
import {
  WEBCODECS_DEFAULT_ENABLED,
  WEBCODECS_FLAG_STORAGE_KEY,
} from "../stage/useVideoChunkDecoder";
import {
  readVideoReferenceSetting,
  writeVideoReferenceSetting,
  type VideoReferenceSetting,
} from "../stage/videoReferencePredict";
import type { LockableField, WorkbenchConfigPatch } from "./useWorkbenchConfig";
import {
  POINT_CLOUD_WEBGPU_STORAGE_KEY,
  readPointCloudWebGpuExperiment,
  writePointCloudWebGpuExperiment,
} from "../stages/three-d/pointCloudExperiment";

export type WorkbenchPreferenceSettingCategory = "common" | "image" | "video" | "pointcloud";
export type WorkbenchSettingCategory = WorkbenchPreferenceSettingCategory | "experiment";

export type WorkbenchSettingValue = boolean | number | string | string[] | LabelContentByType;

export type WorkbenchSettingControl =
  | { type: "toggle"; onText?: string; offText?: string }
  | {
      type: "slider";
      min: number;
      max: number;
      step: number;
      format?: (v: number) => string;
      resetTo?: number;
    }
  | { type: "select"; options: Array<{ value: WorkbenchSettingValue; label: string }> }
  // v0.15.27 · 多选(存 string[]);min 兜底至少保留几项(labelContent 至少留「类别名」)。
  | { type: "multiselect"; options: Array<{ value: string; label: string }>; min?: number }
  | { type: "text"; maxLength: number; placeholder?: string }
  // v0.16.7 · 标签内容按标注类型分段：每段独立 toggle 列，提交整个 LabelContentByType 对象。
  | {
      type: "labelContentByType";
      segments: Array<{
        key: "single" | "track" | "ai";
        label: string;
        options: Array<{ value: string; label: string }>;
      }>;
    };

interface WorkbenchSettingFieldBase {
  /** "image.controlPointsSize" — 与 WorkbenchPreferences 子树路径一致(category.字段名)。 */
  key: `${WorkbenchSettingCategory}.${string}`;
  /** 子设置挂到父开关下面;父开关关闭时子项禁用并置灰。 */
  parentKey?: `${WorkbenchSettingCategory}.${string}`;
  category: WorkbenchSettingCategory;
  /** 仅用于展示分组，不参与偏好存储。 */
  section: WorkbenchSettingSection;
  label: string;
  description?: string;
  control: WorkbenchSettingControl;
  /** 注册但不渲染。v0.15.3 红线:不新增用户可感知项(snapToGrid 现状无设置 UI)。 */
  hidden?: boolean;
}

export interface WorkbenchPreferenceSettingField extends WorkbenchSettingFieldBase {
  key: `${WorkbenchPreferenceSettingCategory}.${string}`;
  category: WorkbenchPreferenceSettingCategory;
  storage?: "preferences";
  /** 是否参与项目锁定(ProjectRenderingConfig 平铺同名字段可覆盖,v0.10.10)。 */
  lockable?: boolean;
}

export interface WorkbenchLocalSettingField extends WorkbenchSettingFieldBase {
  key: `experiment.${string}`;
  category: "experiment";
  storage: "local";
  read: () => WorkbenchSettingValue;
  write: (value: WorkbenchSettingValue) => void;
}

export type WorkbenchSettingField = WorkbenchPreferenceSettingField | WorkbenchLocalSettingField;

export const WORKBENCH_SETTING_CATEGORY_LABELS: Record<WorkbenchSettingCategory, string> = {
  common: "通用",
  image: "图片",
  video: "视频",
  pointcloud: "点云",
  experiment: "实验特性",
};

// 导航按用途组织，category 继续表示原有偏好存储子树。
// notifications 组渲染共享的通知偏好面板，不进 buildFieldPatch/useWorkbenchConfig。
export const WORKBENCH_SETTING_GROUPS = {
  layout: { label: "界面布局", description: "边栏宽度与工作台辅助显示。" },
  appearance: { label: "标注显示", description: "标签、线条、填充和邻帧参考框。" },
  editing: { label: "编辑与辅助", description: "标注操作、图片绘制和 AI 辅助工具。" },
  canvas: { label: "画布与视角", description: "统一调整图片、视频和点云的显示与视角。" },
  playback: { label: "播放与轨迹", description: "视频播放速度、时间轴步进与轨迹续写。" },
  advanced: { label: "性能与实验", description: "资源用量和本机实验特性，按各项说明生效。" },
  notifications: {
    label: "通知",
    description: "按类型设置接收与弹出提示，账号内所有工作台同步生效。",
  },
} as const;

export type WorkbenchSettingGroup = keyof typeof WORKBENCH_SETTING_GROUPS;

export const WORKBENCH_SETTING_SECTIONS = {
  layout: { label: "工作台布局", group: "layout" },
  appearance: { label: "标注外观", group: "appearance" },
  reference: { label: "邻帧参考框", group: "appearance" },
  behavior: { label: "操作行为", group: "editing" },
  drawing: { label: "图片绘制与编辑", group: "editing" },
  ai: { label: "图片 AI 辅助", group: "editing" },
  imageDisplay: { label: "图片画布", group: "canvas" },
  videoDisplay: { label: "视频画布", group: "canvas" },
  points: { label: "点云显示", group: "canvas" },
  color: { label: "点云相机与上色", group: "canvas" },
  camera: { label: "点云视角与选择", group: "canvas" },
  neighbors: { label: "邻帧点云", group: "canvas" },
  playback: { label: "视频播放", group: "playback" },
  tracking: { label: "轨迹操作", group: "playback" },
  performance: { label: "资源与性能", group: "advanced" },
  experiments: { label: "本机实验特性", group: "advanced" },
} as const;

export type WorkbenchSettingSection = keyof typeof WORKBENCH_SETTING_SECTIONS;

function readLocalBoolean(key: string, defaultValue = false): boolean {
  if (typeof window === "undefined") return defaultValue;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return defaultValue;
    return raw === "1" || raw === "true";
  } catch {
    return defaultValue;
  }
}

function writeLocalBoolean(key: string, value: WorkbenchSettingValue): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* local device flag is best-effort */
  }
}

export const WORKBENCH_SETTING_FIELDS: WorkbenchSettingField[] = [
  {
    key: "common.leftWidthPct",
    section: "layout",
    category: "common",
    label: "左栏宽度",
    description:
      "调整画布同侧可见列的宽度；多列分别调整，受面板最小尺寸限制。隐藏或专注时不调整，可拖动分隔条精调。",
    control: { type: "slider", min: 10, max: 35, step: 1, format: (v) => `${v}%`, resetTo: 15 },
  },
  {
    key: "common.rightWidthPct",
    section: "layout",
    category: "common",
    label: "右栏宽度",
    description:
      "调整画布同侧可见列的宽度；多列分别调整，受面板最小尺寸限制。隐藏或专注时不调整，可拖动分隔条精调。",
    control: { type: "slider", min: 10, max: 35, step: 1, format: (v) => `${v}%`, resetTo: 15 },
  },
  {
    key: "common.longTaskSampleRate",
    section: "performance",
    category: "common",
    label: "性能采样率",
    description: "PerformanceObserver longtask 采样率，0–1",
    control: { type: "slider", min: 0, max: 1, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "common.confirmDelete",
    section: "behavior",
    category: "common",
    label: "删除确认",
    description: "删除标注时是否二次确认：从不 / 仅多选删除 / 始终",
    control: {
      type: "select",
      options: [
        { value: "never", label: "不确认" },
        { value: "multi_only", label: "仅多选确认" },
        { value: "always", label: "始终确认" },
      ],
    },
  },
  {
    key: "common.recentClassesLimit",
    section: "behavior",
    category: "common",
    label: "最近类别数量",
    description: "类别面板顶部「最近使用」保留的条目数",
    control: { type: "slider", min: 3, max: 20, step: 1, format: (v) => `${v}` },
  },
  {
    key: "common.petEnabled",
    section: "layout",
    category: "common",
    label: "工作台桌宠",
    description:
      "常驻像素小人:久坐轻提示、标注里程碑庆祝;选中图片或视频标注时举牌,选中 3D 标注时与信息栏合并联动。关闭后退回独立浮层",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "common.focusSelectionEnabled",
    section: "behavior",
    category: "common",
    label: "选中自动聚焦",
    description:
      "键盘循环(Tab / `)或点选对象时,若对象在画布外或过小,自动平移居中并适度放大。视频 + 图片工作台通用",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "common.autoAdvanceOnDecide",
    section: "behavior",
    category: "common",
    label: "决策后自动前进",
    description:
      "采纳 / 忽略选中 AI 候选成功后，自动选择下一个待决对象；按钮与 A / D 通用。视频仅在当前帧内前进，失败或等待补选类别时保留选择。视口聚焦由上一项控制",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "common.showAnnotationComments",
    section: "appearance",
    category: "common",
    label: "标注评论提示",
    description: "在图片和视频画布的已保存标注旁显示评论数量，点击可打开对应的评论列表",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "common.crossFrameOverlayEnabled",
    section: "reference",
    category: "common",
    label: "邻帧框叠加",
    description: "开启后显示相邻帧参考框",
    control: { type: "toggle" },
  },
  {
    key: "common.crossFrameOverlayK",
    section: "reference",
    parentKey: "common.crossFrameOverlayEnabled",
    category: "common",
    label: "邻帧框帧数",
    description: "前后各显示几帧参考框",
    control: {
      type: "select",
      options: [
        { value: 1, label: "前后 1 帧" },
        { value: 3, label: "前后 3 帧" },
        { value: 5, label: "前后 5 帧" },
        { value: 7, label: "前后 7 帧" },
      ],
    },
  },
  {
    key: "common.crossFrameOverlayScope",
    section: "reference",
    parentKey: "common.crossFrameOverlayEnabled",
    category: "common",
    label: "邻帧框叠加范围",
    description: "选中对象=仅叠当前对象的邻帧框；全部=不选对象也叠邻帧所有框(渲染量更大)",
    control: {
      type: "select",
      options: [
        { value: "selected", label: "选中对象" },
        { value: "all", label: "全部" },
      ],
    },
  },
  {
    key: "common.performanceTier",
    section: "performance",
    category: "common",
    label: "性能档位",
    description: "控制视频缓存、预取窗口与点云抽稀上限",
    control: {
      type: "select",
      options: [
        { value: "light", label: "轻量" },
        { value: "standard", label: "标准" },
        { value: "aggressive", label: "激进" },
      ],
    },
  },
  {
    key: "common.labelFontSize",
    section: "appearance",
    category: "common",
    label: "标签字号",
    description: "标注标签文字大小;图片随画布缩放、视频固定像素。图片与视频共用",
    control: { type: "slider", min: 8, max: 24, step: 1, format: (v) => `${v}px` },
  },
  {
    key: "common.labelVisibility",
    section: "appearance",
    category: "common",
    label: "标签显隐",
    description: "标注标签何时显示：始终 / 仅选中对象时 / 从不。图片、视频与点云共用",
    control: {
      type: "select",
      options: [
        { value: "always", label: "始终显示" },
        { value: "selected", label: "仅选中时" },
        { value: "none", label: "不显示" },
      ],
    },
  },
  {
    key: "common.labelContent",
    section: "appearance",
    category: "common",
    label: "标签内容",
    description:
      "按标注类型分段控制标签显示哪些信息;类别名三段恒显。单帧=图片手工框，轨迹=视频轨迹和点云框，AI=图片预测框",
    control: {
      type: "labelContentByType",
      segments: [
        {
          key: "single",
          label: "单帧",
          options: [
            { value: "id", label: "分组号" },
            { value: "attrs", label: "属性" },
          ],
        },
        {
          key: "track",
          label: "轨迹",
          options: [
            { value: "id", label: "轨迹号" },
            { value: "state", label: "状态" },
            { value: "attrs", label: "属性" },
          ],
        },
        {
          key: "ai",
          label: "AI预测",
          options: [
            { value: "source", label: "来源" },
            { value: "score", label: "置信度" },
            { value: "id", label: "分组号" },
            { value: "attrs", label: "属性" },
          ],
        },
      ],
    },
  },
  {
    key: "common.strokeWidth",
    section: "appearance",
    category: "common",
    label: "线宽",
    description: "标注描边粗细;选中对象自动加粗 0.5。图片与视频共用",
    control: { type: "slider", min: 1, max: 5, step: 0.5, format: (v) => v.toFixed(1) },
  },
  {
    key: "common.fillOpacity",
    section: "appearance",
    category: "common",
    label: "填充透明度",
    description: "可闭合标注(框/多边形/旋转框)内部填充透明度;折线和点无填充。图片与视频共用",
    control: { type: "slider", min: 0, max: 0.6, step: 0.01, format: (v) => v.toFixed(2) },
  },
  {
    key: "common.fillOpacitySelected",
    section: "appearance",
    category: "common",
    label: "选中填充透明度",
    description: "选中对象的内部填充加重程度,便于区分当前对象。图片与视频共用",
    control: { type: "slider", min: 0, max: 0.8, step: 0.01, format: (v) => v.toFixed(2) },
  },
  {
    key: "image.smoothImage",
    section: "imageDisplay",
    category: "image",
    label: "图像平滑",
    description: "关闭后像素清晰，适合医学影像 / 像素艺术",
    control: {
      type: "toggle",
      onText: "已开启 — 默认双线性插值",
      offText: "已关闭 — 像素 nearest-neighbor",
    },
    lockable: true,
  },
  {
    key: "image.cssImageFilter",
    section: "imageDisplay",
    category: "image",
    label: "CSS 图像滤镜",
    description: "例：brightness(1.2) contrast(1.1) invert(0)；留空恢复原图",
    control: { type: "text", maxLength: 255, placeholder: "brightness(1.2) contrast(1.1)" },
    lockable: true,
  },
  {
    key: "image.controlPointsSize",
    section: "drawing",
    category: "image",
    label: "控制点大小",
    description: "顶点拖拽手柄半径",
    control: { type: "slider", min: 2, max: 20, step: 1, format: (v) => `${v}px` },
    lockable: true,
  },
  {
    key: "image.autoFitOnResize",
    section: "imageDisplay",
    category: "image",
    label: "自动适应大小",
    description: "展开或收起边栏后自动让图片重新适应画布",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "image.snapToGrid",
    section: "drawing",
    category: "image",
    label: "网格吸附",
    control: { type: "toggle" },
    lockable: true,
    // 现状只有项目级覆盖入口,用户侧无 UI;本版不新增可感知项,先注册不渲染。
    hidden: true,
  },
  {
    key: "image.afterBoxCreate",
    section: "drawing",
    category: "image",
    label: "画框后行为",
    description: "画完一个框后：弹出类别选择 / 直接沿用当前激活类别",
    control: {
      type: "select",
      options: [
        { value: "pick_class", label: "选择类别" },
        { value: "reuse_active", label: "沿用当前类别" },
      ],
    },
  },
  {
    key: "image.snapThresholdPx",
    section: "drawing",
    category: "image",
    label: "吸附阈值",
    description: "顶点/边吸附到邻近标注的触发距离，越大越容易吸附",
    control: { type: "slider", min: 4, max: 16, step: 1, format: (v) => `${v}px` },
  },
  {
    key: "image.zoomStepFactor",
    section: "imageDisplay",
    category: "image",
    label: "滚轮缩放步长",
    description: "每次滚轮缩放画布的倍率",
    control: {
      type: "select",
      options: [
        { value: 1.05, label: "5%" },
        { value: 1.1, label: "10%" },
        { value: 1.15, label: "15%" },
        { value: 1.2, label: "20%" },
      ],
    },
  },
  {
    key: "image.fadedOpacity",
    section: "ai",
    category: "image",
    label: "重复 AI 候选透明度",
    description: "与人工标注高度重叠、已视为重复的 AI 候选透明度，越低越淡",
    control: { type: "slider", min: 0.1, max: 0.8, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "video.defaultPlaybackRate",
    section: "playback",
    category: "video",
    label: "默认播放速率",
    description: "打开视频任务时的初始播放速率",
    control: {
      type: "select",
      options: [
        { value: 0.25, label: "0.25x" },
        { value: 0.5, label: "0.5x" },
        { value: 1, label: "1x" },
        { value: 2, label: "2x" },
        { value: 4, label: "4x" },
      ],
    },
  },
  {
    key: "video.largeFrameStep",
    section: "playback",
    category: "video",
    label: "大步进帧数",
    description: "时间轴聚焦时 Shift+←/→ 使用",
    control: {
      type: "select",
      options: [
        { value: 5, label: "5 帧" },
        { value: 10, label: "10 帧" },
        { value: 30, label: "30 帧" },
        { value: "grid", label: "采样网格" },
      ],
    },
  },
  {
    key: "video.autoFitOnResize",
    section: "videoDisplay",
    category: "video",
    label: "自动适应大小",
    description: "展开、收起或拖宽边栏后自动让视频重新适应画布",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "video.trackContinueAutoAdvance",
    section: "tracking",
    category: "video",
    label: "续写后自动前进",
    description:
      "用轨迹工具跨网格帧续写完一条轨迹后,自动选中同帧下一条待续轨迹(上一网格帧有框、当前帧未画者),连续续写无需逐条 Tab / 点选。默认关",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "pointcloud.pointSize",
    section: "points",
    category: "pointcloud",
    label: "点大小",
    description: "点云渲染点径",
    control: { type: "slider", min: 0.01, max: 0.3, step: 0.01, format: (v) => v.toFixed(2) },
  },
  {
    key: "pointcloud.persistCameraView",
    section: "camera",
    category: "pointcloud",
    label: "持久化 3D 视角",
    description: "记住点云主视角，下次打开时恢复上次相机位置",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "pointcloud.colorizeWithCamera",
    section: "color",
    category: "pointcloud",
    label: "相机上色",
    description: "用标定相机图像给点云采样 RGB",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "pointcloud.colorizeContrast",
    section: "color",
    parentKey: "pointcloud.colorizeWithCamera",
    category: "pointcloud",
    label: "上色对比度",
    description: "相机上色后的 RGB 对比度",
    control: { type: "slider", min: 0.5, max: 2.5, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "pointcloud.colorizeBrightness",
    section: "color",
    parentKey: "pointcloud.colorizeWithCamera",
    category: "pointcloud",
    label: "上色亮度",
    description: "相机上色后的 RGB 亮度偏移",
    control: { type: "slider", min: -0.5, max: 0.5, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "pointcloud.colorizeGamma",
    section: "color",
    parentKey: "pointcloud.colorizeWithCamera",
    category: "pointcloud",
    label: "上色 Gamma",
    description: "相机上色后的中间调曲线",
    control: { type: "slider", min: 0.5, max: 3, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "pointcloud.showDepthHint",
    section: "color",
    category: "pointcloud",
    label: "深度提示",
    description: "相机视图显示深度热力与悬停距离",
    control: { type: "toggle", onText: "已开启", offText: "已关闭" },
  },
  {
    key: "pointcloud.pointMaskSelectMode",
    section: "camera",
    category: "pointcloud",
    label: "点选模式",
    description: "框选点云时的圈选方式：矩形 / 套索 / 多边形",
    control: {
      type: "select",
      options: [
        { value: "rect", label: "矩形" },
        { value: "lasso", label: "套索" },
        { value: "polygon", label: "多边形" },
      ],
    },
  },
  {
    key: "pointcloud.showGrid",
    section: "points",
    category: "pointcloud",
    label: "显示地面网格",
    description: "在 3D 场景显示地面参考网格",
    control: { type: "toggle" },
  },
  {
    key: "pointcloud.showAxisGizmo",
    section: "points",
    category: "pointcloud",
    label: "显示坐标轴",
    description: "在 3D 场景角落显示 XYZ 坐标轴指示器",
    control: { type: "toggle" },
  },
  {
    key: "pointcloud.cameraDamping",
    section: "camera",
    category: "pointcloud",
    label: "相机灵敏度",
    description: "值越小惯性越强",
    control: { type: "slider", min: 0.05, max: 0.3, step: 0.05, format: (v) => v.toFixed(2) },
  },
  {
    key: "pointcloud.neighborPointOverlay",
    section: "neighbors",
    category: "pointcloud",
    label: "邻帧点云叠加",
    description:
      "把前后帧点云按车体位姿对齐叠到当前帧:静止背景加密、动态目标留拖影。需 scene 有 ego 轨迹",
    control: { type: "toggle" },
  },
  {
    key: "pointcloud.neighborPointOverlayK",
    section: "neighbors",
    parentKey: "pointcloud.neighborPointOverlay",
    category: "pointcloud",
    label: "邻帧点云帧数",
    description: "前后各叠几帧点云;点云较重,最多 3 帧",
    control: {
      type: "select",
      options: [
        { value: 1, label: "前后 1 帧" },
        { value: 2, label: "前后 2 帧" },
        { value: 3, label: "前后 3 帧" },
      ],
    },
  },
  {
    key: "pointcloud.neighborPointCull",
    section: "neighbors",
    parentKey: "pointcloud.neighborPointOverlay",
    category: "pointcloud",
    label: "邻帧动态点",
    description:
      "对落在已标注框内的邻帧点:保留=留拖影 / 剔除=只叠静止背景 / 逐目标对齐=把点搬到当前帧位置一起加密(无拖影)。仅对已标注目标有效",
    control: {
      type: "select",
      options: [
        { value: "keep", label: "保留(拖影)" },
        { value: "cull", label: "剔除动态点" },
        { value: "align", label: "逐目标对齐" },
      ],
    },
  },
  {
    key: "experiment.pointCloudWebGpuRenderer",
    section: "experiments",
    category: "experiment",
    storage: "local",
    label: "3D WebGPU 渲染器",
    description:
      "仅当前浏览器，默认关闭；切换点云主视图和框体精修视图的渲染器，刷新或重新打开点云任务后生效。实际后端显示在 3D 状态栏",
    control: { type: "toggle", onText: "实验已启用", offText: "Legacy" },
    read: () => readPointCloudWebGpuExperiment(),
    write: (value) => writePointCloudWebGpuExperiment(Boolean(value)),
  },
  {
    key: "experiment.webcodecs",
    section: "experiments",
    category: "experiment",
    storage: "local",
    label: "WebCodecs 精确解码",
    description:
      "仅当前浏览器，默认开启；视频暂停、逐帧和 seek 优先精确解码，不支持时安全回退。刷新后生效；URL 的 webcodecs 参数优先于本机开关",
    control: { type: "toggle" },
    read: () => readLocalBoolean(WEBCODECS_FLAG_STORAGE_KEY, WEBCODECS_DEFAULT_ENABLED),
    write: (value) => writeLocalBoolean(WEBCODECS_FLAG_STORAGE_KEY, value),
  },
  {
    key: "experiment.videoReferencePredict",
    section: "experiments",
    category: "experiment",
    storage: "local",
    label: "参考框运动预测",
    description: "仅当前浏览器，即时生效；视频轨迹当前帧无框时的参考框预测方式，默认取最近关键帧",
    control: {
      type: "select",
      options: [
        { value: "off", label: "关(最近关键帧)" },
        { value: "linear", label: "恒速外推(前两关键帧)" },
        { value: "kalman-stable", label: "卡尔曼 · 平稳" },
        { value: "kalman-agile", label: "卡尔曼 · 灵敏" },
      ],
    },
    read: () => readVideoReferenceSetting(),
    write: (value) => writeVideoReferenceSetting(value as VideoReferenceSetting),
  },
];

export { POINT_CLOUD_WEBGPU_STORAGE_KEY };

export function isLocalSettingField(
  field: WorkbenchSettingField,
): field is WorkbenchLocalSettingField {
  return field.storage === "local";
}

/** 按 key 的子树路径从配置中取当前值。 */
export function getFieldValue(
  config: WorkbenchPreferences,
  field: WorkbenchSettingField,
): WorkbenchSettingValue {
  if (isLocalSettingField(field)) return field.read();
  const name = field.key.slice(field.category.length + 1);
  return (config[field.category] as unknown as Record<string, WorkbenchSettingValue>)[name];
}

/** 把单字段新值包装成子树级 patch(useWorkbenchConfig.update / setFields 入参)。 */
export function buildFieldPatch(
  field: WorkbenchSettingField,
  value: WorkbenchSettingValue,
): WorkbenchConfigPatch {
  if (isLocalSettingField(field)) {
    throw new Error("Local workbench setting fields do not build preference patches");
  }
  const name = field.key.slice(field.category.length + 1);
  return { [field.category]: { [name]: value } } as WorkbenchConfigPatch;
}

/** lockable 字段对应的 LockableField 名(平铺命名,与 lockedFields 比对);非 lockable 返回 null。 */
export function lockableFieldName(field: WorkbenchSettingField): LockableField | null {
  if (isLocalSettingField(field)) return null;
  if (!field.lockable) return null;
  return field.key.slice(field.category.length + 1) as LockableField;
}

/** 统一设置不按当前任务过滤；未开放的字段仍保持隐藏。 */
export function getVisibleWorkbenchSettingFields() {
  return WORKBENCH_SETTING_FIELDS.filter((field) => !field.hidden);
}

/** 窗口、搜索结果与个人页共用同一套展示顺序。 */
export function groupWorkbenchSettings<T extends { section: WorkbenchSettingSection }>(
  fields: readonly T[],
) {
  return (
    Object.entries(WORKBENCH_SETTING_GROUPS) as [
      WorkbenchSettingGroup,
      (typeof WORKBENCH_SETTING_GROUPS)[WorkbenchSettingGroup],
    ][]
  )
    .map(([key, group]) => ({
      key,
      ...group,
      sections: (
        Object.entries(WORKBENCH_SETTING_SECTIONS) as [
          WorkbenchSettingSection,
          (typeof WORKBENCH_SETTING_SECTIONS)[WorkbenchSettingSection],
        ][]
      )
        .filter(([, section]) => section.group === key)
        .map(([section, { label }]) => ({
          key: section,
          label,
          fields: fields.filter((field) => field.section === section),
        }))
        .filter((section) => section.fields.length > 0),
    }))
    .filter((group) => group.sections.length > 0);
}

type SearchableSetting = Pick<
  WorkbenchSettingField,
  "label" | "description" | "control" | "category" | "section"
> & {
  key: string;
  parentKey?: string;
};

export function filterWorkbenchSettings<T extends SearchableSetting>(
  fields: readonly T[],
  query: string,
): T[] {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [...fields];
  const matches = new Set(
    fields
      .filter((field) => {
        const control = field.control;
        const options =
          control.type === "select" || control.type === "multiselect"
            ? control.options.map((option) => option.label)
            : control.type === "labelContentByType"
              ? control.segments.flatMap((segment) => [
                  segment.label,
                  ...segment.options.map((option) => option.label),
                ])
              : [];
        const text = [
          field.label,
          field.description,
          WORKBENCH_SETTING_CATEGORY_LABELS[field.category],
          WORKBENCH_SETTING_SECTIONS[field.section].label,
          WORKBENCH_SETTING_GROUPS[WORKBENCH_SETTING_SECTIONS[field.section].group].label,
          ...options,
        ]
          .join(" ")
          .toLocaleLowerCase();
        return words.every((word) => text.includes(word));
      })
      .map((field) => field.key),
  );
  const parents = new Set(
    fields.filter((field) => matches.has(field.key)).map((field) => field.parentKey),
  );
  return fields.filter(
    (field) =>
      matches.has(field.key) ||
      parents.has(field.key) ||
      (field.parentKey !== undefined && matches.has(field.parentKey)),
  );
}
