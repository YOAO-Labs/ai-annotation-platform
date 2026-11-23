import { expect, type Page } from "@playwright/test";

export async function openMaskSettings(page: Page) {
  const toolbar = page.getByTestId("mask-toolbar");
  const openToolbar = toolbar.locator(':scope:not([aria-hidden="true"])');
  if ((await openToolbar.count()) === 0) {
    await expect(toolbar).toBeHidden();
    const capsule = page.getByTestId("mask-tool-capsule");
    const trigger = page.getByTestId("mask-settings-trigger");
    await expect(trigger).toBeVisible();
    if ((await trigger.getAttribute("aria-expanded")) !== "true") await trigger.click();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
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
  await expect(openToolbar).toBeVisible();
}

export async function closeMaskSettings(page: Page) {
  const toolbar = page.getByTestId("mask-toolbar");
  if ((await toolbar.locator(':scope:not([aria-hidden="true"])').count()) > 0) {
    await toolbar.evaluate((node) =>
      Promise.allSettled(
        node
          .getAnimations({ subtree: true })
          .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
          .map((animation) => animation.finished),
      ),
    );
    await page.getByRole("button", { name: "收起 Mask 设置", exact: true }).click();
  }
  await expect(toolbar).toBeHidden();
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
}
