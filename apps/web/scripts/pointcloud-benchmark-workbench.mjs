const DEFAULT_TIMEOUT_MS = 10_000;

async function isVisible(locator) {
  return locator.isVisible().catch(() => false);
}

async function activateInspectorPanel(page, timeout) {
  const inspector = page.getByTestId("ai-inspector-panel");
  if (await isVisible(inspector)) return;

  const inspectorTab = page.getByRole("tab", { name: "标注详情", exact: true }).first();
  if (await isVisible(inspectorTab)) {
    await inspectorTab.click();
    await inspector.waitFor({ state: "visible", timeout });
    return;
  }

  await page.getByRole("button", { name: "工作台设置", exact: true }).click();
  const dialog = page.getByTestId("workbench-settings-dialog");
  await dialog.waitFor({ state: "visible", timeout });
  const advanced = dialog.locator("details", { hasText: "面板与高级布局" });
  if ((await advanced.getAttribute("open")) === null) {
    await advanced.locator("summary").click();
  }
  const inspectorToggle = advanced.getByRole("button", { name: "标注详情", exact: true });
  await inspectorToggle.waitFor({ state: "visible", timeout });
  if ((await inspectorToggle.getAttribute("aria-pressed")) === "true") {
    // A collapsed side or maximized canvas can leave the panel in the saved
    // layout while neither its tab nor content is interactive. Re-showing it
    // through the owner command reveals the group and activates the panel.
    await inspectorToggle.click();
  }
  await inspectorToggle.click();
  await dialog.getByRole("button", { name: "关闭设置", exact: true }).click();
  await dialog.waitFor({ state: "hidden", timeout });

  // Showing a parked panel activates it. If the layout restored it into an
  // existing tab group instead, explicitly activate that tab before reading
  // virtualized inspector rows.
  if (!(await isVisible(inspector)) && (await isVisible(inspectorTab))) {
    await inspectorTab.click();
  }
  await inspector.waitFor({ state: "visible", timeout });
}

/**
 * Reveal the first real annotation row required by the refinement benchmark.
 * A docked inspector can remain mounted while another tab is active, so DOM
 * count alone is not evidence that either the section header or row is usable.
 */
export async function revealFirstBenchmarkBox(page, options = {}) {
  const timeout = options.timeout ?? DEFAULT_TIMEOUT_MS;
  const firstBox = page.locator('[data-testid^="box-list-item-"]').first();

  if (!(await isVisible(firstBox))) {
    await activateInspectorPanel(page, timeout);
  }
  if (!(await isVisible(firstBox))) {
    const manualSection = page.getByTestId("section-header-manual");
    await manualSection.waitFor({ state: "visible", timeout });
    if ((await manualSection.getAttribute("aria-expanded")) === "false") {
      await manualSection.click();
    }
  }

  try {
    await firstBox.waitFor({ state: "visible", timeout });
  } catch (cause) {
    throw new Error(
      "The benchmark annotation exists but is not visible in the active inspector; refinement sampling cannot continue",
      { cause },
    );
  }
  return firstBox;
}
