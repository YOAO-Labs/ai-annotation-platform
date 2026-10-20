import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  principalReceipt,
  uuid,
  validDateTime,
  validateInput,
} from "./identity_admission_peer.mjs";

const userSchema = "aap-identity-deactivation/v1";
const dueSchema = "aap-identity-deactivation-due/v1";
const issuer = "aap-identity/v1";
const userEndpoint = "/v1/identity/deactivation/execute";
const dueEndpoint = "/v1/identity/deactivation/process-due";
const authenticateEndpoint = "/v1/identity/authenticate";
const userOperations = new Set(["request_self_deactivation", "cancel_self_deactivation"]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const userFailureCodes = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
  "account_busy",
  "invariant_busy",
  "operation_conflict",
  "policy_rejected",
]);
const dueFailureCodes = new Set(["invalid_request", "invariant_busy", "operation_conflict"]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799;
const alreadyPending = "已有进行中的注销申请";
const noPending = "当前无待生效的注销申请";
const lastSuperAdmin = "您是最后一名超级管理员，请先指定继任者再申请注销";
const allOthersPending = "其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者";
const secretlessFailureCodes = new Set(userFailureCodes);

function fail() {
  throw new Error("Invalid Identity deactivation request or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, names) {
  return (
    isRecord(value) &&
    Object.keys(value).length === names.length &&
    names.every((name) => Object.hasOwn(value, name))
  );
}

function codePointLength(value) {
  return Array.from(value).length;
}

function boundedString(value, min, max) {
  return (
    typeof value === "string" && codePointLength(value) >= min && codePointLength(value) <= max
  );
}

function pathKey(path) {
  return JSON.stringify(path);
}

function scanJson(source) {
  let offset = 0;
  const spans = new Map();
  const skipWhitespace = () => {
    while (offset < source.length && /[\t\n\r ]/.test(source[offset])) offset += 1;
  };
  const readString = () => {
    if (source[offset] !== '"') fail();
    const start = offset;
    offset += 1;
    while (offset < source.length) {
      const char = source[offset];
      if (char === '"') {
        offset += 1;
        try {
          return JSON.parse(source.slice(start, offset));
        } catch {
          fail();
        }
      }
      if (char === "\\") {
        offset += 1;
        if (source[offset] === "u") offset += 5;
        else offset += 1;
      } else {
        offset += 1;
      }
    }
    fail();
  };
  const readValue = (path) => {
    skipWhitespace();
    const start = offset;
    const char = source[offset];
    if (char === "{") {
      offset += 1;
      skipWhitespace();
      const keys = new Set();
      if (source[offset] !== "}") {
        while (true) {
          skipWhitespace();
          const key = readString();
          if (keys.has(key)) fail();
          keys.add(key);
          skipWhitespace();
          if (source[offset] !== ":") fail();
          offset += 1;
          readValue([...path, key]);
          skipWhitespace();
          if (source[offset] === "}") break;
          if (source[offset] !== ",") fail();
          offset += 1;
        }
      }
      offset += 1;
    } else if (char === "[") {
      offset += 1;
      skipWhitespace();
      let index = 0;
      if (source[offset] !== "]") {
        while (true) {
          readValue([...path, index]);
          index += 1;
          skipWhitespace();
          if (source[offset] === "]") break;
          if (source[offset] !== ",") fail();
          offset += 1;
        }
      }
      offset += 1;
    } else if (char === '"') {
      readString();
    } else {
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
      if (offset === start) fail();
    }
    spans.set(pathKey(path), { start, end: offset });
  };

  readValue([]);
  skipWhitespace();
  if (offset !== source.length) fail();
  return spans;
}

function getPath(value, path) {
  let current = value;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = current[key];
  }
  return current;
}

function setPath(value, path, replacement) {
  let parent = value;
  for (const key of path.slice(0, -1)) parent = parent[key];
  parent[path.at(-1)] = replacement;
}

function strictInteger(source, spans, document, path, { min = 0n, max }) {
  const value = getPath(document, path);
  const span = spans.get(pathKey(path));
  if (value === null) return null;
  if (typeof value !== "number" || !span) fail();
  const raw = source.slice(span.start, span.end);
  if (!/^(0|[1-9][0-9]*)$/.test(raw)) fail();
  const integer = BigInt(raw);
  if (integer < min || integer > max) fail();
  setPath(document, path, integer);
  return integer;
}

function parseStrictJson(source, integerSpecs = []) {
  if (typeof source !== "string") fail();
  const spans = scanJson(source);
  let document;
  try {
    document = JSON.parse(source);
  } catch {
    fail();
  }
  for (const spec of integerSpecs) {
    if (spans.has(pathKey(spec.path))) strictInteger(source, spans, document, spec.path, spec);
  }
  return { document, spans };
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const limits = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const [key, max] of Object.entries(limits)) {
    if (value[key] !== null && !boundedString(value[key], 0, max)) fail();
  }
  if (value.ip !== null && isIP(value.ip) === 0) fail();
}

