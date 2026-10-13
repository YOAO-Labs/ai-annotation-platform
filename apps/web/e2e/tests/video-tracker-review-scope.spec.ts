import { openContextToolbar } from "../fixtures/context-toolbar";
import type {
  APIRequestContext,
  APIResponse,
  ConsoleMessage,
  Page,
  Response,
  Route,
} from "@playwright/test";

import { expect, test as base, type SeedData, type SeedTrackerReviewData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Target = "A" | "B";
type Bbox = { x: number; y: number; w: number; h: number };
interface Annotation {
  id: string;
  version: number;
  geometry: {
    keyframes: Array<{ frame_index: number; source: string; bbox: Bbox }>;
    [key: string]: unknown;
  };
}
interface Preview {
  job_id: string;
  job_revision: number;
  expected_source_versions: Record<string, number>;
  candidate_pending: number;
  candidate_accepted: number;
  candidate_rejected: number;
  results: Array<{
    instance_id: Target;
    frame_index: number;
    source_annotation_id: string;
    geometry: Bbox & { type: "bbox" };
  }>;
}
interface ApiFailure {
  path: string;
  status: number;
  reason?: string;
}
interface ReviewCase {
  data: SeedData;
  token: string;
  taskId: string;
  taskIds: string[];
  jobs: SeedTrackerReviewData[];
  expectedErrors: ApiFailure[];
  evidence: Record<string, unknown>[];
}

const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
const review = (page: Page) => page.getByTestId("video-tracker-review-bar");
const decisionPath = (jobId: string) => `/api/v1/video-tracker-jobs/${jobId}/decisions`;
const path = (url: string) => {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
};

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function annotations(
  request: APIRequestContext,
  fixture: ReviewCase,
  taskId = fixture.taskId,
) {
  const rows = await json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: auth(fixture.token),
    }),
  );
  // Seeded rows share created_at values, so the API can reorder equal-time rows.
  // Compare complete annotation state by stable identity, not database tie order.
  return rows.sort((left, right) => left.id.localeCompare(right.id));
}

async function preview(request: APIRequestContext, fixture: ReviewCase, jobId: string) {
  return json<Preview>(
    await request.get(`${API_BASE}/api/v1/video-tracker-jobs/${jobId}/preview`, {
      headers: auth(fixture.token),
    }),
  );
}

const test = base.extend<{ reviewCase: ReviewCase }>({
  reviewCase: async ({ page, request, seed }, provide, testInfo) => {
    const data = await seed.owned();
    const httpErrors: ApiFailure[] = [];
    const consoleErrors: Array<{ path: string; message: string }> = [];
    const pageErrors: string[] = [];
    const pendingErrors: Promise<void>[] = [];
    const onPageError = (error: Error) => pageErrors.push(error.message);
    const onConsole = (message: ConsoleMessage) => {
      if (message.type() === "error") {
        consoleErrors.push({ path: path(message.location().url), message: message.text() });
      }
    };
    const onResponse = (response: Response) => {
      if (response.status() < 400 || !path(response.url()).startsWith("/api/v1/")) return;
      pendingErrors.push(
        (async () => {
          const body = await response.json().catch(() => null);
          httpErrors.push({
            path: path(response.url()),
            status: response.status(),
            reason: body?.detail?.reason,
          });
        })(),
      );
    };
    page.on("pageerror", onPageError);
    page.on("console", onConsole);
    page.on("response", onResponse);
    try {
      const token = await seed.accessToken(data.admin_email);
      const { task_id: taskId } = await seed.videoTask(data.project_id);
      // These are deterministic staged ML results, not model-quality qualification.
      const jobs = [
        await seed.trackerReview(taskId, data.admin_email),
        await seed.trackerReview(taskId, data.admin_email),
      ];
      // Review stored candidates without contacting the seed's unreachable ML backend.
      await json(
        await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
          headers: auth(token),
          data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
        }),
      );
      const disabled = await request.delete(
        `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
        { headers: auth(token) },
      );
      expect(disabled.status(), await disabled.text()).toBe(204);
      await page.setViewportSize({ width: 1440, height: 1000 });
      await seed.injectToken(page, data.admin_email);
      const fixture: ReviewCase = {
        data,
        token,
        taskId,
        taskIds: [taskId],
        jobs,
        expectedErrors: [],
        evidence: [],
      };
      await provide(fixture);
      await Promise.all(pendingErrors);
      await testInfo.attach("tracker-review-evidence.json", {
        contentType: "application/json",
        body: JSON.stringify(
          {
            projectId: data.project_id,
            taskIds: fixture.taskIds,
            jobs: fixture.jobs,
            browser: page.context().browser()?.version(),
            httpErrors,
            consoleErrors,
            pageErrors,
            evidence: fixture.evidence,
          },
          null,
          2,
        ),
      });
      expect(httpErrors).toEqual(fixture.expectedErrors);
      expect(pageErrors).toEqual([]);
      expect(
        consoleErrors.filter(
          (error) =>
            !fixture.expectedErrors.some(
              (expected) =>
                expected.path === error.path &&
                new RegExp(`^Failed to load resource:.*status of ${expected.status}\\b`).test(
                  error.message,
                ),
            ),
        ),
      ).toEqual([]);
    } finally {
      page.off("pageerror", onPageError);
      page.off("console", onConsole);
      page.off("response", onResponse);
      // Retire task sockets/locks before removing this test's jobs and annotations.
      try {
        await page.goto("about:blank");
      } finally {
        await seed.owned();
      }
    }
  },
});

async function open(page: Page, fixture: ReviewCase) {
  await page.goto(`/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 25_000 });
  await openContextToolbar(page, "tracker-review");
  await expect(review(page)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("tracker-review-job").locator("option")).toHaveCount(2);
}

