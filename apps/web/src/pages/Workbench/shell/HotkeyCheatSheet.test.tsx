import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HotkeyCheatSheet } from "./HotkeyCheatSheet";
import type { WorkbenchShortcutPreferencesState } from "../state/useWorkbenchShortcutPreferences";
import { EMPTY_OVERRIDES, resolveEffectiveCommands } from "../state/hotkeyBindings";

vi.mock("@/hooks/useMediaQuery", () => ({ useMediaQuery: () => true }));

function makeShortcuts(
  overrides: Partial<WorkbenchShortcutPreferencesState> = {},
): WorkbenchShortcutPreferencesState {
  return {
    overrides: EMPTY_OVERRIDES,
    issues: [],
    opaque: false,
    effective: resolveEffectiveCommands(EMPTY_OVERRIDES),
    get previewEffective() {
      return this.effective;
    },
    loaded: true,
    loadError: null,
    retryLoad: vi.fn(),
    userId: "user-1",
    saveCommand: vi.fn(),
    retrySave: vi.fn(),
    discardPending: vi.fn(),
    pendingKeys: new Set(),
    failedMap: new Map(),
    ...overrides,
  };
}

function mount(props: Partial<Parameters<typeof HotkeyCheatSheet>[0]> = {}) {
  return render(<HotkeyCheatSheet open onClose={vi.fn()} stageKind="image" {...props} />);
}

