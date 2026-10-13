import { afterEach, describe, expect, it, vi } from "vitest";
import { isWorkbenchInteractionBlocked } from "./workbenchInteractionGuards";

afterEach(() => document.body.replaceChildren());

it.each([
  "workbenchAiToolbar",
  "workbenchTrackContext",
  "workbenchTrackerReview",
  "workbenchIssueNavigation",
  "workbenchDiscussion",
])("%s controls keep their input while canvas shortcuts remain available", (marker) => {
  const toolbar = document.createElement("div");
  toolbar.dataset[marker] = "";
  const button = document.createElement("button");
  toolbar.append(button);
  document.body.append(toolbar);
  expect(isWorkbenchInteractionBlocked(new KeyboardEvent("keydown", { key: "Tab" }))).toBe(false);
  const check = vi.fn((event: Event) => {
    expect(isWorkbenchInteractionBlocked(event)).toBe(true);
    expect(event.defaultPrevented).toBe(false);
  });
  button.addEventListener("keydown", check);
  button.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  expect(check).toHaveBeenCalledOnce();
});

it("discussion editors protect Enter before a capture-phase canvas listener runs", () => {
  const discussion = document.createElement("div");
  discussion.dataset.workbenchDiscussion = "";
  const editor = document.createElement("div");
  editor.contentEditable = "true";
  discussion.append(editor);
  document.body.append(discussion);
  const canvasCommit = vi.fn();
  const sendComment = vi.fn();
  const capture = (event: KeyboardEvent) => {
    if (!isWorkbenchInteractionBlocked(event) && event.key === "Enter") canvasCommit();
  };
  window.addEventListener("keydown", capture, true);
  editor.addEventListener("keydown", sendComment);
  try {
    editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(sendComment).toHaveBeenCalledOnce();
    expect(canvasCommit).not.toHaveBeenCalled();
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(canvasCommit).toHaveBeenCalledOnce();
  } finally {
    window.removeEventListener("keydown", capture, true);
  }
});

it.each([
  ["Enter", true],
  [" ", true],
  ["ArrowDown", true],
  ["b", false],
  ["p", false],
  ["Delete", false],
  ["Escape", false],
])("closed menu trigger handles %s without swallowing other canvas shortcuts", (key, blocked) => {
  const trigger = document.createElement("button");
  trigger.dataset.workbenchToolMenuTrigger = "";
  trigger.dataset.state = "closed";
  document.body.append(trigger);
  const check = vi.fn((event: Event) => expect(isWorkbenchInteractionBlocked(event)).toBe(blocked));
  trigger.addEventListener("keydown", check);
  trigger.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  expect(check).toHaveBeenCalledOnce();
});

describe.each([
  "workbenchSettings",
  "workbenchGuide",
  "workbenchToolMenu",
  "workbenchVideoToolConfirm",
  "workbenchIssueCreate",
  "workbenchDiscussion",
])("workbench %s interaction boundary", (marker) => {
  it("blocks background events only while the marker is open", () => {
    const settings = document.createElement("div");
    settings.dataset[marker] = "";
    settings.dataset.state = "open";
    document.body.append(settings);
    const event = new KeyboardEvent("keydown", { key: "Delete" });

    expect(isWorkbenchInteractionBlocked(event)).toBe(true);
    settings.dataset.state = "closed";
    expect(isWorkbenchInteractionBlocked(event)).toBe(false);
    settings.remove();
    expect(isWorkbenchInteractionBlocked(event)).toBe(false);
  });

  it.each(["keydown", "wheel"])(
    "keeps a removed settings source in the %s path without consuming the event",
    (type) => {
      const settings = document.createElement("div");
      settings.dataset[marker] = "";
      settings.dataset.state = "open";
      const button = document.createElement("button");
      settings.append(button);
      document.body.append(settings);
      button.addEventListener(type, () => {
        settings.dataset.state = "closed";
        settings.remove();
      });
      const background = vi.fn((event: Event) => {
        expect(isWorkbenchInteractionBlocked(event)).toBe(true);
        expect(event.defaultPrevented).toBe(false);
        expect(event.cancelBubble).toBe(false);
      });
      window.addEventListener(type, background, { once: true });

      button.dispatchEvent(new Event(type, { bubbles: true, cancelable: true }));
      expect(background).toHaveBeenCalledTimes(1);
    },
  );
});