async function chooseJob(page: Page, jobId: string) {
  await page.getByTestId("tracker-review-job").selectOption(jobId);
  await openContextToolbar(page, "tracker-review");
  await expect(review(page)).toHaveAttribute("data-review-job-id", jobId);
}

async function setScope(page: Page, targets: Target[], from: number, to: number) {
  for (const target of ["A", "B"] as const) {
    const checkbox = page.getByTestId(`tracker-review-instance-${target}`);
    if (await checkbox.count()) await checkbox.setChecked(targets.includes(target));
  }
  // Set a legal window through the controls even when the old window is disjoint.
  const fromInput = page.getByTestId("tracker-review-from-frame");
  const toInput = page.getByTestId("tracker-review-to-frame");
  if (from > Number(await toInput.inputValue())) {
    await toInput.fill(String(to));
    await fromInput.fill(String(from));
  } else {
    await fromInput.fill(String(from));
    await toInput.fill(String(to));
  }
}

async function expectScope(
  page: Page,
  jobId: string,
  targets: Target[],
  from: number,
  to: number,
  selected: number,
  pending = 20,
) {
  await openContextToolbar(page, "tracker-review");
  await expect(page.getByTestId("tracker-review-job")).toHaveValue(jobId);
  await expect(review(page)).toHaveAttribute("data-review-job-id", jobId);
  await expect(page.getByTestId("tracker-review-from-frame")).toHaveValue(String(from));
  await expect(page.getByTestId("tracker-review-to-frame")).toHaveValue(String(to));
  for (const target of ["A", "B"] as const) {
    const checkbox = page.getByTestId(`tracker-review-instance-${target}`);
    if (targets.includes(target)) await expect(checkbox).toBeChecked();
    else
      await expect(
        page.locator(`input[data-testid="tracker-review-instance-${target}"]:checked`),
      ).toHaveCount(0);
  }
  const summary = `审阅 ${targets.length} 个目标 · F${from}–F${to} · 所选待审 ${selected} · 全部待审 ${pending}`;
  for (const locator of [
    review(page).getByTestId("tracker-review-scope-summary"),
    page.getByTestId("video-track-review-scope"),
    page.getByTestId("timeline-tracker-review-scope"),
  ]) {
    await expect(locator).toBeVisible();
    await expect(locator).toHaveAttribute("data-review-job-id", jobId);
    await expect(locator).toContainText(summary);
  }
}

