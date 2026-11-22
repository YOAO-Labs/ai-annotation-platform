import { expect, type Page } from "@playwright/test";

export async function openSelectedMaskEditor(page: Page, annotationId: string): Promise<void> {
  const row = page.getByTestId(`box-list-item-${annotationId}`);
  await expect(row).toHaveCount(1);
  await row.click();

  const card = page.locator("section").filter({
    has: page.getByText("选中对象", { exact: true }),
  });
  const expand = page.getByLabel(/^展开选中信息卡(?:\(|:)/);
  await expect(card.or(expand)).toBeVisible();
  if (await expand.isVisible()) await expand.click();
  await card.getByRole("button", { name: "编辑 Mask", exact: true }).click();
}
