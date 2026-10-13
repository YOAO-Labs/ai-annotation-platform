import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { expect, test, type SeedAPI, type SeedData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
interface Rle {
  encoding: "coco_rle";
  size: [number, number];
  counts: number[];
}
interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}
interface MaskAnnotation {
  id: string;
  version: number;
  geometry: {
    type: string;
    mask?: MaskReference;
    keyframes?: Array<{ frame_index: number; source?: string; mask: MaskReference }>;
  };
}
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}
async function annotations(request: APIRequestContext, taskId: string, token: string) {
  return json<MaskAnnotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers: auth(token) }),
  );
}
async function content(request: APIRequestContext, id: string, token: string) {
  return json<Rle>(
    await request.get(`${API_BASE}/api/v1/annotations/${id}/mask-content`, {
      headers: auth(token),
    }),
  );
}
const area = (rle: Rle) =>
  rle.counts.reduce((sum, count, index) => sum + (index % 2 ? count : 0), 0);
async function openImage(page: Page, seed: SeedAPI, data: SeedData, taskId: string) {
  await seed.configureRasterMask(data.project_id, true);
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
    timeout: 20_000,
  });
  await expect(page.getByTestId("workbench-stage").locator("canvas").first()).toBeVisible();
}
async function beginEdit(page: Page, annotationId: string) {
  await page.getByTestId(`box-list-item-${annotationId}`).click();
  await page.locator('button[aria-label="编辑 Mask"]:visible').last().click();
  await openMaskSettings(page);
  await expect(page.getByTestId("mask-toolbar")).toContainText("就绪", { timeout: 15_000 });
  await expect(page.getByTestId("mask-primary-action")).toHaveText("已保存");
}
async function chooseAdvanced(page: Page, name: string) {
  await page.getByTestId("mask-toolbar").getByTitle("Mask 高级工具").click();
  await page.getByRole("menuitem", { name, exact: true }).click();
  await expect(page.getByRole("menu")).toBeHidden();
}
async function primaryKey(page: Page, key = "Enter") {
  await closeMaskSettings(page);
  await page.keyboard.press(key);
}
async function paintImage(page: Page, x: number, y: number, size = [64, 48]) {
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0, { timeout: 10_000 });
  await closeMaskSettings(page);
  const box = await page.getByTestId("workbench-stage").boundingBox();
  if (!box) throw new Error("Missing image stage");
  const scale = Math.min(box.width / size[0], box.height / size[1]);
  const px = box.x + (box.width - size[0] * scale) / 2 + x * scale;
  const py = box.y + (box.height - size[1] * scale) / 2 + y * scale;
  await page.mouse.click(px, py);
  await openMaskSettings(page);
  await expect(page.getByTestId("mask-toolbar")).toContainText("未保存");
  return { x: px, y: py };
}
async function saveImage(
  page: Page,
  taskId: string,
  annotationId: string,
  via: "button" | "keyboard",
) {
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/tasks/${taskId}/annotations/${annotationId}`) &&
      response.request().method() === "PATCH" &&
      response.ok(),
  );
  if (via === "button") await page.getByTestId("mask-primary-action").click();
  else await primaryKey(page);
  await saved;
  await openMaskSettings(page);
  await expect(page.getByTestId("mask-primary-action")).toHaveText("已保存");
}

test.describe("Mask phase primary actions", () => {
  test.use({ viewport: { width: 1440, height: 1080 } });
  test.setTimeout(90_000);
  const pageErrors = new WeakMap<Page, string[]>();
  test.beforeEach(({ page }) => {
    const errors: string[] = [];
    pageErrors.set(page, errors);
    page.on("pageerror", (error) => errors.push(error.message));
  });
  test.afterEach(({ page }) => expect(pageErrors.get(page)).toEqual([]));

  for (const via of ["button", "keyboard"] as const) {
    test(`D1 ${via}: apply changes only the draft, the next primary action persists`, async ({
      page,
      seed,
      request,
    }) => {
      const data = await seed.owned();
      const taskId = data.task_ids[0];
      const fixture = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
      const token = await seed.accessToken(data.annotator_email);
      const before = await content(request, fixture.annotation_id, token);
      await openImage(page, seed, data, taskId);
      await beginEdit(page, fixture.annotation_id);
      await chooseAdvanced(page, "膨胀");
      await expect(page.getByTestId("mask-primary-action")).toHaveText("应用区域预览");
      if (via === "button") await page.getByTestId("mask-primary-action").click();
      else await primaryKey(page);
      await openMaskSettings(page);
      await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
      expect(await content(request, fixture.annotation_id, token)).toEqual(before);
      await saveImage(page, taskId, fixture.annotation_id, via);
      const saved = await content(request, fixture.annotation_id, token);
      expect(area(saved)).toBeGreaterThan(area(before));
      await page.reload();
      await beginEdit(page, fixture.annotation_id);
      expect(await content(request, fixture.annotation_id, token)).toEqual(saved);
    });
  }

  test("D2 two instances: repeated pointer and held Enter commit once", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "diagonal_two",
    });
    const token = await seed.accessToken(data.annotator_email);
    await openImage(page, seed, data, taskId);
    await beginEdit(page, fixture.annotation_id);
    await chooseAdvanced(page, "拆分全部组件（保留最大）");
    await expect(page.getByTestId("mask-primary-action")).toHaveText("提交 2 个实例");
    let writes = 0;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    // Explicit latency injection holds the real API write to exercise repeated input.
    await page.route("**/annotations/mask-mutations:commit", async (route) => {
      writes += 1;
      await gate;
      await route.continue();
    });
    try {
      await page.getByTestId("mask-primary-action").dblclick();
      await expect.poll(() => writes).toBe(1);
      await closeMaskSettings(page);
      await page.keyboard.down("Enter");
      await page.keyboard.down("Enter");
      await page.keyboard.down("Enter");
      await page.keyboard.up("Enter");
      await page.keyboard.press("Escape");
      await expect(page.getByTestId("mask-tool-capsule")).toBeVisible();
      expect(writes).toBe(1);
    } finally {
      release();
    }
    await expect.poll(async () => (await annotations(request, taskId, token)).length).toBe(2);
    await page.reload();
    await expect(page.getByTestId(`box-list-item-${fixture.annotation_id}`)).toBeVisible();
    expect(await annotations(request, taskId, token)).toHaveLength(2);
    expect(writes).toBe(1);
  });

  test("D3 Esc preserves previous strokes; button and Enter share empty-result confirmation", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    const token = await seed.accessToken(data.annotator_email);
    const before = await content(request, fixture.annotation_id, token);
    await openImage(page, seed, data, taskId);
    await beginEdit(page, fixture.annotation_id);
    await page.getByTestId("mask-radius-slider").fill("2");
    await paintImage(page, 50, 34);
    await chooseAdvanced(page, "膨胀");
    await primaryKey(page, "Escape");
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
    await expect(page.getByTitle("撤销笔画 (Ctrl+Z)")).toBeEnabled();

    await page.getByTestId("mask-toolbar").getByTitle("Mask 高级工具").click();
    await page.getByLabel("组件与孔洞面积阈值").fill("100000");
    await page.getByRole("menuitem", { name: /去除小组件/ }).click();
    await expect(page.getByTestId("mask-primary-action")).toHaveText("应用区域预览");
    for (const via of ["button", "keyboard"]) {
      if (via === "button") await page.getByTestId("mask-primary-action").click();
      else await primaryKey(page);
      const confirm = page.getByRole("alertdialog");
      await expect(confirm).toContainText("确认清空当前 Mask？");
      expect(await content(request, fixture.annotation_id, token)).toEqual(before);
      if (via === "button") {
        await confirm.getByRole("button", { name: "返回预览" }).click({ timeout: 10_000 });
        await openMaskSettings(page);
        await expect(page.getByTestId("mask-primary-action")).toHaveText("应用区域预览");
      } else {
        await confirm.getByRole("button", { name: "确认清空" }).click();
        await openMaskSettings(page);
        await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
        expect(await content(request, fixture.annotation_id, token)).toEqual(before);
        await primaryKey(page, "Control+z");
        await openMaskSettings(page);
        await expect(page.getByTitle("撤销笔画 (Ctrl+Z)")).toBeEnabled();
      }
    }
    // Escape 触发离开守卫:应用内三选一选「继续编辑」,工具与稿件保持。
    const leaveDialog = page.getByRole("alertdialog");
    await primaryKey(page, "Escape");
    await expect(leaveDialog).toBeVisible({ timeout: 5_000 });
    await leaveDialog.getByRole("button", { name: "继续编辑", exact: true }).click();
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
    await expect(page.getByTitle("撤销笔画 (Ctrl+Z)")).toBeEnabled();
    // secondary(取消编辑)选「丢弃并离开」,稿件丢弃且不落库。
    await page.getByTestId("mask-secondary-action").click();
    await expect(leaveDialog).toBeVisible({ timeout: 5_000 });
    await leaveDialog.getByRole("button", { name: "丢弃并离开", exact: true }).click();
    await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);
    expect(await content(request, fixture.annotation_id, token)).toEqual(before);
  });

  test("D4 held video Mask materializes only the edited current frame", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    await seed.configureRasterMask(data.project_id, true);
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    const fixture = await seed.nativeMaskCandidate(taskId);
    const token = await seed.accessToken(data.admin_email);
    const mask = await json<MaskReference>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
        headers: auth(token),
        data: fixture.rle,
      }),
    );
    const original = await json<MaskAnnotation>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
        headers: auth(token),
        data: {
          annotation_type: "video_track_mask",
          tool_unit_id: "region",
          class_name: "car",
          geometry: {
            type: "video_track_mask",
            track_id: "d-primary-held-mask",
            keyframes: [
              { frame_index: 0, mask, source: "manual", occluded: false },
              { frame_index: 10, mask, source: "manual", occluded: false },
            ],
            outside: [],
          },
        },
      }),
    );
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    const videoStage = page.getByTestId("video-konva-stage");
    await expect(videoStage).toBeVisible({ timeout: 20_000 });
    await page.getByTestId(`video-mask-track-${original.id}`).click();
    // The selected track button intentionally consumes arrow keys while focused;
    // navigation is a canvas/workbench shortcut after focus returns to the page.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.keyboard.press("ArrowRight");
    await expect(videoStage).toHaveAttribute("data-video-frame-index", "1", {
      timeout: 15_000,
    });
    await page.getByTestId("video-tool-btn-mask-track").click();
    await openMaskSettings(page);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar).toContainText("当前帧保持 F0 的 Mask");
    await expect(page.getByTestId("mask-primary-action")).toHaveText("已保存");
    let writes = 0;
    page.on("request", (outgoing) => {
      if (outgoing.method() === "PUT" && outgoing.url().includes("/mask-keyframes/")) writes += 1;
    });
    await primaryKey(page);
    expect((await annotations(request, taskId, token))[0].geometry.keyframes).toEqual(
      original.geometry.keyframes,
    );
    expect(writes).toBe(0);
    await expect(page.locator("[data-sonner-toast]")).toHaveCount(0, { timeout: 10_000 });
    const box = await page.getByTestId("video-konva-stage").boundingBox();
    if (!box) throw new Error("Missing video stage");
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.6);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.65, { steps: 8 });
    await page.mouse.up();
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-primary-action")).toHaveText("保存当前帧关键帧");
    await primaryKey(page);
    await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);
    const saved = (await annotations(request, taskId, token)).find(
      (item) => item.id === original.id,
    )!;
    expect(saved.geometry.keyframes?.map((frame) => frame.frame_index)).toEqual([0, 1, 10]);
    expect(saved.geometry.keyframes?.find((frame) => frame.frame_index === 1)?.source).toBe(
      "manual",
    );
    expect(saved.geometry.keyframes?.filter((frame) => frame.frame_index !== 1)).toEqual(
      original.geometry.keyframes,
    );
    expect(writes).toBe(1);
    await page.reload();
    await expect(page.getByTestId(`video-mask-track-${original.id}`)).toBeVisible();
    expect(
      (await annotations(request, taskId, token)).find((item) => item.id === original.id)?.geometry,
    ).toEqual(saved.geometry);
  });

  test("D5 save failure keeps the draft; recovery is separate from saving", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    const token = await seed.accessToken(data.annotator_email);
    const before = await content(request, fixture.annotation_id, token);
    await openImage(page, seed, data, taskId);
    await beginEdit(page, fixture.annotation_id);
    await paintImage(page, 48, 35);
    let failed = false;
    await page.route(`**/tasks/${taskId}/annotations/${fixture.annotation_id}`, async (route) => {
      if (!failed && route.request().method() === "PATCH") {
        failed = true;
        await route.fulfill({ status: 503, json: { detail: "D5 injected save failure" } });
      } else await route.continue();
    });
    await page.getByTestId("mask-primary-action").click();
    await expect(page.getByTestId("mask-primary-action")).toHaveText("恢复编辑");
    expect(await content(request, fixture.annotation_id, token)).toEqual(before);
    await primaryKey(page);
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
    await expect(page.getByTitle("撤销笔画 (Ctrl+Z)")).toBeEnabled();
    expect(await content(request, fixture.annotation_id, token)).toEqual(before);
    await saveImage(page, taskId, fixture.annotation_id, "button");
    const saved = await content(request, fixture.annotation_id, token);
    expect(saved).not.toEqual(before);
    await page.reload();
    await beginEdit(page, fixture.annotation_id);
    expect(await content(request, fixture.annotation_id, token)).toEqual(saved);
  });

  test("D5 instance version conflict offers refresh and preserves atomicity", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "diagonal_two",
    });
    const token = await seed.accessToken(data.annotator_email);
    await openImage(page, seed, data, taskId);
    await beginEdit(page, fixture.annotation_id);
    await chooseAdvanced(page, "拆分全部组件（保留最大）");
    await expect(page.getByTestId("mask-primary-action")).toHaveText("提交 2 个实例");
    const source = (await annotations(request, taskId, token))[0];
    await json(
      await request.patch(`${API_BASE}/api/v1/tasks/${taskId}/annotations/${source.id}`, {
        headers: { ...auth(token), "If-Match": `W/"${source.version}"` },
        data: { geometry: source.geometry },
      }),
    );
    const conflict = page.waitForResponse(
      (response) =>
        response.url().endsWith("/annotations/mask-mutations:commit") &&
        response.request().method() === "POST",
    );
    await primaryKey(page);
    expect((await conflict).status()).toBe(409);
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-primary-action")).toHaveText("刷新范围");
    expect(await annotations(request, taskId, token)).toHaveLength(1);
    await page.getByTestId("mask-primary-action").click();
    await expect(page.getByTestId("mask-primary-action")).toHaveText("提交 2 个实例");
    await primaryKey(page);
    await expect.poll(async () => (await annotations(request, taskId, token)).length).toBe(2);
    await page.reload();
    await expect(page.getByTestId(`box-list-item-${source.id}`)).toBeVisible();
    expect(await annotations(request, taskId, token)).toHaveLength(2);
  });

  test("D5 controlled tile memory limit blocks painting but still saves the existing draft", async ({
    page,
    seed,
    request,
  }) => {
    // The e2e build bundles this module, so source interception is unavailable in preview.
    // The app consumes this override only in development/e2e builds; pointer/zoom input still
    // drives the real resource-admission path and no editor state is injected.
    await page.addInitScript(() => {
      (
        window as typeof window & { __E2E_MASK_TILE_MAX_BYTES__?: number }
      ).__E2E_MASK_TILE_MAX_BYTES__ = 327776;
    });
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      canvas: "8k",
    });
    const token = await seed.accessToken(data.annotator_email);
    const before = await content(request, fixture.annotation_id, token);
    await openImage(page, seed, data, taskId);
    await beginEdit(page, fixture.annotation_id);
    const point = await paintImage(page, 2816, 4864, [8192, 8192]);
    await closeMaskSettings(page);
    await page.mouse.move(point.x, point.y);
    // Ctrl+wheel is image zoom; ordinary wheel controls the Mask brush radius.
    await page.keyboard.down("Control");
    for (let step = 0; step < 45; step += 1) {
      await page.mouse.wheel(0, -120);
      await page.waitForTimeout(30);
    }
    await page.keyboard.up("Control");
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-toolbar")).toContainText("当前设备无法容纳可见分块", {
      timeout: 20_000,
    });
    await expect(
      page.getByTestId("mask-toolbar").getByRole("radio", { name: "笔刷", exact: true }),
    ).toBeDisabled();
    await expect(page.getByTestId("mask-primary-action")).toHaveText("保存 Mask");
    await expect(page.getByTestId("mask-primary-action")).toBeEnabled();
    await saveImage(page, taskId, fixture.annotation_id, "keyboard");
    const saved = await content(request, fixture.annotation_id, token);
    expect(saved).not.toEqual(before);
    await page.reload();
    await beginEdit(page, fixture.annotation_id);
    expect(await content(request, fixture.annotation_id, token)).toEqual(saved);
  });
});
