// Increment B · 结构化按键绑定层。
//
// 职责（见 docs/plans 快捷键方案）：
//  - 可编辑命令的稳定 ID + 默认组合（与 dispatchKey 的 matchCommand 匹配）；
//  - Fixed 命令清单：本增量内不可改，但必须参与冲突盘点；
//  - 录制事件的规范化（KeyboardEvent.key + 显式修饰键，Mod = 平台主修饰键）；
//  - 生效绑定解析（默认 / 覆盖 / 停用）与保守的重叠冲突检查；
//  - 存量偏好（可能损坏 / 来自更新版本）的容错解析：损坏条目排除出执行并标记，
//    保留原值供修正，绝不用默认值静默覆盖。
//
// 上下文（stage 类型 / 选中态 / 工具）由现有状态所有方持有；这里只声明与冲突
// 判断相关的最小谓词，不构建第二棵工作台状态树。

import type { HotkeyCategory, HotkeyStage } from "./hotkeys";

export type ShortcutDomain = "common" | "image" | "video";
export type ShortcutModifier = "mod" | "alt" | "shift";

export interface ShortcutBinding {
  /** 规范化 KeyboardEvent.key：单字符小写，或小写命名键（如 "arrowright"）。 */
  key: string;
  /** 排序去重后的修饰键；匹配时要求完全一致。 */
  modifiers: ShortcutModifier[];
}

/** 冲突判断用的适用谓词（保守近似，源自各监听方的真实生效条件）。 */
export type ShortcutWhen =
  | "any"
  | "selection"
  | "noSelection"
  | "prediction"
  | "noPrediction"
  | "videoTrack"
  | "videoNoTrack"
  | "sampling";

export interface EditableCommandDef {
  id: string;
  /** 存储桶与作用域：common 命令跨工作台生效。 */
  domain: ShortcutDomain;
  label: string;
  category: HotkeyCategory;
  /** 默认组合；主组合在前，文档化的备用组合在后。 */
  bindings: ShortcutBinding[];
  when: ShortcutWhen;
}

export interface FixedShortcutDef {
  id: string;
  label: string;
  domain: ShortcutDomain | "threed";
  /** Fixed 命令可声明多个物理键（如候选键 Q 或 /）。 */
  bindings: ShortcutBinding[];
  when: ShortcutWhen;
  /** 冲突提示里展示的生效条件（产品语言）。 */
  applies: string;
  /** Existing local capture priority for these commands’ default bindings only. */
  defaultOverlaps?: readonly string[];
}

// ── 组合速记 ─────────────────────────────────────────────────────────────────

const bind = (key: string, ...modifiers: ShortcutModifier[]): ShortcutBinding => ({
  key,
  modifiers: [...modifiers].sort(),
});

const K = {
  mod: (key: string) => bind(key, "mod"),
  alt: (key: string) => bind(key, "alt"),
  shift: (key: string) => bind(key, "shift"),
};

/** Some local listeners deliberately ignore modifiers; reserve every combination they consume. */
const withAllModifiers = (...keys: string[]): ShortcutBinding[] =>
  keys.flatMap((key) =>
    [
      [],
      ["mod"],
      ["alt"],
      ["shift"],
      ["mod", "alt"],
      ["mod", "shift"],
      ["alt", "shift"],
      ["mod", "alt", "shift"],
    ].map((modifiers) => bind(key, ...(modifiers as ShortcutModifier[]))),
  );
const maskBindings = withAllModifiers("b", "e");

// ── 可编辑命令（本增量的显式开放清单）───────────────────────────────────────

