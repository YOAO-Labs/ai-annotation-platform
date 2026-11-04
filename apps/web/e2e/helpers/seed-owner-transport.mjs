import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const apiRoot = fileURLToPath(new URL("../../../api/", import.meta.url));
const python = fileURLToPath(new URL("../../../api/.venv/bin/python", import.meta.url));
const script = fileURLToPath(new URL("../fixtures/seed-request.py", import.meta.url));
let transport;

function startOwner() {
  const child = spawn(python, [script], { cwd: apiRoot });
  const pending = [];
  const closed = Promise.withResolvers();
  let failure;
  let failureKind;
  const fail = (error) => {
    if (failure) return;
    failure = error;
    for (const request of pending) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.length = 0;
    child.kill();
  };
  createInterface({ input: child.stderr }).on("line", (line) => {
    failureKind = line.match(/^Fixture setup failed: (\w+)$/)?.[1] ?? failureKind;
  });
  createInterface({ input: child.stdout }).on("line", (line) => {
    const request = pending.shift();
    if (!request) {
      fail(new Error("Fixture owner returned an unsolicited response"));
      return;
    }
    clearTimeout(request.timer);
    try {
      request.resolve(JSON.parse(line));
    } catch {
      const error = new Error("Fixture owner returned invalid JSON");
      request.reject(error);
      fail(error);
    }
  });
  child.on("error", (error) => {
    fail(new Error(`Fixture setup failed: ${error.code}`));
  });
  child.stdin.on("error", (error) => {
    fail(new Error(`Fixture setup failed: ${error.code}`));
  });
  child.once("close", (code) => {
    if (code !== 0 || pending.length) {
      fail(new Error(`Fixture setup failed: ${failureKind ?? `process exit ${code}`}`));
    }
    closed.resolve();
  });
  return {
    post(payload, timeout) {
      if (failure) return Promise.reject(failure);
      const { promise, resolve, reject } = Promise.withResolvers();
      const timer =
        timeout > 0
          ? setTimeout(() => fail(new Error("Fixture owner request timed out")), timeout)
          : undefined;
      pending.push({ resolve, reject, timer });
      child.stdin.write(`${JSON.stringify(payload)}\n`);
      return promise;
    },
    async close() {
      child.stdin.end();
      await closed.promise;
      if (failure) throw failure;
    },
  };
}

export async function closeFixtureTransport() {
  const active = transport;
  transport = undefined;
  await active?.close();
}

export async function prepareFixtureSuite(suiteId, family) {
  transport ??= startOwner();
  const response = await transport.post(
    { operation: "prepare_fixture_suite", data: { suite_id: suiteId, family } },
    120_000,
  );
  if (response.status !== 200) {
    throw new Error(`Fixture suite preparation failed: ${response.status}`);
  }
  return JSON.parse(response.body);
}

export async function runMaskSliceLifecycle(data) {
  transport ??= startOwner();
  const response = await transport.post({ operation: "mask_slice_lifecycle", data }, 120_000);
  if (response.status !== 200) throw new Error(`Mask lifecycle fixture failed: ${response.status}`);
  return JSON.parse(response.body);
}

async function requestFixture(url, method, options) {
  const target = new URL(url);
  if (!target.pathname.startsWith("/api/v1/__test/seed/")) {
    throw new Error("Setup credentials may only prepare fixture endpoints");
  }
  transport ??= startOwner();
  const response = await transport.post(
    {
      method,
      path: target.pathname + target.search,
      data: options?.data,
      headers: options?.headers,
    },
    options?.timeout ?? 30_000,
  );
  return {
    ok: () => response.status >= 200 && response.status < 300,
    status: () => response.status,
    text: async () => response.body,
    json: async () => JSON.parse(response.body),
  };
}

export function postFixture(url, options) {
  return requestFixture(url, "POST", options);
}

export function getScreenshotCatalog(url, options) {
  if (new URL(url).pathname !== "/api/v1/__test/seed/catalog") {
    throw new Error("Fixture catalog transport only permits the screenshot catalog");
  }
  return requestFixture(url, "GET", options);
}