async function frame(page: Page) {
  return Number(await page.getByTestId("video-konva-stage").getAttribute("data-video-frame-index"));
}

async function seek(page: Page, target: number) {
  // Focus only; navigation uses real keyboard input and the existing frame guard.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("k");
  let current = await frame(page);
  while (current !== target) {
    const next = current + (target > current ? 1 : -1);
    await page.keyboard.press(target > current ? "ArrowRight" : "ArrowLeft");
    await expect.poll(() => frame(page)).toBe(next);
    current = next;
  }
}

async function selectReference(page: Page, annotationId: string) {
  const collapse = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await collapse.isVisible()) await collapse.click();
  await page
    .locator('[aria-label="帧过滤"]')
    .getByRole("button", { name: "全部", exact: true })
    .click();
  await page
    .locator(`[data-testid="video-track-row"][data-annotation-id="${annotationId}"]`)
    .click();
  if (await collapse.isVisible()) await collapse.click();
}

async function refresh(page: Page, jobId: string) {
  const response = page.waitForResponse(
    (item) =>
      item.request().method() === "GET" &&
      path(item.url()) === `/api/v1/video-tracker-jobs/${jobId}/preview`,
  );
  await review(page).getByRole("button", { name: "刷新", exact: true }).click();
  expect((await response).status()).toBe(200);
}

function isDecision(response: Response, jobId: string) {
  return response.request().method() === "POST" && path(response.url()) === decisionPath(jobId);
}

async function decide(page: Page, jobId: string, decision: "accept" | "discard", status = 200) {
  const response = page.waitForResponse((item) => isDecision(item, jobId));
  await page.getByTestId(`tracker-review-${decision}`).click();
  const result = await response;
  expect(result.status(), await result.text()).toBe(status);
  return result;
}

async function candidatePixels(page: Page, source: Preview, sourceFrame: number) {
  const boxes: Partial<Record<Target, Bbox>> = {};
  for (const row of source.results) {
    if (row.frame_index === sourceFrame) boxes[row.instance_id] = row.geometry;
  }
  // Read the actual painted transparent Konva layers. Media and DOM attributes do
  // not count as rendered candidates. Sample each candidate's right-hand edge,
  // away from labels and persisted seed edges, using the existing A/B canvas colors.
  return page.getByTestId("video-konva-stage").evaluate((element, geometry) => {
    const mediaX = Number(element.getAttribute("data-media-x"));
    const mediaY = Number(element.getAttribute("data-media-y"));
    const mediaW = Number(element.getAttribute("data-media-width"));
    const mediaH = Number(element.getAttribute("data-media-height"));
    const canvases = Array.from(
      element.querySelectorAll<HTMLCanvasElement>(".konvajs-content > canvas"),
    ).slice(1);
    if (!canvases.length || mediaW <= 0 || mediaH <= 0)
      throw new Error("video canvas is not ready");
    const colors = { A: [99, 102, 241], B: [168, 85, 247] };
    const counts = { A: 0, B: 0 };
    for (const target of ["A", "B"] as const) {
      const bbox = geometry[target];
      if (!bbox) throw new Error(`missing real preview geometry for ${target}`);
      for (const canvas of canvases) {
        const context = canvas.getContext("2d");
        if (!context) throw new Error("candidate canvas has no 2D context");
        const scale = canvas.width / canvas.getBoundingClientRect().width;
        const left = Math.max(0, Math.floor((mediaX + (bbox.x + bbox.w) * mediaW - 3) * scale));
        const top = Math.max(0, Math.floor((mediaY + (bbox.y + bbox.h * 0.4) * mediaH) * scale));
        const width = Math.min(canvas.width - left, Math.ceil(6 * scale));
        const height = Math.min(canvas.height - top, Math.ceil(bbox.h * 0.4 * mediaH * scale));
        if (width <= 0 || height <= 0) throw new Error("candidate sample lies outside the canvas");
        const pixels = context.getImageData(left, top, width, height).data;
        for (let offset = 0; offset < pixels.length; offset += 4) {
          if (
            pixels[offset + 3] >= 100 &&
            colors[target].every(
              (channel, index) => Math.abs(pixels[offset + index] - channel) <= 6,
            )
          )
            counts[target] += 1;
        }
      }
    }
    return counts;
  }, boxes);
}

