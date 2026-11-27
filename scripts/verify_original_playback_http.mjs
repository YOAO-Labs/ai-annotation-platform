import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { after, before, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import {
  closeFixtureTransport,
  postFixture,
  shutdownFixtureTransport,
  verifyFixtureSuite,
} from "../apps/web/e2e/helpers/seed-owner-transport.mjs";

const scopes = ["task", "item"];
const completed = new Set();
const suiteId = randomUUID();
const suiteDirectory = `.worktree/e2e/fixture-authority/suite-${suiteId.replaceAll("-", "")}`;
const apiRoot = fileURLToPath(new URL("../apps/api/", import.meta.url));
const python = fileURLToPath(new URL("../apps/api/.venv/bin/python", import.meta.url));
const supervisor = fileURLToPath(new URL("./fixture_native_playwright.py", import.meta.url));
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

let origin;
let child;
let exited;
let apiReady = false;

before(async () => {
  const port = process.env.PLAYWRIGHT_ISOLATED_API_PORT;
  assert.ok(
    process.env.YOAO_WORKTREE_MODE === "e2e" &&
      process.env.PLAYWRIGHT_RASTER_OFFLINE !== "1" &&
      /^\d+$/.test(port ?? "") &&
      Number(port) > 0 &&
      Number(port) <= 65535,
    "Original playback acceptance requires dev:worktree exec --mode e2e",
  );
  origin = `http://127.0.0.1:${port}`;
  process.env.PLAYWRIGHT_FIXTURE_RUN_ID = suiteId;
  process.env.AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE = suiteId;
  child = spawn(python, [supervisor, "--port", port], { cwd: apiRoot, stdio: "inherit" });
  exited = new Promise((resolve) => {
    child.once("error", (error) => resolve({ error }));
    child.once("close", (code, signal) => resolve({ code, signal }));
  });

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) {
      const { error, code, signal } = await exited;
      if (error) throw error;
      throw new Error(`API supervisor exited (${code ?? signal}); see ${suiteDirectory}`);
    }
    const response = await fetch(`${origin}/health/db`, {
      redirect: "error",
      signal: AbortSignal.timeout(1_000),
    }).catch(() => null);
    if (response?.status === 200) {
      assert.equal((await response.json()).status, "ok", "Supervised API database health");
      apiReady = true;
      await verifyFixtureSuite(suiteId, "owned");
      return;
    }
    await response?.body?.cancel();
    await delay(100);
  }
  throw new Error(`Supervised API database did not become ready; see ${suiteDirectory}`);
});

after(async () => {
  const failures = [];
  const steps = [
    async () => {
      if (!apiReady) return;
      await seed("cleanup", { suite_id: suiteId }, 0);
      assert.equal((await shutdownFixtureTransport(suiteId)).state, "stopped");
    },
    closeFixtureTransport,
    async () => {
      if (!child) return;
      child.kill("SIGTERM");
      const { error, code, signal } = await exited;
      if (error) throw error;
      assert.equal(code, 0, `API supervisor exit (signal ${signal}); see ${suiteDirectory}`);
    },
  ];
  for (const step of steps) {
    try {
      await step();
    } catch (error) {
      failures.push(error);
    }
  }
  if (failures.length)
    throw new AggregateError(failures, "Original playback fixture teardown failed");
  assert.deepEqual(completed, new Set(scopes), "Both playback cleanup cases must complete");
});

async function seed(operation, data, timeout = 60_000) {
  const response = await postFixture(`${origin}/api/v1/__test/seed/${operation}`, {
    data,
    timeout,
  });
  assert.equal(
    response.status(),
    200,
    `seed/${operation}: ${response.ok() ? "" : await response.text()}`,
  );
  return response.json();
}

async function http(path, { method = "GET", headers, data, status = 200 } = {}) {
  const response = await fetch(`${origin}${path}`, {
    method,
    headers: data ? { ...headers, "Content-Type": "application/json" } : headers,
    body: data && JSON.stringify(data),
    redirect: "error",
    signal: AbortSignal.timeout(30_000),
  });
  assert.equal(response.status, status, `${method} ${path}`);
  return response;
}

