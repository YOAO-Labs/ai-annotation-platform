import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const schema = "aap-identity-actor/v1";
const endpoint = "/v1/identity/actor-context";
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const minInt32 = -(1n << 31n);
const maxInt32 = (1n << 31n) - 1n;
const actorFields = ["id", "name", "email", "role", "is_active", "weekly_target_default"];

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity actor-context request or response");
}

function isRecord(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    !(value instanceof JsonNumber)
  );
}

function exact(value, fields) {
  return (
    isRecord(value) &&
    Object.keys(value).length === fields.length &&
    fields.every((field) => Object.hasOwn(value, field))
  );
}

function uuid(value) {
  if (typeof value !== "string" || !uuidPattern.test(value)) fail();
  return value.toLowerCase();
}

function scanJson(source) {
  let offset = 0;
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
      return;
    }
    if (source[offset] === "[") {
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
      return;
    }
    if (source[offset] === '"') {
      string();
      return;
    }
    const start = offset;
    while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
    if (start === offset) fail();
  };

  value();
  whitespace();
  if (offset !== source.length) fail();
}

function parseStrictJson(source) {
  if (typeof source !== "string") fail();
  scanJson(source);
  try {
    return JSON.parse(source, (key, value, context) => {
      if (typeof value !== "number") return value;
      if (typeof context?.source !== "string") fail();
      return new JsonNumber(context.source);
    });
  } catch {
    fail();
  }
}

