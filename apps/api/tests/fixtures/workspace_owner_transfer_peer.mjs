import { TextDecoder } from "node:util";

const schema = "aap-workspace-owner-transfer/v1";
const endpoint = "/v1/workspace/owner-transfer/execute";
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const integerPattern = /^-?(?:0|[1-9][0-9]*)$/;
const maxBytes = 16 * 1024;

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Workspace owner transfer peer validation failed");
}

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, keys) {
  return (
    record(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

function scanJson(source) {
  let offset = 0;
  let depth = 0;
  const whitespace = () => {
    while (offset < source.length && /[\t\n\r ]/.test(source[offset])) offset += 1;
  };
  const string = () => {
    if (source[offset] !== '"') fail();
    const start = offset++;
    while (offset < source.length) {
      if (source[offset] === '"') {
        offset += 1;
        try {
          return JSON.parse(source.slice(start, offset));
        } catch {
          fail();
        }
      }
      if (source[offset] === "\\") {
        offset += 1;
        if (source[offset] === "u") offset += 5;
        else offset += 1;
      } else {
        offset += 1;
      }
    }
    fail();
  };
  const value = () => {
    whitespace();
    depth += 1;
    if (depth > 64) fail();
    if (source[offset] === "{") {
      offset += 1;
      whitespace();
      const keys = new Set();
      if (source[offset] !== "}") {
        while (true) {
          whitespace();
          const key = string();
          if (keys.has(key)) fail();
          keys.add(key);
          whitespace();
          if (source[offset++] !== ":") fail();
          value();
          whitespace();
          if (source[offset] === "}") break;
          if (source[offset++] !== ",") fail();
        }
      }
      offset += 1;
    } else if (source[offset] === "[") {
      offset += 1;
      whitespace();
      if (source[offset] !== "]") {
        while (true) {
          value();
          whitespace();
          if (source[offset] === "]") break;
          if (source[offset++] !== ",") fail();
        }
      }
      offset += 1;
    } else if (source[offset] === '"') {
      string();
    } else {
      const start = offset;
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
      if (start === offset) fail();
    }
    depth -= 1;
  };

  value();
  whitespace();
  if (offset !== source.length) fail();
}

function parseJson(value) {
  let source;
  try {
    source =
      typeof value === "string" ? value : new TextDecoder("utf-8", { fatal: true }).decode(value);
    if (Buffer.byteLength(source, "utf8") > maxBytes) fail();
    scanJson(source);
    return JSON.parse(source, (_key, value, context) => {
      if (typeof value !== "number") return value;
      if (typeof context?.source !== "string") fail();
      return new JsonNumber(context.source);
    });
  } catch {
    fail();
  }
}

function uuid(value) {
  if (typeof value !== "string" || !uuidPattern.test(value)) fail();
  return value.toLowerCase();
}

function awareTimestamp(value) {
  if (typeof value !== "string") return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!match) return false;
  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    zone,
    ,
    zoneHourText,
    zoneMinuteText,
  ] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const zoneHour = zoneHourText === undefined ? 0 : Number(zoneHourText);
  const zoneMinute = zoneMinuteText === undefined ? 0 : Number(zoneMinuteText);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth =
    [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] ?? 0;
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    zone.length > 0 &&
    zoneHour < 24 &&
    zoneMinute <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}

function validateRequest(value) {
  if (
    !exact(value, ["schema", "operation_id", "actor_id", "project_id", "new_owner_id"]) ||
    value.schema !== schema
  )
    fail();
  return {
    schema,
    operation_id: uuid(value.operation_id),
    actor_id: uuid(value.actor_id),
    project_id: uuid(value.project_id),
    new_owner_id: uuid(value.new_owner_id),
  };
}

function validateReply(requestValue, value) {
  const request = validateRequest(requestValue);
  if (!record(value) || value.schema !== schema || typeof value.ok !== "boolean") fail();
  if (value.ok) {
    if (
      !exact(value, [
        "schema",
        "ok",
        "operation_id",
        "actor_id",
        "project_id",
        "new_owner_id",
        "data",
      ]) ||
      uuid(value.operation_id) !== request.operation_id ||
      uuid(value.actor_id) !== request.actor_id ||
      uuid(value.project_id) !== request.project_id ||
      uuid(value.new_owner_id) !== request.new_owner_id ||
      !exact(value.data, ["previous_owner_id", "updated_at", "changed"])
    )
      fail();
    const previous =
      value.data.previous_owner_id === null ? null : uuid(value.data.previous_owner_id);
    const changed = previous !== request.new_owner_id;
    if (
      !awareTimestamp(value.data.updated_at) ||
      typeof value.data.changed !== "boolean" ||
      value.data.changed !== changed
    )
      fail();
    return value;
  }
  if (
    !exact(value, ["schema", "ok", "error"]) ||
    !exact(value.error, ["status_code", "detail", "rollback"]) ||
    !strictStatusCode(value.error.status_code) ||
    typeof value.error.rollback !== "boolean"
  )
    fail();
  return value;
}

function strictStatusCode(value) {
  if (value instanceof JsonNumber) {
    if (!integerPattern.test(value.raw)) return false;
    const number = BigInt(value.raw);
    return number >= 400n && number <= 599n;
  }
  return Number.isSafeInteger(value) && value >= 400 && value <= 599;
}

function stringifyLossless(value) {
  if (value instanceof JsonNumber) return value.raw;
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function validateCall(value) {
  if (
    !exact(value, ["mode", "origin", "control_token", "request", "expect_transport_failure"]) ||
    value.mode !== "owner_http" ||
    typeof value.origin !== "string" ||
    typeof value.control_token !== "string" ||
    value.control_token.length < 1 ||
    value.control_token.length > 4096 ||
    typeof value.expect_transport_failure !== "boolean"
  )
    fail();
  let parsed;
  try {
    parsed = new URL(value.origin);
  } catch {
    fail();
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !["127.0.0.1", "::1", "[::1]", "localhost"].includes(parsed.hostname) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  )
    fail();
  return {
    origin: parsed.origin,
    control_token: value.control_token,
    request: validateRequest(value.request),
    expect_transport_failure: value.expect_transport_failure,
  };
}

function rejects(callback) {
  try {
    callback();
  } catch {
    return true;
  }
  return false;
}

function selfCheck() {
  const request = {
    schema,
    operation_id: "11111111-1111-4111-8111-111111111111",
    actor_id: "22222222-2222-4222-8222-222222222222",
    project_id: "33333333-3333-4333-8333-333333333333",
    new_owner_id: "44444444-4444-4444-8444-444444444444",
  };
  const success = {
    schema,
    ok: true,
    operation_id: request.operation_id,
    actor_id: request.actor_id,
    project_id: request.project_id,
    new_owner_id: request.new_owner_id,
    data: {
      previous_owner_id: request.actor_id,
      updated_at: "2026-10-21T01:02:03.123456+00:00",
      changed: true,
    },
  };
  const failure = {
    schema,
    ok: false,
    error: { status_code: 409, detail: { reason: "project_busy" }, rollback: false },
  };
  validateReply(request, success);
  validateReply(request, failure);
  const checks = [
    rejects(() => parseJson('{"operation_id":"a","operation_id":"b"}')),
    rejects(() => parseJson('{"x":NaN}')),
    rejects(() => parseJson(Buffer.from([0xc3, 0x28]))),
    rejects(() => parseJson(`${"[".repeat(65)}0${"]".repeat(65)}`)),
    rejects(() => validateRequest({ ...request, extra: true })),
    rejects(() => validateRequest({ ...request, project_id: "not-a-uuid" })),
    rejects(() => validateReply(request, { ...success, project_id: request.actor_id })),
    rejects(() => validateReply(request, { ...success, secret: "forbidden" })),
    rejects(() =>
      validateReply(request, { ...success, data: { ...success.data, changed: false } }),
    ),
    rejects(() =>
      validateReply(request, {
        ...success,
        data: { ...success.data, updated_at: "2026-10-21T01:02:03" },
      }),
    ),
    rejects(() =>
      validateReply(request, {
        ...success,
        data: { ...success.data, updated_at: "2026-02-29T01:02:03Z" },
      }),
    ),
    rejects(() => validateReply(request, { ...failure, error: { ...failure.error, rollback: 0 } })),
    rejects(() =>
      validateReply(
        request,
        parseJson(
          '{"schema":"aap-workspace-owner-transfer/v1","ok":false,"error":{"status_code":409.0,"detail":null,"rollback":false}}',
        ),
      ),
    ),
  ];
  if (!checks.every(Boolean)) fail();
  return { ok: true, checks: checks.length + 2 };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return parseJson(Buffer.concat(chunks));
}

async function callOwner(input) {
  const call = validateCall(input);
  let response;
  try {
    response = await fetch(call.origin + endpoint, {
      method: "POST",
      headers: {
        authorization: `Bearer ${call.control_token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(call.request),
      redirect: "error",
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    if (call.expect_transport_failure) return { ok: true, transport_failure: "connection_lost" };
    fail();
  }
  if (call.expect_transport_failure || response.status !== 200) fail();
  const body = parseJson(Buffer.from(await response.arrayBuffer()));
  const reply = validateReply(call.request, body);
  return { ok: true, reply };
}

async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify(selfCheck())}\n`);
    return;
  }
  const result = await callOwner(await readStdin());
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

main().catch(() => {
  process.stderr.write("Workspace owner transfer peer validation failed\n");
  process.exitCode = 1;
});