describe("decision dialog modal boundary", () => {
  it("an open decision dialog blocks canvas shortcuts through its data-modal marker", () => {
    // DecisionDialogHost 的 AlertDialogContent 挂 data-modal;复检守卫的两条识别路径:
    // 事件源自对话框内部(composedPath)与文档里存在 open 态对话框(querySelector)。
    const content = document.createElement("div");
    content.setAttribute("data-modal", "");
    content.setAttribute("data-state", "open");
    content.setAttribute("role", "alertdialog");
    const option = document.createElement("button");
    content.append(option);
    document.body.append(content);
    const inside = vi.fn((event: KeyboardEvent) =>
      expect(isWorkbenchInteractionBlocked(event)).toBe(true),
    );
    option.addEventListener("keydown", inside);

    // review 流的 A 键在对话框聚焦选项按钮时必须被拦截。
    option.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    expect(inside).toHaveBeenCalledOnce();
    expect(isWorkbenchInteractionBlocked(new KeyboardEvent("keydown", { key: "Delete" }))).toBe(
      true,
    );

    // 退场动画期间 data-state="closed":文档级识别解除,事件仍源自节点内部则继续拦截。
    content.dataset.state = "closed";
    expect(isWorkbenchInteractionBlocked(new KeyboardEvent("keydown", { key: "Delete" }))).toBe(
      false,
    );
    option.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    expect(inside).toHaveBeenCalledTimes(2);
  });
});

it.each([
  ["Enter", true],
  [" ", true],
  ["ArrowLeft", true],
  ["ArrowRight", true],
  ["Home", true],
  ["End", true],
  ["b", false],
  ["p", false],
  ["m", false],
  ["Escape", false],
])(
  "video scope controls retain native %s while ordinary tool shortcuts remain available",
  (key, blocked) => {
    const control = document.createElement("button");
    control.dataset.workbenchVideoToolCommand = "";
    document.body.append(control);
    const check = vi.fn((event: Event) =>
      expect(isWorkbenchInteractionBlocked(event)).toBe(blocked),
    );
    control.addEventListener("keydown", check);
    control.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    expect(check).toHaveBeenCalledOnce();
  },
);

describe("shell notification popover interaction boundary", () => {
  it("panel events are fully shielded while open (presence means open)", () => {
    const panel = document.createElement("div");
    panel.dataset.shellPopover = "notifications";
    const button = document.createElement("button");
    panel.append(button);
    document.body.append(panel);
    const check = vi.fn((event: Event) => {
      expect(isWorkbenchInteractionBlocked(event)).toBe(true);
      expect(event.defaultPrevented).toBe(false);
    });
    button.addEventListener("keydown", check);
    button.addEventListener("pointerdown", check);
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "b", bubbles: true }));
    button.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(check).toHaveBeenCalledTimes(3);
  });

  it("background canvas events stay blocked while the panel is mounted, then resume", () => {
    const panel = document.createElement("div");
    panel.dataset.shellPopover = "notifications";
    document.body.append(panel);
    const event = new KeyboardEvent("keydown", { key: "Delete" });
    expect(isWorkbenchInteractionBlocked(event)).toBe(true);
    panel.remove();
    expect(isWorkbenchInteractionBlocked(event)).toBe(false);
  });

  it.each([
    ["Enter", true],
    [" ", true],
    ["ArrowDown", true],
    ["b", false],
    ["Delete", false],
  ])("notification trigger handles %s without swallowing canvas shortcuts", (key, blocked) => {
    const trigger = document.createElement("button");
    trigger.dataset.shellPopoverTrigger = "notifications";
    document.body.append(trigger);
    const check = vi.fn((event: Event) =>
      expect(isWorkbenchInteractionBlocked(event)).toBe(blocked),
    );
    trigger.addEventListener("keydown", check);
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    expect(check).toHaveBeenCalledOnce();
  });
});
