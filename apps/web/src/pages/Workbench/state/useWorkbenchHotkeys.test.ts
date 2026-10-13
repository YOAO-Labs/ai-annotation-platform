import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { createElement, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { describe, expect, it, vi } from "vitest";
import { isWorkbenchInputFocused, useWorkbenchHotkeys } from "./useWorkbenchHotkeys";
import type { UseMaskEditorReturn } from "./useMaskEditor";
import { isSamCandidateHotkeyBlocked } from "./hotkeys";
import { resolveEffectiveCommands } from "./hotkeyBindings";

function makeArgs(overrides: Partial<Parameters<typeof useWorkbenchHotkeys>[0]> = {}) {
  return {
    s: {
      tool: "select",
      selectedId: null,
      selectedIds: [],
      pendingDrawing: null,
      editingClass: null,
      setTool: vi.fn(),
    },
    history: { pushBatch: vi.fn(), undo: vi.fn(), redo: vi.fn() },
    classes: [],
    currentProject: null,
    annotationsRef: { current: [] },
    batchChanging: false,
    setBatchChanging: vi.fn(),
    showHotkeys: false,
    navigateTask: vi.fn(),
    smartNext: vi.fn(),
    setFitTick: vi.fn(),
    recordRecentClass: vi.fn(),
    handleDeleteBox: vi.fn(),
    handleBatchDelete: vi.fn(),
    handleStartChangeClass: vi.fn(),
    handleStartBatchChangeClass: vi.fn(),
    handleSubmitTask: vi.fn(),
    handleAcceptPrediction: vi.fn(),
    handleUpdateAttributes: vi.fn(),
    aiBoxes: [],
    setShowHotkeys: vi.fn(),
    clipboard: {
      hasClipboard: false,
      copySelection: vi.fn(),
      paste: vi.fn(),
      duplicateSelection: vi.fn(),
    },
    pushToast: vi.fn(),
    stageGeom: { imgW: 1, imgH: 1 },
    polygonDraftPoints: [],
    setPolygonDraftPoints: vi.fn(),
    submitPolygon: vi.fn(),
    submitPolyline: vi.fn(),
    updateMutation: { mutate: vi.fn() },
    taskId: undefined,
    ...overrides,
  } as unknown as Parameters<typeof useWorkbenchHotkeys>[0];
}

describe("notification detail modal input isolation", () => {
  it.each(["后台任务详情", "打开通知目标"])(
    "%s shields selected annotations until closed",
    (title) => {
      const args = makeArgs();
      args.s.selectedId = "box";
      args.s.selectedIds = ["box"];
      args.annotationsRef.current = [{ id: "box" }] as never;
      renderHook(() => useWorkbenchHotkeys(args));
      function Detail() {
        const [open, setOpen] = useState(true);
        return createElement(Modal, {
          open,
          title,
          onClose: () => setOpen(false),
          children: createElement("p", null, "通知详情"),
        });
      }
      render(createElement(Detail));
      const close = screen.getByRole("button", { name: "关闭" });
      close.focus();
      fireEvent.keyDown(close, { key: "Delete" });
      expect(args.handleDeleteBox).not.toHaveBeenCalled();
      fireEvent.click(close);
      fireEvent.keyDown(window, { key: "Delete" });
      expect(args.handleDeleteBox).toHaveBeenCalledWith("box");
    },
  );
});

function makeMaskArgs(
  editorOverrides: Partial<UseMaskEditorReturn> = {},
  overrides: Partial<Parameters<typeof useWorkbenchHotkeys>[0]> = {},
) {
  const editor = {
    active: true,
    dirty: true,
    phase: "dirty",
    canUndo: true,
    canRedo: true,
    operationPreview: null,
    instanceOperationPreview: null,
    setMode: vi.fn(),
    undo: vi.fn(),
    redo: vi.fn(),
    confirmOperation: vi.fn(),
    cancelOperation: vi.fn(),
    ...editorOverrides,
  } as unknown as UseMaskEditorReturn;
  const primary = vi.fn();
  const secondary = vi.fn();
  const args = makeArgs({
    maskEditor: editor,
    onMaskPrimaryAction: primary,
    onMaskSecondaryAction: secondary,
    ...overrides,
  });
  args.s.tool = "mask";
  return { args, editor, primary, secondary };
}

describe("video tool admission", () => {
  it("flushes the active polygon stroke before Enter reads the existing draft", () => {
    let points: [number, number][] = [[0, 0]];
    const args = makeArgs({
      polygonDraftPoints: points,
      polygonDraft: {
        points,
        addPoint: vi.fn(),
        close: vi.fn(),
        cancel: vi.fn(),
        autoPoints: {
          getPoints: () => points,
          append: vi.fn(),
          beforeKey: {
            current: () => {
              points = [
                [0, 0],
                [1, 0],
                [1, 1],
              ];
            },
          },
        },
      },
    });
    args.s.tool = "polygon";
    const view = renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(args.submitPolygon).toHaveBeenCalledWith([
      [0, 0],
      [1, 0],
      [1, 1],
    ]);
    view.unmount();
  });
  it.each([
    ["b", "box"],
    ["p", "polygon"],
    ["m", "mask"],
    ["t", "track"],
    ["v", "select"],
  ])("%s keeps its explicit target and goes through the guarded command", (key, tool) => {
    const requestVideoTool = vi.fn();
    const args = makeArgs({ videoMode: true, requestVideoTool });
    args.s.videoTool = "track";
    args.s.setVideoTool = vi.fn();
    const view = renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })));
    expect(requestVideoTool).toHaveBeenCalledWith(tool);
    expect(args.s.setVideoTool).not.toHaveBeenCalled();
    view.unmount();
  });
});

