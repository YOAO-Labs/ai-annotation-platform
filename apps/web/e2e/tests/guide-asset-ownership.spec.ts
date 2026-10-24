import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { test, expect, type SeedAPI } from "../fixtures/seed";

const API = `${process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010"}/api/v1`;
const PNG = readFileSync(new URL("../fixtures/markdown-example.png", import.meta.url));

type GuideEntry = {
  key: string;
  original_name: string;
  content_type: string;
  size: number;
  uploaded_at: string;
};

type ProjectGuide = {
  id: string;
  annotation_guide: string;
  guide_assets: GuideEntry[];
};

async function prepare(page: Page, request: APIRequestContext, seed: SeedAPI) {
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const url = `${API}/projects/${data.project_id}`;
  const patched = await request.patch(url, {
    headers,
    data: { annotation_guide: "# 图片归属验收\n\n原始字节与独立引用。" },
  });
  expect(patched.ok(), await patched.text()).toBe(true);
  await seed.injectToken(page, data.admin_email);
  return {
    ...data,
    headers,
    url,
    async read(): Promise<ProjectGuide> {
      const response = await request.get(url, { headers });
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    },
  };
}

async function openGuide(page: Page, projectId: string) {
  await page.goto(`/projects/${projectId}/settings?section=annotation-guide`);
  const editor = page.getByTestId("markdown-editor");
  await expect(editor).toBeVisible({ timeout: 30_000 });
  return editor;
}

async function mode(editor: Locator, label: "编辑" | "源码" | "预览") {
  await editor
    .locator("button, [role=tab]")
    .filter({ hasText: new RegExp(`^${label}$`) })
    .click();
}

async function pasteFile(editor: Locator, bytes: Buffer, name: string, mime: string) {
  const target = editor
    .locator('.mdxeditor-contenteditable-wrapper > [contenteditable="true"]')
    .first();
  await target.focus();
  await target.press("Control+End");
  await target.press("Enter");
  await target.evaluate(
    (element, file) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(file.bytes)], file.name, { type: file.mime }));
      element.dispatchEvent(
        new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: transfer,
        }),
      );
    },
    { bytes: Array.from(bytes), name, mime },
  );
}

