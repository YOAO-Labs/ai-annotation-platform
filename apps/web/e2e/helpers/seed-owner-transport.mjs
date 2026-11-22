import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { createConnection } from "node:net";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const apiRoot = fileURLToPath(new URL("../../../api/", import.meta.url));
const python = fileURLToPath(new URL("../../../api/.venv/bin/python", import.meta.url));
const script = fileURLToPath(new URL("../fixtures/seed-request.py", import.meta.url));
let transport;

function startOwner() {
  const offline = process.env.PLAYWRIGHT_RASTER_OFFLINE === "1";
  const socketPath = offline ? process.env.PLAYWRIGHT_RASTER_FIXTURE_SOCKET : undefined;
  const suiteId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
  if (offline && (!socketPath || !suiteId)) {
    throw new Error("Raster fixture relay requires a socket and suite identity");
  }
  const child = socketPath
    ? createConnection(socketPath)
    : spawn(python, [script], { cwd: apiRoot });
  const input = socketPath ? child : child.stdin;
  const output = socketPath ? child : child.stdout;
  const pending = [];
  const closed = Promise.withResolvers();
  let failure;
  let failureKind;
  let closing = false;
  const correlatedFailure = (error, operationId) => {
    const rejected = new Error(error.message, { cause: error });
    rejected.operationId = operationId;
    return rejected;
  };
  const fail = (error) => {
    if (failure) return;
    failure = error;
    for (const request of pending) {
      clearTimeout(request.timer);
      request.reject(correlatedFailure(error, request.operationId));
    }
    pending.length = 0;
    if (socketPath) child.destroy();
    else child.kill();
  };
  if (!socketPath) {
    createInterface({ input: child.stderr }).on("line", (line) => {
      failureKind = line.match(/^Fixture setup failed: (\w+)$/)?.[1] ?? failureKind;
    });
  }
  createInterface({ input: output }).on("line", (line) => {
    const request = pending.shift();
    if (!request) {
      fail(new Error("Fixture owner returned an unsolicited response"));
      return;
    }
    clearTimeout(request.timer);
    try {
      const result = JSON.parse(line);
      if (result.operation_id !== request.operationId) {
        throw new Error("Fixture owner response identity changed");
      }
      request.resolve(result);
    } catch {
      const error = new Error("Fixture owner returned invalid JSON");
      error.operationId = request.operationId;
      request.reject(error);
      fail(error);
    }
  });
  child.on("error", (error) => {
    fail(new Error(`Fixture setup failed: ${error.code}`, { cause: error }));
  });
  input.on("error", (error) => {
    fail(new Error(`Fixture setup failed: ${error.code}`, { cause: error }));
  });
  output.once("end", () => {
    if (!closing || pending.length) {
      fail(new Error("Fixture owner output ended"));
    }
  });
  if (!socketPath) {
    child.once("exit", (code, signal) => {
      if (code !== 0 || !closing || pending.length) {
        const reason =
          failureKind ?? (signal ? `process signal ${signal}` : `process exit ${code}`);
        fail(new Error(`Fixture setup failed: ${reason}`));
      }
    });
  }
  child.once("close", () => {
    if (!failure && (!closing || pending.length)) {
      fail(new Error(socketPath ? "Fixture owner socket closed" : "Fixture owner process closed"));
    }
    closed.resolve();
  });
  if (socketPath) {
    input.write(`${JSON.stringify({ operation: "bind_fixture_suite", suite_id: suiteId })}\n`);
  }
  function exchange(envelope, timeout) {
    if (!failure && (input.writableEnded || input.destroyed || output.readableEnded)) {
      fail(new Error("Fixture owner transport ended"));
    }
    if (failure) return Promise.reject(correlatedFailure(failure, envelope.operation_id));
    const { promise, resolve, reject } = Promise.withResolvers();
    const timer =
      timeout > 0
        ? setTimeout(() => fail(new Error("Fixture owner request timed out")), timeout)
        : undefined;
    pending.push({ resolve, reject, timer, operationId: envelope.operation_id });
    input.write(`${JSON.stringify(envelope)}\n`);
    return promise;
  }
  return {
    async post(payload, timeout, operationId = randomUUID()) {
      const response = await exchange(
        { action: "execute", operation_id: operationId, payload },
        timeout,
      );
      if (response.state === "pending") {
        const error = new Error("Fixture operation outcome remains pending");
        error.operationId = operationId;
        throw error;
      }
      if (
        (response.owner_error !== undefined && !/^[A-Za-z_]\w*$/.test(response.owner_error)) ||
        (response.owner_error === undefined &&
          (!Number.isInteger(response.status) || typeof response.body !== "string")) ||
        (response.journal_error !== undefined && !/^[A-Za-z_]\w*$/.test(response.journal_error)) ||
        (response.disposal_error !== undefined &&
          !/^[A-Za-z_]\w*$/.test(response.disposal_error)) ||
        (response.operation_response !== undefined &&
          (!response.operation_response ||
            typeof response.operation_response !== "object" ||
            !Number.isInteger(response.operation_response.status) ||
            typeof response.operation_response.body !== "string")) ||
        (response.relay_journal_error !== undefined &&
          !/^[A-Za-z_]\w*$/.test(response.relay_journal_error))
      ) {
        const error = new Error("Fixture owner returned an invalid operation result");
        error.operationId = operationId;
        throw error;
      }
      if (
        !["OperationOutcomeUnresolved", "OperationIdentityConflict"].includes(response.owner_error)
      ) {
        try {
          const receipt = await exchange({ action: "ack", operation_id: operationId }, timeout);
          if (receipt.state !== "acknowledged") {
            throw new Error("Fixture operation receipt was not acknowledged");
          }
        } catch (error) {
          error.operationId = operationId;
          Object.defineProperty(error, "fixtureResponse", { value: response });
          throw error;
        }
      }
      const causes = [];
      if (response.operation_response)
        causes.push(
          new Error(`Fixture operation returned HTTP ${response.operation_response.status}`),
        );
      if (response.owner_error && response.owner_error !== response.disposal_error)
        causes.push(new Error(`Fixture operation failed: ${response.owner_error}`));
      if (response.disposal_error)
        causes.push(new Error(`Fixture database disposal failed: ${response.disposal_error}`));
      if (!causes.length && (response.journal_error || response.relay_journal_error))
        causes.push(new Error(`Fixture operation returned HTTP ${response.status}`));
      if (response.journal_error)
        causes.push(new Error(`Fixture evidence journal failed: ${response.journal_error}`));
      if (response.relay_journal_error)
        causes.push(new Error(`Raster evidence journal failed: ${response.relay_journal_error}`));
      if (causes.length) {
        const error =
          causes.length === 1
            ? causes[0]
            : new AggregateError(causes, "Fixture operation and evidence failed");
        error.operationId = operationId;
        Object.defineProperty(error, "fixtureResponse", { value: response });
        throw error;
      }
      return response;
    },
    exchange,
    async close() {
      closing = true;
      input.end();
      await closed.promise;
      if (failure) throw failure;
    },
  };
}