describe("SAM global hotkey fallback", () => {
  it.each([false, true])("yields native button navigation in videoMode=%s", (videoMode) => {
    const args = makeArgs({ videoMode });
    args.s.tool = "smart-point";
    args.s.videoTool = "smart-point";
    const view = renderHook(() => useWorkbenchHotkeys(args));
    const button = document.createElement("button");
    document.body.append(button);
    for (const key of ["Tab", "Enter", "Escape", "r"]) {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      act(() => button.dispatchEvent(event));
      expect(event.defaultPrevented).toBe(false);
    }
    expect(args.s.setTool).not.toHaveBeenCalled();
    button.remove();
    view.unmount();
  });

  it("keeps a popup-closing Esc out of the background after SAM capture yields", () => {
    const args = makeArgs();
    args.s.tool = "smart-point";
    const capture = (event: KeyboardEvent) => {
      isSamCandidateHotkeyBlocked(event);
    };
    window.addEventListener("keydown", capture, true);
    const view = renderHook(() => useWorkbenchHotkeys(args));
    const popup = document.createElement("div");
    popup.setAttribute("role", "dialog");
    document.body.append(popup);
    popup.addEventListener("keydown", () => popup.remove());
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => popup.dispatchEvent(event));
    expect(args.s.setTool).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
    window.removeEventListener("keydown", capture, true);
    view.unmount();
  });
});

