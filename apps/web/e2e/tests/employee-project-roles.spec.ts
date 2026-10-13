/**
 * Project-scoped employee roles — browser acceptance (B6).
 *
 * One platform employee (`employee@e2e.test`) must be able to annotate in
 * project A and review in project B in the same browser session, while the
 * inverse actions and an unrelated project C stay denied.  Authority comes from
 * real project memberships and the real application APIs — permissions are
 * never stubbed with `page.route`.
 *
 * The review task in B is deliberately *not* seeded with `seed.advance_task`
 * (which bypasses the workflow and leaves contributor evidence unknown).
 * Instead the fixture task carries a known-empty accumulator and the spec
 * submits it through the real annotation + submit APIs as the actual peer
 * author, so the frozen round has a complete contributor set and submitter.
 */
import { expect, test, SeedAPI, type ProjectRolesSeedData } from "../fixtures/seed";
import type { APIRequestContext, Page } from "@playwright/test";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

const auth = (token: string) => ({
  Authorization: `Bearer ${token}`,
  Connection: "close",
});

const BBOX = { type: "bbox", x: 0.2, y: 0.2, w: 0.3, h: 0.3 } as const;

const annotationPayload = {
  annotation_type: "bbox",
  tool_unit_id: "bbox",
  class_name: "car",
  geometry: BBOX,
};

async function submitPeerAnnotation(
  request: APIRequestContext,
  seed: SeedAPI,
  data: ProjectRolesSeedData,
): Promise<void> {
  const token = await seed.accessToken(data.peer_email);
  const created = await request.post(
    `${API_BASE}/api/v1/tasks/${data.review_task_id}/annotations`,
    { headers: auth(token), data: annotationPayload },
  );
  expect(created.status(), await created.text()).toBe(201);
  const submitted = await request.post(`${API_BASE}/api/v1/tasks/${data.review_task_id}/submit`, {
    headers: auth(token),
  });
  expect(submitted.status(), await submitted.text()).toBe(200);
}

/** Draw one real bbox and pick the `car` class, matching the proven image flow. */
async function drawAndSaveBbox(page: Page): Promise<void> {
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  const bboxBtn = page.getByTestId("tool-btn-box");
  await bboxBtn.click();
  await expect(bboxBtn).toHaveAttribute("aria-pressed", "true");

  const points = await stage.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const width = Number(node.getAttribute("data-media-width"));
    const height = Number(node.getAttribute("data-media-height"));
    const x = rect.x + Number(node.getAttribute("data-media-x"));
    const y = rect.y + Number(node.getAttribute("data-media-y"));
    return {
      start: { x: Math.round(x + width * 0.25), y: Math.round(y + height * 0.25) },
      end: { x: Math.round(x + width * 0.55), y: Math.round(y + height * 0.55) },
    };
  });
  for (const point of [points.start, points.end]) {
    expect(
      await stage.evaluate((node, at) => {
        const target = document.elementFromPoint(at.x, at.y);
        return target instanceof HTMLCanvasElement && node.contains(target);
      }, point),
      "绘制坐标必须命中画布",
    ).toBe(true);
  }

  await page.mouse.move(points.start.x, points.start.y);
  await page.mouse.down();
  await expect(stage).toHaveAttribute("data-drag-kind", "draw");
  await page.mouse.move(points.end.x, points.end.y, { steps: 8 });
  await page.mouse.up();

  const picker = page.getByTestId("class-picker-popover");
  await expect(picker).toBeVisible();
  await picker.locator("span").filter({ hasText: /^car$/ }).click();
}

/** Read the durable offline queue straight from IndexedDB (deterministic). */
async function readOfflineQueue(
  page: Page,
): Promise<Array<{ taskId?: string; projectId?: string; userId?: string }>> {
  return page.evaluate(
    () =>
      new Promise<Array<{ taskId?: string; projectId?: string; userId?: string }>>(
        (resolve, reject) => {
          const open = indexedDB.open("keyval-store");
          open.onerror = () => reject(open.error);
          open.onsuccess = () => {
            const db = open.result;
            if (!db.objectStoreNames.contains("keyval")) {
              resolve([]);
              return;
            }
            const tx = db.transaction("keyval", "readonly");
            const get = tx.objectStore("keyval").get("anno.offline-queue.v1");
            get.onerror = () => reject(get.error);
            get.onsuccess = () => {
              const value = get.result;
              resolve(Array.isArray(value) ? value : []);
            };
          };
        },
      ),
  );
}

