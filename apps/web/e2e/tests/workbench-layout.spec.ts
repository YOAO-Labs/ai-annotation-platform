import { panelCommand } from "../fixtures/workbench-panel-actions";
import { layoutCommand, openLayoutSettings } from "../helpers/workbench-layout";
import { renderedGroups, workspace } from "../helpers/workbench-layout-matrix";
import type { Page } from "@playwright/test";
import type { NamedWorkspacePreset } from "../../src/api/auth";
import type {
  PanelId,
  WorkspaceNode,
  WorkspaceSnapshot,
} from "../../src/pages/Workbench/layout/workbenchLayoutSnapshot";
import { expect, ownedNamespaceFor, test } from "../fixtures/seed";

const DESKTOP = { width: 1440, height: 900 };
// 每次运行都新起 dev server，首屏要现编译整张模块图：本机实测 4.9–5.7s，
// 超过 expect 的 5s 默认预算。首屏之后的断言不需要这个宽限。
const FIRST_PAINT = { timeout: 20_000 };
const panel = (page: Page, id: string) => page.locator(`[data-workbench-panel="${id}"]`);

async function savedSnapshot(page: Page, context: string): Promise<WorkspaceSnapshot | undefined> {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).workbench.layout.workspace?.contexts[context]?.snapshot;
}

async function savedPresets(page: Page): Promise<Record<string, NamedWorkspacePreset>> {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).workbench.layout.workspace?.namedPresets ?? {};
}

function groupFor(node: WorkspaceNode, id: PanelId): string | undefined {
  if (node.type === "leaf") return node.data.views.includes(id) ? node.data.id : undefined;
  return node.data.map((child) => groupFor(child, id)).find(Boolean);
}

// Ignore floating-point arithmetic noise while retaining subpixel geometry checks.
const preciseSnapshot = (snapshot: WorkspaceSnapshot | undefined) =>
  snapshot &&
  JSON.parse(
    JSON.stringify(snapshot, (_key, value) =>
      typeof value === "number" ? Number(value.toFixed(6)) : value,
    ),
  );

/** Compare actual DOM objects, including the Konva media canvas, after every rearrangement. */
async function rememberCanvas(page: Page, stageId: string) {
  const wrapper = panel(page, "canvas");
  const stage = page.getByTestId(stageId);
  const media = stage.locator(".konvajs-content > canvas").first();
  await expect(media).toBeVisible({ timeout: 20_000 });
  const originalWrapper = await wrapper.elementHandle();
  const originalStage = await stage.elementHandle();
  const originalMedia = await media.elementHandle();
  return async () => {
    await expect(wrapper).toHaveCount(1);
    await expect(stage).toBeVisible();
    expect(await wrapper.evaluate((node, original) => node === original, originalWrapper)).toBe(
      true,
    );
    expect(await stage.evaluate((node, original) => node === original, originalStage)).toBe(true);
    expect(await media.evaluate((node, original) => node === original, originalMedia)).toBe(true);
  };
}

test.use({ viewport: DESKTOP });