function stringifyLossless(value) {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (value instanceof JsonNumber) return value.raw;
  if (typeof value === "bigint") return value.toString(10);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) fail();
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
      .join(",")}}`;
  }
  fail();
}

function int32(value) {
  if (!(value instanceof JsonNumber) || !/^-?(?:0|[1-9][0-9]*)$/.test(value.raw)) fail();
  const number = BigInt(value.raw);
  if (number < minInt32 || number > maxInt32) fail();
  return number;
}

function validateRequest(value) {
  if (!exact(value, ["schema", "actor_id", "admitted_role"]) || value.schema !== schema) fail();
  if (!roles.has(value.admitted_role)) fail();
  return { schema, actor_id: uuid(value.actor_id), admitted_role: value.admitted_role };
}

function validateActorContext(request, value) {
  if (
    !exact(value, actorFields) ||
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    !roles.has(value.role) ||
    value.is_active !== true ||
    (value.weekly_target_default !== null && !(value.weekly_target_default instanceof JsonNumber))
  ) {
    fail();
  }
  value.id = uuid(value.id);
  if (value.id !== request.actor_id || value.role !== request.admitted_role) fail();
  if (value.weekly_target_default !== null) int32(value.weekly_target_default);
  return value;
}

function validateReply(request, source, status, contentType) {
  if (
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseStrictJson(source);
  if (document?.schema !== schema || typeof document.ok !== "boolean") fail();
  if (document.ok === true) {
    if (status !== 200 || !exact(document, ["schema", "ok", "data"])) fail();
    document.data = validateActorContext(request, document.data);
    return document;
  }
  if (
    !exact(document, ["schema", "ok", "error"]) ||
    !exact(document.error, ["code"]) ||
    !["invalid_request", "credential_invalid"].includes(document.error.code)
  ) {
    fail();
  }
  if (
    (document.error.code === "invalid_request" && status !== 400) ||
    (document.error.code === "credential_invalid" && status !== 200)
  ) {
    fail();
  }
  return document;
}

function validateCallInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  if (
    typeof value.url !== "string" ||
    typeof value.control_token !== "string" ||
    !/^[\x21-\x7e]{1,4096}$/.test(value.control_token)
  ) {
    fail();
  }
  let origin;
  try {
    origin = new URL(value.url);
  } catch {
    fail();
  }
  if (
    !["http:", "https:"].includes(origin.protocol) ||
    !origin.hostname ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) {
    fail();
  }
  return {
    origin,
    controlToken: value.control_token,
    request: validateRequest(value.request),
  };
}

function postActorContext(origin, controlToken, request) {
  const url = new URL(endpoint, origin);
  const transport = url.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(request), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const outgoing = transport(
      url,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(15_000),
        headers: {
          authorization: `Bearer ${controlToken}`,
          "content-type": "application/json",
          accept: "application/json",
          "content-length": body.length,
        },
      },
      (response) => {
        const chunks = [];
        let size = 0;
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > 16 * 1024 * 1024) {
            response.destroy(new Error("oversized response"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              body: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Invalid Identity actor-context response"));
          }
        });
        response.on("error", () =>
          rejectResponse(new Error("Identity actor-context request failed")),
        );
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity actor-context request failed")));
    outgoing.end(body);
  });
}

async function callActorContext(value) {
  const { origin, controlToken, request } = validateCallInput(value);
  const response = await postActorContext(origin, controlToken, request);
  return validateReply(request, response.body, response.status, response.contentType);
}

function expectFailure(callback) {
  try {
    callback();
  } catch {
    return;
  }
  fail();
}

function syntheticSuccess(request, data) {
  return stringifyLossless({ schema, ok: true, data });
}

function syntheticActor(request, overrides = {}) {
  return {
    id: request.actor_id,
    name: " Actor Name ",
    email: " actor@example.test ",
    role: request.admitted_role,
    is_active: true,
    weekly_target_default: new JsonNumber("31"),
    ...overrides,
  };
}

function selfCheck() {
  let checks = 0;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const request = validateRequest({
    schema,
    actor_id: actorId,
    admitted_role: "employee",
  });
  const valid = syntheticSuccess(request, syntheticActor(request));
  const decoded = validateReply(request, valid, 200, "application/json; charset=utf-8");
  if (
    decoded.data.name !== " Actor Name " ||
    decoded.data.email !== " actor@example.test " ||
    int32(decoded.data.weekly_target_default) !== 31n
  ) {
    fail();
  }
  checks += 1;

  const nullable = validateReply(
    request,
    syntheticSuccess(request, syntheticActor(request, { weekly_target_default: null })),
    200,
    "application/json",
  );
  if (nullable.data.weekly_target_default !== null) fail();
  checks += 1;

  const boundary = validateReply(
    request,
    syntheticSuccess(
      request,
      syntheticActor(request, { weekly_target_default: new JsonNumber("2147483647") }),
    ),
    200,
    "application/json",
  );
  if (int32(boundary.data.weekly_target_default) !== maxInt32) fail();
  checks += 1;

  const rejected = validateReply(
    request,
    JSON.stringify({ schema, ok: false, error: { code: "credential_invalid" } }),
    200,
    "application/json",
  );
  if (rejected.error.code !== "credential_invalid") fail();
  checks += 1;

  const invalid = validateReply(
    request,
    JSON.stringify({ schema, ok: false, error: { code: "invalid_request" } }),
    400,
    "application/json",
  );
  if (invalid.error.code !== "invalid_request") fail();
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      JSON.stringify({ schema, ok: false, error: { code: "invalid_request" } }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      JSON.stringify({ schema, ok: false, error: { code: "credential_invalid" } }),
      400,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      JSON.stringify({ schema, ok: 1, data: syntheticActor(request) }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      JSON.stringify({ schema, ok: false, error: { code: "credential_invalid", detail: "x" } }),
      200,
      "application/json",
    ),
  );
  checks += 1;

  expectFailure(() =>
    validateRequest({ protocol: schema, actor_id: actorId, admitted_role: "employee" }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({ schema, actor_id: actorId, admitted_role: "legacy-annotator" }),
  );
  checks += 1;
  expectFailure(() => validateRequest({ schema, actor_id: true, admitted_role: "employee" }));
  checks += 1;
  expectFailure(() =>
    validateRequest({ schema, actor_id: actorId, admitted_role: "employee", token: "user-token" }),
  );
  checks += 1;

  for (const number of ["1.0", "1e0", "2147483648", "-2147483649"]) {
    expectFailure(() =>
      validateReply(
        request,
        syntheticSuccess(
          request,
          syntheticActor(request, { weekly_target_default: new JsonNumber(number) }),
        ),
        200,
        "application/json",
      ),
    );
    checks += 1;
  }
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(request, syntheticActor(request, { weekly_target_default: true })),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(request, syntheticActor(request, { is_active: 1 })),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(request, syntheticActor(request, { is_active: false })),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(
        request,
        syntheticActor(request, { id: "22222222-2222-4222-8222-222222222222" }),
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(request, syntheticActor(request, { role: "project_admin" })),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateReply(
      request,
      syntheticSuccess(request, syntheticActor(request, { preferences: {} })),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() => parseStrictJson('{"schema":"aap-identity-actor/v1","ok":true,"ok":false}'));
  checks += 1;
  expectFailure(() => parseStrictJson('{"weekly_target_default":NaN}'));
  checks += 1;
  return checks;
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1024 * 1024) fail();
    chunks.push(chunk);
  }
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify({ ok: true, checks: selfCheck() })}\n`);
    return;
  }
  const input = await readInput();
  process.stdout.write(`${stringifyLossless(await callActorContext(input))}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write("Identity actor-context peer validation failed\n");
    process.exitCode = 1;
  });
}
