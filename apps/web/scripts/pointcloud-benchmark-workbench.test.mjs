import { describe, expect, it } from "vitest";
import { revealFirstBenchmarkBox } from "./pointcloud-benchmark-workbench.mjs";

function fixture(overrides = {}) {
  const state = {
    inspectorVisible: false,
    inspectorTabVisible: true,
    settingsOpen: false,
    advancedOpen: false,
    inspectorEnabled: true,
    manualExpanded: false,
    boxAvailable: true,
    events: [],
    ...overrides,
  };
  const visible = {
    box: () => state.boxAvailable && state.inspectorVisible && state.manualExpanded,
    inspector: () => state.inspectorVisible,
    tab: () => state.inspectorTabVisible,
    dialog: () => state.settingsOpen,
    details: () => state.settingsOpen,
    toggle: () => state.settingsOpen && state.advancedOpen,
    manual: () => state.inspectorVisible,
    settings: () => true,
    close: () => state.settingsOpen,
    summary: () => state.settingsOpen,
  };
  const actions = {
    tab: () => {
      state.events.push("activate inspector tab");
      state.inspectorVisible = true;
    },
    settings: () => {
      state.events.push("open settings");
      state.settingsOpen = true;
    },
    summary: () => {
      state.events.push("open advanced layout");
      state.advancedOpen = true;
    },
    toggle: () => {
      if (state.inspectorEnabled) {
        state.events.push("hide inspector");
        state.inspectorEnabled = false;
        state.inspectorVisible = false;
        state.inspectorTabVisible = false;
      } else {
        state.events.push("show inspector");
        state.inspectorEnabled = true;
        state.inspectorVisible = true;
        state.inspectorTabVisible = true;
      }
    },
    close: () => {
      state.events.push("close settings");
      state.settingsOpen = false;
    },
    manual: () => {
      state.events.push("expand manual section");
      state.manualExpanded = true;
    },
  };

  const locator = (key) => ({
    first() {
      return this;
    },
    locator(selector) {
      if (key === "dialog" && selector === "details") return locator("details");
      if (key === "details" && selector === "summary") return locator("summary");
      throw new Error(`Unexpected nested locator ${key} ${selector}`);
    },
    getByRole(role, options) {
      if (key === "details" && role === "button" && options.name === "标注详情") {
        return locator("toggle");
      }
      if (key === "dialog" && role === "button" && options.name === "关闭设置") {
        return locator("close");
      }
      throw new Error(`Unexpected nested role ${key} ${role} ${options.name}`);
    },
    async isVisible() {
      return visible[key]();
    },
    async waitFor({ state: expected }) {
      state.events.push(`wait ${key} ${expected}`);
      const actual = visible[key]();
      if ((expected === "visible") !== actual) throw new Error(`${key} stayed ${actual}`);
    },
    async getAttribute(name) {
      if (key === "details" && name === "open") return state.advancedOpen ? "" : null;
      if (key === "toggle" && name === "aria-pressed") {
        return state.inspectorEnabled ? "true" : "false";
      }
      if (key === "manual" && name === "aria-expanded") {
        return state.manualExpanded ? "true" : "false";
      }
      return null;
    },
    async click() {
      actions[key]?.();
    },
  });
  const page = {
    locator: () => locator("box"),
    getByTestId(id) {
      if (id === "ai-inspector-panel") return locator("inspector");
      if (id === "workbench-settings-dialog") return locator("dialog");
      if (id === "section-header-manual") return locator("manual");
      throw new Error(`Unexpected test id ${id}`);
    },
    getByRole(role, options) {
      if (role === "tab" && options.name === "标注详情") return locator("tab");
      if (role === "button" && options.name === "工作台设置") return locator("settings");
      throw new Error(`Unexpected role ${role} ${options.name}`);
    },
  };
  return { page, state };
}

describe("point-cloud benchmark inspector preparation", () => {
  it("activates an inactive docked inspector before expanding the manual section", async () => {
    const { page, state } = fixture();

    await revealFirstBenchmarkBox(page);

    expect(state.events).toEqual([
      "activate inspector tab",
      "wait inspector visible",
      "wait manual visible",
      "expand manual section",
      "wait box visible",
    ]);
  });

  it("restores a hidden inspector through workbench settings before reading rows", async () => {
    const { page, state } = fixture({ inspectorTabVisible: false, inspectorEnabled: false });

    await revealFirstBenchmarkBox(page);

    expect(state.events).toEqual([
      "open settings",
      "wait dialog visible",
      "open advanced layout",
      "wait toggle visible",
      "show inspector",
      "close settings",
      "wait dialog hidden",
      "wait inspector visible",
      "wait manual visible",
      "expand manual section",
      "wait box visible",
    ]);
  });

  it("re-shows a layout-visible inspector when its whole dock group is hidden", async () => {
    const { page, state } = fixture({ inspectorTabVisible: false });

    await revealFirstBenchmarkBox(page);

    expect(state.events).toContain("hide inspector");
    expect(state.events).toContain("show inspector");
    expect(state.events.indexOf("hide inspector")).toBeLessThan(
      state.events.indexOf("show inspector"),
    );
    expect(state.events.at(-1)).toBe("wait box visible");
  });

  it("fails instead of silently skipping required refinement sampling", async () => {
    const { page } = fixture({ inspectorVisible: true, manualExpanded: true, boxAvailable: false });

    await expect(revealFirstBenchmarkBox(page, { timeout: 1 })).rejects.toThrow(
      "refinement sampling cannot continue",
    );
  });
});
