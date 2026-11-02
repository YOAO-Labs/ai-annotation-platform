import { expect, type Page } from "@playwright/test";

export async function openMaskSettings(page: Page) {
  const toolbar = page.getByTestId("mask-toolbar");
  if (!(await toolbar.isVisible())) {
    const capsule = page.getByTestId("mask-tool-capsule");
    const trigger = page.getByTestId("mask-settings-trigger");
    await expect(trigger).toBeVisible();
    if ((await trigger.getAttribute("aria-expanded")) !== "true") await trigger.click();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    // Expansion changes the clipped quick-tools width and clamps its scroll position.
    await capsule.evaluate((node) =>
      Promise.allSettled(
        node
          .getAnimations({ subtree: true })
          .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
          .map((animation) => animation.finished),
      ),
    );
    await page.getByRole("button", { name: "更多 Mask 工具", exact: true }).click();
  }
  await expect(toolbar).toBeVisible();
}

export async function closeMaskSettings(page: Page) {
  if (await page.getByTestId("mask-toolbar").isVisible()) {
    await page.getByRole("button", { name: "收起 Mask 设置", exact: true }).click();
    await expect(page.getByTestId("mask-toolbar")).toBeHidden();
  }
  // Restored trigger focus owns Enter; return keyboard input to the workbench.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
}
