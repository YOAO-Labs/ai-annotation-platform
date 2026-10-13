// Single source of truth for Workbench shortcuts.
// useEffect 注册和 HotkeyCheatSheet 都从这里读，避免漂移。
//
// v0.24 快捷键面板重构：
//  - HOTKEYS 是带稳定 `id` 的参考目录（对话框 / 提示 / 文档生成共用）；
//  - 可编辑命令的默认组合与覆盖解析在 `state/hotkeyBindings.ts`，dispatchKey 通过
//    `matchCommand` 按生效绑定匹配，硬编码按键分支只保留 Fixed 命令；
//  - 项目类别直选为数字 1-9 + 0（按工具绑定单元的配置顺序取前十个），字母不再选类别。

import { isWorkbenchInteractionBlocked } from "./workbenchInteractionGuards";
import {
  createCommandEventMatcher,
  EMPTY_OVERRIDES,
  resolveEffectiveCommands,
} from "./hotkeyBindings";

/** 无 overrides 时的默认生效表（模块常量；dispatchKey 直接调用 / 测试用）。 */
const DEFAULT_EFFECTIVE = resolveEffectiveCommands(EMPTY_OVERRIDES);

/** 面板左侧的用途分组（信息架构，与工作台类型正交）。 */
export type HotkeyCategory =
  | "common"
  | "draw"
  | "selection"
  | "canvas"
  | "ai"
  | "playback"
  | "task"
  | "mouse";

/** 工作台类型；条目省略 `stages` 表示全部类型可用。 */
export type HotkeyStage = "image" | "video" | "threed";

export interface HotkeyDef {
  /** 稳定命令身份：对话框、提示与绑定解析共用；不要复用 / 重命名。 */
  id: string;
  keys: string[]; // display labels e.g. ["Ctrl", "Z"]
  desc: string;
  category: HotkeyCategory;
  /** 生效的工作台类型；省略 = 全部类型。 */
  stages?: HotkeyStage[];
  /** 实际生效条件（产品语言，如「视频；已选中轨迹」）。 */
  applies?: string;
  /** 可选的补充说明（第二行帮助文本）。 */
  note?: string;
  /** 收录进「常用」精选视图。 */
  common?: boolean;
  /** keys 的最后一个元素是候选项列表（「或」连接）而非组合步骤。 */
  keysAlt?: boolean;
  /** 聚合展示标签（如数字段「1 — 9、0」），替代逐键渲染。 */
  keysLabel?: string;
  /** 与 dispatch 出的 action.type 关联，供测试 / 文档生成核对分支。 */
  actionType?: string;
  /** Context and target for shortcuts whose shared keys have different routes. */
  context?: "image" | "video" | "image-prediction" | "video-prediction" | "video-no-prediction";
  targetTool?: Extract<HotkeyAction, { type: "setTool" | "setVideoTool" }>["tool"];
}

export function hotkeyIgnoreToken(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey">,
): string | null {
  return event.ctrlKey || event.metaKey ? `Mod+${event.key.toLowerCase()}` : null;
}

export function isMaskContextHotkey(event: KeyboardEvent): boolean {
  const key = event.key.toLowerCase();
  return (
    ["enter", "escape", "b", "e"].includes(key) ||
    ((event.ctrlKey || event.metaKey) && (key === "z" || key === "y"))
  );
}

const blockedMaskEvents = new WeakSet<KeyboardEvent>();
const maskControlSelector =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="combobox"], [role="listbox"], [role="menu"], [role^="menuitem"], [role="dialog"], [role="alertdialog"], [role="tab"], [data-workbench-layout-control], [data-scene-timeline]';
const maskPopupSelector =
  '[role="menu"], [role="dialog"], [role="alertdialog"], [role="listbox"], dialog[open]';

/** Image and video Mask listeners yield without swallowing a control's own key event. */
export function isMaskHotkeyBlocked(event: KeyboardEvent): boolean {
  if (blockedMaskEvents.has(event)) return true;
  const blocked =
    event.defaultPrevented ||
    event.isComposing ||
    event.keyCode === 229 ||
    event.repeat ||
    isWorkbenchInteractionBlocked(event) ||
    event
      .composedPath()
      .some(
        (target) =>
          target instanceof Element &&
          (target.matches(maskControlSelector) ||
            (target instanceof HTMLElement && target.isContentEditable) ||
            (event.key === "Enter" && target.matches('button, [role="button"], a[href], summary'))),
      ) ||
    (typeof document !== "undefined" &&
      Array.from(document.querySelectorAll(maskPopupSelector)).some((popup) => {
        if (popup.closest('[hidden], [aria-hidden="true"], [data-state="closed"]')) return false;
        for (let element: Element | null = popup; element; element = element.parentElement) {
          const style = getComputedStyle(element);
          if (style.display === "none" || style.visibility === "hidden") return false;
        }
        return true;
      }));
  // A popup may remove itself between capture and the later background listener.
  if (blocked) blockedMaskEvents.add(event);
  return blocked;
}

/** Candidate shortcuts yield all navigation keys to native and toolbar controls. */
export function isSamCandidateHotkeyBlocked(event: KeyboardEvent): boolean {
  return (
    isMaskHotkeyBlocked(event) ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    event
      .composedPath()
      .some(
        (target) =>
          target instanceof Element &&
          target.matches('button, [role="button"], a[href], summary, [data-workbench-ai-toolbar]'),
      )
  );
}

