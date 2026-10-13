import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "../fixtures/seed";
import { startAiRequestBackend } from "../fixtures/ai-request-backend";
import {
  closeContextToolbar,
  expectStableContextCapsule,
  openContextToolbar,
} from "../fixtures/context-toolbar";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const apiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../api");

function prepareRasterImage(taskId: string) {
  // The seed fixture uses SVGs; the actual ROI pipeline needs a decodable raster.
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import asyncio, io, os, re, sys, uuid
from PIL import Image
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from app.services.storage import StorageService
task_id = str(uuid.UUID(sys.argv[1]))
url = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
assert (url.database or "").endswith(("_e2e", "_test"))
async def prepare():
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            row = (await connection.execute(text("SELECT dataset_item_id, file_path FROM tasks WHERE id=:id FOR UPDATE"), {"id": task_id})).one()
            # Owned fixtures name the seed SVG after their namespace; derive the
            # directory from the task itself so the replacement PNG stays inside
            # the same owned prefix and owned-cleanup removes it.
            owned = re.fullmatch(r"e2e/owned/([a-z0-9]{4,12})/image/task-[0-9]+[.]svg", row.file_path or "")
            assert owned, f"expected an owned image fixture task path, got {row.file_path!r}"
            storage = StorageService()
            buffer = io.BytesIO()
            Image.new("RGB", (256, 256), "#d7e4ee").save(buffer, format="PNG")
            key = f"e2e/owned/{owned.group(1)}/image/context-toolbar-{task_id}.png"
            storage.client.put_object(Bucket=storage.datasets_bucket, Key=key, Body=buffer.getvalue(), ContentType="image/png")
            await connection.execute(text("UPDATE tasks SET file_path=:key, file_name='context-toolbar.png' WHERE id=:id"), {"id": task_id, "key": key})
            await connection.execute(text("UPDATE dataset_items SET file_path=:key, file_name='context-toolbar.png', width=256, height=256, file_size=:size WHERE id=:id"), {"id": row.dataset_item_id, "key": key, "size": buffer.tell()})
    finally:
        await engine.dispose()
asyncio.run(prepare())
`,
      taskId,
    ],
    { cwd: apiRoot, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", DEBUG: "false" } },
  );
}

// The real secondary endpoint creates this exact temporary crop. Delete only
// this test's source annotation key, never another session's ROI prefix.
function removeTestImages(taskId: string, annotationId?: string) {
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import asyncio, os, re, sys, uuid
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from app.services.storage import StorageService
task_id = str(uuid.UUID(sys.argv[1]))
annotation_id = str(uuid.UUID(sys.argv[2])) if len(sys.argv) > 2 else None
url = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
assert (url.database or "").endswith(("_e2e", "_test"))
def assert_absent(storage, bucket, key):
    assert not storage.client.list_objects_v2(Bucket=bucket, Prefix=key).get("Contents"), f"leftover {bucket}/{key}"
async def cleanup():
    engine = create_async_engine(url)
    # One shared client for every deletion and postcondition check in this run.
    storage = StorageService()
    try:
        async with engine.begin() as connection:
            file_path = await connection.scalar(text("SELECT file_path FROM tasks WHERE id=:id"), {"id": task_id})
        if file_path:
            # Delete only this test's replacement PNG, at the exact owned
            # namespace and exact taskId filename derived from the row; the seed
            # SVG is left to fixture teardown.
            replacement = re.fullmatch(
                rf"e2e/owned/([a-z0-9]{{4,12}})/image/context-toolbar-{re.escape(task_id)}[.]png",
                file_path,
            )
            if replacement:
                storage.client.delete_object(Bucket=storage.datasets_bucket, Key=file_path)
                assert_absent(storage, storage.datasets_bucket, file_path)
        if annotation_id is not None:
            key = f"roi-crops/secondary/{annotation_id}/0.jpg"
            storage.client.delete_object(Bucket=storage.import_bucket, Key=key)
            assert_absent(storage, storage.import_bucket, key)
    finally:
        await engine.dispose()
asyncio.run(cleanup())
`,
      taskId,
      ...(annotationId ? [annotationId] : []),
    ],
    { cwd: apiRoot, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", DEBUG: "false" } },
  );
}