export const EDITABLE_SHORTCUT_COMMANDS: EditableCommandDef[] = [
  // 通用
  {
    id: "common.task.next",
    domain: "common",
    label: "下一题",
    category: "task",
    bindings: [K.mod("arrowright")],
    when: "any",
  },
  {
    id: "common.task.prev",
    domain: "common",
    label: "上一题",
    category: "task",
    bindings: [K.mod("arrowleft")],
    when: "any",
  },
  // 图片工具
  {
    id: "image.tool.select",
    domain: "image",
    label: "选择工具",
    category: "draw",
    bindings: [bind("v"), bind("4", "alt")],
    when: "any",
  },
  {
    id: "image.tool.box",
    domain: "image",
    label: "矩形框工具",
    category: "draw",
    bindings: [bind("b"), bind("1", "alt")],
    when: "any",
  },
  {
    id: "image.tool.rotatedBox",
    domain: "image",
    label: "旋转框 (OBB) 工具",
    category: "draw",
    bindings: [bind("w")],
    when: "any",
  },
  {
    id: "image.tool.polygon",
    domain: "image",
    label: "多边形工具",
    category: "draw",
    bindings: [bind("p"), bind("2", "alt")],
    when: "any",
  },
  {
    id: "image.tool.polyline",
    domain: "image",
    label: "折线工具（开放、不闭合）",
    category: "draw",
    bindings: [bind("l")],
    when: "noSelection",
  },
  {
    id: "image.tool.keypoint",
    domain: "image",
    label: "关键点工具",
    category: "draw",
    bindings: [bind("f")],
    when: "any",
  },
  {
    id: "image.tool.mask",
    domain: "image",
    label: "Mask 笔刷工具",
    category: "draw",
    bindings: [bind("m")],
    when: "any",
  },
  {
    id: "image.tool.aiCycle",
    domain: "image",
    label: "AI 工具循环",
    category: "ai",
    bindings: [bind("s"), bind("3", "alt")],
    when: "any",
  },
  {
    id: "image.tool.magicBox",
    domain: "image",
    label: "Magic Box",
    category: "ai",
    bindings: [bind("g")],
    when: "any",
  },
  // 图片选中态
  {
    id: "image.selection.lock",
    domain: "image",
    label: "锁定 / 解锁选中对象",
    category: "selection",
    bindings: [bind("l")],
    when: "selection",
  },
  {
    id: "image.selection.hide",
    domain: "image",
    label: "隐藏 / 显示选中对象",
    category: "selection",
    bindings: [bind("h")],
    when: "selection",
  },
  // 视频工具
  {
    id: "video.tool.select",
    domain: "video",
    label: "视频选择工具",
    category: "draw",
    bindings: [bind("v"), bind("3", "alt")],
    when: "any",
  },
  {
    id: "video.tool.box",
    domain: "video",
    label: "视频矩形框工具",
    category: "draw",
    bindings: [bind("b"), bind("1", "alt")],
    when: "any",
  },
  {
    id: "video.tool.rotatedBox",
    domain: "video",
    label: "视频旋转框工具",
    category: "draw",
    bindings: [bind("w")],
    when: "any",
  },
  {
    id: "video.tool.keypoint",
    domain: "video",
    label: "视频关键点工具",
    category: "draw",
    bindings: [bind("f")],
    when: "any",
  },
  {
    id: "video.tool.track",
    domain: "video",
    label: "视频轨迹工具",
    category: "draw",
    bindings: [bind("t"), bind("2", "alt")],
    when: "any",
  },
  {
    id: "video.tool.mask",
    domain: "video",
    label: "视频单帧 Mask 工具",
    category: "draw",
    bindings: [bind("m")],
    when: "any",
  },
  {
    id: "video.tool.smartPoint",
    domain: "video",
    label: "视频智能点工具",
    category: "ai",
    bindings: [bind("s")],
    when: "any",
  },
  {
    id: "video.tool.smartBox",
    domain: "video",
    label: "视频智能框工具",
    category: "ai",
    bindings: [bind("d")],
    when: "noPrediction",
  },
  {
    id: "video.tool.exemplar",
    domain: "video",
    label: "视频示例框工具",
    category: "ai",
    bindings: [bind("e")],
    when: "any",
  },
  {
    id: "video.tool.magicBox",
    domain: "video",
    label: "视频 Magic Box 工具",
    category: "ai",
    bindings: [bind("g")],
    when: "any",
  },
  {
    id: "video.tool.polygon",
    domain: "video",
    label: "视频多边形工具",
    category: "ai",
    bindings: [bind("p")],
    when: "any",
  },
  // 视频选中轨迹
  {
    id: "video.track.locked",
    domain: "video",
    label: "锁定 / 解锁轨迹",
    category: "playback",
    bindings: [bind("l")],
    when: "videoTrack",
  },
  {
    id: "video.track.hidden",
    domain: "video",
    label: "隐藏 / 显示轨迹",
    category: "playback",
    bindings: [bind("h")],
    when: "videoTrack",
  },
  {
    id: "video.track.outside",
    domain: "video",
    label: "标记 / 恢复当前帧消失",
    category: "playback",
    bindings: [bind("o")],
    when: "videoTrack",
  },
  {
    id: "video.track.occluded",
    domain: "video",
    label: "标记 / 恢复当前帧遮挡",
    category: "playback",
    bindings: [bind("q"), bind("/")],
    when: "videoTrack",
  },
  {
    id: "video.track.bookmark",
    domain: "video",
    label: "当前帧添加 / 移除书签",
    category: "playback",
    bindings: [K.mod("m")],
    when: "any",
  },
  // 视频帧导航
  {
    id: "video.frame.next",
    domain: "video",
    label: "下一帧（采样开启时按网格跳）",
    category: "playback",
    bindings: [bind("arrowright")],
    when: "any",
  },
  {
    id: "video.frame.prev",
    domain: "video",
    label: "上一帧（采样开启时按网格退）",
    category: "playback",
    bindings: [bind("arrowleft")],
    when: "any",
  },
  {
    id: "video.frame.micro.next",
    domain: "video",
    label: "源帧微调 +1",
    category: "playback",
    bindings: [K.shift("arrowright")],
    when: "sampling",
  },
  {
    id: "video.frame.micro.prev",
    domain: "video",
    label: "源帧微调 -1",
    category: "playback",
    bindings: [K.shift("arrowleft")],
    when: "sampling",
  },
  {
    id: "video.track.keyframe.next",
    domain: "video",
    label: "跳下一关键帧",
    category: "playback",
    bindings: [bind(".")],
    when: "videoTrack",
  },
  {
    id: "video.track.keyframe.prev",
    domain: "video",
    label: "跳上一关键帧",
    category: "playback",
    bindings: [bind(",")],
    when: "videoTrack",
  },
];

