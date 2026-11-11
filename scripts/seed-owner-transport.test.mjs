import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";

async function withRelay(t, onMessage) {
  const directory = await mkdtemp(join(tmpdir(), "seed-owner-"));
  const socketPath = join(directory, "relay.sock");
  const sockets = new Set();
  let connections = 0;
  const messages = [];
  const server = createServer({ allowHalfOpen: true }, (socket) => {
    connections++;
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => {});
    createInterface({ input: socket }).on("line", (line) => {
      const message = JSON.parse(line);
      messages.push(message);
      onMessage(socket, message, messages);
    });
  });
  await new Promise((resolve, reject) => server.once("error", reject).listen(socketPath, resolve));
  const previous = {
    PLAYWRIGHT_RASTER_OFFLINE: process.env.PLAYWRIGHT_RASTER_OFFLINE,
    PLAYWRIGHT_RASTER_FIXTURE_SOCKET: process.env.PLAYWRIGHT_RASTER_FIXTURE_SOCKET,
    PLAYWRIGHT_FIXTURE_RUN_ID: process.env.PLAYWRIGHT_FIXTURE_RUN_ID,
  };
  process.env.PLAYWRIGHT_RASTER_OFFLINE = "1";
  process.env.PLAYWRIGHT_RASTER_FIXTURE_SOCKET = socketPath;
  process.env.PLAYWRIGHT_FIXTURE_RUN_ID = "socket-test-suite";
  const transport = await import(`../apps/web/e2e/helpers/seed-owner-transport.mjs?${socketPath}`);
  t.after(async () => {
    await transport.closeFixtureTransport().catch(() => {});
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    await rm(directory, { recursive: true, force: true });
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  return { transport, messages, connections: () => connections };
}

test("EOF during acknowledgement preserves the operation response and rejects a late request", async (t) => {
  const operationId = "operation-with-refusal";
  const response = {
    operation_id: operationId,
    status: 409,
    body: JSON.stringify({ detail: { code: "fixture_original_store_unqualified" } }),
  };
  const relay = await withRelay(t, (socket, message) => {
    if (message.action === "execute") socket.write(`${JSON.stringify(response)}\n`);
    if (message.action === "ack") socket.end();
  });
  let initial;
  try {
    await relay.transport.postFixture("http://fixture.local/api/v1/__test/seed/inject-raster-mask", {
      operationId,
    });
    assert.fail("operation must not succeed without an acknowledgement");
  } catch (error) {
    initial = error;
  }
  assert.equal(initial.operationId, operationId);
  assert.deepEqual(initial.fixtureResponse, response);
  assert.match(initial.message, /ended|closed/);
  await assert.rejects(relay.transport.recoverFixtureOperation("late-operation"), (error) => {
    assert.equal(error.operationId, "late-operation");
    assert.equal(error.cause, initial.cause);
    return true;
  });
  assert.deepEqual(relay.messages.map((message) => message.action ?? message.operation), [
    "bind_fixture_suite",
    "execute",
    "ack",
  ]);
  assert.equal(relay.connections(), 1);
});

test("EOF rejects every pending operation with its own identity and prevents further writes", async (t) => {
  const relay = await withRelay(t, (socket, message, messages) => {
    if (message.action === "recover" && messages.filter((entry) => entry.action === "recover").length === 2) {
      socket.end();
    }
  });
  const results = await Promise.allSettled([
    relay.transport.recoverFixtureOperation("pending-one"),
    relay.transport.recoverFixtureOperation("pending-two"),
  ]);
  assert.deepEqual(results.map((result) => result.status), ["rejected", "rejected"]);
  assert.deepEqual(results.map((result) => result.reason.operationId), ["pending-one", "pending-two"]);
  assert.equal(results[0].reason.cause, results[1].reason.cause);
  await assert.rejects(relay.transport.acknowledgeFixtureOperation("late-ack"), (error) => {
    assert.equal(error.operationId, "late-ack");
    assert.equal(error.cause, results[0].reason.cause);
    return true;
  });
  assert.deepEqual(relay.messages.map((message) => message.action ?? message.operation), [
    "bind_fixture_suite",
    "recover",
    "recover",
  ]);
  assert.equal(relay.connections(), 1);
});