async function expectCandidates(
  page: Page,
  source: Preview,
  sourceFrame: number,
  visible: Target[],
) {
  await expect
    .poll(async () => {
      const pixels = await candidatePixels(page, source, sourceFrame);
      return { A: pixels.A > 6, B: pixels.B > 6 };
    })
    .toEqual({ A: visible.includes("A"), B: visible.includes("B") });
  return candidatePixels(page, source, sourceFrame);
}

test.describe("Tracker 审阅范围：真实服务与范围所有权", () => {
  test.setTimeout(100_000);

  test("F3-1/2 两个 job 的范围贯穿画布与时间轴，参考轨迹仅经加入或替换修改目标", async ({
    page,
    request,
    reviewCase: fixture,
  }, testInfo) => {
    const [first, second] = fixture.jobs;
    expect(first.job_id).not.toBe(second.job_id);
    const before = await annotations(request, fixture);
    const secondPreview = await preview(request, fixture, second.job_id);
    expect(secondPreview.results.find((row) => row.instance_id === "A")?.source_annotation_id).toBe(
      second.source_annotation_ids[0],
    );
    await open(page, fixture);
    await chooseJob(page, first.job_id);
    await setScope(page, ["B"], 10, 11);
    await chooseJob(page, second.job_id);
    await setScope(page, ["A"], 12, 15);
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await seek(page, 12);
    const onlyA = await expectCandidates(page, secondPreview, 12, ["A"]);
    await testInfo.attach("scope-a-f12.png", {
      body: await page.screenshot(),
      contentType: "image/png",
    });
    await seek(page, 11);
    const outside = await expectCandidates(page, secondPreview, 11, []);
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await page.getByTestId("video-timeline-toggle").click();
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await expect(page.getByTestId("timeline-tracker-review-remaining-10-19")).toBeVisible();
    await page.getByTestId("video-timeline-toggle").click();
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);

    await selectReference(page, second.source_annotation_ids[1]);
    await expect.poll(() => frame(page)).toBe(9);
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await page.getByRole("button", { name: "加入审阅目标", exact: true }).click();
    await expectScope(page, second.job_id, ["A", "B"], 12, 15, 8);
    await selectReference(page, first.source_annotation_ids[0]);
    await expectScope(page, second.job_id, ["A", "B"], 12, 15, 8);
    for (const name of ["加入审阅目标", "替换审阅目标"]) {
      await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
    }
    await expect(page.getByTestId("video-track-context-bar")).toContainText(
      "当前参考轨迹没有可加入的待审目标",
    );
    await selectReference(page, second.source_annotation_ids[0]);
    await expectScope(page, second.job_id, ["A", "B"], 12, 15, 8);
    await page.getByRole("button", { name: "替换审阅目标", exact: true }).click();
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await selectReference(page, second.source_annotation_ids[1]);
    await expectScope(page, second.job_id, ["A"], 12, 15, 4);
    await page.getByRole("button", { name: "替换审阅目标", exact: true }).click();
    await expectScope(page, second.job_id, ["B"], 12, 15, 4);
    await seek(page, 12);
    const onlyB = await expectCandidates(page, secondPreview, 12, ["B"]);
    await refresh(page, second.job_id);
    await expectScope(page, second.job_id, ["B"], 12, 15, 4);
    await chooseJob(page, first.job_id);
    await expectScope(page, first.job_id, ["B"], 10, 11, 2);
    await chooseJob(page, second.job_id);
    await expectScope(page, second.job_id, ["B"], 12, 15, 4);
    await testInfo.attach("scope-b-f12.png", {
      body: await page.screenshot(),
      contentType: "image/png",
    });
    expect(await annotations(request, fixture)).toEqual(before);
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review(page)).toBeVisible({ timeout: 20_000 });
    await chooseJob(page, second.job_id);
    await expect(review(page)).toContainText("已审 0/20");
    expect(await annotations(request, fixture)).toEqual(before);
    expect((await preview(request, fixture, first.job_id)).candidate_pending).toBe(20);
    expect((await preview(request, fixture, second.job_id)).candidate_pending).toBe(20);
    fixture.evidence.push({
      secondPreview,
      pixels: { onlyA, outside, onlyB },
      annotations: before,
    });
  });

  test("F3-3 真实 revision 冲突只保留剩余合法目标，局部接受后的剩余区间只导航", async ({
    page,
    request,
    reviewCase: fixture,
  }) => {
    const job = fixture.jobs[0];
    const before = await annotations(request, fixture);
    const initial = await preview(request, fixture, job.job_id);
    await open(page, fixture);
    await chooseJob(page, job.job_id);
    await setScope(page, ["A", "B"], 12, 15);
    await expectScope(page, job.job_id, ["A", "B"], 12, 15, 8);
    // A second real client resolves all B rows while the browser still holds revision 1.
    const concurrent = await json<Record<string, unknown>>(
      await request.post(`${API_BASE}${decisionPath(job.job_id)}`, {
        headers: auth(fixture.token),
        data: {
          instance_ids: ["B"],
          from_frame: 10,
          to_frame: 19,
          decision: "reject",
          override_manual: false,
          job_revision: initial.job_revision,
          expected_source_versions: initial.expected_source_versions,
        },
      }),
    );
    fixture.expectedErrors.push({
      path: decisionPath(job.job_id),
      status: 409,
      reason: "candidate_decision_conflict",
    });
    const conflict = await decide(page, job.job_id, "accept", 409);
    expect((await conflict.json()).detail.reason).toBe("candidate_decision_conflict");
    await expectScope(page, job.job_id, ["A"], 12, 15, 4, 10);
    await expect(page.getByTestId("tracker-review-instance-B")).toHaveCount(0);
    await expect(review(page)).toContainText("已审 10/20");
    await decide(page, job.job_id, "accept");
    await expectScope(page, job.job_id, ["A"], 12, 15, 0, 6);
    await expect(page.getByTestId("tracker-review-accept")).toBeDisabled();
    await expect(review(page)).toContainText("已审 14/20");
    const saved = await annotations(request, fixture);
    const sourceA = saved.find((item) => item.id === job.source_annotation_ids[0])!;
    expect(sourceA.geometry.keyframes.map((item) => item.frame_index)).toEqual([
      12, 13, 14, 15, 16,
    ]);
    expect(sourceA.geometry.keyframes.find((item) => item.frame_index === 16)).toEqual(
      before.find((item) => item.id === sourceA.id)!.geometry.keyframes[0],
    );
    expect(saved.find((item) => item.id === job.source_annotation_ids[1])).toEqual(
      before.find((item) => item.id === job.source_annotation_ids[1]),
    );
    await page.getByTestId("tracker-review-remaining-16-19").click();
    await expect.poll(() => frame(page)).toBe(16);
    await expectScope(page, job.job_id, ["A"], 12, 15, 0, 6);
    await page.getByTestId("timeline-tracker-review-remaining-10-11").click();
    await expect.poll(() => frame(page)).toBe(10);
    await expectScope(page, job.job_id, ["A"], 12, 15, 0, 6);
    const remaining = await preview(request, fixture, job.job_id);
    expect(remaining).toMatchObject({
      candidate_pending: 6,
      candidate_accepted: 4,
      candidate_rejected: 10,
    });
    expect(remaining.results.map((row) => row.frame_index)).toEqual([10, 11, 16, 17, 18, 19]);
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review(page)).toBeVisible({ timeout: 20_000 });
    await chooseJob(page, job.job_id);
    await expect(review(page)).toContainText("已审 14/20");
    expect(await annotations(request, fixture)).toEqual(saved);
    fixture.evidence.push({ concurrent, remaining, annotations: saved });
  });
});

