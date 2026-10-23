import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/realtime_event.schema.json");
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const dateTimePattern = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/;

async function loadContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

function assertClosedSchemaReferences(contract) {
  const visit = (value) => {
    if (Array.isArray(value)) {
      for (const child of value) visit(child);
      return;
    }
    if (value === null || typeof value !== "object") return;
    if (value.$ref !== undefined) {
      assert.ok(value.$ref.startsWith("#/$defs/"));
      assert.ok(contract.$defs[value.$ref.slice("#/$defs/".length)]);
    }
    for (const child of Object.values(value)) visit(child);
  };
  visit(contract);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateRealtimeMessage(message, contract) {
  assert.ok(isRecord(message));
  const sync = contract.$defs.NotificationSyncEvent;
  if (message.type === sync.properties.type.const) {
    const reason = sync.properties.reason;
    if (Object.hasOwn(message, "reason")) {
      assert.ok(
        message.reason === null ||
          reason.anyOf.some((variant) => typeof message.reason === variant.type),
      );
    }
    return;
  }

  const event = contract.$defs.NotificationEvent;
  const properties = event.properties;
  for (const field of event.required) assert.ok(Object.hasOwn(message, field));
  if (event.additionalProperties === false) {
    for (const field of Object.keys(message)) assert.ok(Object.hasOwn(properties, field));
  }
  assert.ok(typeof message.id === "string" && uuidPattern.test(message.id));
  assert.ok(typeof message.target_id === "string" && uuidPattern.test(message.target_id));
  assert.ok(
    typeof message.type === "string" &&
      Array.from(message.type).length <= properties.type.maxLength,
  );
  assert.ok(
    typeof message.target_type === "string" &&
      Array.from(message.target_type).length <= properties.target_type.maxLength,
  );
  assert.ok(isRecord(message.payload));
  assert.ok(
    typeof message.created_at === "string" &&
      dateTimePattern.test(message.created_at) &&
      Number.isFinite(Date.parse(message.created_at)),
  );
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function validateStdioMessage() {
  const raw = await readStdin();
  try {
    const contract = await loadContract();
    validateRealtimeMessage(JSON.parse(raw), contract);
  } catch {
    process.stderr.write("invalid notification realtime message\n");
    process.exitCode = 1;
    return;
  }
  process.stdout.write(raw);
}

export async function selfCheck() {
  const contract = await loadContract();
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.$id, "aap-notification-realtime/v1");
  assertClosedSchemaReferences(contract);

  const event = contract.$defs.NotificationEvent;
  assert.equal(event.additionalProperties, false);
  assert.deepEqual(event.required, [
    "id",
    "type",
    "target_type",
    "target_id",
    "payload",
    "created_at",
  ]);
  assert.equal(event.properties.id.format, "uuid");
  assert.equal(event.properties.target_id.format, "uuid");
  assert.equal(event.properties.created_at.format, "date-time");
  assert.equal(event.properties.type.maxLength, 60);
  assert.equal(event.properties.type.enum, undefined);

  const sync = contract.$defs.NotificationSyncEvent;
  assert.equal(sync.properties.type.const, "notifications.sync");
  assert.equal(sync.required.includes("reason"), false);
  assert.equal(sync.additionalProperties, true);

  const eventFrame = {
    id: "60000000-0000-4000-8000-000000000001",
    type: "future.module_event",
    target_type: "future_target",
    target_id: "60000000-0000-4000-8000-000000000002",
    payload: { marker: "event" },
    created_at: "2026-10-24T10:00:00+05:30",
  };
  assert.equal(eventFrame.type.length <= event.properties.type.maxLength, true);
  assert.deepEqual(Object.keys(eventFrame), event.required);

  const markerFrame = { type: "notifications.sync", marker: "before" };
  assert.equal(markerFrame.type, sync.properties.type.const);
  assert.equal(Object.hasOwn(markerFrame, "reason"), false);
  assert.equal(Object.hasOwn(markerFrame, "marker"), true);
  assert.doesNotThrow(() => validateRealtimeMessage(eventFrame, contract));
  assert.doesNotThrow(() => validateRealtimeMessage(markerFrame, contract));
  assert.doesNotThrow(() =>
    validateRealtimeMessage(
      { ...eventFrame, type: "😀".repeat(60), target_type: "🎯".repeat(30) },
      contract,
    ),
  );
  assert.throws(() => validateRealtimeMessage({ ...eventFrame, unexpected: true }, contract));
  assert.throws(() => validateRealtimeMessage({ ...eventFrame, type: "😀".repeat(61) }, contract));
  assert.throws(() =>
    validateRealtimeMessage({ ...eventFrame, target_type: "🎯".repeat(31) }, contract),
  );

  process.stdout.write("Notifications realtime-event schema self-check passed\n");
}

if (process.argv.includes("--stdio")) {
  await validateStdioMessage();
} else if (process.argv.includes("--self-check")) {
  await selfCheck();
}
