import { describe, expect, it } from "vitest";
import {
  HOTKEYS,
  dispatchKey,
  hotkeyIgnoreToken,
  type DispatchCtx,
  type HotkeyAction,
} from "./hotkeys";

import {
  createCommandEventMatcher,
  resolveEffectiveCommands,
  getEditableCommand,
  type ShortcutBinding,
} from "./hotkeyBindings";

const baseCtx: DispatchCtx = {
  isInputFocused: false,
  hasSelection: false,
  pendingActive: false,
};

// 鸭子类型：dispatchKey 只读 key / code / *Key 修饰键，构造一个兼容 shape 即可，
// 避免依赖 jsdom 等 DOM 环境。
type FakeEvent = Pick<KeyboardEventInit, "ctrlKey" | "metaKey" | "shiftKey" | "altKey"> & {
  key?: string;
  code?: string;
};

function dispatch(e: FakeEvent, ctx: Partial<DispatchCtx> = {}): HotkeyAction | null {
  return dispatchKey(e as unknown as KeyboardEvent, { ...baseCtx, ...ctx });
}

describe("dispatchKey · 修饰键", () => {
  it("只给 Ctrl / Meta 组合键生成忽略 token", () => {
    expect(hotkeyIgnoreToken({ key: "C", ctrlKey: false, metaKey: false })).toBeNull();
    expect(hotkeyIgnoreToken({ key: "C", ctrlKey: true, metaKey: false })).toBe("Mod+c");
  });
  it("Ctrl+Z → undo", () => {
    expect(dispatch({ key: "z", ctrlKey: true })).toEqual({ type: "undo" });
  });
  it("Ctrl+Shift+Z → redo", () => {
    expect(dispatch({ key: "z", ctrlKey: true, shiftKey: true })).toEqual({ type: "redo" });
  });
  it("Ctrl+Y → redo (备用)", () => {
    expect(dispatch({ key: "y", ctrlKey: true })).toEqual({ type: "redo" });
  });
  it("Ctrl+0 → fitReset", () => {
    expect(dispatch({ key: "0", ctrlKey: true })).toEqual({ type: "fitReset" });
  });
  it("Ctrl+ArrowRight → next task", () => {
    expect(dispatch({ key: "ArrowRight", ctrlKey: true })).toEqual({
      type: "navigateTask",
      dir: "next",
    });
  });
  it("Ctrl+A → selectAllUser", () => {
    expect(dispatch({ key: "a", ctrlKey: true })).toEqual({ type: "selectAllUser" });
  });
  it("Ctrl+C / V / D → 剪贴板", () => {
    expect(dispatch({ key: "c", ctrlKey: true })).toEqual({ type: "copy" });
    expect(dispatch({ key: "v", ctrlKey: true })).toEqual({ type: "paste" });
    expect(dispatch({ key: "d", ctrlKey: true })).toEqual({ type: "duplicate" });
  });
  it("Meta key 等价于 Ctrl（Mac）", () => {
    expect(dispatch({ key: "z", metaKey: true })).toEqual({ type: "undo" });
  });
});

