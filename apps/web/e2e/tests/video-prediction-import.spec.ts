import type { APIResponse } from "@playwright/test";

import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}

interface PredictionShape {
  shape_index: number;
  geometry: { type: string; keyframes?: Array<{ source?: string }> };
}

interface Prediction {
  id: string;
  source: string;
  result: PredictionShape[];
}

interface Annotation {
  id: string;
  annotation_type: string;
  parent_prediction_id?: string | null;
}

function auth(token: string) {
  return { Authorization: `Bearer ${token}` };
}

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

test("AAP JSON 视频预测可预检、审阅并持久化采纳/忽略", async ({ page, request, seed }) => {
  const data = await seed.owned();
  const { task_id: taskId } = await seed.videoTask(data.project_id);
  await seed.configureRasterMask(data.project_id, true);
  const token = await seed.accessToken(data.admin_email);
  const task = await json<{ display_id: string }>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}`, { headers: auth(token) }),
  );
  const rle = {
    encoding: "coco_rle" as const,
    size: [810, 1440] as [number, number],
    counts: [200_000, 20_000, 946_400],
  };
  const mask = await json<MaskReference>(
    await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
      headers: auth(token),
      data: rle,
    }),
  );
  const envelope = {
    schema_version: "1.3",
    mask_objects: { [mask.sha256]: rle },
    tasks: [
      {
        task_match: { display_id: task.display_id },
        media_type: "video",
        predictions: [
          {
            class_name: "car",
            confidence: 0.95,
            shapes: [
              {
                type: "video_bbox",
                frame_index: 0,
                x: 0.08,
                y: 0.12,
                w: 0.18,
                h: 0.24,
              },
              {
                type: "video_track_bbox",
                track_id: "external-bbox",
                keyframes: [
                  {
                    frame_index: 0,
                    bbox: { x: 0.32, y: 0.12, w: 0.18, h: 0.24 },
                    source: "manual",
                  },
                  {
                    frame_index: 10,
                    bbox: { x: 0.42, y: 0.18, w: 0.18, h: 0.24 },
                    source: "manual",
                  },
                ],
              },
            ],
          },
          {
            class_name: "car",
            confidence: 0.85,
            shapes: [
              {
                type: "video_track_polygon",
                track_id: "external-polygon",
                keyframes: [
                  {
                    frame_index: 0,
                    points: [
                      [0.1, 0.55],
                      [0.25, 0.5],
                      [0.28, 0.72],
                    ],
                    source: "manual",
                  },
                  {
                    frame_index: 10,
                    points: [
                      [0.2, 0.55],
                      [0.35, 0.5],
                      [0.38, 0.72],
                    ],
                    source: "manual",
                  },
                ],
              },
              {
                type: "video_track_mask",
                track_id: "external-mask",
                keyframes: [{ frame_index: 0, mask, source: "manual" }],
              },
            ],
          },
          {
            class_name: "car",
            confidence: 0.75,
            geometry: {
              type: "video_track_polyline",
              track_id: "external-polyline",
              keyframes: [
                {
                  frame_index: 0,
                  points: [
                    [0.6, 0.2],
                    [0.72, 0.35],
                    [0.8, 0.25],
                  ],
                  source: "manual",
                },
                {
                  frame_index: 10,
                  points: [
                    [0.62, 0.25],
                    [0.74, 0.4],
                    [0.82, 0.3],
                  ],
                  source: "manual",
                },
              ],
            },
          },
        ],
      },
    ],
  };
  const file = {
    name: "video-predictions.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(envelope)),
  };

  const dryRun = await json<{ imported: number; dry_run: boolean; errors: unknown[] }>(
    await request.post(
      `${API_BASE}/api/v1/projects/${data.project_id}/predictions/import?format=aap_json&dry_run=true`,
      { headers: auth(token), multipart: { file } },
    ),
  );
  expect(dryRun).toMatchObject({ imported: 3, dry_run: true, errors: [] });
  expect(
    await json<Prediction[]>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}/predictions`, {
        headers: auth(token),
      }),
    ),
  ).toEqual([]);

  const imported = await json<{ imported: number; dry_run: boolean; errors: unknown[] }>(
    await request.post(
      `${API_BASE}/api/v1/projects/${data.project_id}/predictions/import?format=aap_json`,
      { headers: auth(token), multipart: { file } },
    ),
  );
  expect(imported).toMatchObject({ imported: 3, dry_run: false, errors: [] });
  const predictions = await json<Prediction[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/predictions`, {
      headers: auth(token),
    }),
  );
  expect(predictions).toHaveLength(3);
  expect(predictions.every((prediction) => prediction.source === "external_import")).toBe(true);
  expect(
    predictions.flatMap((prediction) => prediction.result).map((shape) => shape.geometry.type),
  ).toEqual(
    expect.arrayContaining([
      "video_bbox",
      "video_track_bbox",
      "video_track_polygon",
      "video_track_polyline",
      "video_track_mask",
    ]),
  );
  expect(
    predictions
      .flatMap((prediction) => prediction.result)
      .flatMap((shape) => shape.geometry.keyframes ?? [])
      .every((keyframe) => keyframe.source === "prediction"),
  ).toBe(true);

  const bboxPrediction = predictions.find((prediction) =>
    prediction.result.some((shape) => shape.geometry.type === "video_bbox"),
  )!;
  const regionPrediction = predictions.find((prediction) =>
    prediction.result.some((shape) => shape.geometry.type === "video_track_mask"),
  )!;
  const bboxIndex = bboxPrediction.result.find(
    (shape) => shape.geometry.type === "video_bbox",
  )!.shape_index;
  const polygonIndex = regionPrediction.result.find(
    (shape) => shape.geometry.type === "video_track_polygon",
  )!.shape_index;
  const maskIndex = regionPrediction.result.find(
    (shape) => shape.geometry.type === "video_track_mask",
  )!.shape_index;
  const bboxCandidateId = `pred-${bboxPrediction.id}-${bboxIndex}`;
  const polygonCandidateId = `pred-${regionPrediction.id}-${polygonIndex}`;
  const maskCandidateId = `pred-${regionPrediction.id}-${maskIndex}`;

  await seed.advanceTask({
    taskId,
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  await seed.injectToken(page, data.annotator_email);
  const maskContent = page.waitForResponse(
    (response) =>
      response
        .url()
        .endsWith(
          `/api/v1/tasks/${taskId}/predictions/${regionPrediction.id}/mask-content/${maskIndex}/0`,
        ) && response.request().method() === "GET",
  );
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 15_000 });
  expect((await maskContent).status()).toBe(200);
  const candidateRows = page.locator('[data-testid^="box-list-item-pred-"]');
  await expect(candidateRows).toHaveCount(5, { timeout: 15_000 });
  await expect(candidateRows).toContainText(["导入", "导入", "导入", "导入", "导入"]);
  const maskRow = page.getByTestId(`box-list-item-${maskCandidateId}`);
  await maskRow.click();
  await expect(maskRow).toHaveClass(/!border-brand/);
  await page.getByRole("button", { name: "收起浮窗", exact: true }).click();

  const bboxRow = page.getByTestId(`box-list-item-${bboxCandidateId}`);
  const acceptResponse = page.waitForResponse(
    (response) =>
      response
        .url()
        .includes(`/predictions/${bboxPrediction.id}/accept?shape_index=${bboxIndex}`) &&
      response.request().method() === "POST",
  );
  // 常驻 success toast (右上 sonner) 与行内浮出按钮的过渡动画会拦截合成指针事件,
  // 用 dispatchEvent 直接触发 onClick, 与行内按钮的真实 handler 一致。
  await bboxRow.getByRole("button", { name: "采纳预测" }).dispatchEvent("click");
  const accepted = (await (await acceptResponse).json()) as Annotation[];
  expect(accepted).toHaveLength(1);
  expect(accepted[0]).toMatchObject({
    annotation_type: "video_bbox",
    parent_prediction_id: bboxPrediction.id,
  });
  await expect(bboxRow).toHaveCount(0);

  const polygonRow = page.getByTestId(`box-list-item-${polygonCandidateId}`);
  const rejectResponse = page.waitForResponse(
    (response) =>
      response
        .url()
        .includes(`/predictions/${regionPrediction.id}/reject?shape_index=${polygonIndex}`) &&
      response.request().method() === "POST",
  );
  await polygonRow.getByRole("button", { name: "忽略预测" }).dispatchEvent("click");
  expect((await rejectResponse).status()).toBe(204);
  await expect(polygonRow).toHaveCount(0);

  await page.reload();
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId(`box-list-item-${bboxCandidateId}`)).toHaveCount(0);
  await expect(page.getByTestId(`box-list-item-${polygonCandidateId}`)).toHaveCount(0);
  await expect(page.getByTestId(`box-list-item-${maskCandidateId}`)).toBeVisible();
  const annotations = await json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: auth(token),
    }),
  );
  expect(annotations).toContainEqual(
    expect.objectContaining({
      id: accepted[0].id,
      annotation_type: "video_bbox",
      parent_prediction_id: bboxPrediction.id,
    }),
  );
});
