import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import { test, expect } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

test.describe("mask lock bypass", () => {
  test("锁定 annotation 经 mask 工具不可修改", async ({ page, request, owned: data, seed }) => {
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });

    const annotatorToken = await seed.accessToken(data.annotator_email);
    const headers = {
      Authorization: `Bearer ${annotatorToken}`,
      "Content-Type": "application/json",
    };
    const createRes = await request.post(
      `${API_BASE}/api/v1/tasks/${data.task_ids[0]}/annotations`,
      {
        headers,
        data: {
          annotation_type: "bbox",
          tool_unit_id: "bbox",
          class_name: "car",
          geometry: { type: "bbox", x: 0.3, y: 0.3, w: 0.4, h: 0.4 },
        },
      },
    );
    expect(createRes.status()).toBe(201);
    const annotationId = (await createRes.json()).id;
    const lockResp = await request.post(`${API_BASE}/api/v1/annotations/bulk-update`, {
      headers,
      data: { ids: [annotationId], patch: { is_locked: true } },
    });
    expect(lockResp.ok()).toBeTruthy();

    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
    await page.waitForLoadState("networkidle");

    const annotationRow = page.getByTestId(`box-list-item-${annotationId}`);
    await expect(annotationRow).toBeVisible({ timeout: 10_000 });
    await annotationRow.click();

    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    const cx = box.x + box.width * 0.5;
    const cy = box.y + box.height * 0.5;

    await page.keyboard.press("m");
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible({ timeout: 10_000 });

    await openMaskSettings(page);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar.getByRole("radio", { name: "笔刷" })).toBeDisabled({ timeout: 5_000 });
    await expect(toolbar.getByRole("radio", { name: "橡皮" })).toBeDisabled({ timeout: 5_000 });

    await closeMaskSettings(page);
    await page.keyboard.press("b");
    await openMaskSettings(page);
    await expect(toolbar.getByRole("radio", { name: "笔刷" })).toBeDisabled();
    await closeMaskSettings(page);
    await page.keyboard.press("e");
    await openMaskSettings(page);
    await expect(toolbar.getByRole("radio", { name: "橡皮" })).toBeDisabled();

    let mutationCount = 0;
    page.on("request", (request) => {
      if (
        /\/api\/v1\/(annotations|tasks\/[^/]+\/annotations)/.test(request.url()) &&
        ["POST", "PATCH", "PUT", "DELETE"].includes(request.method())
      )
        mutationCount += 1;
    });
    await closeMaskSettings(page);
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + 40, cy + 40, { steps: 5 });
    await page.mouse.up();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(500);
    expect(mutationCount).toBe(0);
  });
});