/** Fault injection: hold delivery only after route.fetch has received the REAL API result. */
async function holdDecisionResponse(page: Page, jobId: string) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let fetched: { status: number; body: unknown; payload: unknown } | undefined;
  let failure: unknown;
  let handled = false;
  let finished = false;
  const pattern = `**${decisionPath(jobId)}`;
  const handler = async (route: Route) => {
    if (handled || route.request().method() !== "POST") return route.continue();
    handled = true;
    try {
      const response = await route.fetch({ timeout: 20_000 });
      fetched = {
        status: response.status(),
        body: await response.json(),
        payload: route.request().postDataJSON(),
      };
      await gate;
      await route.fulfill({ response });
    } catch (error) {
      failure = String(error);
    } finally {
      finished = true;
    }
  };
  await page.route(pattern, handler);
  return {
    release,
    async fetched() {
      await expect
        .poll(() => ({ ready: Boolean(fetched), failure }), { timeout: 25_000 })
        .toEqual({ ready: true, failure: undefined });
      return fetched!;
    },
    async cleanup() {
      release();
      if (handled) await expect.poll(() => finished, { timeout: 25_000 }).toBe(true);
      await page.unroute(pattern, handler);
      expect(failure).toBeUndefined();
    },
  };
}

async function settleDecision(response: Response) {
  await response.finished();
  await response.json();
}

