import { expect, test, type Locator, type Page } from "@playwright/test";
import type { TrainingParameterSchema } from "../../src/api/generated/types.gen";

const projectId = "108b324d-a322-43b5-8efe-000000000001";
const dataset = {
  id: "training-reset-dataset",
  project_id: projectId,
  name: "COCO8",
  task: "detect",
  status: "ready",
  package_sha256: "d".repeat(64),
  manifest: {},
  contract_profile: {
    schema: 3,
    task: "detect",
    media_type: "image",
    source_media_type: "image",
    format: "ultralytics-yolo",
    geometry: "normalized-bbox",
    bridge: "ultralytics-bbox-1",
  },
};
const schema: TrainingParameterSchema = {
  adapter_revision: 2,
  framework: "ultralytics",
  framework_version: "8.4.177",
  model_family: "yolo11",
  task: "detect",
  schema_id: "training-reset-schema",
  schema_digest: "a".repeat(64),
  source: "native",
  source_sha256: "b".repeat(64),
  groups: { basic: "基础训练", optimizer: "优化器与学习率" },
  fields: [
    { key: "batch", label: "固定批大小", default: 8, group: "basic" },
    { key: "lr0", label: "初始学习率", default: 0.01, group: "optimizer" },
  ].map((field) => ({
    ...field,
    native_default: field.default,
    status: "editable" as const,
    rules: { type: "number", minimum: 0 },
    aliases: [],
    native_type: "number",
    defined_in: "native",
    reason: "",
  })),
};

async function openEditor(page: Page) {
  const user = {
    id: "training-reset-user",
    email: "training-reset@example.test",
    name: "训练交互测试",
    role: "super_admin",
    status: "active",
  };
  await page.addInitScript((user) => {
    localStorage.setItem("token", "training-reset-token");
    localStorage.setItem(
      "auth-storage",
      JSON.stringify({
        state: { user, token: "training-reset-token", sessionId: "reset-session" },
      }),
    );
  }, user);
  // This interaction fixture never reads or changes a backing database or starts training.
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const responses: Record<string, unknown> = {
      "/auth/me": user,
      [`/projects/${projectId}`]: {
        id: projectId,
        name: "训练重置测试",
        status: "in_progress",
        data_type: "image",
        labels: [],
        config: {},
      },
      [`/projects/${projectId}/access`]: {
        project_id: projectId,
        user_id: user.id,
        capabilities: ["project.manage"],
      },
      [`/projects/${projectId}/datasets`]: [],
      [`/projects/${projectId}/training-datasets`]: { total: 1, items: [dataset] },
      [`/projects/${projectId}/training-datasets/${dataset.id}`]: dataset,
      [`/projects/${projectId}/training-runs/parameter-schema`]: schema,
      [`/projects/${projectId}/training-runs/catalog`]: {
        enabled: true,
        online: true,
        busy: false,
        resource_id: "test-gpu",
        items: [
          {
            framework: "ultralytics",
            version: "8.4.177",
            image_digest: `sha256:${"e".repeat(64)}`,
            base_models: [{ id: "yolo11n", filename: "yolo11n.pt", sha256: "f".repeat(64) }],
            capabilities: { tasks: ["detect"] },
          },
        ],
      },
    };
    await route.fulfill({ json: responses[path] ?? { total: 0, items: [] } });
  });
  await page.goto(`/projects/${projectId}/learning/new`);
  const dialog = page.getByRole("dialog", { name: "新建训练实验" });
  await dialog.getByLabel("实验任务", { exact: true }).selectOption("detect");
  await dialog.getByLabel("实验名称", { exact: true }).fill("重置交互");
  await dialog.getByLabel("训练数据", { exact: true }).selectOption(dataset.id);
  await dialog.getByLabel("基础模型", { exact: true }).selectOption("yolo11n");
  await dialog.getByRole("button", { name: "下一步", exact: true }).click();
  await expect(dialog.getByLabel("搜索训练参数")).toBeVisible();
  return dialog;
}

async function clickResetArea(page: Page, button: Locator) {
  await button.scrollIntoViewIfNeeded();
  const box = await button.boundingBox();
  expect(box).not.toBeNull();
  // A disabled locator cannot be clicked normally. Coordinates exercise browser hit testing,
  // including whether an inactive action passes its click through to the chapter underneath.
  await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
}

test("group reset never toggles the chapter, including after it becomes disabled", async ({
  page,
}) => {
  const dialog = await openEditor(page);
  const basic = dialog
    .locator("details")
    .filter({ has: page.locator("#training-parameter-batch") });
  const reset = basic.locator("..").getByRole("button", { name: "重置本组", exact: true });
  const batch = dialog.getByLabel("固定批大小", { exact: true });

  await expect(basic).toHaveAttribute("open", "");
  await expect(reset).toBeDisabled();
  await clickResetArea(page, reset);
  await expect(basic).toHaveAttribute("open", "");

  await batch.fill("4");
  await expect(reset).toBeEnabled();
  await reset.click();
  await expect(batch).toHaveValue("8");
  await expect(reset).toBeDisabled();
  await clickResetArea(page, reset);
  await expect(basic).toHaveAttribute("open", "");

  await basic.locator("summary").click();
  await expect(basic).not.toHaveAttribute("open");
  await clickResetArea(page, reset);
  await expect(basic).not.toHaveAttribute("open");
  await basic.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(basic).toHaveAttribute("open", "");

  await batch.fill("4");
  await basic.locator("summary").click();
  await expect(basic).not.toHaveAttribute("open");
  await reset.click();
  await expect(reset).toBeDisabled();
  await expect(basic).not.toHaveAttribute("open");
});