describe("HotkeyCheatSheet", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders the eight purpose categories and the type filter", () => {
    mount();
    const tabs = screen.getByRole("tablist", { name: "快捷键分类" });
    for (const label of [
      "常用",
      "绘制与工具",
      "选择与编辑",
      "画布与视角",
      "AI 与审核",
      "播放与轨迹",
      "任务与系统",
      "鼠标操作",
    ]) {
      expect(within(tabs).getByRole("tab", { name: label })).toBeVisible();
    }
    expect(screen.getByRole("radio", { name: "当前工作台（图片）" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "全部类型" })).not.toBeChecked();
  });

  it("reserves a secondary tier on both fixed and editable rows to keep row rhythm uniform", () => {
    mount({ shortcuts: makeShortcuts() });
    const rows = [...document.querySelectorAll("[data-hotkey-command]")];
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.querySelector("[data-hotkey-note]")).toBeTruthy();
    }
    expect(
      document.querySelector('[data-hotkey-command="image.undo"] [data-hotkey-note]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-hotkey-command="image.tool.box"] [data-hotkey-note]'),
    ).toBeTruthy();
  });

  it("highlights the matched term in command names and notes", async () => {
    const user = userEvent.setup();
    mount({ shortcuts: makeShortcuts() });
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    await user.type(search, "撤销");
    await waitFor(() =>
      expect(screen.getAllByText("撤销", { selector: "mark" }).length).toBeGreaterThan(0),
    );
    expect(document.querySelectorAll("mark").length).toBeGreaterThan(0);
  });

  it("repeats the shared modifier for alternate fixed keys", async () => {
    const user = userEvent.setup();
    mount({ shortcuts: makeShortcuts(), stageKind: "video" });
    await user.click(screen.getByRole("tab", { name: "选择与编辑" }));
    const row = document.querySelector('[data-hotkey-command="video.delete.track"]');
    expect(row?.textContent).toContain("Ctrl+Backspace");
  });

  it("keeps the overlay unblurred on the 3D stage only", () => {
    const view = mount({ stageKind: "3d" });
    expect(screen.getByTestId("workbench-hotkeys-overlay")).not.toHaveClass(
      "backdrop-blur-overlay",
    );
    view.rerender(<HotkeyCheatSheet open onClose={vi.fn()} stageKind="image" />);
    expect(screen.getByTestId("workbench-hotkeys-overlay")).toHaveClass("backdrop-blur-overlay");
  });

  it("colors each stage chip by workbench stage in the all-types view", async () => {
    const user = userEvent.setup();
    mount({ shortcuts: makeShortcuts() });
    await user.click(screen.getByRole("radio", { name: "全部类型" }));
    const classByStage: Record<string, string> = {
      image: "bg-stage-image-soft",
      video: "bg-stage-video-soft",
      threed: "bg-stage-threed-soft",
    };
    const chips = [...document.querySelectorAll("[data-hotkey-stage]")];
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) {
      const stage = chip.getAttribute("data-hotkey-stage") ?? "";
      expect(chip).toHaveClass(classByStage[stage]);
    }
    const used = new Set(
      chips.map((chip) => classByStage[chip.getAttribute("data-hotkey-stage") ?? ""]),
    );
    expect(used.size).toBeGreaterThan(1);
  });

  it("shows corrected reference content: image Tab flows within category and backquote rows exist", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("tab", { name: "选择与编辑" }));
    expect(screen.getByText("同类流转：下一个")).toBeTruthy();
    expect(screen.getByText("跨类跳转：下一类首个对象")).toBeTruthy();
    expect(screen.queryByText("下一个 user 框（循环）")).toBeNull();
  });

  it("search spans categories and the empty state offers clear and all-type actions", async () => {
    const user = userEvent.setup();
    mount();
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    await user.type(search, "笔刷半径");
    await waitFor(() =>
      expect(screen.getAllByText("笔刷半径", { selector: "mark" }).length).toBeGreaterThan(0),
    );
    await user.clear(search);
    await user.type(search, "不存在的快捷键xyz");
    expect(screen.getByText("没有匹配的快捷键")).toBeTruthy();
    const emptyState = screen.getByText("没有匹配的快捷键").closest("div");
    await user.click(within(emptyState as HTMLElement).getByRole("button", { name: "清空搜索" }));
    expect((search as HTMLInputElement).value).toBe("");
  });

  it("project category and attribute section lives under selection with region semantics", async () => {
    const user = userEvent.setup();
    mount({
      attributeSchema: {
        fields: [
          {
            key: "occluded",
            label: "遮挡",
            type: "boolean",
            hotkey: "7",
          },
        ],
      },
    });
    await user.click(screen.getByRole("tab", { name: "选择与编辑" }));
    expect(screen.getByText("项目类别与属性")).toBeTruthy();
    expect(screen.getByText("1 — 9、0")).toBeTruthy();
    expect(screen.getByText(/遮挡/)).toBeTruthy();
    expect(screen.getByText(/属性快捷键区域聚焦时/)).toBeTruthy();
  });

  it("editable rows expose recording and fixed rows stay read-only", async () => {
    const user = userEvent.setup();
    const saveCommand = vi.fn();
    mount({ shortcuts: makeShortcuts({ saveCommand }) });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const boxRow = document.querySelector('[data-hotkey-command="image.tool.box"]');
    expect(boxRow).toBeTruthy();
    await user.click(boxRow!.querySelector("button")!);
    expect(screen.getAllByText(/按下新组合/).length).toBeGreaterThan(0);
    const polyRow = document.querySelector('[data-hotkey-command="image.polygon.close"]');
    expect(polyRow?.textContent).toContain("固定");
  });

  it("records a candidate binding and confirms through the preference writer", async () => {
    const user = userEvent.setup();
    const saveCommand = vi.fn();
    mount({ shortcuts: makeShortcuts({ saveCommand }) });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const boxRow = document.querySelector('[data-hotkey-command="image.tool.box"]')!;
    await user.click(boxRow.querySelector("button")!);

    fireEvent.keyDown(window, { key: "j", bubbles: true, cancelable: true });
    expect(screen.getByText(/与「循环人工标注」冲突/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "确认" })).toBeNull();

    fireEvent.keyDown(window, { key: "Escape", bubbles: true, cancelable: true });
    expect(screen.queryByText(/按下新组合/)).toBeNull();

    await user.click(boxRow.querySelector("button")!);
    fireEvent.keyDown(window, { key: "i", ctrlKey: true, bubbles: true, cancelable: true });
    await user.click(screen.getByRole("button", { name: "确认" }));
    expect(saveCommand).toHaveBeenCalledWith({
      domain: "image",
      commandId: "image.tool.box",
      bindings: [
        { key: "i", modifiers: ["mod"] },
        { key: "1", modifiers: ["alt"] },
      ],
    });
  });

  it("shows the load failure retry state and blocks editing", () => {
    mount({
      shortcuts: makeShortcuts({
        loaded: false,
        loadError: new Error("offline"),
      }),
    });
    expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("reports unidentifiable stored shortcut data without hiding the reference", () => {
    mount({
      shortcuts: makeShortcuts({
        opaque: true,
      }),
    });
    expect(screen.getByText(/无法识别的快捷键数据/)).toBeTruthy();
    expect(screen.getByText("选择工具")).toBeTruthy();

    mount({
      shortcuts: makeShortcuts({
        issues: [
          { domain: "image", commandId: "legacy.command", reason: "unknown-command" as never },
        ],
      }),
    });
    expect(screen.getAllByText(/无法执行/).length).toBeGreaterThan(0);
  });

  it("reset actions submit null for customized commands of the named domain", async () => {
    const user = userEvent.setup();
    const saveCommand = vi.fn();
    const effective = resolveEffectiveCommands({
      common: {},
      image: { "image.tool.box": [{ key: "k", modifiers: [] }] },
      video: {},
    });
    mount({ shortcuts: makeShortcuts({ saveCommand, effective }) });
    await user.click(screen.getByRole("button", { name: "重置图片快捷键" }));
    expect(saveCommand).toHaveBeenCalledWith({
      domain: "image",
      commandId: "image.tool.box",
      bindings: null,
    });
  });

  it("rejects browser reserved bindings and lets a valid candidate be confirmed by keyboard", async () => {
    const user = userEvent.setup();
    const shortcuts = makeShortcuts();
    mount({ shortcuts });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const row = document.querySelector('[data-hotkey-command="image.tool.keypoint"]')!;
    await user.click(row.querySelector("button")!);
    fireEvent.keyDown(window, { key: "r", ctrlKey: true, cancelable: true });
    expect(screen.queryByRole("button", { name: "确认" })).toBeNull();
    expect(screen.getByText(/该组合由浏览器/)).toBeVisible();
    fireEvent.keyDown(window, { key: "G", shiftKey: true, cancelable: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "确认" })).toHaveFocus());
    await user.keyboard("{Enter}");
    expect(shortcuts.saveCommand).toHaveBeenCalledWith(
      expect.objectContaining({
        bindings: [{ key: "g", modifiers: ["shift"] }],
      }),
    );
  });

  it("cancels recording when its row is hidden or search takes focus", async () => {
    const user = userEvent.setup();
    mount({ shortcuts: makeShortcuts() });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    await user.click(document.querySelector('[data-hotkey-command="image.tool.keypoint"] button')!);
    await user.click(screen.getByRole("tab", { name: "任务与系统" }));
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    await user.type(search, "下一题");
    expect(search).toHaveValue("下一题");
    await user.clear(search);
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    await user.click(document.querySelector('[data-hotkey-command="image.tool.keypoint"] button')!);
    await user.type(search, "关键点");
    expect(search).toHaveValue("关键点");
    expect(screen.queryByText("按下新组合…")).toBeNull();
  });

  it("searches project attributes and the actual customized combination", async () => {
    const user = userEvent.setup();
    mount({
      attributeSchema: {
        fields: [{ key: "review", label: "审查标记", type: "boolean", hotkey: "7" }],
      },
      shortcuts: makeShortcuts({
        effective: resolveEffectiveCommands({
          common: {},
          video: {},
          image: {
            "image.tool.keypoint": [{ key: "g", modifiers: ["shift"] }],
          },
        }),
      }),
    });
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    await user.type(search, "审查标记");
    expect(screen.getByText("审查标记")).toBeVisible();
    await user.clear(search);
    await user.type(search, "Shift+G");
    expect(document.querySelector('[data-hotkey-command="image.tool.keypoint"]')).toBeVisible();
  });

  it("prevents enabling a default binding now occupied by another command", async () => {
    const user = userEvent.setup();
    const shortcuts = makeShortcuts({
      effective: resolveEffectiveCommands({
        common: {},
        video: {},
        image: {
          "image.tool.polygon": [],
          "image.tool.keypoint": [{ key: "p", modifiers: [] }],
        },
      }),
    });
    mount({ shortcuts });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const row = document.querySelector('[data-hotkey-command="image.tool.polygon"]') as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "启用" }));
    expect(shortcuts.saveCommand).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("关键点");
  });

  it("keeps a pending primary binding when adding its alternate", async () => {
    const user = userEvent.setup();
    const shortcuts = makeShortcuts({
      previewEffective: resolveEffectiveCommands({
        common: {},
        video: {},
        image: {
          "image.tool.keypoint": [{ key: "g", modifiers: ["shift"] }],
        },
      }),
      pendingKeys: new Set(["image:image.tool.keypoint"]),
    });
    mount({ shortcuts });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const row = document.querySelector(
      '[data-hotkey-command="image.tool.keypoint"]',
    ) as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "添加备用" }));
    fireEvent.keyDown(window, { key: "i", altKey: true, cancelable: true });
    await user.click(within(row).getByRole("button", { name: "确认" }));
    expect(shortcuts.saveCommand).toHaveBeenCalledWith(
      expect.objectContaining({
        bindings: [
          { key: "g", modifiers: ["shift"] },
          { key: "i", modifiers: ["alt"] },
        ],
      }),
    );
  });

  it("reserves bindings already claimed by a pending edit", async () => {
    const user = userEvent.setup();
    const shortcuts = makeShortcuts({
      previewEffective: resolveEffectiveCommands({
        common: {},
        video: {},
        image: { "image.tool.keypoint": [{ key: "x", modifiers: [] }] },
      }),
    });
    mount({ shortcuts });
    await user.click(screen.getByRole("tab", { name: "绘制与工具" }));
    const row = document.querySelector('[data-hotkey-command="image.tool.polygon"]') as HTMLElement;
    await user.click(row.querySelector("button")!);
    fireEvent.keyDown(window, { key: "x", cancelable: true });
    expect(within(row).queryByRole("button", { name: "确认" })).toBeNull();
    expect(row).toHaveTextContent("关键点工具");
    expect(shortcuts.saveCommand).not.toHaveBeenCalled();
  });

  it("checks a domain reset as a whole and rejects collisions with other domains", async () => {
    const user = userEvent.setup();
    const shortcuts = makeShortcuts({
      effective: resolveEffectiveCommands({
        video: {},
        common: { "common.task.next": [{ key: "i", modifiers: [] }] },
        image: { "image.tool.keypoint": [{ key: "arrowright", modifiers: ["mod"] }] },
      }),
    });
    const view = mount({ shortcuts });
    await user.click(screen.getByRole("button", { name: "重置常用快捷键" }));
    expect(shortcuts.saveCommand).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("关键点");
    shortcuts.effective = resolveEffectiveCommands({
      common: {},
      video: {},
      image: {
        "image.tool.keypoint": [{ key: "p", modifiers: [] }],
        "image.tool.polygon": [{ key: "f", modifiers: [] }],
      },
    });
    view.rerender(
      <HotkeyCheatSheet open onClose={vi.fn()} stageKind="image" shortcuts={shortcuts} />,
    );
    await user.click(screen.getByRole("button", { name: "重置图片快捷键" }));
    expect(shortcuts.saveCommand).toHaveBeenCalledTimes(2);
    expect(shortcuts.saveCommand).toHaveBeenCalledWith({
      domain: "image",
      commandId: "image.tool.keypoint",
      bindings: null,
    });
    expect(shortcuts.saveCommand).toHaveBeenCalledWith({
      domain: "image",
      commandId: "image.tool.polygon",
      bindings: null,
    });
  });
});