async function uploadInEditor(
  page: Page,
  editor: Locator,
  projectId: string,
  bytes: Buffer,
  name: string,
  mime: string,
): Promise<GuideEntry> {
  const uploaded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/projects/${projectId}/guide-assets/upload` &&
      response.request().method() === "POST" &&
      new URL(response.url()).searchParams.get("file_name") === name,
  );
  await pasteFile(editor, bytes, name, mime);
  const response = await uploaded;
  expect(response.ok(), await response.text()).toBe(true);
  expect(response.request().headers()["content-type"]).toBe(mime);
  const entry = (await response.json()) as GuideEntry;
  await expect(editor.getByRole("img", { name, exact: true })).toBeVisible();
  await page.getByTestId("guide-save").click();
  await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
  return entry;
}

async function loadedImage(image: Locator) {
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth))
    .toBeGreaterThan(0);
}

function svgBytes(address: string) {
  expect(address).toMatch(/^data:image\/svg\+xml;base64,/);
  return Buffer.from(address.slice(address.indexOf(",") + 1), "base64");
}

test.describe("guide image ownership in the browser", () => {
  test.setTimeout(120_000);

  test("SVG remains exact in edit, preview and zoom and cannot access application storage", async ({
    page,
    context,
    request,
    seed,
  }, info) => {
    const fixture = await prepare(page, request, seed);
    const marker = `guide-svg-${fixture.project_id}`;
    const executionMarker = `SVG script executed ${fixture.project_id}`;
    const imageScriptExecutions: string[] = [];
    page.on("console", (message) => {
      if (message.text() === executionMarker) imageScriptExecutions.push(message.text());
    });
    const name = "源隔离 图%一.svg";
    const bytes = Buffer.from(
      `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48" viewBox="0 0 64 48">\n` +
        `<rect width="64" height="48" fill="teal"/><text x="4" y="24">指南 图%</text>\n` +
        `<script><![CDATA[console.info(${JSON.stringify(executionMarker)});` +
        `document.documentElement.setAttribute('data-script-ran','yes');` +
        `document.documentElement.setAttribute('data-script-origin',location.origin);` +
        `try{document.documentElement.setAttribute('data-storage-read',localStorage.getItem(${JSON.stringify(marker)}));` +
        `localStorage.setItem(${JSON.stringify(marker)},'svg-document');` +
        `document.documentElement.setAttribute('data-storage-write','allowed');}` +
        `catch{document.documentElement.setAttribute('data-storage-write','blocked');}]]></script>\n</svg>`,
      "utf8",
    );
    let documentPage: Page | undefined;
    try {
      const editor = await openGuide(page, fixture.project_id);
      await page.evaluate((key) => localStorage.setItem(key, "application-only"), marker);
      const asset = await uploadInEditor(
        page,
        editor,
        fixture.project_id,
        bytes,
        name,
        "image/svg+xml",
      );
      const stored = await request.get(`${fixture.url}/guide-assets/content`, {
        headers: fixture.headers,
        params: { key: asset.key },
      });
      expect(stored.ok(), await stored.text()).toBe(true);
      expect(await stored.body()).toEqual(bytes);
      expect(stored.headers()["content-disposition"]).toMatch(/^attachment;/);
      expect(stored.headers()["x-content-type-options"]).toBe("nosniff");
      const editedImage = editor.getByRole("img", { name, exact: true });
      await loadedImage(editedImage);
      expect(svgBytes((await editedImage.getAttribute("src"))!)).toEqual(bytes);
      await mode(editor, "源码");
      await expect(editor.locator(".cm-content:visible")).toContainText(`guide-asset:${asset.key}`);
      expect((await fixture.read()).annotation_guide).toContain(`guide-asset:${asset.key}`);
      await mode(editor, "预览");
      const image = editor.getByRole("img", { name, exact: true });
      await loadedImage(image);
      const address = (await image.getAttribute("src"))!;
      expect(svgBytes(address)).toEqual(bytes);
      await editor.getByRole("button", { name: `放大图片：${name}`, exact: true }).click();
      const zoom = page
        .getByRole("dialog", { name, exact: true })
        .getByRole("img", { name, exact: true });
      await loadedImage(zoom);
      expect(svgBytes((await zoom.getAttribute("src"))!)).toEqual(bytes);
      expect(await page.evaluate((key) => localStorage.getItem(key), marker)).toBe(
        "application-only",
      );
      expect(await page.locator("html").getAttribute("data-script-ran")).toBeNull();
      expect(imageScriptExecutions).toEqual([]);

      // Open the actual rendered address, rather than constructing a substitute.
      documentPage = await context.newPage();
      const browserMessages: string[] = [];
      documentPage.on("console", (message) => browserMessages.push(message.text()));
      let navigationBlocked = false;
      try {
        await documentPage.goto(address, { waitUntil: "load" });
      } catch (error) {
        const evidence = [String(error), ...browserMessages].join("\n");
        expect(evidence).toMatch(
          /not allowed.*(?:navigate|data)|ERR_BLOCKED_BY_(?:CLIENT|RESPONSE)|ERR_UNSAFE_REDIRECT/i,
        );
        expect(documentPage.url()).not.toBe(address);
        navigationBlocked = true;
        await info.attach("svg-document-navigation-blocked", {
          body: JSON.stringify({ outcome: "browser blocked data navigation", evidence }),
          contentType: "application/json",
        });
      }
      if (!navigationBlocked) {
        expect(documentPage.url()).toBe(address);
        const proof = await documentPage.evaluate((key) => {
          let storageAccess = "allowed";
          try {
            localStorage.getItem(key);
            localStorage.setItem(key, "browser-document-probe");
          } catch {
            storageAccess = "blocked";
          }
          return {
            origin: location.origin,
            storageAccess,
            scriptRan: document.documentElement.getAttribute("data-script-ran"),
            scriptOrigin: document.documentElement.getAttribute("data-script-origin"),
            scriptStorage: document.documentElement.getAttribute("data-storage-write"),
          };
        }, marker);
        expect(proof.origin).toBe("null");
        expect(proof.storageAccess).toBe("blocked");
        if (proof.scriptRan === "yes") {
          expect(proof.scriptOrigin).toBe("null");
          expect(proof.scriptStorage).toBe("blocked");
        }
        await info.attach("svg-document-origin", {
          body: JSON.stringify(proof),
          contentType: "application/json",
        });
      }
      expect(await page.evaluate((key) => localStorage.getItem(key), marker)).toBe(
        "application-only",
      );
    } finally {
      try {
        await documentPage?.close();
      } finally {
        try {
          await page.evaluate((key) => localStorage.removeItem(key), marker);
        } finally {
          await page.goto("/projects");
        }
      }
    }
  });

  test("a cloned PNG survives source deletion and the last project unlinks without signed URLs", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(page, request, seed);
    let targetId: string | undefined;
    try {
      const editor = await openGuide(page, fixture.project_id);
      const name = "共享 引用%示例.png";
      const asset = await uploadInEditor(page, editor, fixture.project_id, PNG, name, "image/png");
      await expect
        .poll(async () => (await fixture.read()).annotation_guide)
        .toContain(`guide-asset:${asset.key}`);
      const cloned = await request.post(`${API}/projects`, {
        headers: fixture.headers,
        data: {
          name: `B11 browser copy ${fixture.project_id}`,
          type_label: "图像-检测",
          data_type: "image",
          source_project_id: fixture.project_id,
          copy_annotation_guide: true,
        },
      });
      expect(cloned.ok(), await cloned.text()).toBe(true);
      const target = (await cloned.json()) as ProjectGuide;
      targetId = target.id;
      expect(target.guide_assets).toEqual([asset]);
      expect(asset.key).toMatch(new RegExp(`^projects/${fixture.project_id}/guide/`));
      const removed = await request.delete(fixture.url, { headers: fixture.headers });
      expect(removed.status(), await removed.text()).toBe(204);
      let targetEditor = await openGuide(page, targetId);
      await mode(targetEditor, "预览");
      await loadedImage(targetEditor.getByRole("img", { name, exact: true }));
      await page.reload();
      targetEditor = page.getByTestId("markdown-editor");
      await expect(targetEditor).toBeVisible();
      await mode(targetEditor, "预览");
      const image = targetEditor.getByRole("img", { name, exact: true });
      await loadedImage(image);
      expect(await image.getAttribute("src")).toMatch(/^blob:/);
      const targetURL = `${API}/projects/${targetId}`;
      const retained = await request.get(`${targetURL}/guide-assets/content`, {
        headers: fixture.headers,
        params: { key: asset.key },
      });
      expect(retained.ok(), await retained.text()).toBe(true);
      expect(await retained.body()).toEqual(PNG);
      const legacy = [
        await request.get(`${targetURL}/guide-assets/sign-url`, {
          headers: fixture.headers,
          params: { key: asset.key },
        }),
        await request.post(`${targetURL}/guide-assets/upload-init`, {
          headers: fixture.headers,
          data: { filename: name, content_type: "image/png", size: PNG.byteLength },
        }),
        await request.post(`${targetURL}/guide-assets/upload-complete`, {
          headers: fixture.headers,
          data: { key: asset.key, original_name: name, content_type: "image/png" },
        }),
      ];
      for (const response of legacy) {
        expect(response.status()).toBe(409);
        const body = await response.text();
        expect(body).not.toMatch(/"(?:url|upload_url)"|X-Amz-|AWSAccessKeyId/i);
      }
      const unlinked = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === `/api/v1/projects/${targetId}/guide-assets` &&
          response.request().method() === "DELETE",
      );
      await page
        .getByTestId("guide-asset-list")
        .getByRole("listitem")
        .filter({ hasText: name })
        .getByRole("button", { name: "删除", exact: true })
        .click();
      expect((await unlinked).ok()).toBe(true);
      const noReference = await request.get(`${targetURL}/guide-assets/content`, {
        headers: fixture.headers,
        params: { key: asset.key },
      });
      expect(noReference.status()).toBe(404);
      await page.reload();
      targetEditor = page.getByTestId("markdown-editor");
      await expect(targetEditor).toBeVisible();
      await mode(targetEditor, "预览");
      await expect(targetEditor.getByText("图片已不存在", { exact: true })).toBeVisible();
    } finally {
      try {
        await page.goto("/projects");
      } finally {
        // Only this test's public clone needs explicit cleanup; seed owns its source.
        if (targetId) {
          const removed = await request.delete(`${API}/projects/${targetId}`, {
            headers: fixture.headers,
          });
          expect([204, 404]).toContain(removed.status());
        }
      }
    }
  });
});
