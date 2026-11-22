import { createHash, randomUUID } from "node:crypto";
import type { APIRequestContext, APIResponse, Page, WebSocketRoute } from "@playwright/test";
import { test, expect, type SeedAPI } from "../fixtures/seed";
import { rangeValueForFrame } from "../helpers/video-timeline-seek";

const API = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const source = (page: Page) => page.getByTestId("video-konva-source");
const stage = (page: Page) => page.getByTestId("video-konva-stage");
const mediaPath = /^\/api\/v1\/media\/original-playback\/[0-9a-f-]{36}$/;

async function fixture(page: Page, seed: SeedAPI) {
  const data = await seed.owned();
  const video = await seed.videoWebCodecs(data.project_id, { fixture: "h264-issue-context" });
  const token = await seed.accessToken(data.admin_email);
  await seed.injectToken(page, data.admin_email);
  await page.evaluate(() => localStorage.setItem("video.experimental.webcodecs", "0"));
  await page.setViewportSize({ width: 1440, height: 1000 });
  return { ...video, ...data, token };
}

async function ready(page: Page) {
  await expect(stage(page)).toBeVisible({ timeout: 30_000 });
  await expect
    .poll(() => source(page).evaluate((node: HTMLVideoElement) => node.readyState))
    .toBeGreaterThanOrEqual(2);
  await expect(source(page)).toHaveAttribute("src", mediaPath);
}

async function seek(page: Page, frame: number) {
  const window = await page.getByTestId("video-playback-overlay").evaluate((node) => ({
    from: Number(node.getAttribute("data-timeline-from")),
    to: Number(node.getAttribute("data-timeline-to")),
  }));
  const mapping = rangeValueForFrame(frame, window);
  expect(mapping.expressible).toBe(true);
  await page.getByLabel("视频帧时间轴", { exact: true }).fill(String(mapping.value));
  await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(frame));
  await expect
    .poll(() => source(page).evaluate((node: HTMLVideoElement) => node.seeking))
    .toBe(false);
}

async function polygonDraft(page: Page) {
  const polygon = page.getByTestId("video-tool-btn-polygon");
  if (await polygon.isVisible()) await polygon.click();
  else {
    await page.getByTestId("tool-dock-more").click();
    await page.getByTestId("tool-overflow-item-polygon").click();
  }
  for (const [x, y] of [
    [0.4, 0.4],
    [0.55, 0.4],
  ]) {
    const bounds = await stage(page).evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return {
        x: rect.x + Number(node.getAttribute("data-media-x")),
        y: rect.y + Number(node.getAttribute("data-media-y")),
        w: Number(node.getAttribute("data-media-width")),
        h: Number(node.getAttribute("data-media-height")),
      };
    });
    await page.mouse.click(bounds.x + bounds.w * x, bounds.y + bounds.h * y);
    await page.waitForTimeout(450);
  }
  await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
}

type PlaybackScope = "task" | "item";

type PlaybackViewer = {
  projectId: string;
  taskId: string;
  prepare(): Promise<APIResponse>;
  open(): Promise<{
    url: string;
    expiresAt: number;
    expectBytes(): Promise<void>;
  }>;
};