describe("Mask keyboard action ownership", () => {
  it.each([
    ["idle", { active: false, dirty: false, phase: "idle" }],
    ["clean", { dirty: false, phase: "ready" }],
    ["dirty", {}],
    ["saving", { phase: "saving" }],
    ["operation preview", { operationPreview: { id: 1 } }],
    ["instance preview", { instanceOperationPreview: { id: 2 } }],
  ] as const)("%s delegates Enter/Esc to the current action owner", (_name, state) => {
    const { args, editor, primary, secondary } = makeMaskArgs(
      state as Partial<UseMaskEditorReturn>,
    );
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", cancelable: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", cancelable: true }));
    });
    expect(primary).toHaveBeenCalledTimes(1);
    expect(secondary).toHaveBeenCalledTimes(1);
    expect(editor.confirmOperation).not.toHaveBeenCalled();
    expect(editor.cancelOperation).not.toHaveBeenCalled();
    expect(args.s.setTool).not.toHaveBeenCalled();
  });

  it.each(["maskTaskReadOnly", "maskPixelReadOnly"] as const)(
    "%s keeps action eligibility with the owner while blocking pixel commands",
    (gate) => {
      const { args, editor, primary, secondary } = makeMaskArgs({}, { [gate]: true });
      renderHook(() => useWorkbenchHotkeys(args));
      act(() => {
        for (const key of ["b", "e", "Enter", "Escape"]) {
          window.dispatchEvent(new KeyboardEvent("keydown", { key, cancelable: true }));
        }
        window.dispatchEvent(
          new KeyboardEvent("keydown", { key: "z", ctrlKey: true, cancelable: true }),
        );
      });
      expect(primary).toHaveBeenCalledTimes(1);
      expect(secondary).toHaveBeenCalledTimes(1);
      expect(editor.setMode).not.toHaveBeenCalled();
      expect(editor.undo).not.toHaveBeenCalled();
      expect(args.history.undo).not.toHaveBeenCalled();
      expect(args.s.setTool).not.toHaveBeenCalled();
    },
  );

  it("keeps brush, erase and undo/redo on the editable mask session", () => {
    const { args, editor } = makeMaskArgs();
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      for (const init of [
        { key: "b" },
        { key: "e" },
        { key: "z", ctrlKey: true },
        { key: "z", metaKey: true, shiftKey: true },
        { key: "y", ctrlKey: true },
      ]) {
        window.dispatchEvent(new KeyboardEvent("keydown", { ...init, cancelable: true }));
      }
    });
    expect(editor.setMode).toHaveBeenNthCalledWith(1, "brush");
    expect(editor.setMode).toHaveBeenNthCalledWith(2, "erase");
    expect(editor.undo).toHaveBeenCalledTimes(1);
    expect(editor.redo).toHaveBeenCalledTimes(2);
    expect(args.history.undo).not.toHaveBeenCalled();
    expect(args.history.redo).not.toHaveBeenCalled();
    expect(args.s.setTool).not.toHaveBeenCalled();
  });

  it("falls back to annotation history when the mask session has no local command", () => {
    const { args, editor } = makeMaskArgs({
      active: true,
      dirty: false,
      phase: "ready",
      canUndo: false,
      canRedo: false,
    });
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: "z", ctrlKey: true, cancelable: true }),
      );
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: "y", ctrlKey: true, cancelable: true }),
      );
    });
    expect(editor.undo).not.toHaveBeenCalled();
    expect(editor.redo).not.toHaveBeenCalled();
    expect(args.history.undo).toHaveBeenCalledTimes(1);
    expect(args.history.redo).toHaveBeenCalledTimes(1);
  });

  it("comparison freezes Mask commands without falling through to a tool change", () => {
    const { args, editor, primary, secondary } = makeMaskArgs({}, { maskInteractionFrozen: true });
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      for (const key of ["Enter", "Escape", "b", "e", "z"]) {
        window.dispatchEvent(
          new KeyboardEvent("keydown", { key, ctrlKey: key === "z", cancelable: true }),
        );
      }
    });
    expect(primary).not.toHaveBeenCalled();
    expect(secondary).not.toHaveBeenCalled();
    expect(editor.setMode).not.toHaveBeenCalled();
    expect(editor.undo).not.toHaveBeenCalled();
    expect(args.history.undo).not.toHaveBeenCalled();
    expect(args.s.setTool).not.toHaveBeenCalled();
  });

  it.each(["hidden", "aria-hidden", "closed", "display-none", "hidden-parent"])(
    "%s popup does not keep blocking the current Mask action",
    (state) => {
      const { args, primary } = makeMaskArgs();
      renderHook(() => useWorkbenchHotkeys(args));
      const parent = document.createElement("div");
      const popup = parent.appendChild(document.createElement("div"));
      popup.setAttribute("role", "dialog");
      if (state === "hidden") popup.hidden = true;
      if (state === "aria-hidden") popup.setAttribute("aria-hidden", "true");
      if (state === "closed") popup.dataset.state = "closed";
      if (state === "display-none") popup.style.display = "none";
      if (state === "hidden-parent") parent.style.display = "none";
      document.body.append(parent);
      try {
        act(() =>
          window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", cancelable: true })),
        );
        expect(primary).toHaveBeenCalledTimes(1);
      } finally {
        parent.remove();
      }
    },
  );

  it.each([
    "input",
    "textarea",
    "select",
    "contenteditable",
    "combobox",
    "listbox",
    "menu",
    "dialog",
    "button",
  ])("%s receives its own keys without a background Mask action", (kind) => {
    const { args, primary, secondary } = makeMaskArgs();
    renderHook(() => useWorkbenchHotkeys(args));
    const element = document.createElement(
      ["input", "textarea", "select", "button"].includes(kind) ? kind : "div",
    );
    if (kind === "contenteditable") element.setAttribute("contenteditable", "true");
    else if (!["input", "textarea", "select", "button"].includes(kind))
      element.setAttribute("role", kind);
    const target = ["contenteditable", "combobox", "listbox", "menu", "dialog", "button"].includes(
      kind,
    )
      ? element.appendChild(document.createElement("span"))
      : element;
    const ownKeys = vi.fn();
    element.addEventListener("keydown", ownKeys);
    document.body.append(element);
    try {
      const keys = kind === "button" ? ["Enter"] : ["Enter", "Escape"];
      const events = keys.map(
        (key) => new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
      );
      act(() => events.forEach((event) => target.dispatchEvent(event)));
      expect(ownKeys).toHaveBeenCalledTimes(keys.length);
      expect(events.every((event) => !event.defaultPrevented)).toBe(true);
      expect(primary).not.toHaveBeenCalled();
      expect(secondary).not.toHaveBeenCalled();
      expect(args.s.setTool).not.toHaveBeenCalled();
    } finally {
      element.remove();
    }
  });

  it.each([
    ["long press", { repeat: true }],
    ["IME composition", { isComposing: true }],
    ["IME legacy key", { keyCode: 229 }],
    ["already handled", {}],
  ] as const)("ignores %s without falling through to generic shortcuts", (name, init) => {
    const { args, editor, primary, secondary } = makeMaskArgs();
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      for (const key of ["Enter", "Escape", "b", "e", "z"]) {
        const event = new KeyboardEvent("keydown", {
          ...init,
          key,
          ctrlKey: key === "z",
          cancelable: true,
        });
        if (name === "already handled") event.preventDefault();
        window.dispatchEvent(event);
      }
    });
    expect(primary).not.toHaveBeenCalled();
    expect(secondary).not.toHaveBeenCalled();
    expect(editor.setMode).not.toHaveBeenCalled();
    expect(editor.undo).not.toHaveBeenCalled();
    expect(args.history.undo).not.toHaveBeenCalled();
    expect(args.s.setTool).not.toHaveBeenCalled();
  });

  it("Esc closes an unfocused popup without also exiting the Mask tool", () => {
    const { args, primary, secondary } = makeMaskArgs();
    renderHook(() => useWorkbenchHotkeys(args));
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.append(menu);
    const close = vi.fn(() => menu.remove());
    document.addEventListener("keydown", close);
    try {
      act(() =>
        document.body.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
        ),
      );
      expect(close).toHaveBeenCalledTimes(1);
      expect(primary).not.toHaveBeenCalled();
      expect(secondary).not.toHaveBeenCalled();
      expect(args.s.setTool).not.toHaveBeenCalled();
    } finally {
      menu.remove();
      document.removeEventListener("keydown", close);
    }
  });
});