async function seedViewer(kind, namespace) {
  const owner = { namespace, suite_id: suiteId, run_id: randomUUID() };
  const { project_id: projectId, admin_email: email } = await seed("owned", owner);
  assert.match(projectId, uuid);
  const { task_id: taskId } = await seed("video-task", { ...owner, project_id: projectId });
  assert.match(taskId, uuid);
  const { access_token: token } = await seed("login", { email });
  assert.ok(typeof token === "string" && token.length > 0, "Seed login must issue a Bearer token");
  const headers = { Authorization: `Bearer ${token}` };
  let original = `/api/v1/files/tasks/${taskId}/original`;
  if (kind === "item") {
    const datasets = await (
      await http(`/api/v1/projects/${projectId}/datasets`, { headers })
    ).json();
    assert.equal(datasets.length, 1);
    const [{ id: datasetId }] = datasets;
    assert.match(datasetId, uuid);
    const items = await (
      await http(`/api/v1/datasets/${datasetId}/items?media_type=video`, { headers })
    ).json();
    assert.equal(items.total, 1);
    assert.equal(items.items.length, 1);
    const [{ id: itemId, dataset_id }] = items.items;
    assert.equal(dataset_id, datasetId);
    assert.match(itemId, uuid);
    original = `/api/v1/datasets/${datasetId}/items/${itemId}/original`;
  }
  const { content_id, sha256 } = await (await http(`${original}-identity`, { headers })).json();
  assert.match(content_id, uuid);
  assert.match(sha256, /^[0-9a-f]{64}$/);
  return { owner, projectId, taskId, original, headers, content_id, sha256 };
}

function prepare(viewer, status = 200) {
  return http(`${viewer.original}-playback`, {
    method: "POST",
    headers: viewer.headers,
    data: { content_id: viewer.content_id, sha256: viewer.sha256, session_id: randomUUID() },
    status,
  });
}

async function openLease(viewer) {
  const response = await prepare(viewer);
  const { lease_id: id, media_path: path, expires_at } = await response.json();
  assert.match(id, uuid);
  assert.equal(path, `/api/v1/media/original-playback/${id}`);
  const [setCookie] = response.headers.getSetCookie();
  const [cookie, ...attributes] = (setCookie ?? "").split(/;\s*/);
  assert.ok(
    /^yoao_video_lease=[a-zA-Z0-9_-]+$/.test(cookie) && attributes.includes(`Path=${path}`),
    "Playback preparation must issue a cookie scoped to its media path",
  );
  return {
    path,
    expiresAt: Date.parse(expires_at),
    headers: { Cookie: cookie },
    sha256: viewer.sha256,
  };
}

function head(lease, status = 200) {
  return http(lease.path, { method: "HEAD", headers: lease.headers, status });
}

async function expectRetired(lease) {
  await head(lease, 410);
  assert.ok(lease.expiresAt > Date.now(), "Cleanup must retire the lease before its expiry");
}

async function readBytes(lease) {
  const response = await http(lease.path, { headers: lease.headers });
  const bytes = Buffer.from(await response.arrayBuffer());
  assert.equal(createHash("sha256").update(bytes).digest("hex"), lease.sha256);
  return bytes;
}

for (const scope of scopes) {
  test(`${scope} original playback fixture cleanup`, async () => {
    const a = await seedViewer(scope, `${scope}a`);
    const b = await seedViewer(scope === "task" ? "item" : "task", `${scope}b`);
    const aLease = await openLease(a);
    const bLease = await openLease(b);
    await head(aLease);
    await head(bLease);
    const expectedNeighbor = await readBytes(bLease);
    const expectNeighbor = async () => {
      assert.ok((await readBytes(bLease)).equals(expectedNeighbor), "Neighbor media bytes changed");
    };
    assert.deepEqual(await (await prepare(b, 429)).json(), {
      detail: { code: "video_playback_capacity_exceeded" },
    });

    await seed("owned-cleanup", a.owner);
    await expectRetired(aLease);
    await expectNeighbor();

    await seed("owned-cleanup", a.owner);
    await expectRetired(aLease);
    await expectNeighbor();

    const rebuilt = await seedViewer(scope, a.owner.namespace);
    assert.notEqual(rebuilt.projectId, a.projectId);
    assert.notEqual(rebuilt.taskId, a.taskId);
    assert.notEqual(rebuilt.original, a.original);
    const rebuiltLease = await openLease(rebuilt);
    assert.notEqual(rebuiltLease.path, aLease.path);
    await readBytes(rebuiltLease);
    await seed("owned-cleanup", rebuilt.owner);
    await expectRetired(rebuiltLease);
    await expectNeighbor();

    const replacement = await openLease(b);
    assert.notEqual(replacement.path, bLease.path);
    await head(replacement);
    assert.ok(
      (await readBytes(replacement)).equals(expectedNeighbor),
      "Refilled media bytes changed",
    );
    await seed("owned-cleanup", b.owner);
    await expectRetired(bLease);
    await expectRetired(replacement);
    completed.add(scope);
  });
}