export const EDITABLE_COMMAND_IDS: ReadonlySet<string> = new Set(
  EDITABLE_SHORTCUT_COMMANDS.map((c) => c.id),
);

export function getEditableCommand(id: string): EditableCommandDef | undefined {
  return EDITABLE_SHORTCUT_COMMANDS.find((c) => c.id === id);
}

/** 命令的存储桶 / 作用域（注册表为准）。 */
export function commandDomain(id: string): ShortcutDomain {
  return getEditableCommand(id)?.domain ?? "common";
}

// ── Fixed 命令清单（参与冲突盘点，本增量不可编辑）──────────────────────────

export const FIXED_SHORTCUTS: FixedShortcutDef[] = [
  // 通用 / 系统级
  {
    id: "fixed.undo",
    label: "撤销",
    domain: "common",
    bindings: [K.mod("z")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.redo",
    label: "重做",
    domain: "common",
    bindings: [bind("z", "mod", "shift"), K.mod("y"), bind("y", "mod", "shift")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.selectAll",
    label: "全选当前帧人工标注",
    domain: "common",
    bindings: [K.mod("a")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.copy",
    label: "复制选中对象",
    domain: "common",
    bindings: [K.mod("c")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.paste",
    label: "粘贴",
    domain: "common",
    bindings: [K.mod("v")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.duplicate",
    label: "原地复制",
    domain: "common",
    bindings: [K.mod("d")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.fitReset",
    label: "重置缩放与平移",
    domain: "common",
    bindings: [K.mod("0")],
    when: "any",
    applies: "所有工作台；修饰键完全一致才触发",
  },
  {
    id: "fixed.cheatsheet",
    label: "打开快捷键面板",
    domain: "common",
    bindings: [bind("?"), K.shift("?")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.cancel",
    label: "取消草稿 / 选择 / 关闭弹窗",
    domain: "common",
    bindings: [bind("escape")],
    when: "any",
    applies: "所有工作台",
  },
  {
    id: "fixed.spacePan",
    label: "按住 Space 平移画布",
    domain: "common",
    bindings: [bind(" ")],
    when: "any",
    applies: "二维画布",
  },
  {
    id: "fixed.tabCycle",
    label: "同类流转",
    domain: "common",
    bindings: [bind("tab"), K.shift("tab")],
    when: "any",
    applies: "图片 / 视频",
  },
  {
    id: "fixed.backquoteStep",
    label: "跨类跳转（物理 Backquote）",
    domain: "common",
    bindings: [bind("`"), K.shift("`"), K.shift("~")],
    when: "any",
    applies: "图片 / 视频；物理键位例外，不可改绑",
  },
  {
    id: "fixed.review.approve",
    label: "审核通过",
    domain: "common",
    bindings: [bind("a"), K.shift("a")],
    when: "any",
    applies: "审核模式；A 与 Shift+A",
  },
  {
    id: "fixed.review.reject",
    label: "审核退回",
    domain: "common",
    bindings: [bind("r"), K.shift("r")],
    when: "any",
    applies: "审核模式；R 与 Shift+R",
  },
  // 图片
  {
    id: "fixed.image.delete",
    label: "删除选中对象",
    domain: "image",
    bindings: [bind("delete"), bind("backspace")],
    when: "selection",
    applies: "图片；已选中对象",
  },
  {
    id: "fixed.image.userCycle",
    label: "循环人工标注",
    domain: "image",
    bindings: [bind("j"), bind("k")],
    when: "any",
    applies: "图片",
  },
  {
    id: "fixed.image.smartNext",
    label: "智能切题",
    domain: "image",
    bindings: [bind("n"), bind("u")],
    when: "any",
    applies: "图片",
  },
  {
    id: "fixed.image.submit",
    label: "提交质检",
    domain: "image",
    bindings: [bind("e")],
    when: "any",
    applies: "图片",
  },
  {
    id: "fixed.image.nudge",
    label: "微调选中对象位置（方向键）",
    domain: "image",
    bindings: ["arrowup", "arrowdown", "arrowleft", "arrowright"].flatMap((key) => [
      bind(key),
      K.shift(key),
    ]),
    when: "selection",
    applies: "图片；已选中对象（Shift = 10px）",
  },
  {
    id: "fixed.image.changeClass",
    label: "修改选中对象的类别",
    domain: "image",
    bindings: [bind("c")],
    when: "selection",
    applies: "图片；已选中对象",
  },
  {
    id: "fixed.image.aiDecide",
    label: "采纳 / 忽略待决 AI 候选",
    domain: "image",
    bindings: [bind("a"), bind("d")],
    when: "prediction",
    applies: "图片；选中待决候选时",
  },
  {
    id: "fixed.image.zorder",
    label: "选中对象上 / 下移一层",
    domain: "image",
    bindings: [bind("["), bind("]")],
    when: "selection",
    applies: "图片；已选中对象",
  },
  {
    id: "fixed.image.threshold",
    label: "调整置信度阈值",
    domain: "image",
    bindings: [bind("["), bind("]")],
    when: "noSelection",
    applies: "图片；未选中对象时",
  },
  {
    id: "fixed.image.samPolarity",
    label: "切换智能候选正负向",
    domain: "image",
    bindings: [bind("+"), bind("="), bind("-")],
    when: "any",
    applies: "智能点 / 示例框工具激活时",
  },
  {
    id: "fixed.image.propagate",
    label: "跨帧延续选中框（Alt+方向）",
    domain: "image",
    bindings: [K.alt("arrowright"), K.alt("arrowleft")],
    when: "selection",
    applies: "图片；已选中对象",
  },
  {
    id: "fixed.image.samRefine",
    label: "精修当前 SAM 候选",
    domain: "image",
    bindings: [bind("r"), K.shift("r")],
    when: "any",
    applies: "AI 工具激活且有可接受候选时",
  },
  {
    id: "fixed.image.maskLocal",
    label: "Mask 局部键（笔刷 / 橡皮 / 主动作）",
    domain: "image",
    bindings: [...maskBindings, bind("enter")],
    when: "any",
    applies: "Mask 编辑中；capture 监听优先接管",
    defaultOverlaps: ["image.tool.box"],
  },
  {
    id: "fixed.video.maskLocal",
    label: "视频 Mask 局部键（笔刷 / 橡皮 / 主动作）",
    domain: "video",
    bindings: [...maskBindings, bind("enter")],
    when: "any",
    applies: "视频 Mask 编辑中；capture 监听优先接管",
    defaultOverlaps: ["video.tool.box", "video.tool.exemplar"],
  },
  // 视频播放 / 轨迹（J/K/L 手势、删除、历史等保持 Fixed）
  {
    id: "fixed.video.jog",
    label: "反向 / 暂停播放",
    domain: "video",
    bindings: [bind("j"), bind("k")],
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.jogForward",
    label: "正向播放",
    domain: "video",
    bindings: [bind("l"), K.shift("l")],
    when: "videoNoTrack",
    applies: "视频；未选中轨迹时正向播放",
  },
  {
    id: "fixed.video.playToggle",
    label: "播放 / 暂停",
    domain: "video",
    bindings: [bind(" ")],
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.fit",
    label: "适应视口（Shift+F）",
    domain: "video",
    bindings: [K.shift("f")],
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.actualSize",
    label: "按实际尺寸显示（Shift+0）",
    domain: "video",
    bindings: [K.shift("0"), K.shift(")")],
    when: "any",
    applies: "视频；物理 Digit0 + Shift",
  },
  {
    id: "fixed.video.deleteKeyframe",
    label: "删除当前关键帧",
    domain: "video",
    bindings: [bind("delete"), bind("backspace")],
    when: "selection",
    applies: "视频；已选中对象",
  },
  {
    id: "fixed.video.deleteTrack",
    label: "删除整条选中轨迹",
    domain: "video",
    bindings: [K.mod("delete"), K.mod("backspace")],
    when: "videoTrack",
    applies: "视频；已选中轨迹",
  },
  {
    id: "fixed.video.propagateTrack",
    label: "打开 AI 追踪",
    domain: "video",
    bindings: [K.mod("b"), K.shift("t")],
    when: "videoTrack",
    applies: "视频；已选中轨迹；Mod+B / Shift+T",
  },
  {
    id: "fixed.video.jumpHistory",
    label: "跳转历史后退 / 前进",
    domain: "video",
    bindings: [K.mod("["), K.mod("]")],
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.clearLoop",
    label: "清除播放范围",
    domain: "video",
    bindings: [K.alt("l")],
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.chapter",
    label: "跳转章节",
    domain: "video",
    bindings: withAllModifiers("pageup", "pagedown"),
    when: "any",
    applies: "视频",
  },
  {
    id: "fixed.video.keyframeEdges",
    label: "跳首 / 末关键帧",
    domain: "video",
    bindings: [bind("home"), bind("end"), K.shift("home"), K.shift("end")],
    when: "videoTrack",
    applies: "视频；已选中轨迹，Home / End 及 Shift 变体",
  },
  {
    id: "fixed.video.aiDecide",
    label: "采纳 / 忽略当前帧待决候选",
    domain: "video",
    bindings: [bind("a"), bind("d"), K.shift("a"), K.shift("d")],
    when: "prediction",
    applies: "视频；选中普通待决候选时",
  },
  // 项目类别数字键（显式的项目键策略；1-9 + 0 共十个直选槽）
  {
    id: "fixed.category.digits",
    label: "项目类别直选",
    domain: "common",
    bindings: [
      bind("1"),
      bind("2"),
      bind("3"),
      bind("4"),
      bind("5"),
      bind("6"),
      bind("7"),
      bind("8"),
      bind("9"),
      bind("0"),
    ],
    when: "any",
    applies: "前十个类别（按工具绑定单元配置顺序）；类别弹层打开时归弹层消费",
  },
  // 3D / 点云工作台本地键（common 域命令改绑时必须避开）
  {
    id: "fixed.threed.gizmo",
    label: "切换 gizmo / 放置 / 测量模式",
    domain: "threed",
    bindings: withAllModifiers("w", "e", "r", "b", "p", "m", "v"),
    when: "any",
    applies: "3D / 点云工作台本地监听",
  },
  {
    id: "fixed.threed.fit",
    label: "选中框自动拟合",
    domain: "threed",
    bindings: [bind("q"), K.shift("q"), K.alt("q"), bind("q", "alt", "shift")],
    when: "any",
    applies: "3D；已选中框",
  },
  {
    id: "fixed.threed.propagate",
    label: "跨帧延续选中框",
    domain: "threed",
    bindings: [
      K.shift("arrowright"),
      K.shift("arrowleft"),
      bind("arrowright", "alt", "shift"),
      bind("arrowleft", "alt", "shift"),
    ],
    when: "any",
    applies: "3D",
  },
  {
    id: "fixed.threed.propagateBatch",
    label: "批量延续全部 3D 框",
    domain: "threed",
    bindings: [
      bind("arrowright", "mod", "shift"),
      bind("arrowleft", "mod", "shift"),
      bind("arrowright", "mod", "alt", "shift"),
      bind("arrowleft", "mod", "alt", "shift"),
    ],
    when: "any",
    applies: "3D",
  },
  {
    id: "fixed.threed.delete",
    label: "删除选中 3D 框",
    domain: "threed",
    bindings: withAllModifiers("delete", "backspace"),
    when: "any",
    applies: "3D；已选中对象",
  },
  {
    id: "fixed.threed.clipboardHistory",
    label: "3D 剪贴板与历史",
    domain: "threed",
    bindings: withAllModifiers("z", "y", "c", "v", "d").filter((binding) =>
      binding.modifiers.includes("mod"),
    ),
    when: "any",
    applies: "3D 本地剪贴板 / 历史监听",
  },
  {
    id: "fixed.threed.cameraOverlay",
    label: "相机放大浮层内切换相机",
    domain: "threed",
    bindings: [bind("arrowright"), bind("arrowleft")],
    when: "any",
    applies: "3D；相机放大浮层打开时",
  },
  {
    id: "fixed.threed.triZoom",
    label: "三视图缩放（+ / - / Shift+0）",
    domain: "threed",
    bindings: [bind("+"), bind("="), bind("-"), K.shift("0")],
    when: "any",
    applies: "3D；三视图聚焦时",
  },
  {
    id: "fixed.threed.draft",
    label: "封闭草稿 / 完成测量",
    domain: "threed",
    bindings: [bind("enter")],
    when: "any",
    applies: "3D；point-mask / 测量草稿中",
  },
];

// ── 规范化 ───────────────────────────────────────────────────────────────────

const MODIFIER_ONLY_KEYS = new Set([
  "shift",
  "control",
  "alt",
  "meta",
  "capslock",
  "numlock",
  "scrolllock",
  "fn",
  "fnlock",
  "hyper",
  "super",
  "symbol",
  "symbollock",
  "contextmenu",
]);

/** 浏览器 / 操作系统保留组合：按键事件可能不会送达或语义不可抢占，禁止录制。 */
const BROWSER_RESERVED: ReadonlySet<string> = new Set([
  "mod+t",
  "mod+shift+t",
  "mod+n",
  "mod+shift+n",
  "mod+w",
  "mod+shift+w",
  "mod+q",
  "mod+shift+q",
  "mod+r",
  "mod+shift+r",
  "mod+l",
  "mod+shift+p",
  "mod+shift+i",
  "mod+shift+j",
  "mod+shift+c",
  "+f12",
]);

/** 物理键位例外：产出字符随布局漂移，保持 Fixed，不开放改绑。 */
const PHYSICAL_EXCEPTION_KEYS = new Set(["`", "~", "tab", "enter", "escape"]);

/**
 * 可持久化的多字符命名键。必须与存量解析 (isValidBindingValue) 和后端
 * ShortcutBinding 契约一致：录制只接受单字符或这里的命名键，否则能确认保存
 * 却在重新加载时被判非法（如 F13 / Dead）。
 */
const NAMED_KEYS = new Set([
  "arrowup",
  "arrowdown",
  "arrowleft",
  "arrowright",
  "home",
  "end",
  "pageup",
  "pagedown",
  "delete",
  "backspace",
  "insert",
  "f1",
  "f2",
  "f3",
  "f4",
  "f5",
  "f6",
  "f7",
  "f8",
  "f9",
  "f10",
  "f11",
]);

export interface BindingToken {
  key: string;
  modifiers: ShortcutModifier[];
}

/** 绑定的规范 token（排序修饰键），用于匹配与冲突比对。 */
export function bindingToken(b: BindingToken): string {
  return `${[...b.modifiers].sort().join("+")}+${b.key}`;
}

interface NormalizedEvent {
  binding: ShortcutBinding | null;
  reason?: "ime" | "modifier-only" | "browser" | "physical" | "altgr" | "unsupported";
}

/** 把 keydown 事件规范化为候选绑定；不满足契约时给出原因。 */
export function normalizeRecordedEvent(e: KeyboardEvent): NormalizedEvent {
  if (e.isComposing || e.keyCode === 229) return { binding: null, reason: "ime" };
  const modifiers: ShortcutModifier[] = [];
  if (e.ctrlKey || e.metaKey) modifiers.push("mod");
  if (e.altKey) modifiers.push("alt");
  if (e.shiftKey) modifiers.push("shift");
  const rawKey = e.key;
  const key = rawKey.toLowerCase();
  if (MODIFIER_ONLY_KEYS.has(key)) return { binding: null, reason: "modifier-only" };
  if (key === "altgraph") return { binding: null, reason: "altgr" };
  const binding: ShortcutBinding = { key, modifiers: modifiers.sort() };
  // 保留 / 物理例外的判定优先，再拒绝无法持久化的命名键（多字符键必须可解析，
  // 否则录制与存量解析会给出不同结论，如 F13 / Dead）。
  if (BROWSER_RESERVED.has(bindingToken(binding))) return { binding, reason: "browser" };
  if (PHYSICAL_EXCEPTION_KEYS.has(key)) return { binding, reason: "physical" };
  if (key.length > 1 && !NAMED_KEYS.has(key)) return { binding: null, reason: "unsupported" };
  return { binding };
}

/** dispatchKey 运行期用：事件与绑定要求完全一致（key + 修饰键集合）。 */
export function eventMatchesBinding(e: KeyboardEvent, b: ShortcutBinding): boolean {
  if (typeof e.key !== "string") return false;
  if (Boolean(e.ctrlKey || e.metaKey) !== b.modifiers.includes("mod")) return false;
  if (Boolean(e.altKey) !== b.modifiers.includes("alt")) return false;
  if (Boolean(e.shiftKey) !== b.modifiers.includes("shift")) return false;
  return e.key.toLowerCase() === b.key;
}

// ── 谓词重叠 ─────────────────────────────────────────────────────────────────

const DISJOINT_WHEN: ReadonlyArray<readonly [ShortcutWhen, ShortcutWhen]> = [
  ["selection", "noSelection"],
  ["prediction", "noPrediction"],
  ["videoTrack", "videoNoTrack"],
];

export function whenOverlaps(a: ShortcutWhen, b: ShortcutWhen): boolean {
  return !DISJOINT_WHEN.some(([x, y]) => (a === x && b === y) || (a === y && b === x));
}

export function domainOverlaps(
  a: ShortcutDomain | "threed",
  b: ShortcutDomain | "threed",
): boolean {
  if (a === b) return true;
  return a === "common" || b === "common";
}

// ── 冲突检查 ─────────────────────────────────────────────────────────────────

export interface EffectiveCommandState {
  id: string;
  /** 生效组合（默认或覆盖后）；空数组 = 停用。 */
  bindings: ShortcutBinding[];
  disabled: boolean;
  customized: boolean;
  /** 存量冲突：该命令与哪些命令争用同一按键（执行时全部停用）。 */
  conflictsWith: string[];
}

export interface StoredShortcutIssue {
  domain: ShortcutDomain;
  commandId: string;
  reason: "unknown-command" | "invalid-binding" | "invalid-envelope";
}

/** 每域覆盖：commandId → 组合列表 | null(恢复默认)。undefined 视为缺失。 */
export type ShortcutOverrideMap = Partial<Record<string, ShortcutBinding[] | null>>;
export type ShortcutOverridesByDomain = Record<ShortcutDomain, ShortcutOverrideMap>;

export const EMPTY_OVERRIDES: ShortcutOverridesByDomain = { common: {}, image: {}, video: {} };

/** 命令覆盖只存自己的域桶：common 命令在 common 桶，图片 / 视频命令各归各桶。 */
function overrideBucket(
  domain: ShortcutDomain,
  overrides: ShortcutOverridesByDomain,
): ShortcutOverrideMap {
  return overrides[domain];
}

/**
 * 应用覆盖并做全量冲突复查。互相争用的命令整对停用（保留存量值供修正），
 * 默认组合之间不允许存在冲突（注册表不变式，测试覆盖）。
 */
export function resolveEffectiveCommands(
  overrides: ShortcutOverridesByDomain,
): Map<string, EffectiveCommandState> {
  const states = new Map<string, EffectiveCommandState>();
  for (const def of EDITABLE_SHORTCUT_COMMANDS) {
    const bucket = overrideBucket(def.domain, overrides);
    const override = bucket[def.id];
    const customized = override !== undefined && override !== null;
    states.set(def.id, {
      id: def.id,
      bindings: override ?? def.bindings,
      disabled: Array.isArray(override) && override.length === 0,
      customized,
      conflictsWith: [],
    });
  }
  // 全对冲突复查（含默认 vs 默认；注册表不变式要求这里为空）。
  const list = [...states.values()].filter((s) => !s.disabled);
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const a = states.get(list[i].id)!;
      const b = states.get(list[j].id)!;
      const da = commandDomain(a.id);
      const db = commandDomain(b.id);
      if (!domainOverlaps(da, db)) continue;
      const wa = getEditableCommand(a.id)!.when;
      const wb = getEditableCommand(b.id)!.when;
      if (!whenOverlaps(wa, wb)) continue;
      const aTokens = new Set(a.bindings.map(bindingToken));
      if (b.bindings.some((bind2) => aTokens.has(bindingToken(bind2)))) {
        a.conflictsWith.push(b.id);
        b.conflictsWith.push(a.id);
      }
    }
  }
  for (const state of list) {
    const def = getEditableCommand(state.id)!;
    for (const fixed of FIXED_SHORTCUTS) {
      if (state.bindings.some((binding) => conflictsWithFixed(def, binding, fixed))) {
        state.conflictsWith.push(fixed.id);
      }
    }
  }
  return states;
}

function conflictsWithFixed(
  command: EditableCommandDef,
  binding: ShortcutBinding,
  fixed: FixedShortcutDef,
): boolean {
  if (!domainOverlaps(command.domain, fixed.domain) || !whenOverlaps(command.when, fixed.when)) {
    return false;
  }
  const token = bindingToken(binding);
  // Preserve only the established default capture hand-off (e.g. B opens Box,
  // but remains Brush while Mask owns the event); new overlaps remain conflicts.
  if (
    fixed.defaultOverlaps?.includes(command.id) &&
    command.bindings.some((defaultBinding) => bindingToken(defaultBinding) === token)
  )
    return false;
  return fixed.bindings.some((fixedBinding) => bindingToken(fixedBinding) === token);
}

export interface BindingConflict {
  /** 争用对象（可编辑命令 ID 或 Fixed 清单 ID）。 */
  targetId: string;
  targetLabel: string;
  binding: ShortcutBinding;
  fixed: boolean;
  applies: string;
}

/**
 * 保守检查：候选编辑是否与任何可生效命令 / Fixed 清单争用同一按键。
 * 谓词可证明互斥（如「已选中」vs「未选中」）时允许共用。
 */
export function findBindingConflicts(input: {
  commandId: string;
  bindings: ShortcutBinding[];
  effective: Map<string, EffectiveCommandState>;
}): BindingConflict[] {
  const self = getEditableCommand(input.commandId);
  if (!self) return [];
  const tokens = new Set(input.bindings.map(bindingToken));
  const conflicts: BindingConflict[] = [];
  for (const state of input.effective.values()) {
    if (state.id === input.commandId) continue;
    if (state.disabled) continue;
    const other = getEditableCommand(state.id);
    if (!other) continue;
    if (!domainOverlaps(commandDomain(self.id), commandDomain(other.id))) continue;
    if (!whenOverlaps(self.when, other.when)) continue;
    for (const binding of state.bindings) {
      if (tokens.has(bindingToken(binding))) {
        conflicts.push({
          targetId: state.id,
          targetLabel: other.label,
          binding,
          fixed: false,
          applies:
            other.domain === "video"
              ? "视频工作台"
              : other.domain === "image"
                ? "图片工作台"
                : "所有工作台",
        });
      }
    }
  }
  for (const fixed of FIXED_SHORTCUTS) {
    if (!domainOverlaps(self.domain, fixed.domain)) continue;
    for (const binding of fixed.bindings) {
      if (tokens.has(bindingToken(binding)) && conflictsWithFixed(self, binding, fixed)) {
        conflicts.push({
          targetId: fixed.id,
          targetLabel: fixed.label,
          binding,
          fixed: true,
          applies: fixed.applies,
        });
      }
    }
  }
  return conflicts;
}

// ── 运行期匹配（dispatchKey 消费）────────────────────────────────────────────

export interface CommandEventMatcher {
  match: (commandId: string) => boolean;
  /** 同键意外命中多条互相争用的命令：执行方应吞掉事件并上报一次。 */
  conflict: boolean;
}

/** 运行期匹配器：把事件归一到绑定后查生效表；每次 keydown 构造一个实例。 */
export function createCommandEventMatcher(
  e: KeyboardEvent,
  effective: Map<string, EffectiveCommandState>,
  stage: HotkeyStage,
): CommandEventMatcher {
  const claimed = new Set<string>();
  // 存量冲突命令仍计入 claimed 以便识别重叠并提示，但绝不进入 executable。
  const executable = new Set<string>();
  let conflict = false;
  for (const state of effective.values()) {
    if (state.disabled) continue;
    const domain = commandDomain(state.id);
    if (domain !== "common" && domain !== stage) continue;
    if (!state.bindings.some((b) => eventMatchesBinding(e, b))) continue;
    claimed.add(state.id);
    if (state.conflictsWith.length === 0) executable.add(state.id);
  }
  if (claimed.size > 1) {
    const ids = [...claimed];
    for (let i = 0; i < ids.length && !conflict; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = getEditableCommand(ids[i])!;
        const b = getEditableCommand(ids[j])!;
        if (
          domainOverlaps(commandDomain(a.id), commandDomain(b.id)) &&
          whenOverlaps(a.when, b.when)
        ) {
          conflict = true;
          break;
        }
      }
    }
  }
  return {
    match: (id) => !conflict && executable.has(id),
    conflict,
  };
}

// ── 存量偏好解析（严格写 + 宽容读）──────────────────────────────────────────

function isValidBindingValue(value: unknown): value is ShortcutBinding[] {
  if (!Array.isArray(value) || value.length > 2) return false;
  return value.every((entry) => {
    if (!entry || typeof entry !== "object") return false;
    const { key, modifiers } = entry as Partial<ShortcutBinding>;
    if (typeof key !== "string" || key.length === 0 || key.length > 32) return false;
    if (key !== key.toLowerCase()) return false;
    if (MODIFIER_ONLY_KEYS.has(key)) return false;
    if (key.length > 1 && !NAMED_KEYS.has(key)) return false;
    if (modifiers === undefined) return true;
    if (!Array.isArray(modifiers)) return false;
    return modifiers.every((m) => m === "mod" || m === "alt" || m === "shift");
  });
}

/**
 * 宽容读：仅接受 schemaVersion 1 且结构完整的覆盖；损坏 / 更新版本的条目
 * 排除出执行并记入 issues（保留原始值，由界面标示待修正）。
 */
export function parseStoredShortcutOverrides(raw: unknown): {
  overrides: ShortcutOverridesByDomain;
  issues: StoredShortcutIssue[];
  /** true = 整棵子树无法识别（损坏或更新版本），界面应整体标示。 */
  opaque: boolean;
} {
  const result: ShortcutOverridesByDomain = { common: {}, image: {}, video: {} };
  const issues: StoredShortcutIssue[] = [];
  if (raw === undefined || raw === null) return { overrides: result, issues, opaque: false };
  if (typeof raw !== "object" || Array.isArray(raw)) {
    return { overrides: result, issues, opaque: true };
  }
  const record = raw as Record<string, unknown>;
  if (record.schemaVersion !== 1) return { overrides: result, issues, opaque: true };
  let opaque = false;
  for (const domain of ["common", "image", "video"] as const) {
    const bucket = record[domain];
    if (bucket === undefined) continue;
    if (bucket === null || typeof bucket !== "object" || Array.isArray(bucket)) {
      opaque = true;
      continue;
    }
    for (const [commandId, value] of Object.entries(bucket as Record<string, unknown>)) {
      if (value === null) {
        // 显式重置：已知命令恢复默认；未知 / 历史条目视为已清除，不再报
        // unknown-command（服务端合并后也会移除这类键）。
        if (EDITABLE_COMMAND_IDS.has(commandId)) result[domain][commandId] = null;
        continue;
      }
      if (!EDITABLE_COMMAND_IDS.has(commandId)) {
        issues.push({ domain, commandId, reason: "unknown-command" });
        continue;
      }
      if (!isValidBindingValue(value)) {
        issues.push({ domain, commandId, reason: "invalid-binding" });
        continue;
      }
      result[domain][commandId] = value.map((entry) => {
        const { key, modifiers } = entry as ShortcutBinding;
        return { key, modifiers: [...(modifiers ?? [])].sort() };
      });
    }
  }
  return { overrides: result, issues, opaque };
}

// ── 展示格式化 ───────────────────────────────────────────────────────────────

export function isMacPlatform(): boolean {
  return (
    typeof navigator !== "undefined" &&
    /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)
  );
}

export function modLabel(): string {
  return isMacPlatform() ? "Cmd" : "Ctrl";
}

const KEY_LABELS: Record<string, string> = {
  " ": "Space",
  arrowup: "↑",
  arrowdown: "↓",
  arrowleft: "←",
  arrowright: "→",
  escape: "Esc",
  pageup: "PageUp",
  pagedown: "PageDown",
  backspace: "Backspace",
  delete: "Delete",
  insert: "Insert",
  home: "Home",
  end: "End",
};

/** 绑定的展示键帽序列（修饰键在前，Mod 按平台展开）。 */
export function bindingKeyLabels(b: ShortcutBinding): string[] {
  const labels: string[] = [];
  if (b.modifiers.includes("mod")) labels.push(modLabel());
  if (b.modifiers.includes("alt")) labels.push("Alt");
  if (b.modifiers.includes("shift")) labels.push("Shift");
  const key = KEY_LABELS[b.key] ?? (b.key.length === 1 ? b.key.toUpperCase() : b.key);
  labels.push(key);
  return labels;
}