test.describe("F3-4 故障注入：延迟真实 API 响应交付", () => {
  test.setTimeout(100_000);

  test("旧 job 的真实成功响应在换 job 后到达，保留新范围并可刷新读回旧写入", async ({
    page,
    request,
    reviewCase: fixture,
  }) => {
    const [oldJob, currentJob] = fixture.jobs;
    await open(page, fixture);
    await chooseJob(page, oldJob.job_id);
    await setScope(page, ["A"], 12, 15);
    const delayed = await holdDecisionResponse(page, oldJob.job_id);
    try {
      await page.getByTestId("tracker-review-accept").click();
      const fetched = await delayed.fetched();
      expect(fetched.status).toBe(200);
      expect(fetched.body).toMatchObject({ status: "partially_reviewed" });
      await chooseJob(page, currentJob.job_id);
      await setScope(page, ["B"], 10, 11);
      await seek(page, 10);
      const delivered = page.waitForResponse((item) => isDecision(item, oldJob.job_id));
      delayed.release();
      await settleDecision(await delivered);
      await refresh(page, currentJob.job_id);
      await expectScope(page, currentJob.job_id, ["B"], 10, 11, 2);
      await expect.poll(() => frame(page)).toBe(10);
      const persisted = await annotations(request, fixture);
      const savedA = persisted.find((item) => item.id === oldJob.source_annotation_ids[0])!;
      expect(savedA.geometry.keyframes.map((item) => item.frame_index)).toEqual([
        12, 13, 14, 15, 16,
      ]);
      expect(savedA.geometry.keyframes.find((item) => item.frame_index === 16)?.source).toBe(
        "manual",
      );
      const remaining = await preview(request, fixture, oldJob.job_id);
      expect(remaining).toMatchObject({ candidate_pending: 16, candidate_accepted: 4 });
      await delayed.cleanup();
      await page.reload();
      await openContextToolbar(page, "tracker-review");
      await expect(review(page)).toBeVisible({ timeout: 20_000 });
      await chooseJob(page, oldJob.job_id);
      await expect(review(page)).toContainText("已审 4/20");
      expect(await annotations(request, fixture)).toEqual(persisted);
      fixture.evidence.push({
        faultInjection: "delayed real 200 after job switch",
        fetched,
        remaining,
        annotations: persisted,
      });
    } finally {
      await delayed.cleanup();
    }
  });

  for (const destination of ["job", "task"] as const) {
    test(`旧人工帧真实 409 在换 ${destination} 后到达，不弹确认、不重试、不复活旧候选`, async ({
      page,
      request,
      seed,
      reviewCase: fixture,
    }) => {
      const oldJob = fixture.jobs[0];
      let currentJob = fixture.jobs[1];
      let taskDisplayId: string | undefined;
      let targetTaskId = fixture.taskId;
      if (destination === "task") {
        targetTaskId = (await seed.videoTask(fixture.data.project_id)).task_id;
        currentJob = await seed.trackerReview(targetTaskId, fixture.data.admin_email);
        fixture.taskIds.push(targetTaskId);
        fixture.jobs.push(currentJob);
        taskDisplayId = (
          await json<{ display_id: string }>(
            await request.get(`${API_BASE}/api/v1/tasks/${targetTaskId}`, {
              headers: auth(fixture.token),
            }),
          )
        ).display_id;
      }
      const before = await annotations(request, fixture);
      const destinationBefore = await annotations(request, fixture, targetTaskId);
      await open(page, fixture);
      await chooseJob(page, oldJob.job_id);
      await setScope(page, ["A"], 16, 16);
      // 覆盖确认已迁至应用内决策对话框;409 到达时上下文已切换,不应弹出任何对话框。
      const decisions: unknown[] = [];
      page.on("request", (item) => {
        if (item.method() === "POST" && path(item.url()) === decisionPath(oldJob.job_id)) {
          decisions.push(item.postDataJSON());
        }
      });
      fixture.expectedErrors.push({
        path: decisionPath(oldJob.job_id),
        status: 409,
        reason: "manual_keyframe_protected",
      });
      const delayed = await holdDecisionResponse(page, oldJob.job_id);
      try {
        await page.getByTestId("tracker-review-accept").click();
        const fetched = await delayed.fetched();
        expect(fetched.status).toBe(409);
        expect(fetched.body).toMatchObject({ detail: { reason: "manual_keyframe_protected" } });
        if (destination === "task") {
          // Native click triggers SPA task navigation; the pending JS owner stays alive.
          await page
            .getByRole("tabpanel", { name: "任务队列", exact: true })
            .getByText(taskDisplayId!, { exact: true })
            .click();
          await expect(page).toHaveURL(new RegExp(`task=${targetTaskId}`));
          await openContextToolbar(page, "tracker-review");
          await expect(review(page)).toHaveAttribute("data-review-job-id", currentJob.job_id);
        } else await chooseJob(page, currentJob.job_id);
        await setScope(page, ["B"], 12, 15);
        await seek(page, 14);
        await expectScope(page, currentJob.job_id, ["B"], 12, 15, 4);
        const delivered = page.waitForResponse((item) => isDecision(item, oldJob.job_id));
        delayed.release();
        await settleDecision(await delivered);
        await refresh(page, currentJob.job_id);
        await expectScope(page, currentJob.job_id, ["B"], 12, 15, 4);
        await expect.poll(() => frame(page)).toBe(14);
        await expect(page.getByRole("alertdialog")).toHaveCount(0);
        expect(decisions).toHaveLength(1);
        expect(decisions[0]).toMatchObject({
          instance_ids: ["A"],
          from_frame: 16,
          to_frame: 16,
          override_manual: false,
        });
        if (destination === "task") {
          await expect(
            page.getByTestId("tracker-review-job").locator(`option[value="${oldJob.job_id}"]`),
          ).toHaveCount(0);
          await expect(
            page.locator(
              `[data-testid="video-track-row"][data-annotation-id="${oldJob.source_annotation_ids[0]}"]`,
            ),
          ).toHaveCount(0);
        }
        expect(await annotations(request, fixture)).toEqual(before);
        expect(await annotations(request, fixture, targetTaskId)).toEqual(destinationBefore);
        const remaining = await preview(request, fixture, oldJob.job_id);
        expect(remaining).toMatchObject({ candidate_pending: 20, candidate_accepted: 0 });
        await delayed.cleanup();
        await page.reload();
        await openContextToolbar(page, "tracker-review");
        await expect(review(page)).toBeVisible({ timeout: 20_000 });
        await chooseJob(page, currentJob.job_id);
        await expect(review(page)).toContainText("已审 0/20");
        expect(await annotations(request, fixture, targetTaskId)).toEqual(destinationBefore);
        fixture.evidence.push({
          faultInjection: `delayed real manual 409 after ${destination} switch`,
          fetched,
          decisions,
          noOverrideConfirm: true,
          remaining,
        });
      } finally {
        await delayed.cleanup();
      }
    });
  }
});