describe("dispatchKey · 单键", () => {
  it("B / V / P / S → setTool", () => {
    expect(dispatch({ key: "b" })).toEqual({ type: "setTool", tool: "box" });
    expect(dispatch({ key: "v" })).toEqual({ type: "setTool", tool: "select" });
    expect(dispatch({ key: "p" })).toEqual({ type: "setTool", tool: "polygon" });
    // v0.10.2 · S → "ai-cycle" (具体进入哪个 AI 工具由消费层据 capabilities 决定).
    expect(dispatch({ key: "s" })).toEqual({ type: "setTool", tool: "ai-cycle" });
    expect(dispatch({ key: "S" })).toEqual({ type: "setTool", tool: "ai-cycle" });
  });
  // v0.10.17 · G 单键直达 Magic Box (跳过 ai-cycle).
  it("G → setTool magic-box (v0.10.17)", () => {
    expect(dispatch({ key: "g" })).toEqual({ type: "setTool", tool: "magic-box" });
    expect(dispatch({ key: "G" })).toEqual({ type: "setTool", tool: "magic-box" });
  });
  it("数字键 1-9 → setClassByDigit；0 → 第十个类别", () => {
    expect(dispatch({ key: "3" })).toEqual({ type: "setClassByDigit", idx: 2 });
    expect(dispatch({ key: "9" })).toEqual({ type: "setClassByDigit", idx: 8 });
    expect(dispatch({ key: "0" })).toEqual({ type: "setClassByDigit", idx: 9 });
  });
  it("Shift+数字不再顺延切类别（Shift 变体是独立组合）", () => {
    expect(dispatch({ key: "3", shiftKey: true })).toBeNull();
  });
  it("Alt+1/2/3/4 → setTool (v0.9.6 P2-b 备用切工具)", () => {
    // v0.10.2 · Alt+2 → polygon, Alt+3 → ai-cycle (4 个 AI 工具循环).
    expect(dispatch({ key: "1", altKey: true })).toEqual({ type: "setTool", tool: "box" });
    expect(dispatch({ key: "2", altKey: true })).toEqual({ type: "setTool", tool: "polygon" });
    expect(dispatch({ key: "3", altKey: true })).toEqual({ type: "setTool", tool: "ai-cycle" });
    expect(dispatch({ key: "4", altKey: true })).toEqual({ type: "setTool", tool: "select" });
  });
  it("Alt+5..9 不映射（Alt 备用键只有 1-4；数字切类别只认无修饰数字）", () => {
    expect(dispatch({ key: "5", altKey: true })).toBeNull();
  });
  it("Alt 仅与单字组合生效 (Alt+Ctrl+1 走 ctrl 分支不动 setTool)", () => {
    expect(dispatch({ key: "1", altKey: true, ctrlKey: true })).toBeNull();
  });
  it("字母键（非保留）不再切类别：类别直选只有数字 1-9、0", () => {
    expect(dispatch({ key: "y" })).toBeNull();
    expect(dispatch({ key: "z" })).toBeNull();
    expect(dispatch({ key: "i" })).toBeNull();
    expect(dispatch({ key: "r" })).toBeNull();
  });
  it("保留字母（v/b/a/d/e/n/u/j/k/c）走专用 action 而非 letter", () => {
    expect(dispatch({ key: "n" })).toEqual({ type: "smartNext", mode: "open" });
    expect(dispatch({ key: "u" })).toEqual({ type: "smartNext", mode: "uncertain" });
    expect(dispatch({ key: "e" })).toEqual({ type: "submit" });
  });
  it("Tab / Shift+Tab → imageCycleInCategory (同类流转); ` → imageStepCategory (跨类)", () => {
    expect(dispatch({ key: "Tab" })).toEqual({ type: "imageCycleInCategory", dir: 1 });
    expect(dispatch({ key: "Tab", shiftKey: true })).toEqual({
      type: "imageCycleInCategory",
      dir: -1,
    });
    // Shift+` 在美式布局 key 是 "~" 而非 "`"，故按物理键位 code 判定；测试用 code 模拟真实事件。
    expect(dispatch({ code: "Backquote" })).toEqual({ type: "imageStepCategory", dir: 1 });
    expect(dispatch({ code: "Backquote", shiftKey: true })).toEqual({
      type: "imageStepCategory",
      dir: -1,
    });
  });
  it("J / K → cycleUser 不循环", () => {
    expect(dispatch({ key: "j" })).toEqual({ type: "cycleUser", dir: 1, loop: false });
    expect(dispatch({ key: "k" })).toEqual({ type: "cycleUser", dir: -1, loop: false });
  });
  it("X / 其他未绑定字母不再切类别（类别直选收归数字 1-9、0）", () => {
    expect(dispatch({ key: "x" })).toBeNull();
    // 循环 AI 待审框现在: Tab 在同类内循环, ` 跨到 AI 待审类。
    expect(dispatch({ key: "Tab" })).toEqual({ type: "imageCycleInCategory", dir: 1 });
    expect(dispatch({ code: "Backquote" })).toEqual({ type: "imageStepCategory", dir: 1 });
  });
  it("[ / ] → 阈值微调", () => {
    expect(dispatch({ key: "[" })).toEqual({ type: "thresholdAdjust", delta: -0.05 });
    expect(dispatch({ key: "]" })).toEqual({ type: "thresholdAdjust", delta: 0.05 });
  });
  it("Delete / Backspace → deleteSelected", () => {
    expect(dispatch({ key: "Delete" })).toEqual({ type: "deleteSelected" });
    expect(dispatch({ key: "Backspace" })).toEqual({ type: "deleteSelected" });
  });
  it("Space → spacePanOn", () => {
    expect(dispatch({ key: " " })).toEqual({ type: "spacePanOn" });
  });
  it("? → showHotkeys", () => {
    expect(dispatch({ key: "?" })).toEqual({ type: "showHotkeys" });
  });
  it("Esc → cancel", () => {
    expect(dispatch({ key: "Escape" })).toEqual({ type: "cancel" });
  });
});

