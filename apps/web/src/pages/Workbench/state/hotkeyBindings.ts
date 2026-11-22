import type { HotkeyCategory, HotkeyStage } from "./hotkeys";

export type ShortcutDomain = "common" | "image" | "video";
export type ShortcutModifier = "mod" | "alt" | "shift";

export interface ShortcutBinding {
  key: string;
  modifiers: ShortcutModifier[];
}

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
  domain: ShortcutDomain;
  label: string;
  category: HotkeyCategory;
  bindings: ShortcutBinding[];
  when: ShortcutWhen;
}

export interface FixedShortcutDef {
  id: string;
  label: string;
  domain: ShortcutDomain | "threed";
  bindings: ShortcutBinding[];
  when: ShortcutWhen;
  applies: string;
  defaultOverlaps?: readonly string[];
}

const bind = (key: string, ...modifiers: ShortcutModifier[]): ShortcutBinding => ({
  key,
  modifiers: [...modifiers].sort(),
});

const K = {
  mod: (key: string) => bind(key, "mod"),
  alt: (key: string) => bind(key, "alt"),
  shift: (key: string) => bind(key, "shift"),
};

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

export const EDITABLE_SHORTCUT_COMMANDS: EditableCommandDef[] = [
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

export function commandDomain(id: string): ShortcutDomain {
  return getEditableCommand(id)?.domain ?? "common";
}

export const FIXED_SHORTCUTS: FixedShortcutDef[] = [
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

const PHYSICAL_EXCEPTION_KEYS = new Set(["`", "~", "tab", "enter", "escape"]);

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

export function bindingToken(b: BindingToken): string {
  return `${[...b.modifiers].sort().join("+")}+${b.key}`;
}

interface NormalizedEvent {
  binding: ShortcutBinding | null;
  reason?: "ime" | "modifier-only" | "browser" | "physical" | "altgr" | "unsupported";
}

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
  if (BROWSER_RESERVED.has(bindingToken(binding))) return { binding, reason: "browser" };
  if (PHYSICAL_EXCEPTION_KEYS.has(key)) return { binding, reason: "physical" };
  if (key.length > 1 && !NAMED_KEYS.has(key)) return { binding: null, reason: "unsupported" };
  return { binding };
}

export function eventMatchesBinding(e: KeyboardEvent, b: ShortcutBinding): boolean {
  if (typeof e.key !== "string") return false;
  if (Boolean(e.ctrlKey || e.metaKey) !== b.modifiers.includes("mod")) return false;
  if (Boolean(e.altKey) !== b.modifiers.includes("alt")) return false;
  if (Boolean(e.shiftKey) !== b.modifiers.includes("shift")) return false;
  return e.key.toLowerCase() === b.key;
}

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

export interface EffectiveCommandState {
  id: string;
  bindings: ShortcutBinding[];
  disabled: boolean;
  customized: boolean;
  conflictsWith: string[];
}

export interface StoredShortcutIssue {
  domain: ShortcutDomain;
  commandId: string;
  reason: "unknown-command" | "invalid-binding" | "invalid-envelope";
}

export type ShortcutOverrideMap = Partial<Record<string, ShortcutBinding[] | null>>;
export type ShortcutOverridesByDomain = Record<ShortcutDomain, ShortcutOverrideMap>;

export const EMPTY_OVERRIDES: ShortcutOverridesByDomain = { common: {}, image: {}, video: {} };

function overrideBucket(
  domain: ShortcutDomain,
  overrides: ShortcutOverridesByDomain,
): ShortcutOverrideMap {
  return overrides[domain];
}

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
  if (
    fixed.defaultOverlaps?.includes(command.id) &&
    command.bindings.some((defaultBinding) => bindingToken(defaultBinding) === token)
  )
    return false;
  return fixed.bindings.some((fixedBinding) => bindingToken(fixedBinding) === token);
}

export interface BindingConflict {
  targetId: string;
  targetLabel: string;
  binding: ShortcutBinding;
  fixed: boolean;
  applies: string;
}

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

export interface CommandEventMatcher {
  match: (commandId: string) => boolean;
  conflict: boolean;
}

export function createCommandEventMatcher(
  e: KeyboardEvent,
  effective: Map<string, EffectiveCommandState>,
  stage: HotkeyStage,
): CommandEventMatcher {
  const claimed = new Set<string>();
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

export function parseStoredShortcutOverrides(raw: unknown): {
  overrides: ShortcutOverridesByDomain;
  issues: StoredShortcutIssue[];
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

export function bindingKeyLabels(b: ShortcutBinding): string[] {
  const labels: string[] = [];
  if (b.modifiers.includes("mod")) labels.push(modLabel());
  if (b.modifiers.includes("alt")) labels.push("Alt");
  if (b.modifiers.includes("shift")) labels.push("Shift");
  const key = KEY_LABELS[b.key] ?? (b.key.length === 1 ? b.key.toUpperCase() : b.key);
  labels.push(key);
  return labels;
}