export async function recoverFixtureOperation(operationId) {
  transport ??= startOwner();
  const result = await transport.exchange({ action: "recover", operation_id: operationId }, 30_000);
  if (result.status >= 300) result.body = safeFailureBody(result.body);
  if (result.operation_response?.status >= 300) {
    Object.defineProperty(result, "privateOperationResponse", { value: result.operation_response });
    result.operation_response = {
      ...result.operation_response,
      body: safeFailureBody(result.operation_response.body),
    };
  }
  return result;
}

export async function acknowledgeFixtureOperation(operationId) {
  transport ??= startOwner();
  const result = await transport.exchange({ action: "ack", operation_id: operationId }, 30_000);
  if (result.state !== "acknowledged") throw new Error("Fixture operation is not completed");
}

export async function closeFixtureTransport() {
  const active = transport;
  transport = undefined;
  await active?.close();
}
export async function shutdownFixtureTransport(suiteId) {
  transport ??= startOwner();
  const response = await transport.post({ operation: "owner_shutdown", suite_id: suiteId }, 30_000);
  if (response.status !== 200) throw new Error(`Fixture owner shutdown failed: ${response.status}`);
  return JSON.parse(response.body);
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

export async function verifyFixtureSuite(suiteId, family) {
  transport ??= startOwner();
  const response = await transport.post(
    { operation: "verify_fixture_suite", data: { suite_id: suiteId, family } },
    120_000,
  );
  if (response.status !== 200) {
    throw new Error(`Fixture suite verification failed: ${response.status}`);
  }
  return JSON.parse(response.body);
}

export async function runMaskSliceLifecycle(data) {
  transport ??= startOwner();
  const response = await transport.post({ operation: "mask_slice_lifecycle", data }, 120_000);
  if (response.status !== 200) throw new Error(`Mask lifecycle fixture failed: ${response.status}`);
  return JSON.parse(response.body);
}

function safeFailureBody(body) {
  try {
    const detail = JSON.parse(body)?.detail;
    for (const key of ["code", "reason"]) {
      const value = detail?.[key];
      if (typeof value === "string" && /^[a-z][a-z0-9_]{0,79}$/.test(value)) {
        return JSON.stringify({ detail: { [key]: value } });
      }
    }
  } catch {}
  return JSON.stringify({ detail: "Fixture operation failed; see the private owner journal" });
}

export async function postFixture(url, options) {
  const target = new URL(url);
  if (!target.pathname.startsWith("/api/v1/__test/seed/")) {
    throw new Error("Setup credentials may only prepare fixture endpoints");
  }
  transport ??= startOwner();
  const response = await transport.post(
    {
      method: "POST",
      path: target.pathname + target.search,
      data: options?.data,
      headers: options?.headers,
    },
    options?.timeout ?? 30_000,
    options?.operationId,
  );
  const body =
    response.status >= 200 && response.status < 300
      ? response.body
      : safeFailureBody(response.body);
  return {
    ok: () => response.status >= 200 && response.status < 300,
    status: () => response.status,
    text: async () => body,
    json: async () => JSON.parse(body),
  };
}