describe("dispatchKey · 上下文相关", () => {
  it("带上下文的帮助定义与实际动作和目标工具一致", () => {
    for (const def of HOTKEYS.filter((item) => item.context)) {
      const key = def.keys[def.keys.length - 1].toLowerCase();
      const prediction =
        def.context?.endsWith("-prediction") && def.context !== "video-no-prediction";
      const actual = dispatch(
        { key, altKey: def.keys.includes("Alt") },
        {
          videoMode: def.context?.startsWith("video"),
          hasSelection: !!prediction,
          selectedPrediction: prediction ? { id: "candidate" } : null,
        },
      );
      expect(actual, `${def.context}: ${def.keys.join("+")}`).toEqual({
        type: def.actionType,
        ...(def.targetTool ? { tool: def.targetTool } : {}),
      });
    }
  });
  it("视频当前帧待决候选优先消费 A/D，人工对象仍保留 D 工具键", () => {
    const selected = { videoMode: true, hasSelection: true };
    expect(
      dispatch({ key: "a" }, { ...selected, selectedPrediction: { id: "candidate" } }),
    ).toEqual({ type: "acceptAi" });
    expect(
      dispatch({ key: "d" }, { ...selected, selectedPrediction: { id: "candidate" } }),
    ).toEqual({ type: "rejectAi" });
    expect(dispatch({ key: "a" }, selected)).toBeNull();
    expect(dispatch({ key: "d" }, selected)).toEqual({ type: "setVideoTool", tool: "smart-box" });
    expect(
      dispatch(
        { key: "d" },
        { ...selected, selectedPrediction: { id: "candidate" }, pendingActive: true },
      ),
    ).toBeNull();
  });
  it("input 聚焦时禁用所有 hotkey", () => {
    expect(dispatch({ key: "b" }, { isInputFocused: true })).toBeNull();
    expect(dispatch({ key: "z", ctrlKey: true }, { isInputFocused: true })).toBeNull();
  });

  it("popover 活跃时类别字母不消费", () => {
    expect(dispatch({ key: "f" }, { pendingActive: true })).toBeNull();
    // 但 Ctrl+Z 等系统级仍消费
    expect(dispatch({ key: "z", ctrlKey: true }, { pendingActive: true })).toEqual({
      type: "undo",
    });
    // Esc 也仍消费
    expect(dispatch({ key: "Escape" }, { pendingActive: true })).toEqual({ type: "cancel" });
  });

  it("无选中时 a/d 不映射为 acceptAi/rejectAi", () => {
    expect(dispatch({ key: "a" }, { hasSelection: false })).toBeNull();
    expect(dispatch({ key: "d" }, { hasSelection: false })).toBeNull();
  });
  it("有选中时 a/d → acceptAi/rejectAi", () => {
    expect(dispatch({ key: "a" }, { hasSelection: true })).toEqual({ type: "acceptAi" });
    expect(dispatch({ key: "d" }, { hasSelection: true })).toEqual({ type: "rejectAi" });
  });

  it("有选中时 c → changeClass", () => {
    expect(dispatch({ key: "c" }, { hasSelection: true })).toEqual({ type: "changeClass" });
  });
  it("无选中时 c 不消费", () => {
    expect(dispatch({ key: "c" })).toBeNull();
  });

  it("方向键 nudge 仅在有选中时映射", () => {
    expect(dispatch({ key: "ArrowUp" })).toBeNull();
    expect(dispatch({ key: "ArrowUp" }, { hasSelection: true })).toEqual({
      type: "arrowNudge",
      dx: 0,
      dy: -1,
    });
    expect(dispatch({ key: "ArrowRight" }, { hasSelection: true })).toEqual({
      type: "arrowNudge",
      dx: 1,
      dy: 0,
    });
  });
  it("Shift + 方向键 → 10x 步长", () => {
    expect(dispatch({ key: "ArrowDown", shiftKey: true }, { hasSelection: true })).toEqual({
      type: "arrowNudge",
      dx: 0,
      dy: 10,
    });
  });
  it("Alt + → / ← (有选中) → crossFramePropagate", () => {
    expect(dispatch({ key: "ArrowRight", altKey: true }, { hasSelection: true })).toEqual({
      type: "crossFramePropagate",
      dir: "next",
    });
    expect(dispatch({ key: "ArrowLeft", altKey: true }, { hasSelection: true })).toEqual({
      type: "crossFramePropagate",
      dir: "prev",
    });
  });
  it("Alt + → 无选中 → 不触发跨帧", () => {
    expect(dispatch({ key: "ArrowRight", altKey: true })).toBeNull();
  });
});