function pythonStrip(value) {
  return value.replace(
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu,
    "",
  );
}

export function validateDeactivationRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== userSchema ||
    !userOperations.has(value.operation)
  ) {
    fail();
  }
  value.operation_id = uuid(value.operation_id);
  if (typeof value.token !== "string" || value.token.length < 1 || value.token.length > 16_384) {
    fail();
  }
  validateAuditContext(value.audit_context);
  if (value.operation === "request_self_deactivation") {
    if (
      !exact(value.payload, ["reason"]) ||
      (value.payload.reason !== null && !boundedString(value.payload.reason, 0, 500))
    ) {
      fail();
    }
  } else if (!exact(value.payload, [])) {
    fail();
  }
  return { request: value, operationId: value.operation_id };
}

export function validateDueRequest(value) {
  if (
    !exact(value, ["schema", "operation_id", "limit"]) ||
    value.schema !== dueSchema ||
    typeof value.limit !== "bigint" ||
    value.limit < 1n ||
    value.limit > 500n
  ) {
    fail();
  }
  value.operation_id = uuid(value.operation_id);
  return { request: value, operationId: value.operation_id };
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    fail();
  }
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) fail();
  return bytes;
}

function decodeJwtJson(segment, integerSpecs = []) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let source;
  try {
    source = decoder.decode(decodeBase64Url(segment));
  } catch {
    fail();
  }
  return parseStrictJson(source, integerSpecs).document;
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || decodeBase64Url(parts[2]).length !== 32) fail();
  const header = decodeJwtJson(parts[0]);
  if (!exact(header, ["alg", "typ"]) || header.alg !== "HS256" || header.typ !== "JWT") fail();
  const claims = decodeJwtJson(parts[1], [{ path: ["gen"], min: 0n, max: maxInt64 }]);
  if (
    !isRecord(claims) ||
    typeof claims.exp !== "number" ||
    !Number.isSafeInteger(claims.exp) ||
    claims.exp < 1 ||
    claims.exp > maxPythonEpochSeconds ||
    claims.iss !== issuer
  ) {
    fail();
  }
  const generation = claims.gen === undefined ? 0n : claims.gen;
  if (typeof generation !== "bigint") fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = new Date(claims.exp * 1000);
  if (!Number.isFinite(expiry.valueOf()) || !validDateTime(expiry.toISOString())) fail();
  return { actorId, credentialId, generation, expiry: claims.exp };
}