async function seedPlaybackViewer(
  request: APIRequestContext,
  seed: SeedAPI,
  namespace: string,
  scope: PlaybackScope,
): Promise<PlaybackViewer> {
  const { project_id: projectId, admin_email: email } = await seed.owned(namespace);
  const { task_id: taskId } = await seed.videoTask(projectId);
  const headers = { Authorization: `Bearer ${await seed.accessToken(email)}` };
  let original = `${API}/api/v1/files/tasks/${taskId}/original`;
  if (scope === "item") {
    const datasets = await request.get(`${API}/api/v1/projects/${projectId}/datasets`, { headers });
    expect(datasets.status(), await datasets.text()).toBe(200);
    const linked = (await datasets.json()) as { id: string }[];
    expect(linked).toHaveLength(1);
    const items = await request.get(`${API}/api/v1/datasets/${linked[0].id}/items`, {
      headers,
      params: { media_type: "video" },
    });
    expect(items.status(), await items.text()).toBe(200);
    const page = (await items.json()) as {
      total: number;
      items: { id: string; dataset_id: string }[];
    };
    expect(page.total).toBe(1);
    expect(page.items).toHaveLength(1);
    const [{ id, dataset_id }] = page.items;
    expect(dataset_id).toBe(linked[0].id);
    original = `${API}/api/v1/datasets/${dataset_id}/items/${id}/original`;
  }
  const describe = await request.get(`${original}-identity`, { headers });
  expect(describe.status(), await describe.text()).toBe(200);
  const { content_id, sha256 } = (await describe.json()) as { content_id: string; sha256: string };

  const prepare = () =>
    request.post(`${original}-playback`, {
      headers,
      data: { content_id, sha256, session_id: randomUUID() },
    });
  return {
    projectId,
    taskId,
    prepare,
    async open() {
      const prepared = await prepare();
      expect(prepared.status(), await prepared.text()).toBe(200);
      const { media_path, expires_at } = (await prepared.json()) as {
        media_path: string;
        expires_at: string;
      };
      expect(media_path).toMatch(mediaPath);
      const url = `${API}${media_path}`;
      return {
        url,
        expiresAt: Date.parse(expires_at),
        async expectBytes() {
          const response = await request.get(url);
          expect(response.status()).toBe(200);
          expect(
            createHash("sha256")
              .update(await response.body())
              .digest("hex"),
          ).toBe(sha256);
        },
      };
    },
  };
}

function playbackCleanupTest(scope: PlaybackScope) {
  return test.extend<{ viewers: { a: PlaybackViewer; b: PlaybackViewer } }>({
    viewers: [
      async ({ request, seed }, use, testInfo) => {
        testInfo.skip(
          !process.env.AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE ||
            process.env.PLAYWRIGHT_RASTER_OFFLINE === "1",
          "requires the supervised worktree API and separate fixture owner",
        );
        const a = await seedPlaybackViewer(request, seed, "playbacka", scope);
        const neighborScope = scope === "task" ? "item" : "task";
        const b = await seedPlaybackViewer(request, seed, "playbackb", neighborScope);
        await use({ a, b });
      },
      { timeout: 120_000 },
    ],
  });
}