describe("dispatchKey · video mode", () => {
  const videoCtx: Partial<DispatchCtx> = { videoMode: true };

  it("Space → videoSpaceDown", () => {
    expect(dispatch({ key: " " }, videoCtx)).toEqual({ type: "videoSpaceDown" });
  });

  it("J / K / L → video jog playback controls", () => {
    expect(dispatch({ key: "j" }, videoCtx)).toEqual({ type: "videoJogPlayback", dir: -1 });
    expect(dispatch({ key: "K" }, videoCtx)).toEqual({ type: "videoPausePlayback" });
    expect(dispatch({ key: "l" }, videoCtx)).toEqual({ type: "videoJogPlayback", dir: 1 });
  });

  it("selected video track owns O / Q / H / L state toggles", () => {
    const selectedTrackCtx: Partial<DispatchCtx> = { videoMode: true, hasSelectedVideoTrack: true };
    expect(dispatch({ key: "o" }, selectedTrackCtx)).toEqual({ type: "videoToggleOutside" });
    expect(dispatch({ key: "Q" }, selectedTrackCtx)).toEqual({ type: "videoToggleOccluded" });
    expect(dispatch({ key: "/" }, selectedTrackCtx)).toEqual({ type: "videoToggleOccluded" });
    expect(dispatch({ key: "h" }, selectedTrackCtx)).toEqual({ type: "videoToggleHiddenTrack" });
    expect(dispatch({ key: "l" }, selectedTrackCtx)).toEqual({ type: "videoToggleLockedTrack" });
  });

  it("V / B / T → switch video select / box / track tools", () => {
    expect(dispatch({ key: "v" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "select" });
    expect(dispatch({ key: "V" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "select" });
    expect(dispatch({ key: "b" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "box" });
    expect(dispatch({ key: "T" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "track" });
    expect(dispatch({ key: "3", altKey: true }, videoCtx)).toEqual({
      type: "setVideoTool",
      tool: "select",
    });
  });

  it("Ctrl+B opens selected video track propagation only in video mode", () => {
    expect(
      dispatch({ key: "b", ctrlKey: true }, { videoMode: true, hasSelectedVideoTrack: true }),
    ).toEqual({ type: "videoPropagateTrack" });
    expect(dispatch({ key: "b", ctrlKey: true }, videoCtx)).toBeNull();
    expect(dispatch({ key: "b", ctrlKey: true }, { hasSelectedVideoTrack: true })).toBeNull();
  });

  it("ArrowLeft / ArrowRight → videoSeek", () => {
    expect(dispatch({ key: "ArrowRight" }, videoCtx)).toEqual({ type: "videoSeek", delta: 1 });
    expect(dispatch({ key: "ArrowLeft" }, videoCtx)).toEqual({ type: "videoSeek", delta: -1 });
  });

  it(", / . do not act as frame-step aliases without a selected track", () => {
    expect(dispatch({ key: "." }, videoCtx)).toBeNull();
    expect(dispatch({ key: "," }, videoCtx)).toBeNull();
  });

  it(", / . → videoSeekKeyframe when a video track is selected", () => {
    const selectedTrackCtx: Partial<DispatchCtx> = { videoMode: true, hasSelectedVideoTrack: true };
    expect(dispatch({ key: "." }, selectedTrackCtx)).toEqual({ type: "videoSeekKeyframe", dir: 1 });
    expect(dispatch({ key: "," }, selectedTrackCtx)).toEqual({
      type: "videoSeekKeyframe",
      dir: -1,
    });
  });

  it("Shift + ArrowLeft / ArrowRight 不再顺延为逐帧 seek（修饰键必须完全匹配）", () => {
    // Shift+←/→ 是「源帧微调」专用组合，仅采样开启时生效；非采样下不命中任何命令。
    expect(dispatch({ key: "ArrowRight", shiftKey: true }, videoCtx)).toBeNull();
    expect(dispatch({ key: "ArrowLeft", shiftKey: true }, videoCtx)).toBeNull();
  });

  it("Shift + ArrowLeft / ArrowRight with a selected track also stay unbound when sampling is off", () => {
    const selectedTrackCtx: Partial<DispatchCtx> = { videoMode: true, hasSelectedVideoTrack: true };
    expect(dispatch({ key: "ArrowRight", shiftKey: true }, selectedTrackCtx)).toBeNull();
    expect(dispatch({ key: "ArrowLeft", shiftKey: true }, selectedTrackCtx)).toBeNull();
  });

  it("Ctrl+M and Ctrl+[ / ] map to video bookmark navigation in video mode", () => {
    expect(dispatch({ key: "m", ctrlKey: true }, videoCtx)).toEqual({
      type: "videoToggleBookmark",
    });
    expect(dispatch({ key: "[", ctrlKey: true }, videoCtx)).toEqual({
      type: "videoJumpHistory",
      dir: -1,
    });
    expect(dispatch({ key: "]", ctrlKey: true }, videoCtx)).toEqual({
      type: "videoJumpHistory",
      dir: 1,
    });
  });

  it("Alt+L clears the video loop region in video mode", () => {
    expect(dispatch({ key: "l", altKey: true }, videoCtx)).toEqual({
      type: "videoClearLoopRegion",
    });
  });

  it("Delete / Backspace → video keyframe delete by default", () => {
    expect(dispatch({ key: "Delete" }, videoCtx)).toEqual({
      type: "videoDeleteSelected",
      scope: "keyframe",
    });
    expect(dispatch({ key: "Backspace" }, videoCtx)).toEqual({
      type: "videoDeleteSelected",
      scope: "keyframe",
    });
  });

  it("Ctrl+Delete / Ctrl+Backspace → video track delete", () => {
    const selectedVideoCtx: Partial<DispatchCtx> = {
      videoMode: true,
      hasSelection: true,
      hasSelectedVideoTrack: true,
    };
    expect(dispatch({ key: "Delete", ctrlKey: true }, selectedVideoCtx)).toEqual({
      type: "videoDeleteSelected",
      scope: "track",
    });
    expect(dispatch({ key: "Backspace", ctrlKey: true }, selectedVideoCtx)).toEqual({
      type: "videoDeleteSelected",
      scope: "track",
    });
  });

  it("Tab / Shift+Tab → videoCycleInCategory (同类流转)", () => {
    expect(dispatch({ key: "Tab" }, videoCtx)).toEqual({ type: "videoCycleInCategory", dir: 1 });
    expect(dispatch({ key: "Tab", shiftKey: true }, videoCtx)).toEqual({
      type: "videoCycleInCategory",
      dir: -1,
    });
  });

  it("` / Shift+` → videoStepCategory (跨类跳转)", () => {
    // Shift+` 在美式布局 key 是 "~" 而非 "`"，故按物理键位 code 判定；测试用 code 模拟真实事件。
    expect(dispatch({ code: "Backquote" }, videoCtx)).toEqual({
      type: "videoStepCategory",
      dir: 1,
    });
    expect(dispatch({ code: "Backquote", shiftKey: true }, videoCtx)).toEqual({
      type: "videoStepCategory",
      dir: -1,
    });
  });

  it("Esc → cancel", () => {
    expect(dispatch({ key: "Escape" }, videoCtx)).toEqual({ type: "cancel" });
  });

  it("1-9 → setClassByDigit and video V/B/T switch video tools", () => {
    expect(dispatch({ key: "4" }, videoCtx)).toEqual({ type: "setClassByDigit", idx: 3 });
    expect(dispatch({ key: "v" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "select" });
    expect(dispatch({ key: "b" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "box" });
    expect(dispatch({ key: "T" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "track" });
    expect(dispatch({ key: "2", altKey: true }, videoCtx)).toEqual({
      type: "setVideoTool",
      tool: "track",
    });
    // v0.21.23 · 视频交互式 SAM: S/D 直达 (图片侧 S 是「AI 工具循环」, 视频只有两个 AI 工具)。
    expect(dispatch({ key: "s" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "smart-point" });
    expect(dispatch({ key: "D" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "smart-box" });
    expect(dispatch({ key: "e" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "exemplar" });
    // G / P 与图片侧同键 (图片: G=magic-box, P=polygon)。
    expect(dispatch({ key: "G" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "magic-box" });
    expect(dispatch({ key: "p" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "polygon" });
    // 视频 L 是播放 jog, 不是折线工具 —— 工具栏角标曾谎称 L 能切折线。
    expect(dispatch({ key: "l" }, videoCtx)).toEqual({ type: "videoJogPlayback", dir: 1 });
    expect(dispatch({ key: "w" }, videoCtx)).toEqual({
      type: "setVideoTool",
      tool: "rotated-box",
    });
    expect(dispatch({ key: "f" }, videoCtx)).toEqual({ type: "setVideoTool", tool: "keypoint" });
  });

  it("pending popover owns video-mode keys except Esc cancel", () => {
    const pendingVideoCtx: Partial<DispatchCtx> = { videoMode: true, pendingActive: true };
    expect(dispatch({ key: "4" }, pendingVideoCtx)).toBeNull();
    expect(dispatch({ key: "Delete" }, pendingVideoCtx)).toBeNull();
    expect(dispatch({ key: "Backspace" }, pendingVideoCtx)).toBeNull();
    expect(dispatch({ key: "Escape" }, pendingVideoCtx)).toEqual({ type: "cancel" });
  });
});

describe("dispatchKey · video sampling grid (v0.10.29)", () => {
  const gridCtx: Partial<DispatchCtx> = { videoMode: true, samplingActive: true };

  it("ArrowLeft / ArrowRight → videoSeekGrid (grid jump)", () => {
    expect(dispatch({ key: "ArrowRight" }, gridCtx)).toEqual({ type: "videoSeekGrid", dir: 1 });
    expect(dispatch({ key: "ArrowLeft" }, gridCtx)).toEqual({ type: "videoSeekGrid", dir: -1 });
  });

  it("Shift + ArrowLeft / ArrowRight → videoMicroStep ±1 (escape hatch)", () => {
    expect(dispatch({ key: "ArrowRight", shiftKey: true }, gridCtx)).toEqual({
      type: "videoMicroStep",
      dir: 1,
    });
    expect(dispatch({ key: "ArrowLeft", shiftKey: true }, gridCtx)).toEqual({
      type: "videoMicroStep",
      dir: -1,
    });
  });

  it(", / . do not act as micro-step aliases when no track is selected", () => {
    expect(dispatch({ key: "." }, gridCtx)).toBeNull();
    expect(dispatch({ key: "," }, gridCtx)).toBeNull();
  });

  it(", / . → videoSeekKeyframe when a track is selected", () => {
    const selectedTrackCtx: Partial<DispatchCtx> = {
      videoMode: true,
      samplingActive: true,
      hasSelectedVideoTrack: true,
    };
    expect(dispatch({ key: "." }, selectedTrackCtx)).toEqual({ type: "videoSeekKeyframe", dir: 1 });
    expect(dispatch({ key: "," }, selectedTrackCtx)).toEqual({
      type: "videoSeekKeyframe",
      dir: -1,
    });
  });

  it("Alt + ArrowLeft / ArrowRight are not video navigation aliases", () => {
    const altCtx: Partial<DispatchCtx> = {
      videoMode: true,
      samplingActive: true,
      hasSelectedVideoTrack: true,
    };
    expect(dispatch({ key: "ArrowRight", altKey: true }, altCtx)).toBeNull();
    expect(dispatch({ key: "ArrowLeft", altKey: true }, altCtx)).toBeNull();
    expect(dispatch({ key: "ArrowRight", altKey: true }, gridCtx)).toBeNull();
    expect(dispatch({ key: "ArrowLeft", altKey: true }, gridCtx)).toBeNull();
    expect(dispatch({ key: "ArrowRight", altKey: true, shiftKey: true }, gridCtx)).toBeNull();
  });

  it("sampling-off (step=1) keeps the simplified keymap", () => {
    const offCtx: Partial<DispatchCtx> = { videoMode: true, samplingActive: false };
    expect(dispatch({ key: "ArrowRight" }, offCtx)).toEqual({ type: "videoSeek", delta: 1 });
    expect(dispatch({ key: "ArrowLeft" }, offCtx)).toEqual({ type: "videoSeek", delta: -1 });
    // Shift 变体不再顺延为逐帧 seek（Shift+←/→ 仅在采样开启时是源帧微调）。
    expect(dispatch({ key: "ArrowRight", shiftKey: true }, offCtx)).toBeNull();
    expect(dispatch({ key: "." }, offCtx)).toBeNull();
  });
});

describe("dispatchKey · 属性 hotkey 绑定 (D.1)", () => {
  it("无选中按 1 → setClassByDigit (保留原行为)", () => {
    expect(dispatch({ key: "1" }, { hasSelection: false })).toEqual({
      type: "setClassByDigit",
      idx: 0,
    });
  });

  it("选中态 + boolean hotkey 命中 → setAttribute toggle", () => {
    const lookup = (digit: string) =>
      digit === "2" ? { key: "occluded", type: "boolean" as const, currentValue: false } : null;
    expect(dispatch({ key: "2" }, { hasSelection: true, attributeHotkey: lookup })).toEqual({
      type: "setAttribute",
      key: "occluded",
      value: true,
    });
    // current=true 时 toggle 为 false
    const lookupTrue = (digit: string) =>
      digit === "2" ? { key: "occluded", type: "boolean" as const, currentValue: true } : null;
    expect(dispatch({ key: "2" }, { hasSelection: true, attributeHotkey: lookupTrue })).toEqual({
      type: "setAttribute",
      key: "occluded",
      value: false,
    });
  });

  it("选中态 + select hotkey 命中 → setAttribute cycle", () => {
    const lookup = (digit: string) =>
      digit === "3"
        ? {
            key: "orientation",
            type: "select" as const,
            options: ["north", "south", "east", "west"],
            currentValue: "north",
          }
        : null;
    expect(dispatch({ key: "3" }, { hasSelection: true, attributeHotkey: lookup })).toEqual({
      type: "setAttribute",
      key: "orientation",
      value: "south",
    });
  });

  it("select cycle 至末尾绕回首项", () => {
    const lookup = (digit: string) =>
      digit === "3"
        ? {
            key: "orientation",
            type: "select" as const,
            options: ["a", "b", "c"],
            currentValue: "c",
          }
        : null;
    expect(dispatch({ key: "3" }, { hasSelection: true, attributeHotkey: lookup })).toEqual({
      type: "setAttribute",
      key: "orientation",
      value: "a",
    });
  });

  it("选中态但 hotkey 未命中 → fallback 到 setClassByDigit", () => {
    const lookup = () => null;
    expect(dispatch({ key: "1" }, { hasSelection: true, attributeHotkey: lookup })).toEqual({
      type: "setClassByDigit",
      idx: 0,
    });
  });
});

describe("v0.10.5 M4-β · shape 状态位快捷键", () => {
  it("无选中 [ → thresholdAdjust -0.05", () => {
    expect(dispatch({ key: "[" })).toEqual({ type: "thresholdAdjust", delta: -0.05 });
  });
  it("无选中 ] → thresholdAdjust +0.05", () => {
    expect(dispatch({ key: "]" })).toEqual({ type: "thresholdAdjust", delta: 0.05 });
  });
  it("选中态 [ → bumpZOrder -1", () => {
    expect(dispatch({ key: "[" }, { hasSelection: true })).toEqual({
      type: "bumpZOrder",
      delta: -1,
    });
  });
  it("选中态 ] → bumpZOrder +1", () => {
    expect(dispatch({ key: "]" }, { hasSelection: true })).toEqual({
      type: "bumpZOrder",
      delta: 1,
    });
  });
  it("选中态 L → toggleShapeFlag is_locked", () => {
    expect(dispatch({ key: "l" }, { hasSelection: true })).toEqual({
      type: "toggleShapeFlag",
      flag: "is_locked",
    });
    expect(dispatch({ key: "L" }, { hasSelection: true })).toEqual({
      type: "toggleShapeFlag",
      flag: "is_locked",
    });
  });
  it("选中态 H → toggleShapeFlag is_hidden", () => {
    expect(dispatch({ key: "h" }, { hasSelection: true })).toEqual({
      type: "toggleShapeFlag",
      flag: "is_hidden",
    });
  });
  it("无选中 H/O → null（不消费）", () => {
    expect(dispatch({ key: "h" })).toBeNull();
    expect(dispatch({ key: "o" })).toBeNull();
  });
  // v0.10.28 · 无选中 L → 折线工具（与选中态 L=lock 互补）。
  it("无选中 L → setTool polyline", () => {
    expect(dispatch({ key: "l" })).toEqual({ type: "setTool", tool: "polyline" });
  });
  it("F → setTool keypoint (v0.10.28; K 已被 cycleUser 占用)", () => {
    expect(dispatch({ key: "f" })).toEqual({ type: "setTool", tool: "keypoint" });
    expect(dispatch({ key: "F" })).toEqual({ type: "setTool", tool: "keypoint" });
  });
});

describe("自定义命令派发不依赖旧键位", () => {
  const cases: [string, HotkeyAction, Partial<DispatchCtx>][] = [
    ["common.task.next", { type: "navigateTask", dir: "next" }, {}],
    ["common.task.prev", { type: "navigateTask", dir: "prev" }, {}],
    ...(
      [
        ["select", "select"],
        ["box", "box"],
        ["rotatedBox", "rotated-box"],
        ["polygon", "polygon"],
        ["polyline", "polyline"],
        ["keypoint", "keypoint"],
        ["mask", "mask"],
        ["aiCycle", "ai-cycle"],
        ["magicBox", "magic-box"],
      ] as const
    ).map(([id, tool]): [string, HotkeyAction, Partial<DispatchCtx>] => [
      `image.tool.${id}`,
      { type: "setTool", tool },
      {},
    ]),
    [
      "image.selection.lock",
      { type: "toggleShapeFlag", flag: "is_locked" },
      { hasSelection: true },
    ],
    [
      "image.selection.hide",
      { type: "toggleShapeFlag", flag: "is_hidden" },
      { hasSelection: true },
    ],
    ...(
      [
        ["select", "select"],
        ["box", "box"],
        ["rotatedBox", "rotated-box"],
        ["keypoint", "keypoint"],
        ["track", "track"],
        ["mask", "mask"],
        ["smartPoint", "smart-point"],
        ["smartBox", "smart-box"],
        ["exemplar", "exemplar"],
        ["magicBox", "magic-box"],
        ["polygon", "polygon"],
      ] as const
    ).map(([id, tool]): [string, HotkeyAction, Partial<DispatchCtx>] => [
      `video.tool.${id}`,
      { type: "setVideoTool", tool },
      { videoMode: true },
    ]),
    [
      "video.track.locked",
      { type: "videoToggleLockedTrack" },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
    [
      "video.track.hidden",
      { type: "videoToggleHiddenTrack" },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
    [
      "video.track.outside",
      { type: "videoToggleOutside" },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
    [
      "video.track.occluded",
      { type: "videoToggleOccluded" },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
    ["video.track.bookmark", { type: "videoToggleBookmark" }, { videoMode: true }],
    ["video.frame.next", { type: "videoSeek", delta: 1 }, { videoMode: true }],
    ["video.frame.prev", { type: "videoSeek", delta: -1 }, { videoMode: true }],
    [
      "video.frame.next",
      { type: "videoSeekGrid", dir: 1 },
      { videoMode: true, samplingActive: true },
    ],
    [
      "video.frame.prev",
      { type: "videoSeekGrid", dir: -1 },
      { videoMode: true, samplingActive: true },
    ],
    [
      "video.frame.micro.next",
      { type: "videoMicroStep", dir: 1 },
      { videoMode: true, samplingActive: true },
    ],
    [
      "video.frame.micro.prev",
      { type: "videoMicroStep", dir: -1 },
      { videoMode: true, samplingActive: true },
    ],
    [
      "video.track.keyframe.next",
      { type: "videoSeekKeyframe", dir: 1 },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
    [
      "video.track.keyframe.prev",
      { type: "videoSeekKeyframe", dir: -1 },
      { videoMode: true, hasSelectedVideoTrack: true },
    ],
  ];
  const modifiers: ShortcutBinding["modifiers"][] = [
    [],
    ["mod"],
    ["alt"],
    ["shift"],
    ["alt", "shift"],
  ];
  it.each(cases)("%s 的裸键与修饰组合都能执行且旧键失效", (id, expected, context) => {
    for (const mods of modifiers) {
      const domain = getEditableCommand(id)!.domain;
      const effective = resolveEffectiveCommands({
        common: {},
        image: {},
        video: {},
        [domain]: { [id]: [{ key: "x", modifiers: mods }] },
      });
      const dispatchBinding = (binding: ShortcutBinding) => {
        const event = new KeyboardEvent("keydown", {
          key: binding.key,
          ctrlKey: binding.modifiers.includes("mod"),
          altKey: binding.modifiers.includes("alt"),
          shiftKey: binding.modifiers.includes("shift"),
        });
        return dispatchKey(event, {
          ...baseCtx,
          ...context,
          matchCommand: createCommandEventMatcher(
            event,
            effective,
            context.videoMode ? "video" : "image",
          ).match,
        });
      };
      expect(dispatchBinding({ key: "x", modifiers: mods }), mods.join("+")).toEqual(expected);
      for (const binding of getEditableCommand(id)!.bindings) {
        expect(dispatchBinding(binding), "旧键应停止该命令").not.toEqual(expected);
      }
    }
  });

  it.each([
    ["image.selection.hide", {}],
    ["image.tool.polyline", { hasSelection: true }],
    ["video.track.locked", { videoMode: true }],
    ["video.frame.micro.next", { videoMode: true, samplingActive: false }],
    ["video.tool.smartBox", { videoMode: true, selectedPrediction: { id: "pending" } }],
    ["video.tool.box", { videoMode: true, pendingActive: true }],
    ["image.tool.box", { pendingActive: true }],
  ] as [string, Partial<DispatchCtx>][])("%s 保留状态保护", (id, context) => {
    expect(
      dispatch({ key: "x" }, { ...context, matchCommand: (command) => command === id }),
    ).toBeNull();
  });
});
