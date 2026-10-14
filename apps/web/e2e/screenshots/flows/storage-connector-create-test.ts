/**
 * 高清母版：创建脱敏 S3 / OSS 连接器并真实测试目录样本数。
 *
 * 只表达连接器创建和连通性检查，不混入数据集导入向导。
 */
import { expect, type Locator, type Page, type Response } from "@playwright/test";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

export const STORAGE_CONNECTOR_RECORDING_NAME = "道路场景素材（演示）";

const ENDPOINT = process.env.MINIO_ENDPOINT ?? "172.17.0.1:9000";
const BUCKET = process.env.MINIO_DATASETS_BUCKET ?? "datasets";
const BASE_PREFIX = "coco8-dev/train/";
const DEMO_CREDENTIAL = "minioadmin";
const EXPECTED_SAMPLE_COUNT = 4;
const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[storage-connector-create-test] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 100, y: 100 }, target, 240);
  pointerByPage.set(page, target);
}

async function enter(page: Page, input: Locator, value: string, delay = 35): Promise<void> {
  await moveTo(page, input);
  await input.click();
  await input.pressSequentially(value, { delay });
  await expect(input).toHaveValue(value);
  await page.waitForTimeout(350);
}

function assertResponseOk(response: Response, action: string): void {
  if (!response.ok()) {
    throw new Error(
      `[storage-connector-create-test] ${action}失败：HTTP ${response.status()} ${response.url()}`,
    );
  }
}

export async function runStorageConnectorCreateTest(
  page: Page,
  onCreated: (connectionId: string) => void,
): Promise<DrawWindow> {
  await page.goto("/datasets");
  await expect(page.getByRole("heading", { name: "数据集", exact: true })).toBeVisible({
    timeout: 10_000,
  });

  const connectorTab = page.getByRole("button", { name: "数据连接器", exact: true });
  await moveTo(page, connectorTab);
  await connectorTab.click();
  await expect(page.getByRole("heading", { name: "数据源连接器", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText("暂无连接器", { exact: true })).toBeVisible();
  await page.waitForTimeout(900);

  const drawStartMs = Date.now();
  await page.waitForTimeout(1_400);
  const createEntry = page.getByRole("button", { name: "新建数据源", exact: true }).first();
  await moveTo(page, createEntry);
  await createEntry.click();

  const dialog = page.getByRole("dialog", { name: "新建数据源" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox").first()).toHaveValue("s3");
  await page.waitForTimeout(650);

  await enter(page, dialog.getByPlaceholder("external-data"), STORAGE_CONNECTOR_RECORDING_NAME, 55);
  await enter(page, dialog.getByPlaceholder("http://minio.example:9000"), ENDPOINT);
  await enter(page, dialog.getByPlaceholder("datasets"), BUCKET, 55);
  await enter(page, dialog.getByPlaceholder("imports/"), BASE_PREFIX);

  const https = dialog.getByRole("checkbox", { name: "HTTPS" });
  await expect(https).toBeChecked();
  await moveTo(page, https);
  await https.click();
  await expect(https).not.toBeChecked();
  await page.waitForTimeout(550);

  await enter(page, dialog.getByPlaceholder("AK"), DEMO_CREDENTIAL, 45);
  await enter(page, dialog.getByPlaceholder("SK"), DEMO_CREDENTIAL, 45);
  await page.waitForTimeout(700);

  const submit = dialog.getByRole("button", { name: "新建数据源", exact: true });
  await moveTo(page, submit);
  const createdResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/storage-connections",
    { timeout: 20_000 },
  );
  await submit.click();
  const created = await createdResponse;
  assertResponseOk(created, "创建连接器");
  const createdText = await created.text();
  const createdBody = JSON.parse(createdText) as {
    id?: string;
    name?: string;
    kind?: string;
    secret_set?: boolean;
    config?: Record<string, unknown>;
  };
  if (!createdBody.id) throw new Error("[storage-connector-create-test] 创建响应缺少连接器 ID");
  onCreated(createdBody.id);
  if (
    createdBody.name !== STORAGE_CONNECTOR_RECORDING_NAME ||
    createdBody.kind !== "s3" ||
    createdBody.secret_set !== true ||
    createdBody.config?.endpoint !== ENDPOINT ||
    createdBody.config?.bucket !== BUCKET ||
    createdBody.config?.base_prefix !== BASE_PREFIX ||
    createdText.includes('"secret"') ||
    createdText.includes('"secret_key"')
  ) {
    throw new Error(`[storage-connector-create-test] 创建结果不完整或泄露密钥：${createdText}`);
  }

  await expect(dialog).toBeHidden();
  await expect(page.getByText("连接器已创建", { exact: true })).toBeVisible();
  const row = page
    .locator('[class*="connectionRow"]')
    .filter({ hasText: STORAGE_CONNECTOR_RECORDING_NAME });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("已加密", { timeout: 10_000 });
  await expect(row).toContainText(`${BUCKET} / ${ENDPOINT} / ${BASE_PREFIX}`);
  await page.waitForTimeout(1_100);

  const testButton = row.getByRole("button", { name: "测试", exact: true });
  await moveTo(page, testButton);
  const testResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/storage-connections/${createdBody.id}/test`,
    { timeout: 20_000 },
  );
  await testButton.click();
  await expect(row.getByRole("button", { name: "测试中", exact: true })).toBeVisible();
  const tested = await testResponse;
  assertResponseOk(tested, "测试连接");
  const testedBody = (await tested.json()) as {
    ok?: boolean;
    message?: string;
    sample_count?: number | null;
  };
  if (
    testedBody.ok !== true ||
    testedBody.message !== "连接成功" ||
    testedBody.sample_count !== EXPECTED_SAMPLE_COUNT
  ) {
    throw new Error(
      `[storage-connector-create-test] 连接测试语义不完整：${JSON.stringify(testedBody)}`,
    );
  }

  await expect(
    row.getByText(`连接成功 · 样本 ${EXPECTED_SAMPLE_COUNT}`, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("连接成功", { exact: true }).last()).toBeVisible();
  await page.waitForTimeout(3_000);

  return { drawStartMs, drawEndMs: Date.now() };
}
