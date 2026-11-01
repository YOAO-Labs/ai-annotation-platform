import { expect, type Page } from "@playwright/test";

/** Open the one seeded prediction shape, rather than whichever row appears first. */
export async function openPredictionRefinement(page: Page, predictionId: string): Promise<void> {
  const shapeId = `pred-${predictionId}-0`;
  const row = page.getByTestId(`box-list-item-${shapeId}`);
  await expect(row).toHaveCount(1);
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "更多操作" }).hover();
  await row.getByTestId(`ai-refine-${shapeId}`).click();
}

/** Confirm the fixture's expected conversion decision, never derive it from the result. */
export async function confirmMaskRefinement(page: Page, expectedLossy: boolean): Promise<void> {
  const report = page.getByRole("alertdialog", { name: "将精修结果保存为原生 Mask？" });
  await expect(report).toBeVisible();
  await expect(report).toContainText(expectedLossy ? "· 有损" : "· 无损");
  await report.getByRole("button", { name: "保存", exact: true }).click();
  if (expectedLossy) {
    const warning = page.getByRole("alertdialog", { name: "确认丢弃源前景像素？" });
    await expect(warning).toBeVisible();
    await warning.getByRole("button", { name: "确认继续" }).click();
  }
}