function validateAccountRecord(value) {
  const fields = [
    "id",
    "email",
    "name",
    "role",
    "group_name",
    "group_id",
    "status",
    "is_active",
    "disabled_kind",
    "disabled_at",
    "disabled_by",
    "disabled_reason",
    "email_verified_at",
    "last_login_at",
    "last_seen_at",
    "password_admin_reset_at",
    "deactivation_requested_at",
    "deactivation_scheduled_at",
    "preferences",
    "avatar_ref",
    "created_at",
  ];
  if (!exact(value, fields)) fail();
  const accountId = uuid(value.id);
  if (
    !boundedString(value.email, 3, 255) ||
    !boundedString(value.name, 1, 100) ||
    !roles.has(value.role) ||
    (value.group_name !== null && !boundedString(value.group_name, 0, 255)) ||
    (value.group_id !== null && typeof value.group_id !== "string") ||
    typeof value.status !== "string" ||
    typeof value.is_active !== "boolean" ||
    (value.disabled_kind !== null && !boundedString(value.disabled_kind, 0, 32)) ||
    (value.disabled_reason !== null && !boundedString(value.disabled_reason, 0, 500)) ||
    !isRecord(value.preferences) ||
    (value.avatar_ref !== null && !boundedString(value.avatar_ref, 0, 80)) ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  if (value.group_id !== null) uuid(value.group_id);
  if (value.disabled_by !== null) uuid(value.disabled_by);
  for (const field of [
    "disabled_at",
    "email_verified_at",
    "last_login_at",
    "last_seen_at",
    "password_admin_reset_at",
    "deactivation_requested_at",
    "deactivation_scheduled_at",
  ]) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  value.id = accountId;
  return value;
}

function bindPrincipal(raw, receipt, claims, expectedApiKey) {
  if (claims !== null) {
    if (
      receipt.actor_id !== claims.actorId ||
      receipt.credential_type !== "jwt" ||
      receipt.credential_id !== claims.credentialId ||
      receipt.generation_decimal !== claims.generation.toString(10) ||
      !validDateTime(raw.expires_at) ||
      Date.parse(raw.expires_at) !== claims.expiry * 1000
    ) {
      fail();
    }
    return;
  }
  if (
    expectedApiKey === null ||
    receipt.actor_id !== expectedApiKey.actor_id ||
    receipt.credential_type !== "api_key" ||
    receipt.credential_id !== expectedApiKey.credential_id
  ) {
    fail();
  }
}

function validateUserFailure(request, error) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    !secretlessFailureCodes.has(error.code)
  ) {
    fail();
  }
  if (
    typeof error.detail === "string" &&
    request.request.token.length >= 8 &&
    error.detail.includes(request.request.token)
  ) {
    fail();
  }
  if (error.code !== "policy_rejected") {
    if (error.status_code !== null || error.detail !== null) fail();
    return error;
  }
  if (typeof error.status_code !== "bigint" || typeof error.detail !== "string") fail();
  if (error.status_code !== 400n) fail();
  if (
    request.request.operation === "request_self_deactivation" &&
    [alreadyPending, lastSuperAdmin, allOthersPending].includes(error.detail)
  ) {
    return error;
  }
  if (request.request.operation === "cancel_self_deactivation" && error.detail === noPending) {
    return error;
  }
  fail();
}

function decodeUserResponse(request, source, status, contentType, expectedApiKey = null) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const parsed = parseStrictJson(source, [
    { path: ["principal", "generation"], min: 0n, max: maxInt64 },
    { path: ["error", "status_code"], min: 100n, max: 599n },
  ]).document;
  if (!isRecord(parsed) || parsed.schema !== userSchema || typeof parsed.ok !== "boolean") fail();
  if (!parsed.ok) {
    if (!exact(parsed, ["schema", "ok", "error"])) fail();
    return {
      schema: userSchema,
      ok: false,
      error: validateUserFailure(request, parsed.error),
    };
  }
  if (
    !exact(parsed, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    parsed.operation !== request.request.operation ||
    uuid(parsed.operation_id) !== request.operationId ||
    !exact(parsed.data, ["account"])
  ) {
    fail();
  }
  const claims = request.request.token.startsWith("ak_")
    ? null
    : incomingJwtClaims(request.request.token);
  const principal = principalReceipt(parsed.principal);
  bindPrincipal(parsed.principal, principal, claims, expectedApiKey);
  const account = validateAccountRecord(parsed.data.account);
  if (account.id !== principal.actor_id || !account.is_active) fail();
  if (request.request.operation === "request_self_deactivation") {
    if (
      account.deactivation_requested_at === null ||
      account.deactivation_scheduled_at === null ||
      Date.parse(account.deactivation_scheduled_at) -
        Date.parse(account.deactivation_requested_at) !==
        7 * 24 * 60 * 60 * 1000
    ) {
      fail();
    }
    const reason = request.request.payload.reason;
    if (
      reason !== null &&
      pythonStrip(reason) !== "" &&
      account.disabled_reason === pythonStrip(reason)
    ) {
      fail();
    }
  } else if (
    account.deactivation_requested_at !== null ||
    account.deactivation_scheduled_at !== null
  ) {
    fail();
  }
  return {
    schema: userSchema,
    ok: true,
    operation: parsed.operation,
    operation_id: request.operationId,
    principal,
    data: { account },
  };
}

