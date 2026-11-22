import { expect, ownedNamespaceFor, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

test("token injection preserves origin storage without booting the previous actor's app", async ({
  page,
  request,
  seed,
}) => {
  const data = await seed.owned();
  const origin = "https://seed-auth.test";
  const requests: string[] = [];
  await page.route(`${origin}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/")) requests.push(path);
    await route.fulfill({
      contentType: "text/html",
      body:
        path === "/"
          ? '<script>fetch("/api/v1/dashboard/admin")</script>'
          : "<!doctype html><title>Fixture page</title>",
    });
  });

  await page.goto(`${origin}/previous`);
  await page.evaluate(() => {
    localStorage.setItem("video.experimental.webcodecs", "1");
  });
  for (const [email, role] of [
    [data.admin_email, "super_admin"],
    [data.annotator_email, "employee"],
    [data.reviewer_email, "employee"],
  ]) {
    await seed.injectToken(page, email, origin);
    await page.goto(`${origin}/workbench`);
    const storage = await page.evaluate(() => ({
      token: localStorage.getItem("token"),
      auth: JSON.parse(localStorage.getItem("auth-storage")!).state,
      webCodecs: localStorage.getItem("video.experimental.webcodecs"),
    }));
    expect(storage.token).toBeTruthy();
    expect(storage.auth.token).toBe(storage.token);
    expect(storage.auth.user).toMatchObject({ email, role });
    expect(storage.webCodecs).toBe("1");

    const identity = await request.get(`${API_BASE}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${storage.token}` },
    });
    expect(identity.ok(), await identity.text()).toBe(true);
    expect(await identity.json()).toMatchObject({ id: storage.auth.user.id, email, role });
  }
  expect(requests).toEqual([]);
  await page.goto(origin);
  await expect.poll(() => requests).toEqual(["/api/v1/dashboard/admin"]);
});

// eslint-disable-next-line no-empty-pattern
test("fixture namespaces retain retry identity without crossing runs", async ({}, testInfo) => {
  const previous = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
  try {
    process.env.PLAYWRIGHT_FIXTURE_RUN_ID = "8ef09c94-9b6d-4241-9f2d-6a16fc082640";
    const first = ownedNamespaceFor(testInfo);
    const retry = { ...testInfo, retry: testInfo.retry + 1 };
    expect(ownedNamespaceFor(retry)).toBe(first);
    process.env.PLAYWRIGHT_FIXTURE_RUN_ID = "ef79e1fb-93a6-44b3-a03c-eafc505170ec";
    expect(ownedNamespaceFor(retry)).not.toBe(first);
  } finally {
    if (previous === undefined) delete process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
    else process.env.PLAYWRIGHT_FIXTURE_RUN_ID = previous;
  }
});