test("AI 候选和多选不会作为标注身份查询隐藏讨论面板", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const taskId = data.task_ids[0];
  const annotationCommentRequests: string[] = [];
  const scopedAnnotationIds: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (/\/annotations\/[^/]+\/comments\/page$/.test(pathname)) {
      annotationCommentRequests.push(pathname);
    }
    if (pathname.endsWith("/discussion/page") && url.searchParams.get("scope") === "annotation") {
      scopedAnnotationIds.push(url.searchParams.get("annotation_id") ?? "");
    }
  });
  try {
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const annotations: Awaited<ReturnType<typeof seed.createTaskAnnotation>>[] = [];
    for (const x of [0.1, 0.5]) {
      annotations.push(
        await seed.createTaskAnnotation(taskId, data.admin_email, {
          annotation_type: "bbox",
          tool_unit_id: "bbox",
          class_name: "car",
          geometry: { type: "bbox", x, y: 0.2, w: 0.2, h: 0.2 },
        }),
      );
    }
    // A deterministic candidate exercises selection identity; no ML service is needed.
    const prediction = await seed.injectPrediction({
      taskId,
      projectId: data.project_id,
      label: "car",
      polygon: [
        [0.2, 0.5],
        [0.4, 0.5],
        [0.4, 0.7],
      ],
    });
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
      "data-image-ready",
      "true",
      FIRST_PAINT,
    );
    await layoutCommand(page, "标准标注布局");
    const discussion = panel(page, "discussion");
    const editor = discussion.locator('[contenteditable="true"]');
    const readScope = discussion.getByRole("combobox", {
      name: "评论阅读范围",
      includeHidden: true,
    });
    const annotationRows = annotations.map((annotation) =>
      page.getByTestId(`box-list-item-${annotation.id}`),
    );
    const firstComments = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname === `/api/v1/tasks/${taskId}/discussion/page` &&
        url.searchParams.get("scope") === "annotation" &&
        url.searchParams.get("annotation_id") === annotations[0].id
      );
    });
    await expect(editor).toBeVisible();
    await annotationRows[0].click();
    await expect(readScope).toHaveValue("all");
    await readScope.selectOption("annotation");
    expect((await firstComments).ok()).toBe(true);
    await expect(editor).toBeVisible();
    const collapseSelection = page.getByRole("button", { name: "收起浮窗", exact: true });
    if (await collapseSelection.isVisible()) await collapseSelection.click();

    await panelCommand(page, "讨论", "隐藏面板");
    await expect(discussion).toHaveAttribute("aria-hidden", "true");
    const candidate = page.getByTestId(`box-list-item-pred-${prediction.prediction_id}-0`);
    await candidate.click();
    await expect(candidate).toHaveClass(/border-brand/);
    // Hidden Dockview panels remain mounted. Assert their settled task-comment state,
    // rather than relying on a delay or accepting the absence of every comment request.
    await expect(readScope).toHaveValue("task");
    await expect(editor).toHaveCount(1);
    await layoutCommand(page, "讨论");
    await expect(editor).toBeVisible();

    await annotationRows[0].click();
    await expect(editor).toBeVisible();
    await annotationRows[1].click({ modifiers: ["Shift"] });
    await expect(editor).toBeVisible();
    await expect(readScope.locator('option[value="annotation"]')).toHaveCount(0);
    await annotationRows[1].click();
    await readScope.selectOption("annotation");
    await expect(editor).toBeVisible();
    await expect.poll(() => scopedAnnotationIds).toContain(annotations[1].id);
    expect(scopedAnnotationIds.length).toBeGreaterThanOrEqual(2);
    expect(scopedAnnotationIds.every((id) => annotations.some((ann) => ann.id === id))).toBe(true);
    expect(annotationCommentRequests).not.toEqual(
      expect.arrayContaining([expect.stringContaining("/annotations/pred-")]),
    );
    expect(
      annotationCommentRequests.every((pathname) =>
        annotations.some(
          (annotation) => pathname === `/api/v1/annotations/${annotation.id}/comments/page`,
        ),
      ),
    ).toBe(true);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("图片布局预设、面板隐藏和浮动保留画布及未发送讨论草稿，刷新恢复已保存树", async ({
  page,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  await seed.advanceTask({
    taskId: data.task_ids[0],
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  const annotation = await seed.createTaskAnnotation(data.task_ids[0], data.admin_email, {
    annotation_type: "bbox",
    tool_unit_id: "bbox",
    class_name: "car",
    geometry: { type: "bbox", x: 0.3, y: 0.3, w: 0.3, h: 0.3 },
  });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await layoutCommand(page, "标准标注布局");
  const sameCanvas = await rememberCanvas(page, "workbench-stage");

  // Keep a persisted selection while layout changes preserve the task composer.
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", FIRST_PAINT);
  await page.getByTestId(`box-list-item-${annotation.id}`).click();

  const discussion = panel(page, "discussion");
  await page.getByRole("button", { name: "收起浮窗", exact: true }).click();
  await discussion.getByRole("tab", { name: /^评论/ }).click();
  const editor = discussion.locator('[contenteditable="true"]');
  await expect(editor).toBeVisible();
  const originalEditor = await editor.elementHandle();
  const draft = "布局调整期间保留的未发送评论";
  await editor.fill(draft);
  const sameDraft = async () => {
    await expect(editor).toHaveText(draft);
    expect(await editor.evaluate((node, original) => node === original, originalEditor)).toBe(true);
  };

  for (const name of ["专注画布布局", "审核协作布局", "标准标注布局"]) {
    await layoutCommand(page, name);
    await sameCanvas();
    await sameDraft();
  }
  await panelCommand(page, "讨论", "隐藏面板");
  await expect(discussion).toHaveAttribute("aria-hidden", "true");
  await sameDraft();
  await layoutCommand(page, "讨论");
  await expect(discussion).toHaveAttribute("aria-hidden", "false");
  await layoutCommand(page, "讨论");
  await expect(discussion).toHaveAttribute("aria-hidden", "true");
  await layoutCommand(page, "讨论");
  await expect(discussion).toHaveCount(1);
  await expect(discussion).toHaveAttribute("aria-hidden", "false");
  await sameDraft();

  await panelCommand(page, "讨论", "浮动面板");
  await expect
    .poll(async () =>
      (await savedSnapshot(page, "annotate:image"))?.layout.floatingGroups?.some((group) =>
        group.data?.views.includes("discussion"),
      ),
    )
    .toBe(true);
  await sameCanvas();
  await sameDraft();
  const floating = page.getByRole("dialog", { name: "讨论", exact: true });
  const oldRect = await floating.boundingBox();
  const handle = await floating.locator(".dv-resize-handle-bottomright").boundingBox();
  if (!oldRect || !handle) throw new Error("Floating resize handle has no bounds");
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2 + 80, handle.y + handle.height / 2 + 40, {
    steps: 8,
  });
  await page.mouse.up();
  await expect
    .poll(async () => (await floating.boundingBox())?.width)
    .toBeGreaterThan(oldRect.width + 60);
  await expect
    .poll(
      async () =>
        (await savedSnapshot(page, "annotate:image"))?.layout.floatingGroups?.find((group) =>
          group.data?.views.includes("discussion"),
        )?.position.width,
    )
    .toBeGreaterThan(oldRect.width + 60);
  await panelCommand(page, "讨论", "与标注详情合并为标签");
  await expect
    .poll(async () => {
      const root = (await savedSnapshot(page, "annotate:image"))?.layout.grid.root;
      if (!root) return false;
      const discussionGroup = groupFor(root, "discussion");
      return !!discussionGroup && discussionGroup === groupFor(root, "inspector");
    })
    .toBe(true);
  await sameCanvas();
  await sameDraft();
  const mergedDiscussionGroup = groupFor(
    (await savedSnapshot(page, "annotate:image"))!.layout.grid.root,
    "discussion",
  );
  await panelCommand(page, "讨论", "停靠到底部");
  await expect
    .poll(async () => {
      const snapshot = await savedSnapshot(page, "annotate:image");
      const discussionGroup = snapshot && groupFor(snapshot.layout.grid.root, "discussion");
      return !!discussionGroup && discussionGroup !== mergedDiscussionGroup;
    })
    .toBe(true);
  const dockedDiscussionGroup = groupFor(
    (await savedSnapshot(page, "annotate:image"))!.layout.grid.root,
    "discussion",
  );
  await expect
    .poll(async () => {
      const canvasRect = await panel(page, "canvas").boundingBox();
      const discussionRect = await discussion.boundingBox();
      return (
        !!canvasRect && !!discussionRect && discussionRect.y >= canvasRect.y + canvasRect.height
      );
    })
    .toBe(true);
  await sameCanvas();
  await sameDraft();

  // Native tab regrouping persists once and a rejected canvas-center drop preserves the tree.
  await page.waitForTimeout(650);
  const dragWrites: string[] = [];
  const businessWrites: string[] = [];
  page.on("request", (request) => {
    if (!["POST", "PATCH", "DELETE"].includes(request.method())) return;
    if (
      request.url().includes("/preferences") &&
      request.postDataJSON()?.workbench?.layout?.workspace
    )
      dragWrites.push(request.postData()!);
    if (/\/tasks\/[^/]+(?:\/annotations)?$/.test(new URL(request.url()).pathname))
      businessWrites.push(request.url());
  });
  await page
    .getByRole("tab", { name: "任务队列", exact: true })
    .dragTo(page.getByRole("tab", { name: "类别面板", exact: true }));
  await expect
    .poll(async () => {
      const snapshot = await savedSnapshot(page, "annotate:image");
      return (
        snapshot &&
        groupFor(snapshot.layout.grid.root, "task-queue") ===
          groupFor(snapshot.layout.grid.root, "class-palette")
      );
    })
    .toBe(true);
  await page.waitForTimeout(650);
  expect(dragWrites).toHaveLength(1);
  const beforeRejectedDrop = await savedSnapshot(page, "annotate:image");
  await page.getByRole("tab", { name: "类别面板", exact: true }).dragTo(stage);
  await sameCanvas();
  await page.waitForTimeout(650);
  const afterRejectedDrop = await savedSnapshot(page, "annotate:image");
  for (const id of ["canvas", "task-queue", "class-palette"] as const)
    expect(groupFor(afterRejectedDrop!.layout.grid.root, id)).toBe(
      groupFor(beforeRejectedDrop!.layout.grid.root, id),
    );
  expect(businessWrites).toEqual([]);
  await sameCanvas();
  await sameDraft();

  await page.reload();
  await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 20_000 });
  await expect(discussion).toHaveAttribute("aria-hidden", "false");
  await expect(
    page.getByText("保存的布局无法恢复，请从布局菜单重置。", { exact: true }),
  ).toHaveCount(0);
  expect(
    groupFor((await savedSnapshot(page, "annotate:image"))!.layout.grid.root, "discussion"),
  ).toBe(dockedDiscussionGroup);
});