test.describe("verified original native video", () => {
  test.setTimeout(90_000);

  test("native source uses scoped cookie and current raw pixels; leaving releases it", async ({
    page,
    seed,
  }) => {
    const data = await fixture(page, seed);
    const nativeRequests: Array<{ path: string; bearer: boolean }> = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (mediaPath.test(url.pathname))
        nativeRequests.push({
          path: url.pathname + url.search,
          bearer: !!request.headers().authorization,
        });
    });
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_id}`);
    await ready(page);
    const path = (await source(page).getAttribute("src"))!;
    const cookies = await page.context().cookies(new URL(path, page.url()).href);
    expect(
      cookies.some(
        (cookie) => cookie.path === path && cookie.httpOnly && cookie.sameSite === "Strict",
      ),
    ).toBe(true);
    await seek(page, 17);
    const pixels = await source(page).evaluate((node: HTMLVideoElement) => {
      const canvas = document.createElement("canvas");
      canvas.width = node.videoWidth;
      canvas.height = node.videoHeight;
      const context = canvas.getContext("2d")!;
      context.drawImage(node, 0, 0);
      return {
        bytes: context
          .getImageData(0, 0, canvas.width, canvas.height)
          .data.some((value, index) => index % 4 !== 3 && value > 0),
        jpeg: canvas.toDataURL("image/jpeg").startsWith("data:image/jpeg"),
      };
    });
    expect(pixels).toEqual({ bytes: true, jpeg: true });
    expect(nativeRequests.length).toBeGreaterThan(0);
    expect(nativeRequests.every((request) => mediaPath.test(request.path) && !request.bearer)).toBe(
      true,
    );
    const release = page.waitForResponse(
      (response) =>
        response.request().method() === "DELETE" &&
        response.url().endsWith(path.split("/").at(-1)!),
    );
    await page
      .getByTestId("workbench-topbar")
      .getByRole("button", { name: "返回", exact: true })
      .click();
    expect((await release).status()).toBe(204);
    expect(
      await page.evaluate(async (url) => (await fetch(url, { method: "HEAD" })).status, path),
    ).toBe(410);
  });

  test("same-content renewal keeps the latest frame and polygon draft", async ({ page, seed }) => {
    const data = await fixture(page, seed);
    let prepares = 0;
    let allowRenewal: (() => void) | undefined;
    await page.route(`**/api/v1/files/tasks/${data.task_id}/original-playback`, async (route) => {
      const response = await route.fetch({
        headers: { ...(await route.request().allHeaders()), "sec-fetch-site": "same-origin" },
      });
      expect(response.ok()).toBe(true);
      const body = await response.json();
      prepares++;
      if (prepares === 1) body.expires_at = new Date(Date.now() + 40_000).toISOString();
      else if (prepares === 2)
        await new Promise<void>((resolve) => {
          allowRenewal = resolve;
        });
      await route.fulfill({ response, json: body });
    });
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_id}`);
    await ready(page);
    const first = await source(page).getAttribute("src");
    await seek(page, 17);
    await polygonDraft(page);
    await expect.poll(() => prepares, { timeout: 20_000 }).toBe(2);
    expect(allowRenewal).toBeDefined();
    allowRenewal!();
    await expect(source(page)).not.toHaveAttribute("src", first!);
    await ready(page);
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "17");
    await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
    await page.keyboard.press("Escape");
  });

  test("token refresh retires old media while the route revalidates permission", async ({
    page,
    seed,
  }) => {
    const data = await fixture(page, seed);
    const sockets: WebSocketRoute[] = [];
    await page.routeWebSocket(/\/ws\/notifications/, (socket) => {
      socket.connectToServer();
      sockets.push(socket);
    });
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_id}`);
    await ready(page);
    await expect.poll(() => sockets.length).toBeGreaterThan(0);
    const first = (await source(page).getAttribute("src"))!;
    let fetching = false;
    const accessGate = Promise.withResolvers<void>();
    await page.route(`**/api/v1/projects/${data.project_id}/access`, async (route) => {
      fetching = true;
      await accessGate.promise;
      await route.continue();
    });
    const renewed = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/v1/auth/refresh",
    );
    const released = page.waitForResponse(
      (response) =>
        response.request().method() === "DELETE" &&
        response.url().endsWith(first.split("/").at(-1)!),
    );
    try {
      for (const socket of [...sockets])
        await socket.close({ code: 1008, reason: "token expired" });
      const renewal = await renewed;
      expect(renewal.status()).toBe(200);
      const renewalBody: unknown = await renewal.json();
      if (
        !renewalBody ||
        typeof renewalBody !== "object" ||
        !("access_token" in renewalBody) ||
        typeof renewalBody.access_token !== "string"
      )
        throw new Error("auth refresh response carried no access token");
      const nextToken = renewalBody.access_token;
      expect(nextToken).not.toBe(data.token);
      await expect.poll(() => page.evaluate(() => localStorage.getItem("token"))).toBe(nextToken);
      await expect.poll(() => fetching).toBe(true);
      await expect(page.getByText("正在校验项目权限…")).toBeVisible();
      await expect(source(page)).toHaveCount(0);
      expect((await released).status()).toBe(204);
      expect(
        await page.evaluate(async (path) => (await fetch(path, { method: "HEAD" })).status, first),
      ).toBe(410);
    } finally {
      accessGate.resolve();
      await Promise.allSettled([renewed, released]);
    }
    await ready(page);
    await expect(source(page)).not.toHaveAttribute("src", first);
  });

  test("a server-retired lease recovers through HEAD410 without changing frames", async ({
    page,
    request,
    seed,
  }) => {
    const data = await fixture(page, seed);
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_id}`);
    await ready(page);
    await seek(page, 23);
    const first = (await source(page).getAttribute("src"))!;
    const leaseId = first.split("/").at(-1)!;
    const session = await page.evaluate(
      () => JSON.parse(localStorage.getItem("auth-storage")!).state.sessionId as string,
    );
    const release = await request.delete(
      `${API}/api/v1/files/tasks/${data.task_id}/original-playback/${leaseId}`,
      { headers: { Authorization: `Bearer ${data.token}` }, data: { session_id: session } },
    );
    expect(release.status()).toBe(204);
    await source(page).evaluate((node) => node.dispatchEvent(new Event("error")));
    await expect(source(page)).not.toHaveAttribute("src", first, { timeout: 20_000 });
    await ready(page);
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "23");
  });

  test("unknown original identity remains an explicit error without native preparation", async ({
    page,
    seed,
  }) => {
    const data = await fixture(page, seed);
    let prepares = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/original-playback")) prepares++;
    });
    await page.route(`**/api/v1/files/tasks/${data.task_id}/original-identity`, (route) =>
      route.fulfill({ status: 409, json: { detail: { code: "content_identity_unknown" } } }),
    );
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_id}`);
    await expect(page.getByTestId("video-original-error")).toBeVisible({ timeout: 20_000 });
    expect(prepares).toBe(0);
    await expect(source(page)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "重新准备视频" })).toBeVisible();
  });

  for (const scope of ["task", "item"] as const) {
    const cleanupTest = playbackCleanupTest(scope);
    const title =
      scope === "task"
        ? "owned fixture cleanup frees only its stage in the supervised API"
        : "owned item fixture cleanup frees only its stage in the supervised API";
    cleanupTest(title, async ({ request, seed, viewers: { a, b } }) => {
      const aLease = await a.open();
      const bLease = await b.open();
      expect((await request.head(aLease.url)).status()).toBe(200);
      expect((await request.head(bLease.url)).status()).toBe(200);
      const blocked = await b.prepare();
      expect(blocked.status()).toBe(429);
      expect(await blocked.json()).toEqual({
        detail: { code: "video_playback_capacity_exceeded" },
      });

      await seed.cleanupOwned("playbacka");
      expect(aLease.expiresAt).toBeGreaterThan(Date.now());
      expect((await request.head(aLease.url)).status()).toBe(410);
      await bLease.expectBytes();

      await seed.cleanupOwned("playbacka");
      expect((await request.head(aLease.url)).status()).toBe(410);
      await bLease.expectBytes();

      const rebuilt = await seedPlaybackViewer(request, seed, "playbacka", scope);
      expect(rebuilt.projectId).not.toBe(a.projectId);
      expect(rebuilt.taskId).not.toBe(a.taskId);
      const rebuiltLease = await rebuilt.open();
      expect(rebuiltLease.url).not.toBe(aLease.url);
      await rebuiltLease.expectBytes();
      await seed.cleanupOwned("playbacka");
      expect(rebuiltLease.expiresAt).toBeGreaterThan(Date.now());
      expect((await request.head(rebuiltLease.url)).status()).toBe(410);
      await bLease.expectBytes();

      const replacement = await b.open();
      expect(replacement.url).not.toBe(bLease.url);
      expect((await request.head(replacement.url)).status()).toBe(200);
      await seed.cleanupOwned("playbackb");
      expect(Math.min(bLease.expiresAt, replacement.expiresAt)).toBeGreaterThan(Date.now());
      expect((await request.head(bLease.url)).status()).toBe(410);
      expect((await request.head(replacement.url)).status()).toBe(410);
    });
  }
});
