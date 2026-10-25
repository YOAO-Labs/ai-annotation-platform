import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/realtime_event.schema.json");

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

function matchesPattern(value, schema) {
  if (typeof value !== "string") return false;
  const match = new RegExp(schema.pattern).exec(value);
  return match !== null && match[0] === value;
}

function validDateTime(value, schema) {
  if (!matchesPattern(value, schema)) return false;
  const match = new RegExp(schema.pattern).exec(value);
  const [
    ,
    rawYear,
    rawMonth,
    rawDay,
    rawHour,
    rawMinute,
    rawSecond,
    ,
    zone,
    ,
    rawOffsetHour,
    rawOffsetMinute,
  ] = match;
  const year = Number(rawYear);
  const month = Number(rawMonth);
  const day = Number(rawDay);
  const hour = Number(rawHour);
  const minute = Number(rawMinute);
  const second = Number(rawSecond);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) {
    return false;
  }
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (day < 1 || day > daysInMonth[month - 1]) return false;
  if (zone !== "Z" && (Number(rawOffsetHour) > 23 || Number(rawOffsetMinute) > 59)) {
    return false;
  }
  return true;
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
  assert.ok(
    matchesPattern(message.id, properties.id) && message.id.length === properties.id.maxLength,
  );
  assert.ok(
    matchesPattern(message.target_id, properties.target_id) &&
      message.target_id.length === properties.target_id.maxLength,
  );
  assert.ok(
    typeof message.type === "string" &&
      Array.from(message.type).length <= properties.type.maxLength,
  );
  assert.ok(
    typeof message.target_type === "string" &&
      Array.from(message.target_type).length <= properties.target_type.maxLength,
  );
  assert.ok(validDateTime(message.created_at, properties.created_at));
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
  assert.equal(event.properties.id.minLength, 36);
  assert.equal(event.properties.id.maxLength, 36);
  assert.equal(event.properties.created_at.format, "date-time");
  assert.equal(event.properties.type.maxLength, 60);
  assert.equal(event.properties.type.enum, undefined);
  assert.equal(event.properties.payload.type, undefined);
  assert.equal(event.properties.payload.anyOf, undefined);

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