describe("useWorkbenchHotkeys module", () => {
  it("连续模式的 Esc 先取消手工草稿，再退出模式，不受已保存选中对象阻挡", () => {
    const cancelManualDrawing = vi.fn().mockReturnValueOnce(true).mockReturnValue(false);
    const args = makeArgs({ cancelManualDrawing });
    args.s.tool = "keypoint";
    args.s.selectedId = "saved-object";
    args.s.setSelectedId = vi.fn();
    args.s.setContinuousCreation = vi.fn();
    args.s.continuousCreation = {
      projectId: "project",
      tool: "keypoint",
      toolUnitId: "keypoint",
      className: "car",
    };
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(args.s.setContinuousCreation).not.toHaveBeenCalled();
    expect(args.s.setTool).not.toHaveBeenCalled();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(args.s.setContinuousCreation).toHaveBeenCalledWith(null);
    expect(args.s.setTool).toHaveBeenCalledWith("select");
    expect(args.s.setSelectedId).not.toHaveBeenCalled();
  });

  it("更多工具菜单的 Enter/Esc 不提交或取消背景多边形", () => {
    const args = makeArgs({
      polygonDraftPoints: [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    });
    args.s.tool = "polygon";
    args.s.setPendingDrawing = vi.fn();
    renderHook(() => useWorkbenchHotkeys(args));
    const menu = document.createElement("div");
    menu.dataset.workbenchToolMenu = "";
    menu.dataset.state = "open";
    menu.setAttribute("role", "menu");
    const item = document.createElement("button");
    item.setAttribute("role", "menuitem");
    menu.append(item);
    document.body.append(menu);
    const menuKey = vi.fn();
    item.addEventListener("keydown", menuKey);
    try {
      act(() => {
        item.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        item.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      expect(args.submitPolygon).not.toHaveBeenCalled();
      expect(args.setPolygonDraftPoints).not.toHaveBeenCalled();
      expect(menuKey).toHaveBeenCalledTimes(2);
    } finally {
      menu.remove();
    }
  });

  it("在画布按住 Space 后进入更多工具菜单再松开，只释放按住态", () => {
    const togglePlayback = vi.fn();
    const args = makeArgs({
      videoMode: true,
      videoControlsRef: { current: { togglePlayback } } as never,
    });
    const view = renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: " " })));
    expect(view.result.current.spacePan).toBe(true);
    const trigger = document.createElement("button");
    trigger.dataset.workbenchToolMenu = "";
    document.body.append(trigger);
    try {
      act(() => trigger.dispatchEvent(new KeyboardEvent("keyup", { key: " ", bubbles: true })));
      expect(view.result.current.spacePan).toBe(false);
      expect(togglePlayback).not.toHaveBeenCalled();
    } finally {
      trigger.remove();
    }
  });
  it.each(["select", "combobox", "listbox"])("%s 的 A/D 选项定位不触发候选写入", (kind) => {
    const args = makeArgs({ videoMode: true, handleRejectPrediction: vi.fn() });
    args.s.selectedId = "candidate";
    args.s.videoFrameIndex = 0;
    args.aiBoxes = [
      {
        id: "candidate",
        predictionId: "p",
        shapeIndex: 0,
        geometry: { type: "video_bbox", frame_index: 0 },
      },
    ] as never;
    renderHook(() => useWorkbenchHotkeys(args));
    const element = document.createElement(kind === "select" ? "select" : "button");
    if (kind !== "select") element.setAttribute("role", kind);
    document.body.append(element);
    element.focus();
    act(() => {
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "d", bubbles: true }));
    });
    expect(args.handleAcceptPrediction).not.toHaveBeenCalled();
    expect(args.handleRejectPrediction).not.toHaveBeenCalled();
    element.remove();
  });
  it("SAM 补类弹层打开时 A/D 不触发候选或切工具", () => {
    const args = makeArgs({ videoMode: true, classPickerActive: true });
    args.s.setVideoTool = vi.fn();
    renderHook(() => useWorkbenchHotkeys(args));
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "d" }));
    });
    expect(args.handleAcceptPrediction).not.toHaveBeenCalled();
    expect(args.s.setVideoTool).not.toHaveBeenCalled();
  });
  it("当前帧候选键只调用公共决策，不提前改选；长按不提交", () => {
    const args = makeArgs({ videoMode: true });
    args.s.selectedId = "candidate";
    args.s.videoFrameIndex = 0;
    args.s.setSelectedId = vi.fn();
    args.aiBoxes = [
      {
        id: "candidate",
        predictionId: "prediction",
        shapeIndex: 0,
        geometry: { type: "video_bbox", frame_index: 0 },
      },
    ] as never;
    const view = renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" })));
    expect(args.handleAcceptPrediction).toHaveBeenCalledTimes(1);
    expect(args.s.setSelectedId).not.toHaveBeenCalled();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "a", repeat: true })));
    expect(args.handleAcceptPrediction).toHaveBeenCalledTimes(1);
    args.s.videoFrameIndex = 10;
    view.rerender();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" })));
    expect(args.handleAcceptPrediction).toHaveBeenCalledTimes(1);
  });
  it("exports the hook", () => {
    expect(typeof useWorkbenchHotkeys).toBe("function");
  });

  it("does not block hotkeys while the video timeline range is focused", () => {
    const timeline = document.createElement("input");
    timeline.type = "range";
    timeline.className = "video-timeline-range";

    const textInput = document.createElement("input");
    textInput.type = "text";

    expect(isWorkbenchInputFocused(timeline)).toBe(false);
    expect(isWorkbenchInputFocused(textInput)).toBe(true);
    const sceneTimeline = document.createElement("section");
    sceneTimeline.dataset.sceneTimeline = "";
    const playButton = document.createElement("button");
    sceneTimeline.append(playButton);
    expect(isWorkbenchInputFocused(playButton)).toBe(true);
  });

  it("does not send single-key annotation shortcuts from docking tabs or menus", () => {
    for (const role of ["tab", "menu", "menuitem"]) {
      const element = document.createElement("div");
      element.setAttribute("role", role);
      const child = document.createElement("span");
      element.append(child);
      expect(isWorkbenchInputFocused(child)).toBe(true);
    }
    const control = document.createElement("button");
    control.dataset.workbenchLayoutControl = "";
    expect(isWorkbenchInputFocused(control)).toBe(true);
  });

  it("blocks the Mask hotkey with the ToolDock reason", () => {
    const setTool = vi.fn();
    const pushToast = vi.fn();
    const args = makeArgs();
    args.s.setTool = setTool;
    const view = renderHook(() =>
      useWorkbenchHotkeys({ ...args, pushToast, maskToolDisabledReason: "图片超过 Mask 上限" }),
    );

    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "m" })));

    expect(setTool).not.toHaveBeenCalled();
    expect(pushToast).toHaveBeenCalledWith({ msg: "图片超过 Mask 上限", kind: "warning" });
    view.unmount();
  });

  it("disabling clears held space without toggling video playback on release", () => {
    const togglePlayback = vi.fn();
    const args = makeArgs({
      videoMode: true,
      videoControlsRef: { current: { togglePlayback } } as never,
    });
    const view = renderHook(({ disabled }) => useWorkbenchHotkeys({ ...args, disabled }), {
      initialProps: { disabled: false },
    });
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: " " })));
    expect(view.result.current.spacePan).toBe(true);
    view.rerender({ disabled: true });
    expect(view.result.current.spacePan).toBe(false);
    act(() => window.dispatchEvent(new KeyboardEvent("keyup", { key: " " })));
    view.rerender({ disabled: false });
    act(() => window.dispatchEvent(new KeyboardEvent("keyup", { key: " " })));
    expect(togglePlayback).not.toHaveBeenCalled();

    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: " " })));
    act(() => window.dispatchEvent(new KeyboardEvent("keyup", { key: " " })));
    expect(togglePlayback).toHaveBeenCalledTimes(1);
  });

  it("flushes an existing nudge once when disabled and restores hotkeys on close", () => {
    const args = makeArgs();
    args.s.selectedId = "box";
    args.s.selectedIds = ["box"];
    args.annotationsRef.current = [
      {
        id: "box",
        geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      },
    ] as never;
    args.stageGeom = { imgW: 100, imgH: 100 };
    const view = renderHook(({ disabled }) => useWorkbenchHotkeys({ ...args, disabled }), {
      initialProps: { disabled: false },
    });
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" })));
    expect(view.result.current.nudgeMap.get("box")?.x).toBeCloseTo(0.11);
    view.rerender({ disabled: true });
    expect(args.updateMutation.mutate).toHaveBeenCalledTimes(1);
    expect(args.history.pushBatch).toHaveBeenCalledTimes(1);
    expect(view.result.current.nudgeMap.size).toBe(0);
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));
    expect(args.handleDeleteBox).not.toHaveBeenCalled();
    view.rerender({ disabled: false });
    act(() => window.dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowRight" })));
    expect(args.updateMutation.mutate).toHaveBeenCalledTimes(1);
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));
    expect(args.handleDeleteBox).toHaveBeenCalledWith("box");
  });

  it("settings button events reach the button without submitting a polygon", () => {
    const args = makeArgs({
      polygonDraftPoints: [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    });
    args.s.tool = "polygon";
    renderHook(() => useWorkbenchHotkeys(args));
    const settings = document.createElement("button");
    settings.dataset.workbenchSettings = "";
    settings.dataset.state = "open";
    document.body.append(settings);
    const buttonHandler = vi.fn();
    settings.addEventListener("keydown", buttonHandler);
    act(() =>
      settings.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })),
    );
    expect(buttonHandler).toHaveBeenCalledTimes(1);
    expect(args.submitPolygon).not.toHaveBeenCalled();
    settings.remove();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })));
    expect(args.submitPolygon).toHaveBeenCalledTimes(1);
  });
});

describe("3D 命令域隔离", () => {
  it("图片隐藏键不修改 3D 选中对象，common 改绑仍可切题", () => {
    const args = makeArgs({
      stage: "threed",
      shortcutsEffective: resolveEffectiveCommands({
        common: { "common.task.next": [{ key: "x", modifiers: [] }] },
        image: { "image.selection.hide": [{ key: "i", modifiers: [] }] },
        video: {},
      }),
      handlePatchShapeFlag: vi.fn(),
    });
    args.s.selectedId = "box-3d";
    args.annotationsRef.current = [
      { id: "box-3d", is_hidden: false },
    ] as typeof args.annotationsRef.current;
    const view = renderHook(() => useWorkbenchHotkeys(args));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "i", bubbles: true })));
    expect(args.handlePatchShapeFlag).not.toHaveBeenCalled();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "x", bubbles: true })));
    expect(args.navigateTask).toHaveBeenCalledWith("next");
    view.unmount();
  });
});