test("a root-edge queue dock keeps the reserved group collapsed and the saved tree", async ({
  page,
  seed,
}) => {
  test.setTimeout(120_000);
  const data = await seed.owned();
  await seed.advanceTask({
    taskId: data.task_ids[0],
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await layoutCommand(page, "标准标注布局");
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
    "data-image-ready",
    "true",
    FIRST_PAINT,
  );

  // Leave a merged tab group plus a floating group, then exercise the queue
  // docks one at a time and assert each actually lands: right of the canvas,
  // then below it (leaving the canvas at the workspace left edge), then the
  // final left drop inside Dockview's root-edge band. On the pre-fix build all
  // of these drags were no-ops because the queue already started on the left,
  // so each geometry assertion below fails without the real dock.
  const dockedGroups = async () => (await renderedGroups(page)).filter((group) => !group.floating);
  const rectFor = (groups: Awaited<ReturnType<typeof dockedGroups>>, tab: string) =>
    groups.find((group) => group.tabs.includes(tab))?.rect;

  await panelCommand(page, "讨论", "与标注详情合并为标签");
  await expect
    .poll(async () => {
      const root = (await savedSnapshot(page, "annotate:image"))?.layout.grid.root;
      return !!root && groupFor(root, "discussion") === groupFor(root, "inspector");
    })
    .toBe(true);
  await panelCommand(page, "类别面板", "浮动面板");
  await expect
    .poll(async () => {
      const snapshot = await savedSnapshot(page, "annotate:image");
      return (
        snapshot?.layout.floatingGroups?.some((group) =>
          group.data?.views.includes("class-palette"),
        ) ?? false
      );
    })
    .toBe(true);

  await panelCommand(page, "任务队列", "停靠到右侧");
  await expect
    .poll(async () => {
      const groups = await dockedGroups();
      const queue = rectFor(groups, "task-queue");
      const canvas = rectFor(groups, "canvas");
      return !!queue && !!canvas && queue.x >= canvas.x + canvas.width - 2;
    })
    .toBe(true);

  await panelCommand(page, "任务队列", "停靠到底部");
  await expect
    .poll(async () => {
      const groups = await dockedGroups();
      const queue = rectFor(groups, "task-queue");
      const canvas = rectFor(groups, "canvas");
      return !!queue && !!canvas && queue.y >= canvas.y + canvas.height - 2;
    })
    .toBe(true);

  // Root-edge precondition: after the bottom dock the canvas starts at the
  // workspace left, so the next 8px-from-left target is inside the root-edge band.
  const workspaceBox = await workspace(page).boundingBox();
  await expect
    .poll(async () => {
      const canvas = rectFor(await dockedGroups(), "canvas");
      return !!canvas && !!workspaceBox && Math.abs(canvas.x - workspaceBox.x) <= 2;
    })
    .toBe(true);

  await panelCommand(page, "任务队列", "停靠到左侧");
  await expect
    .poll(async () => {
      const groups = await dockedGroups();
      const queue = rectFor(groups, "task-queue");
      const canvas = rectFor(groups, "canvas");
      return !!queue && !!canvas && queue.x + queue.width <= canvas.x + 2;
    })
    .toBe(true);

  await expect
    .poll(async () => {
      const snapshot = await savedSnapshot(page, "annotate:image");
      const root = snapshot?.layout.grid.root;
      return {
        merged: !!root && groupFor(root, "discussion") === groupFor(root, "inspector"),
        floating:
          snapshot?.layout.floatingGroups?.some((group) =>
            group.data?.views.includes("class-palette"),
          ) ?? false,
      };
    })
    .toEqual({ merged: true, floating: true });

  // The reserved parking group must consume no docked grid space: the docked
  // groups must still cover the workspace edges with no blank reserved column.
  await expect
    .poll(async () => {
      const docked = (await renderedGroups(page)).filter((group) => !group.floating);
      const rect = await workspace(page).boundingBox();
      if (!docked.length || !rect) return Infinity;
      const left = Math.min(...docked.map((group) => group.rect.x));
      const right = Math.max(...docked.map((group) => group.rect.x + group.rect.width));
      return Math.max(Math.abs(left - rect.x), Math.abs(right - (rect.x + rect.width)));
    })
    .toBeLessThanOrEqual(2);

  // Cleanup acceptance: once the dock drags finish, the workspace must not keep
  // the overlay passthrough class, so ordinary canvas pointer input is restored.
  await expect
    .poll(() =>
      page.evaluate(() =>
        (document.querySelector("[data-workbench-workspace]")?.className.split(/\s+/) ?? []).some(
          (name) => name.includes("dragPassthrough"),
        ),
      ),
    )
    .toBe(false);

  const saved = await savedSnapshot(page, "annotate:image");
  const mergedDiscussionGroup = groupFor(saved!.layout.grid.root, "discussion");
  expect(mergedDiscussionGroup).toBeDefined();
  expect(mergedDiscussionGroup).toBe(groupFor(saved!.layout.grid.root, "inspector"));
  expect(
    saved!.layout.floatingGroups?.some((group) => group.data?.views.includes("class-palette")),
  ).toBe(true);
  await expect(
    page.getByText("保存的布局无法恢复，请从布局菜单重置。", { exact: true }),
  ).toHaveCount(0);

  await page.reload();
  await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByText("保存的布局无法恢复，请从布局菜单重置。", { exact: true }),
  ).toHaveCount(0);
  await expect
    .poll(async () => {
      const groups = await renderedGroups(page);
      return {
        merged: groups.some((group) => group.tabs.join(",") === "inspector,discussion"),
        floating: groups.some((group) => group.floating && group.tabs.includes("class-palette")),
      };
    })
    .toEqual({ merged: true, floating: true });
  const restored = await savedSnapshot(page, "annotate:image");
  expect(groupFor(restored!.layout.grid.root, "discussion")).toBe(mergedDiscussionGroup);

  // Bounded native-cancel acceptance: start a real native tab drag, prove the
  // overlay passthrough is active, cancel with Escape, then verify the class is
  // reset and the canvas still answers a real pointer hit-test.
  const passthroughActive = () =>
    page.evaluate(() =>
      (document.querySelector("[data-workbench-workspace]")?.className.split(/\s+/) ?? []).some(
        (name) => name.includes("dragPassthrough"),
      ),
    );
  const discussionTab = page.getByRole("tab", { name: "讨论", exact: true });
  const tabBox = await discussionTab.boundingBox();
  expect(tabBox).not.toBeNull();
  await page.mouse.move(tabBox!.x + tabBox!.width / 2, tabBox!.y + tabBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(tabBox!.x + tabBox!.width / 2 + 60, tabBox!.y + 90, { steps: 10 });
  await expect.poll(passthroughActive).toBe(true);
  await page.keyboard.press("Escape");
  await page.mouse.up();
  await expect.poll(passthroughActive).toBe(false);

  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true");
  await stage.click({ position: { x: 80, y: 80 } });
  await expect(stage).toBeVisible();
});

test("标准和浮动布局使用日间与夜间语义主题", { tag: "@visual" }, async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  await seed.injectToken(page, data.admin_email);
  // Theme screenshots exercise the layout without depending on the seed's external ML service.
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  // The queue badge shows the task's own display id; derive it from the API
  // instead of assuming the retired shared-fixture value.
  const taskResponse = await page.request.get(`/api/v1/tasks/${data.task_ids[0]}`, { headers });
  expect(taskResponse.ok(), await taskResponse.text()).toBe(true);
  const taskDisplayId = ((await taskResponse.json()) as { display_id: string }).display_id;
  expect(taskDisplayId).toBe(`T-E2E-${ownedNamespaceFor(test.info())}000001`);
  const configured = await page.request.patch(`/api/v1/projects/${data.project_id}`, {
    headers,
    data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
  });
  expect(configured.ok(), await configured.text()).toBe(true);
  const removed = await page.request.delete(
    `/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
    { headers },
  );
  expect(removed.status()).toBe(204);
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await expect(
    page.getByTestId("workbench-stage").locator(".konvajs-content > canvas").first(),
  ).toBeVisible(FIRST_PAINT);
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
    "data-image-ready",
    "true",
    FIRST_PAINT,
  );
  await layoutCommand(page, "标准标注布局");
  await page.mouse.move(0, 0);
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0, { timeout: 12_000 });
  const workspace = page.locator("[data-workbench-workspace]");
  const queue = page.getByRole("tabpanel", { name: "任务队列", exact: true });
  await expect(queue.getByText("1 / 5", { exact: true })).toBeVisible();
  await expect(queue.getByText(taskDisplayId, { exact: true })).toHaveClass(/text-brand/);
  await expect(queue.getByText("task-1.png", { exact: true })).toBeVisible();
  // Keep the fixture private to this test; only its path-derived IDs vary in screenshots.
  // The fourth and fifth rows sit behind the category panel, so masking them would
  // paint over that panel even though their text is occluded in the real UI.
  const taskIdLabels = queue.getByText(/^T-E2E-[a-z0-9]{4,12}\d{6}$/);
  await expect(taskIdLabels.first()).toHaveText(taskDisplayId);
  await expect(taskIdLabels.nth(2)).toBeVisible();
  const visibleTaskIds = [taskIdLabels.nth(0), taskIdLabels.nth(1), taskIdLabels.nth(2)];
  for (const floating of [false, true]) {
    if (floating) await panelCommand(page, "讨论", "浮动面板");
    for (const theme of ["light", "dark"]) {
      const current = await page.locator("html").getAttribute("data-theme");
      if (current !== theme) await page.getByRole("button", { name: /当前.*切到/ }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(workspace).toHaveScreenshot(
        `workspace-${floating ? "floating" : "standard"}-${theme}.png`,
        {
          animations: "disabled",
          mask: visibleTaskIds,
          maxDiffPixelRatio: 0.01,
        },
      );
    }
  }
});

test("命名布局预设随账号保存，可跨刷新应用与删除", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  await seed.injectToken(page, data.admin_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await layoutCommand(page, "标准标注布局");

  await panelCommand(page, "讨论", "隐藏面板");
  await expect(panel(page, "discussion")).toHaveAttribute("aria-hidden", "true");
  let dialog = await openLayoutSettings(page);
  await dialog.getByLabel("预设名称").fill("无讨论精简");
  await dialog.getByRole("button", { name: "保存当前布局", exact: true }).click();
  await expect(dialog.getByText("1 / 5")).toBeVisible();
  await expect
    .poll(async () => Object.values(await savedPresets(page)).map((preset) => preset.name))
    .toEqual(["无讨论精简"]);
  await dialog.getByRole("button", { name: "关闭设置", exact: true }).click();

  // 换成内置预设，再从保存的预设套回来。
  await layoutCommand(page, "标准标注布局");
  await expect(panel(page, "discussion")).toHaveAttribute("aria-hidden", "false");
  await page.reload();
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
    "data-image-ready",
    "true",
    FIRST_PAINT,
  );
  // 重载会重建 DOM；套用预设不应再动画布实例。
  const sameCanvas = await rememberCanvas(page, "workbench-stage");
  dialog = await openLayoutSettings(page);
  await expect(dialog.getByText("无讨论精简")).toBeVisible();
  await dialog.getByRole("button", { name: "应用", exact: true }).click();
  await dialog.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(panel(page, "discussion")).toHaveAttribute("aria-hidden", "true");
  await sameCanvas();

  dialog = await openLayoutSettings(page);
  await dialog.getByRole("button", { name: "删除预设 无讨论精简", exact: true }).click();
  await dialog.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(dialog.getByText("0 / 5")).toBeVisible();
  await expect.poll(async () => await savedPresets(page)).toEqual({});
});

test("预设撤销恢复自定义树，后续预设替换撤销点且紧凑模式清除撤销入口", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  await seed.injectToken(page, data.admin_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await layoutCommand(page, "标准标注布局");
  const sameCanvas = await rememberCanvas(page, "workbench-stage");
  await panelCommand(page, "任务队列", "与类别面板合并为标签");
  await panelCommand(page, "标注详情", "隐藏面板");
  await panelCommand(page, "讨论", "浮动面板");
  await page.waitForTimeout(650);
  const custom = await savedSnapshot(page, "annotate:image");
  await layoutCommand(page, "审核协作布局");
  await page
    .locator("[data-sonner-toast][data-removed='false']")
    .getByRole("button", { name: "撤销", exact: true })
    .click();
  await expect
    .poll(async () => preciseSnapshot(await savedSnapshot(page, "annotate:image")))
    .toEqual(preciseSnapshot(custom));
  await sameCanvas();

  await layoutCommand(page, "标准标注布局");
  await page.waitForTimeout(650);
  const standard = await savedSnapshot(page, "annotate:image");
  await layoutCommand(page, "审核协作布局");
  await expect(page.locator("[data-sonner-toast][data-removed='false']")).toHaveCount(1);
  await page
    .locator("[data-sonner-toast][data-removed='false']")
    .getByRole("button", { name: "撤销", exact: true })
    .click();
  await expect
    .poll(async () => preciseSnapshot(await savedSnapshot(page, "annotate:image")))
    .toEqual(preciseSnapshot(standard));
  await layoutCommand(page, "审核协作布局");
  await page.waitForTimeout(650);
  const desktop = await savedSnapshot(page, "annotate:image");
  await page.setViewportSize({ width: 1024, height: DESKTOP.height });
  await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute("data-compact", "true");
  await expect(
    page
      .locator("[data-sonner-toast][data-removed='false']")
      .getByRole("button", { name: "撤销", exact: true }),
  ).toHaveCount(0);
  await page.setViewportSize(DESKTOP);
  await sameCanvas();
  await page.waitForTimeout(650);
  expect(await savedSnapshot(page, "annotate:image")).toEqual(desktop);
});

test("视频紧凑布局禁止桌面写入，退出后恢复浮窗与非零帧并保持同一画布和解码元素", async ({
  page,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const video = await seed.videoTask(data.project_id);
  await seed.injectToken(page, data.admin_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${video.task_id}`);
  await layoutCommand(page, "标准标注布局");
  const stage = page.getByTestId("video-konva-stage");
  const sameCanvas = await rememberCanvas(page, "video-konva-stage");
  const source = page.getByTestId("video-konva-source");
  const originalSource = await source.elementHandle();
  await expect
    .poll(() => source.evaluate((node) => (node as HTMLVideoElement).readyState), {
      timeout: 20_000,
    })
    .toBeGreaterThanOrEqual(2);
  // Focus the selectable canvas, away from the contextual capsule at its top-left edge.
  await page.getByTestId("video-tool-btn-select").click();
  await stage.click();
  for (let frame = 1; frame <= 3; frame += 1) {
    await page.keyboard.press("ArrowRight");
    await expect(stage).toHaveAttribute("data-video-frame-index", String(frame));
  }
  await panelCommand(page, "讨论", "浮动面板");
  await expect
    .poll(async () =>
      (await savedSnapshot(page, "annotate:video"))?.layout.floatingGroups?.some((group) =>
        group.data?.views.includes("discussion"),
      ),
    )
    .toBe(true);
  // Drain the 300 ms writer before observing the negative compact-mode write contract.
  await page.waitForTimeout(650);
  const before = await savedSnapshot(page, "annotate:video");
  const discussion = panel(page, "discussion");
  const desktopRect = await discussion.boundingBox();
  if (!desktopRect) throw new Error("Floating discussion panel has no bounds");
  const playback = await source.evaluate((node) => ({
    time: (node as HTMLVideoElement).currentTime,
    paused: (node as HTMLVideoElement).paused,
  }));
  const workspaceWrites: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "PATCH" &&
      new URL(request.url()).pathname === "/api/v1/auth/me/preferences" &&
      request.postDataJSON()?.workbench?.layout?.workspace
    ) {
      workspaceWrites.push(request.postData()!);
    }
  });

  await page.setViewportSize({ width: 1024, height: DESKTOP.height });
  await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute("data-compact", "true");
  await page.getByRole("button", { name: "布局", exact: true }).click();
  for (const name of ["标准标注布局", "专注画布布局"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await page.keyboard.press("Escape");
  const settings = await openLayoutSettings(page);
  await settings.locator("summary").filter({ hasText: "面板与高级布局" }).click();
  for (const name of ["标准标注", "专注画布", "审核协作", "重置为标准布局"]) {
    await expect(settings.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await settings.getByRole("button", { name: "任务队列", exact: true }).click();
  await settings.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(panel(page, "task-queue")).toBeVisible();
  await expect(page.getByRole("button", { name: "任务队列菜单", exact: true })).toHaveCount(0);
  await layoutCommand(page, "讨论");
  await expect(discussion).toHaveAttribute("aria-hidden", "false");
  await expect(panel(page, "task-queue")).toBeHidden();
  await sameCanvas();
  await page.waitForTimeout(650);
  expect(workspaceWrites).toEqual([]);
  expect(await savedSnapshot(page, "annotate:video")).toEqual(before);

  await page.setViewportSize(DESKTOP);
  await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute("data-compact", "false");
  await expect
    .poll(async () => {
      const restored = await discussion.boundingBox();
      return (
        restored &&
        Math.max(
          ...(["x", "y", "width", "height"] as const).map((key) =>
            Math.abs(restored[key] - desktopRect[key]),
          ),
        )
      );
    })
    .toBeLessThanOrEqual(1);
  await sameCanvas();
  expect(await source.evaluate((node, original) => node === original, originalSource)).toBe(true);
  await expect(stage).toHaveAttribute("data-video-frame-index", "3");
  expect(await source.evaluate((node) => (node as HTMLVideoElement).paused)).toBe(playback.paused);
  expect(await source.evaluate((node) => (node as HTMLVideoElement).currentTime)).toBeCloseTo(
    playback.time,
    3,
  );
  await page.waitForTimeout(650);
  expect(workspaceWrites).toEqual([]);
  expect(await savedSnapshot(page, "annotate:video")).toEqual(before);
});
