import { expect, test } from "../fixtures/seed";

// Measure rendered controls, not class names: both overlap and excessive wrapping
// previously passed component tests while wasting canvas space.
export function measureTopbar(bar: HTMLElement) {
  const outer = bar.getBoundingClientRect();
  const controls = Array.from(
    bar.querySelectorAll<HTMLElement>("button, select, span[title], span.mono"),
  ).filter((element) => element.getBoundingClientRect().width > 0);
  const overlaps: string[] = [];
  const outside: string[] = [];
  controls.forEach((element, index) => {
    const a = element.getBoundingClientRect();
    if (a.left < outer.left - 1 || a.right > outer.right + 1)
      outside.push(element.textContent ?? "");
    controls.slice(index + 1).forEach((other) => {
      if (element.contains(other) || other.contains(element)) return;
      const b = other.getBoundingClientRect();
      if (
        Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
        Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1
      ) {
        overlaps.push(`${element.textContent} / ${other.textContent}`);
      }
    });
  });
  const previous = bar.querySelector<HTMLElement>('[aria-label="上一个任务"]')!;
  const metadata = previous.parentElement!.firstElementChild as HTMLElement;
  const filename = metadata.querySelector<HTMLElement>("span[title]");
  const index = filename?.nextElementSibling as HTMLElement | null;
  const lastMetadata = Array.from(metadata.children)
    .filter((element) => element.getBoundingClientRect().width > 0)
    .at(-1);
  const navigationGap = lastMetadata
    ? previous.getBoundingClientRect().left - lastMetadata.getBoundingClientRect().right
    : 0;
  const filenameGap =
    filename && index && filename.getBoundingClientRect().width > 0
      ? index.getBoundingClientRect().left - filename.getBoundingClientRect().right
      : 0;
  return { height: outer.height, overlaps, outside, navigationGap, filenameGap };
}

test("topbar stays compact and does not overlap as its container narrows", async ({
  page,
  seed,
}) => {
  const data = await seed.owned();
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  const bar = page.getByTestId("workbench-topbar");
  await expect(bar).toBeVisible();
  for (const width of [1440, 1280, 1024, 800, 640, 375]) {
    await bar.evaluate((element, size) => {
      element.style.width = `${size}px`;
    }, width);
    const result = await bar.evaluate(measureTopbar);
    expect(result.overlaps, `overlap at ${width}px`).toEqual([]);
    expect(result.outside, `clipping at ${width}px`).toEqual([]);
    expect(result.navigationGap, `navigation spacing at ${width}px`).toBeLessThanOrEqual(24);
    expect(result.filenameGap, `filename spacing at ${width}px`).toBeLessThanOrEqual(12);
    expect(result.height, `single row at ${width}px`).toBeLessThanOrEqual(52);
  }
});