function decodeDueResponse(request, source, status, contentType) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseStrictJson(source, [
    { path: ["processed"], min: 0n, max: maxInt64 },
    { path: ["examined"], min: 0n, max: maxInt64 },
    { path: ["blocked_last_super_admin"], min: 0n, max: maxInt64 },
  ]).document;
  if (!isRecord(document) || document.schema !== dueSchema || typeof document.ok !== "boolean")
    fail();
  if (!document.ok) {
    if (!exact(document, ["schema", "ok", "error"]) || !exact(document.error, ["code"])) fail();
    if (typeof document.error.code !== "string" || !dueFailureCodes.has(document.error.code))
      fail();
    return { schema: dueSchema, ok: false, error: { code: document.error.code } };
  }
  if (
    !exact(document, [
      "schema",
      "ok",
      "operation_id",
      "processed",
      "examined",
      "blocked_last_super_admin",
      "has_more",
    ]) ||
    uuid(document.operation_id) !== request.operationId ||
    typeof document.processed !== "bigint" ||
    typeof document.examined !== "bigint" ||
    typeof document.blocked_last_super_admin !== "bigint" ||
    typeof document.has_more !== "boolean" ||
    document.processed + document.blocked_last_super_admin !== document.examined ||
    document.examined > request.request.limit
  ) {
    fail();
  }
  return {
    schema: dueSchema,
    ok: true,
    operation_id: request.operationId,
    processed: document.processed,
    examined: document.examined,
    blocked_last_super_admin: document.blocked_last_super_admin,
    has_more: document.has_more,
  };
}

function validatePeerInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  if (!isRecord(value.request)) fail();
  const due = value.request.schema === dueSchema;
  const request = due
    ? validateDueRequest(value.request)
    : validateDeactivationRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: due ? "deactivation-due" : request.request.token,
  });
  return { request, due, origin: checked.origin, controlToken: checked.controlToken };
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 128 * 1024) fail();
    chunks.push(chunk);
  }
  let source;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail();
  }
  const parsed = parseStrictJson(source, [{ path: ["request", "limit"], min: 1n, max: 500n }]);
  return parsed.document;
}

function postDeactivationRequest(origin, controlToken, path, payload) {
  const endpoint = new URL(path, origin);
  const transport = endpoint.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(stringifyLossless(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
      endpoint,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(60_000),
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
            const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text,
            });
          } catch {
            rejectResponse(new Error("Invalid Identity deactivation response"));
          }
        });
        response.on("error", () =>
          rejectResponse(new Error("Identity deactivation request failed")),
        );
      },
    );
    request.on("error", () => rejectResponse(new Error("Identity deactivation request failed")));
    request.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  if (!/^ak_[A-Za-z0-9_-]{32}$/.test(token)) fail();
  const response = await postIdentityRequest(origin, controlToken, authenticateEndpoint, { token });
  parseStrictJson(response.text, [{ path: ["data", "generation"], min: 0n, max: maxInt64 }]);
  const result = parseIdentityReceipt(response.text, response.status, response.contentType);
  return result.ok ? result.principal : null;
}

