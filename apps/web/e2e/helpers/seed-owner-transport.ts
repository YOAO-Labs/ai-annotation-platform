/// <reference lib="esnext.promise" />
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import type { APIRequestContext, APIResponse } from "@playwright/test";

export type SeedResponse = Pick<APIResponse, "ok" | "status" | "text" | "json">;
type FixtureResult = { status: number; body: string };
type Pending = {
  resolve: (result: FixtureResult) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout | undefined;
};
interface FixtureOwnerTransport {
  post(payload: unknown, timeout: number): Promise<FixtureResult>;
  close(): Promise<void>;
}

const apiRoot = fileURLToPath(new URL("../../../api/", import.meta.url));
const python = fileURLToPath(new URL("../../../api/.venv/bin/python", import.meta.url));
const script = fileURLToPath(new URL("../fixtures/seed-request.py", import.meta.url));
let transport: FixtureOwnerTransport | undefined;

function startOwner(): FixtureOwnerTransport {
  const child = spawn(python, [script], { cwd: apiRoot });
  const pending: Pending[] = [];
  const closed = Promise.withResolvers<void>();
  let failure: Error | undefined;
  let failureKind: string | undefined;
  const fail = (error: Error) => {
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
  child.on("error", (error: NodeJS.ErrnoException) => {
    fail(new Error(`Fixture setup failed: ${error.code}`));
  });
  child.stdin.on("error", (error: NodeJS.ErrnoException) => {
    fail(new Error(`Fixture setup failed: ${error.code}`));
  });
  child.once("close", (code) => {
    if (code !== 0 || pending.length) {
      fail(new Error(`Fixture setup failed: ${failureKind ?? `process exit ${code}`}`));
    }
    closed.resolve();
  });
  return {
    post(payload: unknown, timeout: number): Promise<FixtureResult> {
      if (failure) return Promise.reject(failure);
      const { promise, resolve, reject } = Promise.withResolvers<FixtureResult>();
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

export async function closeFixtureTransport(): Promise<void> {
  const active = transport;
  transport = undefined;
  await active?.close();
}

export async function runMaskSliceLifecycle(data: {
  action: "gc" | "drop_revision" | "expire_revision";
  task_id: string;
  operation_id: string;
  namespace: string;
}): Promise<{ orphan_deleted?: boolean }> {
  transport ??= startOwner();
  const response = await transport.post({ operation: "mask_slice_lifecycle", data }, 120_000);
  if (response.status !== 200) throw new Error(`Mask lifecycle fixture failed: ${response.status}`);
  return JSON.parse(response.body);
}

export async function postFixture(
  url: string,
  options?: Parameters<APIRequestContext["post"]>[1],
): Promise<SeedResponse> {
  const target = new URL(url);
  if (!target.pathname.startsWith("/api/v1/__test/seed/")) {
    throw new Error("Setup credentials may only prepare fixture endpoints");
  }
  transport ??= startOwner();
  const response = await transport.post(
    {
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