export const HOTKEYS: HotkeyDef[] = [
  // ── 绘制与工具 ────────────────────────────────────────────────────────────
  {
    id: "image.tool.select",
    keys: ["V"],
    desc: "选择工具",
    category: "draw",
    stages: ["image"],
    note: "点选 / 移动已有标注与预标注；Esc 可随时回到它",
    common: true,
    actionType: "setTool",
  },
  {
    id: "image.tool.box",
    keys: ["B"],
    desc: "矩形框工具",
    category: "draw",
    stages: ["image"],
    common: true,
    actionType: "setTool",
  },
  {
    id: "image.tool.rotatedBox",
    keys: ["W"],
    desc: "旋转框 (OBB) 工具",
    category: "draw",
    stages: ["image"],
    note: "拖框后用顶部手柄旋转",
    actionType: "setTool",
  },
  {
    id: "image.tool.polygon",
    keys: ["P"],
    desc: "多边形工具",
    category: "draw",
    stages: ["image"],
    actionType: "setTool",
  },
  {
    id: "image.tool.polyline",
    keys: ["L"],
    desc: "折线工具（开放、不闭合）",
    category: "draw",
    stages: ["image"],
    applies: "未选中对象时",
    actionType: "setTool",
  },
  {
    id: "image.tool.keypoint",
    keys: ["F"],
    desc: "关键点工具",
    category: "draw",
    stages: ["image"],
    note: "按骨骼模板依次落点；Alt = 遮挡，右键 = 跳过",
    actionType: "setTool",
  },
  {
    id: "image.tool.mask",
    keys: ["M"],
    desc: "Mask 笔刷工具",
    category: "draw",
    stages: ["image"],
    common: true,
    actionType: "setTool",
  },
  {
    id: "image.tool.box.alt",
    keys: ["Alt", "1"],
    desc: "图片矩形框工具（备用）",
    category: "draw",
    stages: ["image"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setTool",
    context: "image",
    targetTool: "box",
  },
  {
    id: "image.tool.polygon.alt",
    keys: ["Alt", "2"],
    desc: "图片多边形工具（备用）",
    category: "draw",
    stages: ["image"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setTool",
    context: "image",
    targetTool: "polygon",
  },
  {
    id: "image.tool.select.alt",
    keys: ["Alt", "4"],
    desc: "图片选择工具（备用）",
    category: "draw",
    stages: ["image"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setTool",
    context: "image",
    targetTool: "select",
  },
  {
    id: "image.polygon.close",
    keys: ["Enter"],
    desc: "闭合多边形",
    category: "draw",
    stages: ["image"],
    applies: "多边形草稿 ≥3 顶点时",
    note: "折线草稿 ≥2 顶点即可落线",
  },
  {
    id: "image.polygon.backspace",
    keys: ["Backspace"],
    desc: "撤销多边形上一点",
    category: "draw",
    stages: ["image"],
    applies: "多边形 / 折线草稿中",
    note: "无草稿时 Backspace 删除选中对象",
  },
  {
    id: "video.tool.select",
    keys: ["V"],
    desc: "视频选择工具",
    category: "draw",
    stages: ["video"],
    common: true,
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.box",
    keys: ["B"],
    desc: "视频矩形框工具",
    category: "draw",
    stages: ["video"],
    common: true,
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.rotatedBox",
    keys: ["W"],
    desc: "视频旋转框工具",
    category: "draw",
    stages: ["video"],
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.keypoint",
    keys: ["F"],
    desc: "视频关键点工具",
    category: "draw",
    stages: ["video"],
    note: "Alt = 遮挡，右键 = 跳过",
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.track",
    keys: ["T"],
    desc: "视频轨迹工具",
    category: "draw",
    stages: ["video"],
    common: true,
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.mask",
    keys: ["M"],
    desc: "视频单帧 Mask 工具",
    category: "draw",
    stages: ["video"],
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.polygon",
    keys: ["P"],
    desc: "视频多边形工具",
    category: "draw",
    stages: ["video"],
    note: "点击落点画当前帧多边形",
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.box.alt",
    keys: ["Alt", "1"],
    desc: "视频矩形框工具（备用）",
    category: "draw",
    stages: ["video"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setVideoTool",
    context: "video",
    targetTool: "box",
  },
  {
    id: "video.tool.track.alt",
    keys: ["Alt", "2"],
    desc: "视频轨迹工具（备用）",
    category: "draw",
    stages: ["video"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setVideoTool",
    context: "video",
    targetTool: "track",
  },
  {
    id: "video.tool.select.alt",
    keys: ["Alt", "3"],
    desc: "视频选择工具（备用）",
    category: "draw",
    stages: ["video"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setVideoTool",
    context: "video",
    targetTool: "select",
  },
  {
    id: "threed.tool.translateGizmo",
    keys: ["W"],
    desc: "3D：选中框时切平移 gizmo",
    category: "draw",
    stages: ["threed"],
    applies: "已选中框",
  },
  {
    id: "threed.tool.rotateGizmo",
    keys: ["E"],
    desc: "3D：选中框时切旋转 gizmo",
    category: "draw",
    stages: ["threed"],
    applies: "已选中框",
  },
  {
    id: "threed.tool.scaleGizmo",
    keys: ["R"],
    desc: "3D：选中框时切缩放 gizmo",
    category: "draw",
    stages: ["threed"],
    applies: "已选中框",
  },
  {
    id: "threed.tool.box",
    keys: ["B"],
    desc: "3D：进 3D 框放置模式",
    category: "draw",
    stages: ["threed"],
  },
  {
    id: "threed.tool.pointMask",
    keys: ["P"],
    desc: "3D：进 point-mask 放置模式",
    category: "draw",
    stages: ["threed"],
  },
  {
    id: "threed.tool.measure",
    keys: ["M"],
    desc: "3D：进点云测量模式",
    category: "draw",
    stages: ["threed"],
  },
  {
    id: "threed.tool.select",
    keys: ["V", "Esc"],
    desc: "3D：回到选择工具",
    category: "draw",
    stages: ["threed"],
    keysAlt: true,
    note: "测量有草稿时 Esc 先取消草稿",
  },
  {
    id: "threed.draft.complete",
    keys: ["Enter"],
    desc: "3D：封闭 point-mask 多边形或完成当前测量",
    category: "draw",
    stages: ["threed"],
  },

  // ── 选择与编辑 ────────────────────────────────────────────────────────────
  {
    id: "image.tab.cycle",
    keys: ["Tab"],
    desc: "同类流转：下一个",
    category: "selection",
    stages: ["image"],
    note: "在 AI 待审与人工标注间按当前类别循环",
    common: true,
    actionType: "imageCycleInCategory",
  },
  {
    id: "image.tab.cyclePrev",
    keys: ["Shift", "Tab"],
    desc: "同类流转：上一个",
    category: "selection",
    stages: ["image"],
    actionType: "imageCycleInCategory",
  },
  {
    id: "image.backquote.next",
    keys: ["`"],
    desc: "跨类跳转：下一类首个对象",
    category: "selection",
    stages: ["image"],
    note: "AI 待审 → 人工；Shift+` 反向",
    actionType: "imageStepCategory",
  },
  {
    id: "image.backquote.prev",
    keys: ["Shift", "`"],
    desc: "跨类跳转：上一类首个对象",
    category: "selection",
    stages: ["image"],
    actionType: "imageStepCategory",
  },
  {
    id: "image.user.next",
    keys: ["J"],
    desc: "下一个人工标注（不循环）",
    category: "selection",
    stages: ["image"],
    actionType: "cycleUser",
  },
  {
    id: "image.user.prev",
    keys: ["K"],
    desc: "上一个人工标注（不循环）",
    category: "selection",
    stages: ["image"],
    actionType: "cycleUser",
  },
  {
    id: "image.selection.nudge",
    keys: ["↑ ↓ ← →"],
    desc: "微调选中对象位置",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    note: "1px；Shift = 10px",
    actionType: "arrowNudge",
  },
  {
    id: "image.selection.lock",
    keys: ["L"],
    desc: "锁定 / 解锁选中对象",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "toggleShapeFlag",
  },
  {
    id: "image.selection.hide",
    keys: ["H"],
    desc: "隐藏 / 显示选中对象",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "toggleShapeFlag",
  },
  {
    id: "image.selection.propagate.next",
    keys: ["Alt", "→"],
    desc: "跨帧延续选中框到同 scene 邻帧",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "crossFramePropagate",
  },
  {
    id: "image.selection.propagate.prev",
    keys: ["Alt", "←"],
    desc: "跨帧延续选中框到同 scene 邻帧",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "crossFramePropagate",
  },
  {
    id: "image.selectAll",
    keys: ["Ctrl", "A"],
    desc: "全选当前帧人工标注",
    category: "selection",
    stages: ["image"],
    actionType: "selectAllUser",
  },
  {
    id: "image.copy",
    keys: ["Ctrl", "C"],
    desc: "复制选中对象",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "copy",
  },
  {
    id: "image.paste",
    keys: ["Ctrl", "V"],
    desc: "粘贴",
    category: "selection",
    stages: ["image"],
    note: "偏移 +10px",
    actionType: "paste",
  },
  {
    id: "image.duplicate",
    keys: ["Ctrl", "D"],
    desc: "原地复制",
    category: "selection",
    stages: ["image"],
    note: "偏移 +10px",
    actionType: "duplicate",
  },
  {
    id: "image.delete",
    keys: ["Delete"],
    desc: "删除选中对象",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    note: "多选时批量删除；无草稿时 Backspace 等效",
    actionType: "deleteSelected",
  },
  {
    id: "image.changeClass",
    keys: ["C"],
    desc: "修改选中对象的类别",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "changeClass",
  },
  {
    id: "image.zorder.down",
    keys: ["["],
    desc: "选中对象下移一层",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "bumpZOrder",
  },
  {
    id: "image.zorder.up",
    keys: ["]"],
    desc: "选中对象上移一层",
    category: "selection",
    stages: ["image"],
    applies: "已选中对象时",
    actionType: "bumpZOrder",
  },
  {
    id: "image.undo",
    keys: ["Ctrl", "Z"],
    desc: "撤销",
    category: "selection",
    stages: ["image"],
    common: true,
    actionType: "undo",
  },
  {
    id: "image.redo",
    keys: ["Ctrl", "Shift", "Z"],
    desc: "重做",
    category: "selection",
    stages: ["image"],
    common: true,
    actionType: "redo",
  },
  {
    id: "image.redo.alt",
    keys: ["Ctrl", "Y"],
    desc: "重做（备用）",
    category: "selection",
    stages: ["image"],
    actionType: "redo",
  },
  {
    id: "image.category.digit",
    keys: [],
    keysLabel: "1 — 9、0",
    desc: "切换类别",
    category: "selection",
    stages: ["image"],
    applies: "前十个类别；其余用类别面板搜索或点击",
    common: true,
    actionType: "setClassByDigit",
  },
  {
    id: "video.tab.cycle",
    keys: ["Tab"],
    desc: "同类流转：下一个",
    category: "selection",
    stages: ["video"],
    note: "AI 待审 / 人工 / 轨迹按选中类循环",
    common: true,
    actionType: "videoCycleInCategory",
  },
  {
    id: "video.tab.cyclePrev",
    keys: ["Shift", "Tab"],
    desc: "同类流转：上一个",
    category: "selection",
    stages: ["video"],
    actionType: "videoCycleInCategory",
  },
  {
    id: "video.backquote.next",
    keys: ["`"],
    desc: "跨类跳转：下一类首个对象",
    category: "selection",
    stages: ["video"],
    note: "AI 待审 → 人工 → 轨迹；Shift+` 反向",
    actionType: "videoStepCategory",
  },
  {
    id: "video.backquote.prev",
    keys: ["Shift", "`"],
    desc: "跨类跳转：上一类首个对象",
    category: "selection",
    stages: ["video"],
    actionType: "videoStepCategory",
  },
  {
    id: "video.category.digit",
    keys: [],
    keysLabel: "1 — 9、0",
    desc: "切换视频类别",
    category: "selection",
    stages: ["video"],
    applies: "前十个类别",
    note: "有选中对象时改选中对象的类别",
    common: true,
    actionType: "setClassByDigit",
  },
  {
    id: "video.delete.keyframe",
    keys: ["Delete", "Backspace"],
    desc: "删除当前关键帧或选中单帧框",
    category: "selection",
    stages: ["video"],
    applies: "已选中对象时",
    keysAlt: true,
    actionType: "videoDeleteSelected",
  },
  {
    id: "video.delete.track",
    keys: ["Ctrl", "Delete", "Backspace"],
    desc: "删除整条选中轨迹",
    category: "selection",
    stages: ["video"],
    applies: "已选中轨迹时",
    keysAlt: true,
    actionType: "videoDeleteSelected",
  },
  {
    id: "threed.selection.fit",
    keys: ["Q"],
    desc: "3D：选中框自动拟合（收尺寸 + 贴地）",
    category: "selection",
    stages: ["threed"],
    applies: "已选中框时",
  },
  {
    id: "threed.selection.fitShrink",
    keys: ["Shift", "Q"],
    desc: "3D：选中框仅收尺寸",
    category: "selection",
    stages: ["threed"],
    applies: "已选中框时",
  },
  {
    id: "threed.selection.fitGround",
    keys: ["Alt", "Q"],
    desc: "3D：选中框仅贴地",
    category: "selection",
    stages: ["threed"],
    applies: "已选中框时",
  },
  {
    id: "threed.selection.propagate",
    keys: ["Shift", "→", "←"],
    desc: "3D：跨帧延续选中框到同 scene 邻帧",
    category: "selection",
    stages: ["threed"],
    applies: "已选中框时",
    keysAlt: true,
  },
  {
    id: "threed.selection.propagateBatch",
    keys: ["Ctrl", "Shift", "→", "←"],
    desc: "3D：批量延续当前帧全部 3D 框到邻帧",
    category: "selection",
    stages: ["threed"],
    keysAlt: true,
  },

  // ── 画布与视角 ────────────────────────────────────────────────────────────
  {
    id: "view.fitReset",
    keys: ["Ctrl", "0"],
    desc: "重置缩放与平移",
    category: "canvas",
    note: "修饰键必须完全一致：Shift+0 不触发",
    common: true,
    actionType: "fitReset",
  },
  {
    id: "video.fit",
    keys: ["Shift", "F"],
    desc: "视频适应视口",
    category: "canvas",
    stages: ["video"],
  },
  {
    id: "video.actualSize",
    keys: ["Shift", "0"],
    desc: "视频按实际尺寸显示",
    category: "canvas",
    stages: ["video"],
    note: "物理 Digit0 + Shift；避免与第十个类别键冲突",
  },
  {
    id: "threed.view.triZoomReset",
    keys: ["Shift", "0"],
    desc: "3D：重置聚焦视图的缩放",
    category: "canvas",
    stages: ["threed"],
    applies: "三视图聚焦时",
    note: "物理 Digit0 + Shift；避免与第十个类别键冲突",
  },
  {
    id: "threed.view.triZoomIn",
    keys: ["+", "="],
    desc: "3D：聚焦视图放大",
    category: "canvas",
    stages: ["threed"],
    applies: "三视图聚焦时",
    keysAlt: true,
  },
  {
    id: "threed.view.triZoomOut",
    keys: ["-"],
    desc: "3D：聚焦视图缩小",
    category: "canvas",
    stages: ["threed"],
    applies: "三视图聚焦时",
  },
  {
    id: "threed.camera.next",
    keys: ["→"],
    desc: "3D：相机放大浮层内切换相机",
    category: "canvas",
    stages: ["threed"],
    applies: "相机放大浮层打开时",
    note: "Esc 关闭浮层",
  },
  {
    id: "threed.camera.prev",
    keys: ["←"],
    desc: "3D：相机放大浮层内切换相机",
    category: "canvas",
    stages: ["threed"],
    applies: "相机放大浮层打开时",
    note: "Esc 关闭浮层",
  },

  // ── AI 与审核 ─────────────────────────────────────────────────────────────
  {
    id: "image.tool.aiCycle",
    keys: ["S"],
    desc: "AI 工具循环",
    category: "ai",
    stages: ["image"],
    note: "智能点 → 智能框 → Magic Box → Exemplar → 退出（跳过置灰）",
    common: true,
    actionType: "setTool",
  },
  {
    id: "image.tool.aiCycle.alt",
    keys: ["Alt", "3"],
    desc: "图片 AI 工具循环（备用）",
    category: "ai",
    stages: ["image"],
    applies: "类别弹层占用数字键时仍可用",
    actionType: "setTool",
    context: "image",
    targetTool: "ai-cycle",
  },
  {
    id: "image.tool.magicBox",
    keys: ["G"],
    desc: "Magic Box",
    category: "ai",
    stages: ["image"],
    note: "粗框 → SAM 收紧到对象紧凑外接矩形 → 落框",
    actionType: "setTool",
  },
  {
    id: "image.ai.accept",
    keys: ["A"],
    desc: "采纳选中待决 AI 候选",
    category: "ai",
    stages: ["image"],
    applies: "选中待决候选时",
    context: "image-prediction",
    actionType: "acceptAi",
    common: true,
  },
  {
    id: "image.ai.reject",
    keys: ["D"],
    desc: "忽略选中待决 AI 候选",
    category: "ai",
    stages: ["image"],
    applies: "选中待决候选时",
    context: "image-prediction",
    actionType: "rejectAi",
  },
  {
    id: "sam.polarity.positive",
    keys: ["+", "="],
    desc: "切换正向候选",
    category: "ai",
    stages: ["image"],
    applies: "智能点 / 示例框工具激活时",
    keysAlt: true,
    actionType: "samPolarity",
  },
  {
    id: "sam.polarity.negative",
    keys: ["-"],
    desc: "切换负向候选",
    category: "ai",
    stages: ["image"],
    applies: "智能点 / 示例框工具激活时",
    actionType: "samPolarity",
  },
  {
    id: "ai.threshold.down",
    keys: ["["],
    desc: "降低置信度阈值",
    category: "ai",
    stages: ["image"],
    applies: "未选中对象时",
    actionType: "thresholdAdjust",
  },
  {
    id: "ai.threshold.up",
    keys: ["]"],
    desc: "升高置信度阈值",
    category: "ai",
    stages: ["image"],
    applies: "未选中对象时",
    actionType: "thresholdAdjust",
  },
  {
    id: "video.tool.smartPoint",
    keys: ["S"],
    desc: "视频智能点工具",
    category: "ai",
    stages: ["video"],
    note: "交互式 SAM 分割当前帧；Alt+点击落负点",
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.smartBox",
    keys: ["D"],
    desc: "视频智能框工具",
    category: "ai",
    stages: ["video"],
    applies: "未选中当前帧待决 AI 候选时",
    context: "video-no-prediction",
    targetTool: "smart-box",
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.exemplar",
    keys: ["E"],
    desc: "视频示例框工具",
    category: "ai",
    stages: ["video"],
    note: "框一个例子找出当前帧所有同类；Alt+框排除误检",
    actionType: "setVideoTool",
  },
  {
    id: "video.tool.magicBox",
    keys: ["G"],
    desc: "视频 Magic Box 工具",
    category: "ai",
    stages: ["video"],
    note: "粗框 → SAM 收紧 → 落矩形框",
    actionType: "setVideoTool",
  },
  {
    id: "video.ai.accept",
    keys: ["A"],
    desc: "采纳选中当前帧待决 AI 候选",
    category: "ai",
    stages: ["video"],
    applies: "选中普通待决候选时",
    note: "轨迹候选接受整个 shape",
    context: "video-prediction",
    actionType: "acceptAi",
    common: true,
  },
  {
    id: "video.ai.reject",
    keys: ["D"],
    desc: "忽略选中当前帧待决 AI 候选",
    category: "ai",
    stages: ["video"],
    applies: "选中普通待决候选时",
    note: "保留当前工具",
    context: "video-prediction",
    actionType: "rejectAi",
  },
  {
    id: "video.track.propagate",
    keys: ["Ctrl", "B"],
    desc: "选中轨迹时打开 AI 追踪",
    category: "ai",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoPropagateTrack",
  },

  // ── 播放与轨迹 ────────────────────────────────────────────────────────────
  {
    id: "video.play.toggle",
    keys: ["Space"],
    desc: "视频播放 / 暂停",
    category: "playback",
    stages: ["video"],
    note: "按住 Space 拖拽可平移画布",
    common: true,
    actionType: "videoSpaceDown",
  },
  {
    id: "video.play.backward",
    keys: ["J"],
    desc: "视频反向多速率播放",
    category: "playback",
    stages: ["video"],
    actionType: "videoJogPlayback",
  },
  {
    id: "video.play.pause",
    keys: ["K"],
    desc: "视频暂停播放",
    category: "playback",
    stages: ["video"],
    actionType: "videoJogPlayback",
  },
  {
    id: "video.play.forward",
    keys: ["L"],
    desc: "视频正向多速率播放",
    category: "playback",
    stages: ["video"],
    applies: "未选中轨迹时",
    actionType: "videoJogPlayback",
  },
  {
    id: "video.frame.prev",
    keys: ["←"],
    desc: "上一帧",
    category: "playback",
    stages: ["video"],
    note: "采样开启时按网格退",
    actionType: "videoSeek",
  },
  {
    id: "video.frame.next",
    keys: ["→"],
    desc: "下一帧",
    category: "playback",
    stages: ["video"],
    note: "采样开启时按网格跳",
    actionType: "videoSeek",
  },
  {
    id: "video.frame.micro.prev",
    keys: ["Shift", "←"],
    desc: "源帧微调 -1",
    category: "playback",
    stages: ["video"],
    applies: "采样开启时",
    actionType: "videoMicroStep",
  },
  {
    id: "video.frame.micro.next",
    keys: ["Shift", "→"],
    desc: "源帧微调 +1",
    category: "playback",
    stages: ["video"],
    applies: "采样开启时",
    actionType: "videoMicroStep",
  },
  {
    id: "video.track.keyframe.prev",
    keys: [","],
    desc: "跳上一关键帧",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoSeekKeyframe",
  },
  {
    id: "video.track.keyframe.next",
    keys: ["."],
    desc: "跳下一关键帧",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoSeekKeyframe",
  },
  {
    id: "video.track.keyframe.first",
    keys: ["Home"],
    desc: "跳首关键帧",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoSeekKeyframe",
  },
  {
    id: "video.track.keyframe.last",
    keys: ["End"],
    desc: "跳末关键帧",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoSeekKeyframe",
  },
  {
    id: "video.track.bookmark",
    keys: ["Ctrl", "M"],
    desc: "当前帧添加 / 移除书签",
    category: "playback",
    stages: ["video"],
    actionType: "videoToggleBookmark",
  },
  {
    id: "video.track.outside",
    keys: ["O"],
    desc: "标记 / 恢复当前帧消失",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoToggleOutside",
  },
  {
    id: "video.track.occluded",
    keys: ["Q", "/"],
    desc: "标记 / 恢复当前帧遮挡",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    keysAlt: true,
    actionType: "videoToggleOccluded",
  },
  {
    id: "video.track.locked",
    keys: ["L"],
    desc: "锁定 / 解锁轨迹",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoToggleLockedTrack",
  },
  {
    id: "video.track.hidden",
    keys: ["H"],
    desc: "隐藏 / 显示轨迹",
    category: "playback",
    stages: ["video"],
    applies: "已选中轨迹时",
    actionType: "videoToggleHiddenTrack",
  },
  {
    id: "video.history.prev",
    keys: ["Ctrl", "["],
    desc: "视频跳转历史后退",
    category: "playback",
    stages: ["video"],
    actionType: "videoJumpHistory",
  },
  {
    id: "video.history.next",
    keys: ["Ctrl", "]"],
    desc: "视频跳转历史前进",
    category: "playback",
    stages: ["video"],
    actionType: "videoJumpHistory",
  },
  {
    id: "video.loop.clear",
    keys: ["Alt", "L"],
    desc: "清除视频播放范围",
    category: "playback",
    stages: ["video"],
    actionType: "videoClearLoopRegion",
  },
  {
    id: "video.chapter.prev",
    keys: ["PageUp"],
    desc: "跳到上一章节",
    category: "playback",
    stages: ["video"],
  },
  {
    id: "video.chapter.next",
    keys: ["PageDown"],
    desc: "跳到下一章节",
    category: "playback",
    stages: ["video"],
  },

  // ── 任务与系统 ────────────────────────────────────────────────────────────
  {
    id: "common.task.next",
    keys: ["Ctrl", "→"],
    desc: "下一题",
    category: "task",
    common: true,
    actionType: "navigateTask",
  },
  {
    id: "common.task.prev",
    keys: ["Ctrl", "←"],
    desc: "上一题",
    category: "task",
    common: true,
    actionType: "navigateTask",
  },
  {
    id: "image.smartNext.open",
    keys: ["N"],
    desc: "智能切题：下一未标注",
    category: "task",
    stages: ["image"],
    common: true,
    actionType: "smartNext",
  },
  {
    id: "image.smartNext.uncertain",
    keys: ["U"],
    desc: "智能切题：下一最不确定",
    category: "task",
    stages: ["image"],
    actionType: "smartNext",
  },
  {
    id: "task.submit",
    keys: ["E"],
    desc: "提交质检",
    category: "task",
    stages: ["image"],
    actionType: "submit",
  },
  {
    id: "system.cheatsheet",
    keys: ["?"],
    desc: "打开快捷键面板",
    category: "task",
    common: true,
    actionType: "showHotkeys",
  },
  {
    id: "system.cancel",
    keys: ["Esc"],
    desc: "取消草稿 / 选择 / 关闭弹窗",
    category: "task",
    note: "都没有时回到选择工具",
    actionType: "cancel",
  },

  // ── 鼠标操作 ──────────────────────────────────────────────────────────────
  {
    id: "mouse.zoomCursor",
    keys: ["Ctrl", "滚轮"],
    desc: "以光标为锚点缩放",
    category: "mouse",
    applies: "二维画布",
  },
  {
    id: "mouse.spacePan",
    keys: ["Space", "拖拽"],
    desc: "平移画布",
    category: "mouse",
    applies: "二维画布",
  },
  {
    id: "mouse.doubleClickFit",
    keys: ["双击空白"],
    desc: "适应视口",
    category: "mouse",
    applies: "二维画布",
  },
  {
    id: "mouse.maskBrushSize",
    keys: ["滚轮"],
    desc: "Mask 笔刷半径 ±2px",
    category: "mouse",
    applies: "Mask 编辑中",
  },
  {
    id: "mouse.vertexDrag",
    keys: ["拖动顶点"],
    desc: "移动多边形顶点",
    category: "mouse",
    stages: ["image"],
    applies: "选中多边形时",
  },
  {
    id: "mouse.edgeInsert",
    keys: ["Alt", "点击边"],
    desc: "边上插入新顶点",
    category: "mouse",
    stages: ["image"],
    applies: "多边形草稿中",
  },
  {
    id: "mouse.vertexDelete",
    keys: ["Shift", "点击顶点"],
    desc: "删除该顶点",
    category: "mouse",
    stages: ["image"],
    applies: "多边形草稿中",
    note: "剩余 ≤3 顶点时拒绝",
  },
  {
    id: "mouse.selectionAdd",
    keys: ["Shift", "点击"],
    desc: "叠加多选",
    category: "mouse",
    applies: "二维画布",
  },
];

/** 面板导航顺序与标签（信息架构：常用为精选视图，其余按用途划分）。 */
export const HOTKEY_CATEGORIES: HotkeyCategory[] = [
  "common",
  "draw",
  "selection",
  "canvas",
  "ai",
  "playback",
  "task",
  "mouse",
];

export const HOTKEY_CATEGORY_LABEL: Record<HotkeyCategory, string> = {
  common: "常用",
  draw: "绘制与工具",
  selection: "选择与编辑",
  canvas: "画布与视角",
  ai: "AI 与审核",
  playback: "播放与轨迹",
  task: "任务与系统",
  mouse: "鼠标操作",
};

export const HOTKEY_STAGE_LABEL: Record<HotkeyStage, string> = {
  image: "图片",
  video: "视频",
  threed: "3D / 点云",
};

// ── pure dispatch ───────────────────────────────────────────────────────────
// 把 KeyboardEvent + 简单上下文映射为 HotkeyAction。
// WorkbenchShell 的 useEffect 据此 switch；hotkeys.test.ts 据此覆盖分支。
// 可编辑命令的匹配经 `matchCommand`（生效绑定解析）；Fixed 命令保留显式分支。

export type HotkeyAction =
  | { type: "undo" }
  | { type: "redo" }
  | { type: "fitReset" }
  | { type: "navigateTask"; dir: "next" | "prev" }
  | { type: "crossFramePropagate"; dir: "next" | "prev" }
  | { type: "selectAllUser" }
  | { type: "copy" }
  | { type: "paste" }
  | { type: "duplicate" }
  | { type: "cancel" }
  | { type: "showHotkeys" }
  | { type: "spacePanOn" }
  | { type: "arrowNudge"; dx: number; dy: number }
  | { type: "thresholdAdjust"; delta: number }
  | { type: "cycleUser"; dir: 1 | -1; loop: boolean }
  | { type: "smartNext"; mode: "open" | "uncertain" }
  | { type: "changeClass" }
  | {
      type: "setTool";
      tool:
        | "select"
        | "box"
        | "rotated-box"
        | "hand"
        | "polygon"
        | "polyline"
        | "keypoint"
        | "mask"
        | "smart-point"
        | "smart-box"
        | "text-prompt"
        | "exemplar"
        | "magic-box"
        | "ai-cycle";
    }
  | {
      type: "setVideoTool";
      tool:
        | "select"
        | "box"
        | "rotated-box"
        | "keypoint"
        | "track"
        | "mask"
        | "smart-point"
        | "smart-box"
        | "exemplar"
        | "magic-box"
        | "polygon";
    }
  | { type: "setClassByDigit"; idx: number }
  | { type: "setAttribute"; key: string; value: unknown }
  | { type: "deleteSelected" }
  // v0.10.5 M4-β · I15 shape 状态位快捷键。
  | { type: "toggleShapeFlag"; flag: "is_locked" | "is_hidden" }
  | { type: "bumpZOrder"; delta: -1 | 1 }
  | { type: "submit" }
  | { type: "acceptAi" }
  | { type: "rejectAi" }
  | { type: "samPolarity"; polarity: "positive" | "negative" }
  | { type: "videoTogglePlayback" }
  | { type: "videoSpaceDown" }
  | { type: "videoJogPlayback"; dir: -1 | 1 }
  | { type: "videoPausePlayback" }
  | { type: "videoSeek"; delta: number }
  // v0.10.29 · 软网格导航：采样开启 (step>1) 时 ←/→ 走网格跳。
  | { type: "videoSeekGrid"; dir: -1 | 1 }
  // v0.10.29 · 逃生口：±1 源帧微调 (采样开启时 Shift+←/→ 与 ,/. )。
  | { type: "videoMicroStep"; dir: -1 | 1 }
  | { type: "videoSeekKeyframe"; dir: -1 | 1 }
  | { type: "videoToggleBookmark" }
  | { type: "videoToggleOutside" }
  | { type: "videoToggleOccluded" }
  | { type: "videoToggleHiddenTrack" }
  | { type: "videoToggleLockedTrack" }
  | { type: "videoPropagateTrack" }
  | { type: "videoJumpHistory"; dir: -1 | 1 }
  | { type: "videoClearLoopRegion" }
  | { type: "videoDeleteSelected"; scope: "keyframe" | "track" }
  | { type: "videoCycleInCategory"; dir: 1 | -1 }
  | { type: "videoStepCategory"; dir: 1 | -1 }
  | { type: "imageCycleInCategory"; dir: 1 | -1 }
  | { type: "imageStepCategory"; dir: 1 | -1 };

/** 属性 hotkey 解析结果（D.1）：
 * 由 WorkbenchShell 根据当前 selected box 的 class_name + project.attribute_schema 计算
 * 当某个数字键命中某个 boolean / select 字段时，dispatcher 决策接下来的下一个值。
 */
export interface AttributeHotkeyHit {
  key: string;
  type: "boolean" | "select";
  /** select 类型必填；boolean 忽略。 */
  options?: string[];
  /** 当前值（用于 select 计算 next；boolean 用于反转）。 */
  currentValue?: unknown;
}

export interface DispatchCtx {
  /** 焦点在 input/textarea/contenteditable 上时，禁用 hotkey。 */
  isInputFocused: boolean;
  /** 是否有任意选中（决定方向键 nudge / a/d AI accept-reject 等是否激活）。 */
  hasSelection: boolean;
  /** Selected ordinary pending prediction, already filtered to the displayed frame. */
  selectedPrediction?: { id: string } | null;
  /** pendingDrawing | editingClass | batchChanging 中任一活跃 → 类别按键归 popover 消费。 */
  pendingActive: boolean;
  /** D.1：选中标注且当前数字键命中某个属性 hotkey 时，返回属性元数据；否则返回 null。
   * 实现由 WorkbenchShell 层注入（绑了项目 schema 与当前 annotation.attributes）。
   * 仅当属性快捷键区域显式聚焦时才返回命中（见 AttributeForm 的 region 契约）。
   */
  attributeHotkey?: (digit: string) => AttributeHotkeyHit | null;
  /** video stage active: consume video namespace before image drawing shortcuts. */
  videoMode?: boolean;
  /** Explicit stage ownership; 3D must never consume image command overrides. */
  stage?: HotkeyStage;
  /** selected annotation is a video_track; used for contextual video timeline shortcuts. */
  hasSelectedVideoTrack?: boolean;
  /** v0.10.29 · 视频采样网格生效 (step>1)：←/→ 改为网格跳，Shift/Alt 重映射。
   *  step=1 (不采样) 时为 false，键位维持现状不变 (向后兼容)。 */
  samplingActive?: boolean;
  /** Increment B：可编辑命令的生效绑定匹配（无 overrides 时等价默认组合）。 */
  matchCommand?: (commandId: string) => boolean;
  /** Increment B：同一按键意外命中多条重叠命令时置位；dispatch 吞掉并上报一次。 */
  commandBindingConflict?: boolean;
  /** Increment B：上报运行期绑定冲突（每会话每按键一次）。 */
  onCommandBindingConflict?: () => void;
}

/** Command admission is contextual; its recorded key and modifiers are owned by the matcher. */
function dispatchEditableCommand(
  e: KeyboardEvent,
  ctx: DispatchCtx,
  stage: HotkeyStage,
  match: (id: string) => boolean,
): HotkeyAction | null {
  if (match("common.task.next")) return { type: "navigateTask", dir: "next" };
  if (match("common.task.prev")) return { type: "navigateTask", dir: "prev" };
  if (stage === "threed") return null;
  if (stage === "video") {
    if (match("video.track.bookmark")) return { type: "videoToggleBookmark" };
    if (ctx.pendingActive) return null;
    if (ctx.hasSelectedVideoTrack) {
      if (match("video.track.outside")) return { type: "videoToggleOutside" };
      if (match("video.track.occluded")) return { type: "videoToggleOccluded" };
      if (match("video.track.hidden")) return { type: "videoToggleHiddenTrack" };
      if (match("video.track.locked")) return { type: "videoToggleLockedTrack" };
      if (match("video.track.keyframe.next")) return { type: "videoSeekKeyframe", dir: 1 };
      if (match("video.track.keyframe.prev")) return { type: "videoSeekKeyframe", dir: -1 };
    }
    if (ctx.samplingActive) {
      if (match("video.frame.micro.next")) return { type: "videoMicroStep", dir: 1 };
      if (match("video.frame.micro.prev")) return { type: "videoMicroStep", dir: -1 };
    }
    if (match("video.frame.next"))
      return ctx.samplingActive
        ? { type: "videoSeekGrid", dir: 1 }
        : { type: "videoSeek", delta: 1 };
    if (match("video.frame.prev"))
      return ctx.samplingActive
        ? { type: "videoSeekGrid", dir: -1 }
        : { type: "videoSeek", delta: -1 };
    if (match("video.tool.select")) return { type: "setVideoTool", tool: "select" };
    if (match("video.tool.box")) return { type: "setVideoTool", tool: "box" };
    if (match("video.tool.rotatedBox")) return { type: "setVideoTool", tool: "rotated-box" };
    if (match("video.tool.keypoint")) return { type: "setVideoTool", tool: "keypoint" };
    if (match("video.tool.track")) return { type: "setVideoTool", tool: "track" };
    if (match("video.tool.mask")) return { type: "setVideoTool", tool: "mask" };
    if (match("video.tool.smartPoint")) return { type: "setVideoTool", tool: "smart-point" };
    if (!ctx.selectedPrediction && match("video.tool.smartBox"))
      return { type: "setVideoTool", tool: "smart-box" };
    if (match("video.tool.exemplar")) return { type: "setVideoTool", tool: "exemplar" };
    if (match("video.tool.magicBox")) return { type: "setVideoTool", tool: "magic-box" };
    if (match("video.tool.polygon")) return { type: "setVideoTool", tool: "polygon" };
    return null;
  }
  // Keep the existing alternate-tool escape hatch while the image class picker owns bare keys.
  if (ctx.pendingActive) {
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      if (match("image.tool.box")) return { type: "setTool", tool: "box" };
      if (match("image.tool.polygon")) return { type: "setTool", tool: "polygon" };
      if (match("image.tool.aiCycle")) return { type: "setTool", tool: "ai-cycle" };
      if (match("image.tool.select")) return { type: "setTool", tool: "select" };
    }
    return null;
  }
  if (ctx.hasSelection) {
    if (match("image.selection.lock")) return { type: "toggleShapeFlag", flag: "is_locked" };
    if (match("image.selection.hide")) return { type: "toggleShapeFlag", flag: "is_hidden" };
  } else if (match("image.tool.polyline")) return { type: "setTool", tool: "polyline" };
  if (match("image.tool.select")) return { type: "setTool", tool: "select" };
  if (match("image.tool.box")) return { type: "setTool", tool: "box" };
  if (match("image.tool.rotatedBox")) return { type: "setTool", tool: "rotated-box" };
  if (match("image.tool.keypoint")) return { type: "setTool", tool: "keypoint" };
  if (match("image.tool.aiCycle")) return { type: "setTool", tool: "ai-cycle" };
  if (match("image.tool.magicBox")) return { type: "setTool", tool: "magic-box" };
  if (match("image.tool.polygon")) return { type: "setTool", tool: "polygon" };
  if (match("image.tool.mask")) return { type: "setTool", tool: "mask" };
  return null;
}

/** 纯函数：解析 keydown 事件为 HotkeyAction。返回 null 表示不消费。 */
export function dispatchKey(e: KeyboardEvent, ctx: DispatchCtx): HotkeyAction | null {
  if (ctx.isInputFocused) return null;
  // IME 组合期 / AltGr（Windows 上 Ctrl+Alt 字符）事件不参与任何 Workbench 命令。
  if (e.isComposing || e.keyCode === 229) return null;
  if (e.altKey && (e.ctrlKey || e.metaKey) && e.key.length === 1) return null;

  const stage = ctx.stage ?? (ctx.videoMode ? "video" : "image");
  const match = ctx.matchCommand ?? createCommandEventMatcher(e, DEFAULT_EFFECTIVE, stage).match;

  // Increment B：同键意外命中多条重叠命令 → 一律不执行并上报（保留指针入口）。
  if (ctx.commandBindingConflict) {
    ctx.onCommandBindingConflict?.();
    return null;
  }

  const editableAction = dispatchEditableCommand(e, ctx, stage, match);
  if (editableAction) return editableAction;

  // 系统级（带 Ctrl/Meta）
  if (e.ctrlKey || e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === "z") return e.shiftKey ? { type: "redo" } : { type: "undo" };
    if (k === "y") return { type: "redo" };
    if (e.key === "0" && !e.shiftKey) return { type: "fitReset" };
    if (ctx.videoMode && ctx.hasSelectedVideoTrack && k === "b" && !e.shiftKey) {
      return { type: "videoPropagateTrack" };
    }
    if (ctx.videoMode && e.key === "[" && !e.shiftKey) {
      return { type: "videoJumpHistory", dir: -1 };
    }
    if (ctx.videoMode && e.key === "]" && !e.shiftKey) {
      return { type: "videoJumpHistory", dir: 1 };
    }
    if (
      ctx.videoMode &&
      ctx.hasSelection &&
      !e.shiftKey &&
      (e.key === "Delete" || e.key === "Backspace")
    ) {
      return { type: "videoDeleteSelected", scope: "track" };
    }
    if (k === "a" && !e.shiftKey) return { type: "selectAllUser" };
    if (k === "c" && !e.shiftKey) return { type: "copy" };
    if (k === "v" && !e.shiftKey) return { type: "paste" };
    if (k === "d" && !e.shiftKey) return { type: "duplicate" };
    return null;
  }

  if (ctx.videoMode) {
    if (ctx.pendingActive) {
      if (e.key === "Escape") return { type: "cancel" };
      return null;
    }
    if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) return null;
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      if (e.key === "l" || e.key === "L") return { type: "videoClearLoopRegion" };
      return null;
    }
    if (ctx.selectedPrediction) {
      if (e.key === "a" || e.key === "A") return { type: "acceptAi" };
      if (e.key === "d" || e.key === "D") return { type: "rejectAi" };
    }
    if (e.key === " " && !e.altKey && !e.shiftKey) return { type: "videoSpaceDown" };
    if ((e.key === "j" || e.key === "J") && !e.altKey && !e.shiftKey) {
      return { type: "videoJogPlayback", dir: -1 };
    }
    if ((e.key === "k" || e.key === "K") && !e.altKey && !e.shiftKey) {
      return { type: "videoPausePlayback" };
    }
    if (!e.altKey && !ctx.hasSelectedVideoTrack && (e.key === "l" || e.key === "L")) {
      return { type: "videoJogPlayback", dir: 1 };
    }
    if (e.key === "Tab") return { type: "videoCycleInCategory", dir: e.shiftKey ? -1 : 1 };
    // v0.21.11 · ` / Shift+` 跨类跳转(AI 待审 → 人工 → 轨迹); 紧邻 Tab、当前未占用。
    // e.code 而非 e.key: Shift+` 的 key 在美式布局是 "~" 而非 "`", 用 key 判定会让反向(Shift)永不触发。
    if (e.code === "Backquote") return { type: "videoStepCategory", dir: e.shiftKey ? -1 : 1 };
    if (e.key === "Escape") return { type: "cancel" };
    if ((e.key === "Delete" || e.key === "Backspace") && !e.shiftKey) {
      return { type: "videoDeleteSelected", scope: "keyframe" };
    }
    if ((e.key >= "1" && e.key <= "9" && !e.shiftKey) || (e.key === "0" && !e.shiftKey)) {
      return { type: "setClassByDigit", idx: e.key === "0" ? 9 : parseInt(e.key, 10) - 1 };
    }
    if (e.key === "?") return { type: "showHotkeys" };
    return null;
  }

  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    // v0.14.1 · Alt+→ / Alt+← 跨帧目标延续(2D / 3D 统一键; 2D 的 Shift+← / → 已被
    //           10px nudge 占用, 故跨帧用 Alt+方向)。仅在有选中时消费。
    if (ctx.hasSelection && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      return { type: "crossFramePropagate", dir: e.key === "ArrowRight" ? "next" : "prev" };
    }
    return null;
  }

  // 方向键 nudge（仅在有选中时；上层进一步过滤是否含 user 框）
  if (ctx.hasSelection) {
    const ARR: Record<string, [number, number]> = {
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
    };
    if (e.key in ARR) {
      const step = e.shiftKey ? 10 : 1;
      const [ux, uy] = ARR[e.key];
      return { type: "arrowNudge", dx: ux * step, dy: uy * step };
    }
  }

  if (e.key === " " && !e.altKey && !e.shiftKey) return { type: "spacePanOn" };
  if (e.key === "?") return { type: "showHotkeys" };
  if (e.key === "Escape") return { type: "cancel" };

  // popover 活跃时，类别数字键归它消费
  if (ctx.pendingActive) return null;

  // v0.10.5 M4-β I15 · 有选中时 `[`/`]` 调层级（向下/向上）；无选中时维持原 threshold 行为。
  if (e.key === "[") {
    if (ctx.hasSelection) return { type: "bumpZOrder", delta: -1 };
    return { type: "thresholdAdjust", delta: -0.05 };
  }
  if (e.key === "]") {
    if (ctx.hasSelection) return { type: "bumpZOrder", delta: 1 };
    return { type: "thresholdAdjust", delta: 0.05 };
  }

  // v0.9.4 phase 2 · SAM 子工具栏 polarity (sam-point 下生效, 由消费端 gate by tool/samSubTool).
  // "+" 需要 Shift+=, "=" 单按 = SAM positive; "-" 单按 = SAM negative。
  // 按产出字符匹配（"Shift 才打得出的 +" 与 "=" 都视为正向），Shift+其它字母不受影响。
  if (e.key === "+" || e.key === "=") return { type: "samPolarity", polarity: "positive" };
  if (e.key === "-") return { type: "samPolarity", polarity: "negative" };

  // v0.21.11 · 图片 Tab 升级为「同类流转」(AI 待审 / 人工, 按选中对象类型环内循环),
  // ` 跨类跳转。J/K 保留为人工框专属循环(老肌肉记忆)。
  if (e.key === "Tab") return { type: "imageCycleInCategory", dir: e.shiftKey ? -1 : 1 };
  // e.code 而非 e.key: Shift+` 的 key 在美式布局是 "~" 而非 "`", 用 key 判定会让反向(Shift)永不触发。
  if (e.code === "Backquote") return { type: "imageStepCategory", dir: e.shiftKey ? -1 : 1 };
  if ((e.key === "j" || e.key === "J") && !e.shiftKey) {
    return { type: "cycleUser", dir: 1, loop: false };
  }
  if ((e.key === "k" || e.key === "K") && !e.shiftKey) {
    return { type: "cycleUser", dir: -1, loop: false };
  }

  if ((e.key === "n" || e.key === "N") && !e.shiftKey) return { type: "smartNext", mode: "open" };
  if ((e.key === "u" || e.key === "U") && !e.shiftKey) {
    return { type: "smartNext", mode: "uncertain" };
  }

  // C 键（无修饰）：选中态走改类别；否则不消费。a/d 同理（接 AI accept/reject）。
  if ((e.key === "c" || e.key === "C") && !e.shiftKey && ctx.hasSelection) {
    return { type: "changeClass" };
  }

  if ((e.key >= "1" && e.key <= "9" && !e.shiftKey) || (e.key === "0" && !e.shiftKey)) {
    // D.1：属性快捷键区域显式聚焦时，命中属性字段优先；否则数字键切类别（0 = 第十个）。
    if (ctx.attributeHotkey) {
      const hit = ctx.attributeHotkey(e.key);
      if (hit) {
        if (hit.type === "boolean") {
          return { type: "setAttribute", key: hit.key, value: !hit.currentValue };
        }
        if (hit.type === "select" && hit.options && hit.options.length > 0) {
          const cur = hit.currentValue == null ? "" : String(hit.currentValue);
          const idx = hit.options.indexOf(cur);
          const next = hit.options[(idx + 1) % hit.options.length];
          return { type: "setAttribute", key: hit.key, value: next };
        }
      }
    }
    return { type: "setClassByDigit", idx: e.key === "0" ? 9 : parseInt(e.key, 10) - 1 };
  }

  if ((e.key === "Delete" || e.key === "Backspace") && !e.shiftKey) {
    return { type: "deleteSelected" };
  }
  if ((e.key === "e" || e.key === "E") && !e.shiftKey) return { type: "submit" };

  if ((e.key === "a" || e.key === "A") && !e.shiftKey && ctx.hasSelection) {
    return { type: "acceptAi" };
  }
  if ((e.key === "d" || e.key === "D") && !e.shiftKey && ctx.hasSelection) {
    return { type: "rejectAi" };
  }

  return null;
}

export const ARROW_KEY_SET = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
