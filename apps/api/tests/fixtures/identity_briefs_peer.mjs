import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const schema = "aap-identity-briefs/v1";
const endpoint = "/v1/identity/briefs/execute";
const operations = new Set(["get_user_briefs", "get_project_user_briefs"]);
const userFields = ["id", "name", "email", "role", "avatar_initial", "avatar_ref"];
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const pythonWhitespace =
  /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/u;

function fail() {
  throw new Error("Invalid Identity briefs request or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
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
    return JSON.parse(source);
  } catch {
    fail();
  }
}

function pythonStrip(value) {
  const points = Array.from(value);
  let start = 0;
  let end = points.length;
  while (start < end && pythonWhitespace.test(points[start])) start += 1;
  while (end > start && pythonWhitespace.test(points[end - 1])) end -= 1;
  return points.slice(start, end).join("");
}

function legacyInitial(name, email) {
  const source = name || email || "?";
  const stripped = pythonStrip(source);
  const first = Array.from(stripped)[0];
  return first === undefined ? "?" : first.toUpperCase();
}

function validateRequest(value) {
  if (!exact(value, ["schema", "operation", "payload"]) || value.schema !== schema) fail();
  if (!operations.has(value.operation)) fail();
  const projectOperation = value.operation === "get_project_user_briefs";
  const payloadFields = projectOperation ? ["project_id", "user_ids"] : ["user_ids"];
  if (!exact(value.payload, payloadFields) || !Array.isArray(value.payload.user_ids)) fail();
  const projectId = projectOperation ? uuid(value.payload.project_id) : null;
  const userIds = value.payload.user_ids.map(uuid);
  const request = {
    schema,
    operation: value.operation,
    payload: projectOperation
      ? { project_id: projectId, user_ids: userIds }
      : { user_ids: userIds },
  };
  return {
    request,
    requestedIds: new Set(userIds),
    expectedProjectId: projectId,
  };
}

function validateUserBrief(value) {
  if (
    !exact(value, userFields) ||
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    (value.role !== null && typeof value.role !== "string") ||
    typeof value.avatar_initial !== "string" ||
    (value.avatar_ref !== null && typeof value.avatar_ref !== "string")
  ) {
    fail();
  }
  value.id = uuid(value.id);
  if (value.avatar_initial !== legacyInitial(value.name, value.email)) fail();
  return value;
}

function validateBriefsReply(checked, source, status, contentType) {
  if (
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseStrictJson(source);
  if (status === 400) {
    if (
      !exact(document, ["schema", "ok", "error"]) ||
      document.schema !== schema ||
      document.ok !== false ||
      !exact(document.error, ["code"]) ||
      document.error.code !== "invalid_request"
    ) {
      fail();
    }
    return document;
  }
  if (status !== 200) fail();
  if (
    !exact(document, ["schema", "ok", "operation", "data"]) ||
    document.schema !== schema ||
    document.ok !== true ||
    document.operation !== checked.request.operation ||
    !exact(document.data, ["project_id", "users"]) ||
    !Array.isArray(document.data.users)
  ) {
    fail();
  }
  const projectId = document.data.project_id === null ? null : uuid(document.data.project_id);
  if (projectId !== checked.expectedProjectId) fail();
  const users = document.data.users.map(validateUserBrief);
  const seen = new Set();
  for (let index = 0; index < users.length; index += 1) {
    const userId = users[index].id;
    if (!checked.requestedIds.has(userId) || seen.has(userId)) fail();
    if (index > 0 && users[index - 1].id >= userId) fail();
    seen.add(userId);
  }
  document.data.project_id = projectId;
  document.data.users = users;
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
    checked: validateRequest(value.request),
  };
}

function postBriefs(origin, controlToken, request) {
  const endpointUrl = new URL(endpoint, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(request), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const outgoing = transport(
      endpointUrl,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(30_000),
        headers: {
          authorization: `Bearer ${controlToken}`,
          "content-type": "application/json",
          accept: "application/json",
          "content-length": body.length,
        },
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Invalid Identity briefs response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity briefs request failed")));
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity briefs request failed")));
    outgoing.end(body);
  });
}

async function callBriefs(value) {
  const { origin, controlToken, checked } = validateCallInput(value);
  const response = await postBriefs(origin, controlToken, checked.request);
  return validateBriefsReply(checked, response.text, response.status, response.contentType);
}

function expectFailure(callback) {
  try {
    callback();
  } catch {
    return;
  }
  fail();
}

function testRecord(id, fields = {}) {
  const value = {
    id,
    name: "Brief user",
    email: "brief@example.test",
    role: "historical-role",
    avatar_initial: "B",
    avatar_ref: null,
    ...fields,
  };
  value.avatar_initial = legacyInitial(value.name, value.email);
  return value;
}

function successReply(operation, projectId, users) {
  return JSON.stringify({
    schema,
    ok: true,
    operation,
    data: { project_id: projectId, users },
  });
}