test.describe("project-scoped employee roles", () => {
  test("same employee annotates A and reviews B in two tabs of one account", async ({
    page,
    context,
    request,
    seed,
  }) => {
    test.setTimeout(120_000);
    const data = await seed.projectRoles();
    await submitPeerAnnotation(request, seed, data);

    await seed.injectToken(page, data.employee_email);
    await page.setViewportSize({ width: 1440, height: 900 });

    // Tab 1: project A annotate — annotation controls only.
    await page.goto(
      `/projects/${data.projects.a.project_id}/annotate?task=${data.annotation_task_id}`,
    );
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
      timeout: 20_000,
    });
    await expect(page.getByTestId("tool-btn-box")).toBeVisible();
    await expect(page.getByTestId("workbench-submit")).toBeVisible();
    await expect(page.getByTestId("review-approve")).toHaveCount(0);

    // Tab 2 (same browser context / same account): project B review — review
    // controls only, while tab 1 keeps its annotation identity.
    const reviewTab = await context.newPage();
    await reviewTab.setViewportSize({ width: 1440, height: 900 });
    const claimResponse = reviewTab.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/v1/tasks/${data.review_task_id}/review/claim` &&
        response.request().method() === "POST",
    );
    await reviewTab.goto(
      `/projects/${data.projects.b.project_id}/review?task=${data.review_task_id}`,
    );
    const claim = await claimResponse;
    expect(claim.status(), await claim.text()).toBe(200);
    expect((await claim.json()).is_self).toBe(true);
    await expect(reviewTab.getByTestId("review-approve")).toBeVisible({ timeout: 15_000 });
    await expect(reviewTab.getByTestId("review-reject")).toBeVisible();
    await expect(reviewTab.getByTestId("workbench-submit")).toHaveCount(0);

    await page.bringToFront();
    await expect(page.getByTestId("workbench-submit")).toBeVisible();
    await expect(page.getByTestId("review-approve")).toHaveCount(0);

    // A: real UI save then submit.
    await drawAndSaveBbox(page);
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-user-box-count", "1");
    const [submitted] = await Promise.all([
      page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === `/api/v1/tasks/${data.annotation_task_id}/submit` &&
          response.request().method() === "POST",
      ),
      page.getByTestId("workbench-submit").click(),
    ]);
    expect(submitted.status(), await submitted.text()).toBe(200);

    // B: UI approve after the auto-claim.
    await reviewTab.bringToFront();
    const [approved] = await Promise.all([
      reviewTab.waitForResponse(
        (response) =>
          new URL(response.url()).pathname ===
            `/api/v1/tasks/${data.review_task_id}/review/approve` &&
          response.request().method() === "POST",
      ),
      reviewTab.getByTestId("review-approve").click(),
    ]);
    expect(approved.status(), await approved.text()).toBe(200);

    // Server truth for both projects.
    // Read final state through the legitimate project managers: an annotator
    // loses batch visibility for a task that left the annotation phase, so the
    // employee's own GET can legitimately 404 even though the submit succeeded.
    const aOwnerTask = await request.get(`${API_BASE}/api/v1/tasks/${data.annotation_task_id}`, {
      headers: auth(await seed.accessToken(data.owner_email_a)),
    });
    expect(aOwnerTask.status(), await aOwnerTask.text()).toBe(200);
    expect((await aOwnerTask.json()).status).toBe("review");
    const bOwnerTask = await request.get(`${API_BASE}/api/v1/tasks/${data.review_task_id}`, {
      headers: auth(await seed.accessToken(data.owner_email_b)),
    });
    expect(bOwnerTask.status(), await bOwnerTask.text()).toBe(200);
    expect((await bOwnerTask.json()).status).toBe("completed");

    await reviewTab.close();
  });

  test("opposite project actions are denied and project C stays invisible", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await seed.projectRoles();
    await submitPeerAnnotation(request, seed, data);
    const token = await seed.accessToken(data.employee_email);
    const headers = auth(token);

    // The A annotator may not claim/review A work.
    const claimA = await request.post(
      `${API_BASE}/api/v1/tasks/${data.annotation_task_id}/review/claim`,
      { headers },
    );
    expect(claimA.status(), await claimA.text()).toBe(403);

    // The B reviewer may not submit the peer's annotation work.
    const submitB = await request.post(`${API_BASE}/api/v1/tasks/${data.review_task_id}/submit`, {
      headers,
    });
    expect(submitB.status(), await submitB.text()).toBe(403);

    // C: an explicit task assignment alone grants nothing.
    const cAccess = await request.get(
      `${API_BASE}/api/v1/projects/${data.projects.c.project_id}/access`,
      { headers },
    );
    expect([403, 404]).toContain(cAccess.status());
    const annotateC = await request.post(
      `${API_BASE}/api/v1/tasks/${data.c_assigned_task_id}/annotations`,
      { headers, data: annotationPayload },
    );
    expect([403, 404]).toContain(annotateC.status());

    // UI deep links fail closed instead of mounting a writable editor.
    await seed.injectToken(page, data.employee_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    for (const url of [
      `/projects/${data.projects.c.project_id}/annotate?task=${data.c_assigned_task_id}`,
      `/projects/${data.projects.a.project_id}/review?task=${data.annotation_task_id}`,
      `/projects/${data.projects.b.project_id}/annotate?task=${data.review_task_id}`,
    ]) {
      await page.goto(url);
      await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
    }
  });

  test("owner previews and applies a member role change with CAS, viewers stay read-only", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await seed.projectRoles();
    const ownerToken = await seed.accessToken(data.owner_email_a);
    const ownerHeaders = auth(ownerToken);

    // Viewer memberships are explicit and viewer-only.
    const viewerAsAnnotator = await request.post(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members`,
      {
        headers: ownerHeaders,
        data: { user_id: data.users.viewer_unassigned.id, role: "annotator" },
      },
    );
    expect([400, 409]).toContain(viewerAsAnnotator.status());
    const viewerAsViewer = await request.post(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members`,
      {
        headers: ownerHeaders,
        data: { user_id: data.users.viewer_unassigned.id, role: "viewer" },
      },
    );
    expect(viewerAsViewer.status(), await viewerAsViewer.text()).toBe(201);

    // Real UI management flow on an idle member.
    await seed.injectToken(page, data.owner_email_a);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${data.projects.a.project_id}/settings`);
    await page.getByTestId("settings-tab-members").click();

    const viewerRow = page.getByRole("row").filter({ hasText: data.viewer_email });
    await expect(viewerRow.getByText("观察者").first()).toBeVisible({ timeout: 15_000 });

    const spareRow = page.getByRole("row").filter({ hasText: data.spare_email });
    await spareRow.getByRole("button", { name: "更改职责" }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: "更改职责" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel("目标项目职责")).toHaveValue("viewer");
    await dialog.getByLabel("目标项目职责").selectOption("annotator");
    await expect(dialog.getByText(/当前职责.*目标职责/)).toBeVisible({ timeout: 15_000 });
    await dialog.getByLabel("变更原因").fill("B6 验收：调整项目职责");
    const saveRole = dialog.getByRole("button", { name: "保存职责" });
    await expect(saveRole).toBeEnabled({ timeout: 15_000 });
    await saveRole.click();
    await expect(dialog).toBeHidden({ timeout: 15_000 });
    await expect(
      page.getByRole("row").filter({ hasText: data.spare_email }).getByText("标注员"),
    ).toBeVisible();

    // CAS: the preview is single-use; replaying it against the new version 409s.
    const membersResponse = await request.get(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members`,
      { headers: ownerHeaders },
    );
    expect(membersResponse.ok(), await membersResponse.text()).toBe(true);
    const spareMember = (
      (await membersResponse.json()) as Array<{ id: string; user_email: string; version: number }>
    ).find((member) => member.user_email === data.spare_email);
    if (!spareMember) throw new Error("spare membership missing");

    const previewResponse = await request.post(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members/${spareMember.id}/role/preview`,
      { headers: ownerHeaders, data: { project_role: "reviewer" } },
    );
    expect(previewResponse.ok(), await previewResponse.text()).toBe(true);
    const preview = (await previewResponse.json()) as {
      current_version: number;
      preview_token: string;
    };
    const changePayload = {
      project_role: "reviewer",
      expected_version: preview.current_version,
      preview_token: preview.preview_token,
      reason: "B6 验收：职责切换",
    };
    const applied = await request.patch(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members/${spareMember.id}/role`,
      { headers: ownerHeaders, data: changePayload },
    );
    expect(applied.ok(), await applied.text()).toBe(true);
    const replay = await request.patch(
      `${API_BASE}/api/v1/projects/${data.projects.a.project_id}/members/${spareMember.id}/role`,
      { headers: ownerHeaders, data: changePayload },
    );
    expect(replay.status(), await replay.text()).toBe(409);
  });

  test("revoking the current project denies the queued draft while the authorized project works", async ({
    page,
    context,
    request,
    seed,
  }) => {
    test.setTimeout(120_000);
    const data = await seed.projectRoles();
    await submitPeerAnnotation(request, seed, data);

    await seed.injectToken(page, data.employee_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    const dAccessPath = `/api/v1/projects/${data.projects.d.project_id}/access`;

    // Load D's open-pool workbench while online, then go offline so the save
    // fails as a real network error and becomes a durable local draft.  Staying
    // offline also prevents the pre-revocation drain from succeeding before the
    // membership is removed.
    await page.goto(
      `/projects/${data.projects.d.project_id}/annotate?task=${data.open_pool_task_id}`,
    );
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
      timeout: 20_000,
    });
    await context.setOffline(true);
    await drawAndSaveBbox(page);
    await expect
      .poll(async () => (await readOfflineQueue(page)).length, { timeout: 15_000 })
      .toBeGreaterThan(0);

    // Release the task lock so the membership is idle, then revoke through the
    // real API. D is an open pool, so no handoff blocker applies.
    const token = await seed.accessToken(data.employee_email);
    const released = await request.delete(
      `${API_BASE}/api/v1/tasks/${data.open_pool_task_id}/lock`,
      { headers: auth(token) },
    );
    expect(released.status(), await released.text()).toBe(204);
    const ownerToken = await seed.accessToken(data.owner_email_a);
    const ownerHeaders = auth(ownerToken);
    const membersResponse = await request.get(
      `${API_BASE}/api/v1/projects/${data.projects.d.project_id}/members`,
      { headers: ownerHeaders },
    );
    const employeeMember = (
      (await membersResponse.json()) as Array<{ id: string; user_email: string }>
    ).find((member) => member.user_email === data.employee_email);
    if (!employeeMember) throw new Error("employee membership missing");
    const removed = await request.delete(
      `${API_BASE}/api/v1/projects/${data.projects.d.project_id}/members/${employeeMember.id}`,
      { headers: ownerHeaders },
    );
    expect(removed.status(), await removed.text()).toBe(204);
    const revokedAccess = await request.get(dAccessPath, { headers: auth(token) });
    expect([403, 404]).toContain(revokedAccess.status());

    // Going back online drains the account queue.  The B4 drain re-checks each
    // operation's project authority first: with D revoked it must observe an
    // HTTP 403/404 access denial and retain the op, not perform a forbidden
    // annotation write.
    const drainAccessCheck = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === dAccessPath && response.request().method() === "GET",
      { timeout: 20_000 },
    );
    await context.setOffline(false);
    const drainAccess = await drainAccessCheck;
    expect([403, 404]).toContain(drainAccess.status());
    await expect
      .poll(
        async () =>
          (await readOfflineQueue(page)).some((op) => op.taskId === data.open_pool_task_id),
        { timeout: 15_000 },
      )
      .toBe(true);

    // No D write was persisted; the draft only exists locally.
    const dAnnotations = await request.get(
      `${API_BASE}/api/v1/tasks/${data.open_pool_task_id}/annotations`,
      { headers: ownerHeaders },
    );
    expect(dAnnotations.status(), await dAnnotations.text()).toBe(200);
    expect(await dAnnotations.json()).toEqual([]);

    // The unrelated authorized project B still works end to end.
    await page.goto(`/projects/${data.projects.b.project_id}/review?task=${data.review_task_id}`);
    await expect(page.getByTestId("review-approve")).toBeVisible({ timeout: 20_000 });
    const [approved] = await Promise.all([
      page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname ===
            `/api/v1/tasks/${data.review_task_id}/review/approve` &&
          response.request().method() === "POST",
      ),
      page.getByTestId("review-approve").click(),
    ]);
    expect(approved.status(), await approved.text()).toBe(200);
  });

  test("employee dashboard separates annotator and reviewer projects", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await seed.projectRoles();
    await submitPeerAnnotation(request, seed, data);

    // The fixture projects carry namespaced names; resolve the exact names from
    // the API instead of assuming the retired shared-fixture values.
    const projectName = async (key: "a" | "b" | "c" | "d") => {
      const response = await request.get(
        `${API_BASE}/api/v1/projects/${data.projects[key].project_id}`,
        { headers: auth(await seed.accessToken(data.projects[key].owner_email)) },
      );
      expect(response.ok(), await response.text()).toBe(true);
      return ((await response.json()) as { name: string }).name;
    };
    const names = {
      a: await projectName("a"),
      b: await projectName("b"),
      c: await projectName("c"),
      d: await projectName("d"),
    };

    await seed.injectToken(page, data.employee_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/dashboard");
    await expect(page.getByTestId("employee-tab-annotate")).toBeVisible({ timeout: 20_000 });

    // Annotate pane shows only the employee's annotator projects (A and D);
    // review-only B must not appear, and C is not visible at all.  The project
    // name also shows up inside sub-headers and batch labels, so match the
    // project card text exactly instead of by substring.
    await expect(page.getByText(names.a, { exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText(names.d, { exact: true })).toBeVisible();
    await expect(page.getByText(names.b, { exact: true })).toHaveCount(0);
    await expect(page.getByText(names.c, { exact: true })).toHaveCount(0);

    // Review pane shows B's pending review work and no annotator project.  The
    // project name also appears inside the priority/batch labels, so match the
    // pending task's project badge exactly instead of using a substring match.
    await page.getByTestId("employee-tab-review").click();
    await expect(page.getByText(names.b, { exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText(names.a, { exact: true })).toHaveCount(0);
    await expect(page.getByText(names.d, { exact: true })).toHaveCount(0);
    await expect(page.getByText(names.c, { exact: true })).toHaveCount(0);
  });

  test("viewer project entry reaches the read-only data manager", async ({ page, seed }) => {
    test.setTimeout(90_000);
    const data = await seed.projectRoles();

    await seed.injectToken(page, data.viewer_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/dashboard");

    const projectRow = page.getByRole("row").filter({ hasText: "E2E Project Roles A" });
    await expect(projectRow).toBeVisible({ timeout: 20_000 });
    await projectRow.click();
    await expect(page).toHaveURL(
      new RegExp(`/projects/${data.projects.a.project_id}/data-manager`),
      { timeout: 15_000 },
    );
    await expect(page.getByText("Data Manager · 项目工作面")).toBeVisible({ timeout: 20_000 });
    // A viewer reaches the read-only surface: no editor and no management actions.
    await expect(page.getByTestId("workbench-stage")).toHaveCount(0);
    await expect(page.getByTestId("data-manager-task-actions")).toHaveCount(0);
  });

  test("employee without projects sees the empty state", async ({ page, seed }) => {
    test.setTimeout(60_000);
    const data = await seed.projectRoles();

    await seed.injectToken(page, data.solo_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/dashboard");
    await expect(page.getByText("等待分配项目")).toBeVisible({ timeout: 20_000 });
  });
});