async function main() {
  const input = await readInput();
  const { request, due, origin, controlToken } = validatePeerInput(input);
  if (due) {
    const response = await postDeactivationRequest(
      origin,
      controlToken,
      dueEndpoint,
      request.request,
    );
    const result = decodeDueResponse(request, response.text, response.status, response.contentType);
    process.stdout.write(`${stringifyLossless(result)}\n`);
    return;
  }
  const expectedApiKey = request.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(origin, controlToken, request.request.token)
    : null;
  const response = await postDeactivationRequest(
    origin,
    controlToken,
    userEndpoint,
    request.request,
  );
  const result = decodeUserResponse(
    request,
    response.text,
    response.status,
    response.contentType,
    expectedApiKey,
  );
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

function makeAuditContext() {
  return { method: "POST", path: "/api/v1/me/deactivation", ip: "127.0.0.1", request_id: null };
}

function makeTokenFromClaimsJson(claimsJson) {
  const input =
    Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url") +
    "." +
    Buffer.from(claimsJson).toString("base64url");
  return `${input}.${Buffer.alloc(32, 9).toString("base64url")}`;
}

function makeToken(actorId, credentialId, generation = 0n, role = "super_admin") {
  return makeTokenFromClaimsJson(
    stringifyLossless({
      sub: actorId,
      role,
      exp: 1_893_456_000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
    }),
  );
}

function makeUserRequest(operation, operationId, token, payload) {
  return validateDeactivationRequest({
    schema: userSchema,
    operation,
    operation_id: operationId,
    token,
    audit_context: makeAuditContext(),
    payload,
  });
}

function makeDueRequest(operationId, limit) {
  return validateDueRequest({ schema: dueSchema, operation_id: operationId, limit });
}

function testAccount(id, overrides = {}) {
  return {
    id,
    email: "member@example.test",
    name: "Member",
    role: "super_admin",
    group_name: null,
    group_id: null,
    status: "online",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    email_verified_at: null,
    last_login_at: null,
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: "2029-12-25T00:00:00Z",
    deactivation_scheduled_at: "2030-01-01T00:00:00Z",
    preferences: {},
    avatar_ref: null,
    created_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

function testPrincipal(actorId, credentialId, generation = 0n, role = "super_admin") {
  return {
    actor_id: actorId,
    name: "Current account name",
    email: "member@example.test",
    role,
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testUserReply(request, data, principal) {
  return stringifyLossless({
    schema: userSchema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    principal,
    data,
  });
}

function expectFailure(callback) {
  let rejected = false;
  try {
    callback();
  } catch {
    rejected = true;
  }
  if (!rejected) fail();
}

function selfCheck() {
  let checks = 0;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const operationId = "33333333-3333-4333-8333-333333333333";
  const anotherOperationId = "44444444-4444-4444-8444-444444444444";
  const token = makeToken(actorId, credentialId);
  const principal = testPrincipal(actorId, credentialId);
  const account = testAccount(actorId);
  const cancelledAccount = testAccount(actorId, {
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
  });

  for (const [operation, payload] of [
    ["request_self_deactivation", { reason: "  family reasons  " }],
    ["cancel_self_deactivation", {}],
  ]) {
    const request = makeUserRequest(operation, operationId, token, payload);
    const result = decodeUserResponse(
      request,
      testUserReply(
        request,
        { account: operation === "request_self_deactivation" ? account : cancelledAccount },
        principal,
      ),
      200,
      "application/json",
    );
    if (!result.ok || result.data.account.id !== actorId) fail();
    checks += 1;
  }

  const scheduleRequest = makeUserRequest("request_self_deactivation", operationId, token, {
    reason: null,
  });
  expectFailure(() =>
    decodeUserResponse(
      scheduleRequest,
      testUserReply(
        scheduleRequest,
        {
          account: testAccount(actorId, {
            deactivation_scheduled_at: "2030-01-02T00:00:00Z",
          }),
        },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;
  const cancelWithPending = makeUserRequest("cancel_self_deactivation", operationId, token, {});
  expectFailure(() =>
    decodeUserResponse(
      cancelWithPending,
      testUserReply(cancelWithPending, { account }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;
  const reasonMatchesProfile = makeUserRequest("request_self_deactivation", operationId, token, {
    reason: account.name,
  });
  if (
    !decodeUserResponse(
      reasonMatchesProfile,
      testUserReply(reasonMatchesProfile, { account }, principal),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;

  const requested = makeUserRequest("request_self_deactivation", operationId, token, {
    reason: "Do not return this reason",
  });
  const accountWithReason = testAccount(actorId, {
    disabled_reason: "Do not return this reason",
  });
  expectFailure(() =>
    decodeUserResponse(
      requested,
      testUserReply(requested, { account: accountWithReason }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const wrongActor = testPrincipal(anotherOperationId, credentialId);
  expectFailure(() =>
    decodeUserResponse(
      requested,
      testUserReply(requested, { account }, wrongActor),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeUserResponse(
      requested,
      testUserReply(requested, { account: { ...account, reason: "extra" } }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeUserResponse(
      requested,
      testUserReply(requested, { account }, principal).replace(
        requested.operationId,
        anotherOperationId,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const busyFailure = stringifyLossless({
    schema: userSchema,
    ok: false,
    error: { code: "account_busy", status_code: null, detail: null },
  });
  const busyRequest = makeUserRequest("cancel_self_deactivation", operationId, token, {});
  if (decodeUserResponse(busyRequest, busyFailure, 200, "application/json").ok) fail();
  checks += 1;
  expectFailure(() =>
    decodeUserResponse(
      busyRequest,
      JSON.stringify({
        schema: userSchema,
        ok: false,
        error: { code: "account_busy", status_code: null, detail: token },
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const policyCases = [
    ["request_self_deactivation", "已有进行中的注销申请"],
    ["request_self_deactivation", lastSuperAdmin],
    ["request_self_deactivation", allOthersPending],
    ["cancel_self_deactivation", "当前无待生效的注销申请"],
  ];
  for (const [operation, detail] of policyCases) {
    const request = makeUserRequest(
      operation,
      operationId,
      token,
      operation === "request_self_deactivation" ? { reason: null } : {},
    );
    const error = parseStrictJson(
      stringifyLossless({
        schema: userSchema,
        ok: false,
        error: { code: "policy_rejected", status_code: 400n, detail },
      }),
      [{ path: ["error", "status_code"], min: 1n, max: 999n }],
    ).document;
    const result = decodeUserResponse(request, stringifyLossless(error), 200, "application/json");
    if (result.ok || result.error.detail !== detail) fail();
    checks += 1;
  }
  expectFailure(() => {
    const request = makeUserRequest("cancel_self_deactivation", operationId, token, {});
    decodeUserResponse(
      request,
      '{"schema":"aap-identity-deactivation/v1","ok":false,"error":{"code":"policy_rejected","status_code":400,"detail":"已有进行中的注销申请"}}',
      200,
      "application/json",
    );
  });
  checks += 1;

  const wideGeneration = maxInt64;
  const wideToken = makeToken(actorId, credentialId, wideGeneration, "reviewer");
  const widePrincipal = testPrincipal(actorId, credentialId, wideGeneration, "super_admin");
  const legacyRequest = makeUserRequest("cancel_self_deactivation", operationId, wideToken, {});
  const legacyResult = decodeUserResponse(
    legacyRequest,
    testUserReply(legacyRequest, { account: cancelledAccount }, widePrincipal),
    200,
    "application/json",
  );
  if (legacyResult.principal.generation_decimal !== maxInt64.toString(10)) fail();
  checks += 1;

  for (const legacyRole of ["annotator", "reviewer"]) {
    const legacyToken = makeTokenFromClaimsJson(
      JSON.stringify({
        sub: actorId,
        role: legacyRole,
        exp: 1_893_456_000,
        jti: credentialId,
        iss: issuer,
        extra_historical_claim: true,
      }),
    );
    const request = makeUserRequest("cancel_self_deactivation", operationId, legacyToken, {});
    const result = decodeUserResponse(
      request,
      testUserReply(request, { account: cancelledAccount }, principal),
      200,
      "application/json",
    );
    if (result.principal.generation_decimal !== "0") fail();
    checks += 1;
  }

  const integralFloatExpiry = makeTokenFromClaimsJson(
    `{"sub":"${actorId}","role":"reviewer","exp":1893456000.0,"jti":"${credentialId}","iss":"${issuer}","gen":0}`,
  );
  const integralFloatRequest = makeUserRequest(
    "cancel_self_deactivation",
    operationId,
    integralFloatExpiry,
    {},
  );
  decodeUserResponse(
    integralFloatRequest,
    testUserReply(integralFloatRequest, { account: cancelledAccount }, principal),
    200,
    "application/json",
  );
  checks += 1;
  const floatGeneration = makeTokenFromClaimsJson(
    `{"sub":"${actorId}","role":"annotator","exp":1893456000,"jti":"${credentialId}","iss":"${issuer}","gen":0.0}`,
  );
  const floatGenerationRequest = makeUserRequest(
    "cancel_self_deactivation",
    operationId,
    floatGeneration,
    {},
  );
  expectFailure(() =>
    decodeUserResponse(
      floatGenerationRequest,
      testUserReply(floatGenerationRequest, { account }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const due = makeDueRequest(operationId, 256n);
  const dueReply = stringifyLossless({
    schema: dueSchema,
    ok: true,
    operation_id: operationId,
    processed: 255n,
    examined: 256n,
    blocked_last_super_admin: 1n,
    has_more: true,
  });
  const dueResult = decodeDueResponse(due, dueReply, 200, "application/json");
  if (dueResult.processed + dueResult.blocked_last_super_admin !== dueResult.examined) fail();
  checks += 1;
  expectFailure(() =>
    decodeDueResponse(
      due,
      '{"schema":"aap-identity-deactivation-due/v1","ok":true,"operation_id":"33333333-3333-4333-8333-333333333333","processed":255.0,"examined":256,"blocked_last_super_admin":1,"has_more":true}',
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeDueResponse(
      due,
      dueReply.replace('"has_more":true', '"has_more":true,"email":"private"'),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeDueResponse(
      due,
      dueReply.replace('"has_more":true', '"has_more":1'),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeDueResponse(
      due,
      stringifyLossless({
        schema: dueSchema,
        ok: true,
        operation_id: operationId,
        processed: 255n,
        examined: 257n,
        blocked_last_super_admin: 1n,
        has_more: true,
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    validateDueRequest({ schema: dueSchema, operation_id: operationId, limit: 1 }),
  );
  checks += 1;
  expectFailure(() =>
    parseStrictJson('{"request":{"limit":1.0}}', [
      { path: ["request", "limit"], min: 1n, max: 500n },
    ]),
  );
  checks += 1;

  expectFailure(() => parseStrictJson('{"schema":"x","schema":"y"}'));
  checks += 1;
  const duplicateDueResponse =
    '{"schema":"aap-identity-deactivation-due/v1","ok":true,"operation_id":"33333333-3333-4333-8333-333333333333","processed":0,"examined":0,"blocked_last_super_admin":0,"has_more":false,"has_more":false}';
  expectFailure(() => decodeDueResponse(due, duplicateDueResponse, 200, "application/json"));
  checks += 1;
  expectFailure(() =>
    decodeUserResponse(
      requested,
      testUserReply(requested, { account }, principal).replace('"ok":true', '"ok":true,"ok":true'),
      200,
      "application/json",
    ),
  );
  checks += 1;

  return { ok: true, checks };
}

function isDirectExecution() {
  return (
    process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])
  );
}

if (isDirectExecution()) {
  if (process.argv[2] === "--self-check") {
    try {
      process.stdout.write(`${JSON.stringify(selfCheck())}\n`);
    } catch {
      process.stderr.write("Identity deactivation peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity deactivation peer failed\n");
      process.exitCode = 1;
    });
  }
}