function selfCheck() {
  let checks = 0;
  const base = "00000000-0000-4000-8000-000000000000";
  const ids = Array.from({ length: 501 }, (_, index) => {
    const tail = (index + 1).toString(16).padStart(12, "0");
    return `00000000-0000-4000-8000-${tail}`;
  });
  const globalRequest = validateRequest({
    schema,
    operation: "get_user_briefs",
    payload: { user_ids: [ids[0], ...ids, ids[0]] },
  });
  if (globalRequest.request.payload.user_ids.length !== 503) fail();
  checks += 1;

  const globalUsers = [
    testRecord(ids[0], { name: "ßeta", role: "legacy-annotator" }),
    testRecord(ids[1], { name: " \u3000\t", email: "fallback@example.test" }),
    testRecord(ids[2], { name: "", email: "email-only@example.test", role: null }),
    testRecord(ids[3], { name: "\u0301a", avatar_ref: "preset:pixel-12" }),
  ].sort((left, right) => left.id.localeCompare(right.id));
  if (globalUsers[0].avatar_initial !== "SS" || globalUsers[1].avatar_initial !== "?") fail();
  const globalReply = validateBriefsReply(
    globalRequest,
    successReply("get_user_briefs", null, globalUsers),
    200,
    "application/json",
  );
  if (globalReply.data.users.length !== 4) fail();
  checks += 1;

  const invalidRequestReply = validateBriefsReply(
    globalRequest,
    JSON.stringify({
      schema,
      ok: false,
      error: { code: "invalid_request" },
    }),
    400,
    "application/json",
  );
  if (invalidRequestReply.error.code !== "invalid_request") fail();
  checks += 1;
  expectFailure(() =>
    validateBriefsReply(
      globalRequest,
      JSON.stringify({
        schema,
        ok: false,
        error: { code: "invalid_request", detail: "unexpected" },
      }),
      400,
      "application/json",
    ),
  );
  checks += 1;

  const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const projectRequest = validateRequest({
    schema,
    operation: "get_project_user_briefs",
    payload: { project_id: projectId, user_ids: [ids[4], ids[5], ids[4]] },
  });
  const projectUsers = [
    testRecord(ids[4], { role: "reviewer" }),
    testRecord(ids[5], { role: "employee" }),
  ];
  const projectReply = validateBriefsReply(
    projectRequest,
    successReply("get_project_user_briefs", projectId, projectUsers),
    200,
    "application/json; charset=utf-8",
  );
  if (projectReply.data.project_id !== projectId) fail();
  checks += 1;

  const empty = validateRequest({
    schema,
    operation: "get_user_briefs",
    payload: { user_ids: [] },
  });
  if (
    validateBriefsReply(empty, successReply("get_user_briefs", null, []), 200, "application/json")
      .data.users.length !== 0
  ) {
    fail();
  }
  checks += 1;

  expectFailure(() => parseStrictJson('{"schema":"x","schema":"y"}'));
  checks += 1;
  expectFailure(() => parseStrictJson('{"value":NaN}'));
  checks += 1;
  expectFailure(() =>
    validateRequest({ protocol: schema, operation: "get_user_briefs", payload: { user_ids: [] } }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({
      schema,
      operation: "get_user_briefs",
      payload: { user_ids: [] },
      token: "must-not-be-a-user-credential",
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({ schema, operation: "get_user_briefs", payload: { user_ids: [null] } }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({ schema, operation: "get_user_briefs", payload: { user_ids: [1] } }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({
      schema,
      operation: "get_user_briefs",
      payload: { user_ids: [], extra: true },
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({
      schema,
      operation: "get_project_user_briefs",
      payload: { user_ids: [] },
    }),
  );
  checks += 1;

  const unsorted = successReply("get_user_briefs", null, [...globalUsers].reverse());
  expectFailure(() => validateBriefsReply(globalRequest, unsorted, 200, "application/json"));
  checks += 1;
  const duplicateRows = successReply("get_user_briefs", null, [globalUsers[0], globalUsers[0]]);
  expectFailure(() => validateBriefsReply(globalRequest, duplicateRows, 200, "application/json"));
  checks += 1;
  const extraField = successReply("get_user_briefs", null, [
    { ...globalUsers[0], password_hash: "forbidden" },
  ]);
  expectFailure(() => validateBriefsReply(globalRequest, extraField, 200, "application/json"));
  checks += 1;
  const wrongInitial = successReply("get_user_briefs", null, [
    { ...globalUsers[0], avatar_initial: "ß" },
  ]);
  expectFailure(() => validateBriefsReply(globalRequest, wrongInitial, 200, "application/json"));
  checks += 1;
  const wrongScope = successReply("get_project_user_briefs", base, projectUsers);
  expectFailure(() => validateBriefsReply(projectRequest, wrongScope, 200, "application/json"));
  checks += 1;
  const unrequested = successReply("get_user_briefs", null, [
    testRecord("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
  ]);
  expectFailure(() => validateBriefsReply(globalRequest, unrequested, 200, "application/json"));
  checks += 1;
  const wrongOperation = successReply("get_project_user_briefs", null, []);
  expectFailure(() => validateBriefsReply(globalRequest, wrongOperation, 200, "application/json"));
  checks += 1;

  return checks;
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
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
  const result = await callBriefs(input);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write("Identity briefs peer validation failed\n");
    process.exitCode = 1;
  });
}