test("secondary capsule preserves configuration and real child writes across collapse and reload", async ({
  page,
  request,
  seed,
}, testInfo) => {
  test.setTimeout(120_000);
  const data = await seed.owned();
  const token = await seed.accessToken(data.admin_email);
  const taskId = data.task_ids[0];
  const backend = await startAiRequestBackend({ secondary: true });
  let detach: (() => Promise<void>) | undefined;
  let annotationId: string | undefined;
  const errors: string[] = [];
  const apiErrors: Array<{ status: number; path: string }> = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400 && response.url().includes("/api/")) {
      apiErrors.push({ status: response.status(), path: new URL(response.url()).pathname });
    }
  });
  try {
    detach = await backend.attach(request, {
      apiBase: API_BASE,
      projectId: data.project_id,
      token,
      defaultBackendId: data.ml_backend_id,
    });
    prepareRasterImage(taskId);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const parent = await seed.createTaskAnnotation(taskId, data.admin_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.7, h: 0.7 },
    });
    annotationId = parent.id;
    await seed.injectToken(page, data.admin_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 20_000 });
    await page.getByTestId(`box-list-item-${parent.id}`).click();
    const capsule = page.getByTestId("secondary-tool-capsule");
    await expect(capsule).toBeVisible();
    await expect(page.getByTestId("secondary-quick-disclosure")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await page.screenshot({ path: testInfo.outputPath("secondary-capsule.png") });
    await page.getByTestId("secondary-settings-trigger").hover();
    await expect(page.getByTestId("secondary-quick-disclosure")).toHaveAttribute(
      "aria-hidden",
      "false",
    );
    await expect(page.getByTestId("secondary-prompt")).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("secondary-hover.png"),
      animations: "disabled",
    });
    await expect(page.getByTestId("secondary-cap-select")).toBeHidden();
    await page.getByTestId("secondary-settings-trigger").hover();
    await expect(page.getByTestId("secondary-run")).toBeDisabled();
    await page.getByTestId("secondary-prompt").fill("car");
    await expect(page.getByTestId("secondary-run")).toBeEnabled();
    const quickConfidence = page.getByTestId("secondary-quick-confidence").getByRole("slider");
    await quickConfidence.focus();
    await quickConfidence.press("End");
    await quickConfidence.press("ArrowLeft");
    const selectedConfidence = Number(await quickConfidence.inputValue());
    await expectStableContextCapsule(page, "secondary");
    await openContextToolbar(page, "secondary");
    await page
      .getByTestId("secondary-toolbar")
      .getByTestId("ai-variant-size")
      .selectOption("large");
    await page.getByTestId("secondary-params-toggle").click();
    const confidence = page
      .getByTestId("secondary-toolbar")
      .getByTestId("schema-field-confidence")
      .getByRole("slider");
    await expect(confidence).toHaveValue(String(selectedConfidence));
    await closeContextToolbar(page, "secondary");
    await expect(page.getByTestId("secondary-prompt")).toHaveValue("car");
    expect(backend.requests).toHaveLength(0);

    await page.getByTestId("secondary-settings-trigger").hover();
    backend.hold();
    const write = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/annotations/${parent.id}/secondary-inference`) &&
        response.request().method() === "POST",
    );
    await page.getByTestId("secondary-run").dblclick();
    await expect.poll(() => backend.requests.length).toBe(1);
    await expect(page.getByTestId("secondary-run")).toBeDisabled();
    await openContextToolbar(page, "secondary");
    await expect(page.getByTestId("secondary-toolbar").getByTestId("ai-variant-size")).toHaveValue(
      "large",
    );
    await expect(page.getByTestId("secondary-prompt")).toHaveValue("car");
    backend.release();
    const response = await write;
    expect(response.ok(), await response.text()).toBe(true);
    const result = await response.json();
    expect(result.created_children).toHaveLength(1);
    expect(result.created_children[0].parent_annotation_id).toBe(parent.id);
    expect(backend.requests[0].context).toMatchObject({
      model_id: "e2-detect",
      model_variants: { size: "large" },
    });
    expect(JSON.stringify(backend.requests[0].context)).toContain(String(selectedConfidence));
    await closeContextToolbar(page, "secondary");

    // The same primary inputs remain bounded and actionable in a narrowed canvas.
    for (const theme of ["light", "dark"] as const) {
      await page.evaluate(
        (value) => document.documentElement.setAttribute("data-theme", value),
        theme,
      );
      for (const width of [1440, 1024]) {
        await page.setViewportSize({ width, height: 900 });
        await openContextToolbar(page, "secondary");
        const panel = page.getByTestId("secondary-toolbar");
        await expect
          .poll(async () => {
            const bounds = await panel.boundingBox();
            const host = await capsule.evaluate((element) => {
              const rect = (element as HTMLElement).offsetParent?.getBoundingClientRect();
              return rect
                ? { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }
                : null;
            });
            return (
              !!bounds &&
              !!host &&
              bounds.x >= host.left - 1 &&
              bounds.x + bounds.width <= host.right + 1 &&
              bounds.y >= host.top - 1 &&
              bounds.y + bounds.height <= host.bottom + 1
            );
          })
          .toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`secondary-${theme}-${width}.png`) });
        await closeContextToolbar(page, "secondary");
      }
    }

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.reload();
    await expect(page.getByTestId(`box-list-item-${result.created_children[0].id}`)).toBeVisible({
      timeout: 20_000,
    });
    await page.getByTestId(`box-list-item-${parent.id}`).click();
    await openContextToolbar(page, "secondary");
    await expect(page.getByTestId("secondary-toolbar").getByTestId("ai-variant-size")).toHaveValue(
      "large",
    );
    await page.getByTestId("secondary-params-toggle").click();
    await expect(
      page
        .getByTestId("secondary-toolbar")
        .getByTestId("schema-field-confidence")
        .getByRole("slider"),
    ).toHaveValue(String(selectedConfidence));
    // Prompt is intentionally session-only, while model parameters persist.
    await expect(page.getByTestId("secondary-prompt")).toHaveValue("");
    await page.getByTestId("secondary-prompt").fill("car");
    backend.failNext();
    const failed = page.waitForResponse(
      (item) => item.url().endsWith("/secondary-inference") && item.status() === 503,
    );
    await page.getByTestId("secondary-run").click();
    await failed;
    await expect(page.getByTestId("secondary-run")).toBeEnabled();
    await closeContextToolbar(page, "secondary");
    await expect(page.getByTestId("secondary-prompt")).toHaveValue("car");
    expect(errors).toEqual([]);
    expect(apiErrors).toEqual([
      { status: 503, path: `/api/v1/tasks/${taskId}/annotations/${parent.id}/secondary-inference` },
    ]);
    expect(backend.requests).toHaveLength(2);
  } finally {
    backend.release();
    await page.goto("about:blank", { timeout: 5_000 }).catch(() => {});
    try {
      removeTestImages(taskId, annotationId);
    } finally {
      try {
        await detach?.();
      } finally {
        await backend.close();
        await seed.owned();
      }
    }
  }
});
