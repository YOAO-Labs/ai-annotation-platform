import type { APIResponse } from "@playwright/test";
import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

interface AnnotationDto {
  id: string;
  annotation_type: string;
  geometry: {
    type: string;
    x?: number;
    y?: number;
    w?: number;
    h?: number;
    keyframes?: Array<{ frame_index: number }>;
  };
  version: number;
}

interface ConversionSummary {
  source_count: number;
  result_count: number;
  materialized_held_frames: number;
  lossy_count: number;
}

async function json<T>(response: APIResponse): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `${response.request().method()} ${response.url()} failed: ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as T;
}

test.describe("annotation conversion center", () => {
  test.afterEach(async ({ seed }) => {
    await seed.owned();
  });

  test("polygon copy to Mask, then replace Mask with polygon", async ({ page, request, seed }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({
      taskId,
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    const token = await seed.accessToken(data.annotator_email);
    const headers = { Authorization: `Bearer ${token}` };
    const project = await json<{
      tool_bindings?: { region?: { classes?: Array<{ name: string }> } };
    }>(await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, { headers }));
    const className = project.tool_bindings?.region?.classes?.[0]?.name;
    if (!className) throw new Error("seed project has no region class");

    const source = await json<AnnotationDto>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
        headers,
        data: {
          annotation_type: "polygon",
          tool_unit_id: "region",
          class_name: className,
          geometry: {
            type: "polygon",
            points: [
              [0.2, 0.2],
              [0.7, 0.2],
              [0.7, 0.7],
              [0.2, 0.7],
            ],
          },
        },
      }),
    );

    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId(`box-list-item-${source.id}`).click();
    await page.locator('button[aria-label="转为 Mask"]:visible').last().click();

    const dialog = page.getByRole("dialog", { name: "标注转换中心" });
    await expect(dialog).toContainText("Polygon · 1 个对象");
    const dryRun = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:dry-run`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await dialog.getByRole("button", { name: "生成预览" }).click();
    const copyPreview = await json<{ summary: ConversionSummary }>(await dryRun);
    await expect(dialog.getByLabel("转换预览报告")).toContainText("polygon");
    await expect(dialog.getByLabel("转换预览报告")).toContainText("raster_mask");

    const copyExecute = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await dialog.getByRole("button", { name: "执行转换" }).click();
    const copyResult = await json<{
      created_annotations: AnnotationDto[];
      report: ConversionSummary;
    }>(await copyExecute);
    expect(copyResult.report).toEqual(copyPreview.summary);
    const raster = copyResult.created_annotations[0];
    expect(raster.geometry.type).toBe("raster_mask");
    await expect(dialog).toBeHidden();
    await expect(page.getByTestId(`box-list-item-${raster.id}`)).toBeVisible({ timeout: 15_000 });

    await page.locator('button[aria-label="转为矢量几何"]:visible').last().click();
    const replaceDialog = page.getByRole("dialog", { name: "标注转换中心" });
    await replaceDialog.getByLabel("结果策略").click();
    await page.getByRole("option", { name: "替换来源" }).click();
    await replaceDialog.getByRole("button", { name: "生成预览" }).click();
    await expect(replaceDialog.getByLabel("转换预览报告")).toContainText("替换来源");

    const beforeConflict = (
      await json<AnnotationDto[]>(
        await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers }),
      )
    ).find((annotation) => annotation.id === raster.id);
    if (!beforeConflict) throw new Error("raster source disappeared before conflict check");
    const bumped = await request.patch(
      `${API_BASE}/api/v1/tasks/${taskId}/annotations/${raster.id}`,
      {
        headers: { ...headers, "If-Match": `W/"${beforeConflict.version}"` },
        data: { geometry: beforeConflict.geometry },
      },
    );
    expect(bumped.ok(), await bumped.text()).toBe(true);

    await replaceDialog.getByRole("button", { name: "执行转换" }).click();
    let confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("来源对象会被替换");
    const conflictExecute = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST",
    );
    await confirm.getByRole("button", { name: "确认执行" }).click();
    expect((await conflictExecute).status()).toBe(409);
    await expect(replaceDialog).toContainText("转换未完成");
    const afterConflict = await json<AnnotationDto[]>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers }),
    );
    expect(afterConflict).toHaveLength(2);
    expect(afterConflict.find((annotation) => annotation.id === raster.id)?.geometry.type).toBe(
      "raster_mask",
    );

    await replaceDialog.getByRole("button", { name: "重新配置" }).click();
    await replaceDialog.getByRole("button", { name: "生成预览" }).click();
    await expect(replaceDialog.getByLabel("转换预览报告")).toContainText("替换来源");
    await replaceDialog.getByRole("button", { name: "执行转换" }).click();
    confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("来源对象会被替换");

    const replaceExecute = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await confirm.getByRole("button", { name: "确认执行" }).click();
    const replaceResult = await json<{
      updated_annotations: AnnotationDto[];
    }>(await replaceExecute);
    expect(replaceResult.updated_annotations[0].id).toBe(raster.id);
    expect(replaceResult.updated_annotations[0].geometry.type).toMatch(/polygon/);

    const annotations = await json<AnnotationDto[]>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers }),
    );
    expect(annotations).toHaveLength(2);
    expect(annotations.every((annotation) => annotation.geometry.type.includes("polygon"))).toBe(
      true,
    );
  });

  test("video polygon track keyframes convert to Mask and held preview stays side-effect free", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await seed.owned();
    await seed.configureRasterMask(data.project_id, true);
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    const token = await seed.accessToken(data.admin_email);
    const headers = { Authorization: `Bearer ${token}` };
    const source = await json<AnnotationDto>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
        headers,
        data: {
          annotation_type: "video_track_polygon",
          tool_unit_id: "region",
          class_name: "car",
          geometry: {
            type: "video_track_polygon",
            track_id: "trk_e2e_conversion_polygon",
            keyframes: [
              {
                frame_index: 0,
                points: [
                  [0.1, 0.1],
                  [0.4, 0.1],
                  [0.4, 0.4],
                  [0.1, 0.4],
                ],
                source: "manual",
              },
              {
                frame_index: 10,
                points: [
                  [0.5, 0.5],
                  [0.8, 0.5],
                  [0.8, 0.8],
                  [0.5, 0.8],
                ],
                source: "manual",
              },
            ],
            outside: [],
          },
        },
      }),
    );

    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 20_000 });
    const sourceRow = page.getByTestId(`box-list-item-${source.id}`);
    await sourceRow.click({ force: true });
    await page.locator('button[aria-label="转 Mask"]:visible').last().click();
    let dialog = page.getByRole("dialog", { name: "标注转换中心" });
    await dialog.getByLabel("转换范围").click();
    await page.getByRole("option", { name: "全部可见关键帧" }).click();
    await dialog.getByRole("button", { name: "生成预览" }).click();
    const report = dialog.getByLabel("转换预览报告");
    await expect(report).toContainText("全部可见关键帧");
    await expect(report).toContainText("帧 0, 10");
    await expect(report.getByText("物化帧").locator("..")).toContainText("0");

    const executed = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await dialog.getByRole("button", { name: "执行转换" }).click();
    const keyframeResult = await json<{
      created_annotations: AnnotationDto[];
    }>(await executed);
    const converted = keyframeResult.created_annotations[0];
    expect(converted.geometry.type).toBe("video_track_mask");
    expect(converted.geometry.keyframes?.map((keyframe) => keyframe.frame_index)).toEqual([0, 10]);

    await expect(dialog).toBeHidden({ timeout: 10_000 });
    await sourceRow.dispatchEvent("click");
    await expect(page.locator('button[aria-label="转 Mask"]:visible')).toBeVisible({
      timeout: 10_000,
    });
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    for (let frame = 1; frame <= 5; frame += 1) {
      await page.keyboard.press("ArrowRight");
      await expect(page.getByText(new RegExp(`F ${frame} /`))).toBeVisible({ timeout: 10_000 });
    }
    await page.locator('button[aria-label="转 Mask"]:visible').last().click();
    dialog = page.getByRole("dialog", { name: "标注转换中心" });
    await dialog.getByLabel("允许物化 held / 插值帧").click();
    await dialog.getByRole("button", { name: "生成预览" }).click();
    const heldReport = dialog.getByLabel("转换预览报告");
    await expect(heldReport).toContainText("当前帧");
    await expect(heldReport).toContainText("帧 5");
    await expect(heldReport.getByText("物化帧").locator("..")).toContainText("1");
    await dialog.getByRole("button", { name: "取消" }).click();
    expect(
      await json<AnnotationDto[]>(
        await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers }),
      ),
    ).toHaveLength(2);
  });

  test("raster Mask converts to an exact tight BBox without pretending to be rotated", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "donut_three",
    });
    await seed.advanceTask({
      taskId,
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    const token = await seed.accessToken(data.annotator_email);
    const headers = { Authorization: `Bearer ${token}` };
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId(`box-list-item-${fixture.annotation_id}`).click({ force: true });
    await page.locator('button[aria-label="转为矢量几何"]:visible').last().click();

    const dialog = page.getByRole("dialog", { name: "标注转换中心" });
    await dialog.getByLabel("目标类型").click();
    await page.getByRole("option", { name: "紧致 BBox" }).click();
    await dialog.getByRole("button", { name: "生成预览" }).click();
    await expect(dialog.getByLabel("转换预览报告")).toContainText("紧致 BBox");
    await expect(dialog.getByLabel("转换预览报告")).toContainText("紧致框包含了背景像素");
    await dialog.getByRole("button", { name: "执行转换" }).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("会改变像素真值");
    const executed = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await confirm.getByRole("button", { name: "确认执行" }).click();
    const result = await json<{ created_annotations: AnnotationDto[] }>(await executed);
    const bbox = result.created_annotations[0].geometry;
    expect(bbox.type).toBe("bbox");
    expect(bbox.x).toBeCloseTo(3 / 64, 8);
    expect(bbox.y).toBeCloseTo(3 / 48, 8);
    expect(bbox.w).toBeCloseTo(57 / 64, 8);
    expect(bbox.h).toBeCloseTo(40 / 48, 8);
    const final = await json<AnnotationDto[]>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers }),
    );
    expect(final).toHaveLength(2);
    expect(final.some((annotation) => annotation.geometry.type === "rotated_bbox")).toBe(false);
  });
});
