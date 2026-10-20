import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  uuid,
  validDateTime,
  validateInput,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-suspensions/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/suspensions/execute";
const authEndpoint = "/v1/identity/authenticate";
const operations = new Set(["suspend_account", "reactivate_account"]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const managerRoles = new Set(["super_admin", "project_admin"]);
const authFailures = new Set([
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);
const businessFailures = new Set([
  "invalid_request",
  "manager_required",
  "target_not_found",
  "self_suspend",
  "self_reactivate",
  "suspend_role_forbidden",
  "reactivate_role_forbidden",
  "reactivate_not_managed",
  "suspend_scope_forbidden",
  "reactivate_scope_forbidden",
  "last_super_admin",
  "already_active",
  "not_reactivatable",
  "account_busy",
  "project_busy",
  "operation_conflict",
  "unavailable",
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const userFields = [
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
const nullableDateFields = [
  "disabled_at",
  "email_verified_at",
  "last_login_at",
  "last_seen_at",
  "password_admin_reset_at",
  "deactivation_requested_at",
  "deactivation_scheduled_at",
];
const jwtHeader = { alg: "HS256", typ: "JWT" };

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity suspension request or response");
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

function int64(value) {
  if (!(value instanceof JsonNumber) || !/^(?:0|[1-9][0-9]*)$/.test(value.raw)) fail();
  const number = BigInt(value.raw);
  if (number > maxInt64) fail();
  return number;
}

function nullableString(value, max = Number.MAX_SAFE_INTEGER) {
  return value === null || (typeof value === "string" && Array.from(value).length <= max);
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  if (
    !nullableString(value.method, 8) ||
    !nullableString(value.path, 256) ||
    !nullableString(value.ip, 64) ||
    !nullableString(value.request_id, 36)
  ) {
    fail();
  }
}

function validateRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (typeof value.token !== "string" || value.token.length < 1 || value.token.length > 16_384)
    fail();
  validateAuditContext(value.audit_context);
  const targetId = uuid(value.payload?.target_user_id);
  if (value.operation === "suspend_account") {
    if (!exact(value.payload, ["target_user_id"])) fail();
  } else if (
    !exact(value.payload, ["target_user_id", "reason"]) ||
    !nullableString(value.payload.reason, 500)
  ) {
    fail();
  }
  return {
    request: {
      ...value,
      operation_id: operationId,
      payload: { ...value.payload, target_user_id: targetId },
    },
    operationId,
    targetId,
  };
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1)
    fail();
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) fail();
  return bytes;
}

function decodeTokenJson(segment) {
  try {
    return parseStrictJson(
      new TextDecoder("utf-8", { fatal: true }).decode(decodeBase64Url(segment)),
    );
  } catch {
    fail();
  }
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || decodeBase64Url(parts[2]).length !== 32) fail();
  const header = decodeTokenJson(parts[0]);
  if (
    !isRecord(header) ||
    header.alg !== "HS256" ||
    (header.typ !== undefined && header.typ !== "JWT")
  ) {
    fail();
  }
  const claims = decodeTokenJson(parts[1]);
  if (!isRecord(claims) || claims.iss !== issuer) fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = int64(claims.exp);
  if (expiry < 1n || expiry > maxPythonEpochSeconds) fail();
  const generation = claims.gen === undefined ? 0n : int64(claims.gen);
  return { actorId, credentialId, expiry, generation };
}

function validatePrincipal(value) {
  if (
    !exact(value, [
      "actor_id",
      "name",
      "email",
      "role",
      "is_active",
      "credential_type",
      "credential_id",
      "generation",
      "scopes",
      "expires_at",
    ]) ||
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    !roles.has(value.role) ||
    value.is_active !== true ||
    !["jwt", "api_key"].includes(value.credential_type)
  ) {
    fail();
  }
  value.actor_id = uuid(value.actor_id);
  value.credential_id = uuid(value.credential_id);
  if (value.credential_type === "jwt") {
    value.generation = int64(value.generation);
    if (value.scopes !== null || !validDateTime(value.expires_at)) fail();
  } else if (
    value.generation !== null ||
    !Array.isArray(value.scopes) ||
    value.scopes.some((scope) => typeof scope !== "string") ||
    (value.expires_at !== null && !validDateTime(value.expires_at))
  ) {
    fail();
  }
  if (!managerRoles.has(value.role)) fail();
  return value;
}

function bindPrincipal(principal, request, expectedApiPrincipal) {
  if (request.token.startsWith("ak_")) {
    if (
      !expectedApiPrincipal ||
      principal.actor_id !== expectedApiPrincipal.actor_id ||
      principal.credential_type !== "api_key" ||
      principal.credential_id !== expectedApiPrincipal.credential_id ||
      principal.generation !== null
    ) {
      fail();
    }
    return;
  }
  const claims = incomingJwtClaims(request.token);
  if (
    principal.actor_id !== claims.actorId ||
    principal.credential_type !== "jwt" ||
    principal.credential_id !== claims.credentialId ||
    principal.generation !== claims.generation ||
    BigInt(Math.floor(Date.parse(principal.expires_at) / 1000)) !== claims.expiry
  ) {
    fail();
  }
}

function validateUser(value) {
  if (!exact(value, userFields)) fail();
  value.id = uuid(value.id);
  if (value.group_id !== null && typeof value.group_id !== "string") {
    fail();
  }
  if (value.group_id !== null) value.group_id = uuid(value.group_id);
  if (
    typeof value.email !== "string" ||
    typeof value.name !== "string" ||
    typeof value.role !== "string" ||
    !nullableString(value.group_name) ||
    typeof value.status !== "string" ||
    typeof value.is_active !== "boolean" ||
    !nullableString(value.disabled_kind) ||
    !nullableString(value.disabled_reason) ||
    !isRecord(value.preferences) ||
    !nullableString(value.avatar_ref) ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  if (value.disabled_by !== null) value.disabled_by = uuid(value.disabled_by);
  for (const field of nullableDateFields) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  return value;
}

const failureDetails = {
  manager_required: [403, "当前账号已无管理权限"],
  target_not_found: [404, "用户不存在"],
  self_suspend: [400, "不能停用自己"],
  self_reactivate: [400, "不能恢复自己"],
  suspend_role_forbidden: [403, "项目管理员仅能停用标注员/审核员账号"],
  reactivate_role_forbidden: [403, "项目管理员不能恢复该角色"],
  reactivate_not_managed: [403, "该用户不在你管理的项目内"],
  suspend_scope_forbidden: [403, "该用户跨多个项目，须由超级管理员处理"],
  reactivate_scope_forbidden: [403, "该用户存在不在你管理范围内的项目"],
  last_super_admin: [400, "不能停用最后一名超级管理员"],
  already_active: [409, "账号当前已启用"],
  not_reactivatable: [409, "该停用状态不可恢复"],
  account_busy: [409, "账号正在更新，请刷新后重试"],
  operation_conflict: [409, null],
  unavailable: [503, null],
};

function validateFailure(request, error, actorId) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    (!authFailures.has(error.code) && !businessFailures.has(error.code))
  ) {
    fail();
  }
  if (authFailures.has(error.code)) {
    if (error.status_code !== null || error.detail !== null) fail();
    return error;
  }
  if (error.code === "invalid_request") {
    if (int64(error.status_code) !== 400n || error.detail !== null) fail();
    return error;
  }
  if (error.code === "project_busy") {
    if (
      request.operation !== "suspend_account" ||
      actorId === null ||
      request.payload.target_user_id === actorId ||
      int64(error.status_code) !== 409n ||
      !exact(error.detail, ["reason"]) ||
      error.detail.reason !== "project_busy"
    ) {
      fail();
    }
    return error;
  }
  const expected = failureDetails[error.code];
  if (!expected || int64(error.status_code) !== BigInt(expected[0]) || error.detail !== expected[1])
    fail();
  if (
    (error.code === "self_suspend" &&
      (request.operation !== "suspend_account" ||
        actorId === null ||
        actorId !== request.payload.target_user_id)) ||
    (error.code === "self_reactivate" &&
      (request.operation !== "reactivate_account" ||
        actorId === null ||
        actorId !== request.payload.target_user_id)) ||
    (error.code === "target_not_found" &&
      actorId !== null &&
      request.payload.target_user_id === actorId) ||
    (["suspend_role_forbidden", "suspend_scope_forbidden", "last_super_admin"].includes(
      error.code,
    ) &&
      (request.operation !== "suspend_account" ||
        actorId === null ||
        request.payload.target_user_id === actorId)) ||
    ([
      "reactivate_role_forbidden",
      "reactivate_not_managed",
      "reactivate_scope_forbidden",
      "already_active",
      "not_reactivatable",
    ].includes(error.code) &&
      (request.operation !== "reactivate_account" ||
        actorId === null ||
        request.payload.target_user_id === actorId))
  ) {
    fail();
  }
  return error;
}

function decodeReply(checked, source, status, contentType, expectedApiPrincipal = null) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseStrictJson(source);
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") fail();
  const request = checked.request;
  if (!document.ok) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    let actorId = null;
    if (request.token.startsWith("ak_")) {
      actorId = expectedApiPrincipal?.actor_id ?? null;
    } else {
      actorId = incomingJwtClaims(request.token).actorId;
    }
    return {
      schema,
      ok: false,
      operation: request.operation,
      operation_id: checked.operationId,
      error: validateFailure(request, document.error, actorId),
    };
  }
  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.operation ||
    uuid(document.operation_id) !== checked.operationId ||
    !exact(document.data, ["user"])
  ) {
    fail();
  }
  document.operation_id = uuid(document.operation_id);
  const principal = validatePrincipal(document.principal);
  bindPrincipal(principal, request, expectedApiPrincipal);
  const user = validateUser(document.data.user);
  if (
    user.id !== checked.targetId ||
    user.id === principal.actor_id ||
    user.is_active !== (request.operation === "reactivate_account")
  ) {
    fail();
  }
  if (request.operation === "reactivate_account") {
    if (
      user.status !== "offline" ||
      user.disabled_kind !== null ||
      user.disabled_at !== null ||
      user.disabled_by !== null ||
      user.disabled_reason !== null
    ) {
      fail();
    }
  }
  return document;
}

function validatePublicInput(value) {
  if (
    !exact(value, ["mode", "status", "content_type", "body", "target_user_id"]) ||
    !["suspend", "reactivate"].includes(value.mode)
  ) {
    fail();
  }
  const status = int64(value.status);
  const expectedActive = value.mode === "reactivate";
  if (
    status !== 200n ||
    typeof value.content_type !== "string" ||
    value.content_type.split(";", 1)[0].trim().toLowerCase() !== "application/json" ||
    typeof value.body !== "string"
  ) {
    fail();
  }
  const user = validateUser(parseStrictJson(value.body));
  if (user.id !== uuid(value.target_user_id) || user.is_active !== expectedActive) fail();
  if (
    value.mode === "reactivate" &&
    (user.status !== "offline" ||
      user.disabled_kind !== null ||
      user.disabled_at !== null ||
      user.disabled_by !== null ||
      user.disabled_reason !== null)
  ) {
    fail();
  }
  return user;
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

function postJson(origin, controlToken, path, payload) {
  const endpointUrl = new URL(path, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(stringifyLossless(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const outgoing = transport(
      endpointUrl,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(90_000),
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
            rejectResponse(new Error("Invalid Identity suspension response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity request failed")));
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity request failed")));
    outgoing.end(body);
  });
}

function postSuspensionRequest(origin, controlToken, payload) {
  return postJson(origin, controlToken, endpoint, payload);
}

async function apiKeyPrincipal(origin, controlToken, token) {
  const response = await postJson(origin, controlToken, authEndpoint, { token });
  parseStrictJson(response.text);
  const admission = parseIdentityReceipt(response.text, response.status, response.contentType);
  return admission.ok && admission.principal.credential_type === "api_key"
    ? admission.principal
    : null;
}

function testAuditContext() {
  return {
    method: "POST",
    path: "/api/v1/users/33333333-3333-4333-8333-333333333333/reactivate",
    ip: "127.0.0.1",
    request_id: "44444444-4444-4444-8444-444444444444",
  };
}

function testToken(actorId, credentialId, generation = 0n, role = "super_admin") {
  const claims = stringifyLossless({
    sub: actorId,
    role,
    exp: 1893456000n,
    jti: credentialId,
    gen: generation,
    iss: issuer,
    extension: "legacy",
  });
  return `${Buffer.from(JSON.stringify(jwtHeader)).toString("base64url")}.${Buffer.from(claims).toString("base64url")}.${Buffer.alloc(32, 5).toString("base64url")}`;
}

function testUser(id, overrides = {}) {
  return {
    id,
    email: "member@example.test",
    name: "Member",
    role: "annotator",
    group_name: null,
    group_id: null,
    status: "offline",
    is_active: false,
    disabled_kind: "suspended",
    disabled_at: "2030-01-01T00:00:00Z",
    disabled_by: "11111111-1111-4111-8111-111111111111",
    disabled_reason: "管理员停用账号",
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
    name: "Current administrator",
    email: "admin@example.test",
    role,
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testRequest(operation, operationId, token, targetId, reason = null) {
  return validateRequest({
    schema,
    operation,
    operation_id: operationId,
    token,
    audit_context: testAuditContext(),
    payload:
      operation === "suspend_account"
        ? { target_user_id: targetId }
        : { target_user_id: targetId, reason },
  });
}

function testReply(request, user, principal) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    principal,
    data: { user },
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
  const targetId = "33333333-3333-4333-8333-333333333333";
  const operationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const token = testToken(actorId, credentialId);
  const principal = testPrincipal(actorId, credentialId);

  const suspended = testRequest("suspend_account", operationId, token, targetId);
  const frozenNoop = testUser(targetId, {
    disabled_kind: "deleted",
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
  });
  const noOpReply = decodeReply(
    suspended,
    testReply(suspended, frozenNoop, principal),
    200,
    "application/json",
  );
  if (noOpReply.data.user.disabled_kind !== "deleted") fail();
  checks += 1;

  const opaque = {
    token: "opaque preference value",
    password: new JsonNumber("123456789012345678901"),
    decimal: new JsonNumber("0.1234567890123456789"),
    nested: { wide: new JsonNumber("9007199254740993") },
  };
  const resultAfterReactivate = testUser(targetId, {
    role: "reviewer",
    status: "offline",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    preferences: opaque,
  });
  const oldReservation = [
    resultAfterReactivate.deactivation_requested_at,
    resultAfterReactivate.deactivation_scheduled_at,
  ];
  const reactivate = testRequest(
    "reactivate_account",
    operationId,
    token,
    targetId,
    "　\u2003do-not-echo\u00a0",
  );
  const reactivateRaw = testReply(reactivate, resultAfterReactivate, principal);
  const reactivated = decodeReply(reactivate, reactivateRaw, 200, "application/json");
  if (
    reactivated.data.user.is_active !== true ||
    reactivated.data.user.disabled_kind !== null ||
    reactivated.data.user.deactivation_requested_at !== oldReservation[0] ||
    reactivated.data.user.deactivation_scheduled_at !== oldReservation[1]
  ) {
    fail();
  }
  for (const literal of [
    '"wide":9007199254740993',
    '"decimal":0.1234567890123456789',
    '"password":123456789012345678901',
    '"token":"opaque preference value"',
  ]) {
    if (!stringifyLossless(reactivated).includes(literal)) fail();
  }
  if (reactivated.data.user.disabled_reason !== null) fail();
  checks += 1;
  const publicReactivated = validatePublicInput({
    mode: "reactivate",
    status: new JsonNumber("200"),
    content_type: "application/json; charset=utf-8",
    body: stringifyLossless(resultAfterReactivate),
    target_user_id: targetId,
  });
  if (publicReactivated.id !== targetId) fail();
  checks += 1;

  const reasonMatchesName = testRequest(
    "reactivate_account",
    operationId,
    token,
    targetId,
    "Member",
  );
  const namedUser = { ...resultAfterReactivate, name: "Member" };
  if (
    decodeReply(
      reasonMatchesName,
      testReply(reasonMatchesName, namedUser, principal),
      200,
      "application/json",
    ).data.user.name !== "Member"
  ) {
    fail();
  }
  checks += 1;

  const nulReason = "private\u0000reactivation reason";
  const nulReasonRequest = testRequest(
    "reactivate_account",
    operationId,
    token,
    targetId,
    nulReason,
  );
  const nulRoundTrip = validateRequest(
    parseStrictJson(stringifyLossless(nulReasonRequest.request)),
  );
  if (nulRoundTrip.request.payload.reason !== nulReason) fail();
  checks += 1;

  const legacyClaims = Buffer.from(
    JSON.stringify({
      sub: actorId,
      role: "reviewer",
      exp: 1893456000,
      jti: credentialId,
      iss: issuer,
      historical_extra: true,
    }),
  ).toString("base64url");
  const legacyToken = `${Buffer.from(JSON.stringify(jwtHeader)).toString("base64url")}.${legacyClaims}.${Buffer.alloc(32, 5).toString("base64url")}`;
  const legacyRequest = testRequest("suspend_account", operationId, legacyToken, targetId);
  const zeroPrincipal = testPrincipal(actorId, credentialId, new JsonNumber("0"));
  if (
    decodeReply(
      legacyRequest,
      testReply(legacyRequest, frozenNoop, zeroPrincipal),
      200,
      "application/json",
    ).principal.generation !== 0n
  ) {
    fail();
  }
  checks += 1;

  const maximumToken = testToken(actorId, credentialId, maxInt64);
  const maximumPrincipal = testPrincipal(
    actorId,
    credentialId,
    new JsonNumber(maxInt64.toString(10)),
  );
  const maximumRequest = testRequest("suspend_account", operationId, maximumToken, targetId);
  const maximumReply = decodeReply(
    maximumRequest,
    testReply(maximumRequest, frozenNoop, maximumPrincipal),
    200,
    "application/json",
  );
  if (
    maximumReply.principal.generation !== maxInt64 ||
    !stringifyLossless(maximumReply).includes(maxInt64.toString(10))
  ) {
    fail();
  }
  expectFailure(() =>
    decodeReply(
      maximumRequest,
      testReply(
        maximumRequest,
        frozenNoop,
        testPrincipal(actorId, credentialId, new JsonNumber((maxInt64 + 1n).toString(10))),
      ),
      200,
      "application/json",
    ),
  );
  checks += 2;

  const apiKeyToken = `ak_${"A".repeat(32)}`;
  const apiKeyRequest = testRequest("suspend_account", operationId, apiKeyToken, targetId);
  const apiPrincipal = {
    ...principal,
    credential_type: "api_key",
    generation: null,
    scopes: ["*"],
    expires_at: null,
  };
  const expectedApiPrincipal = {
    actor_id: actorId,
    credential_type: "api_key",
    credential_id: credentialId,
    generation_decimal: null,
    scopes: ["*"],
    expires_at: null,
  };
  decodeReply(
    apiKeyRequest,
    testReply(apiKeyRequest, frozenNoop, apiPrincipal),
    200,
    "application/json",
    expectedApiPrincipal,
  );
  expectFailure(() =>
    decodeReply(
      apiKeyRequest,
      testReply(apiKeyRequest, frozenNoop, apiPrincipal),
      200,
      "application/json",
    ),
  );
  checks += 2;

  const failures = [
    ["invalid_request", "suspend_account", 400, null],
    ["manager_required", "suspend_account", 403, "当前账号已无管理权限"],
    ["target_not_found", "suspend_account", 404, "用户不存在"],
    ["self_suspend", "suspend_account", 400, "不能停用自己"],
    ["self_reactivate", "reactivate_account", 400, "不能恢复自己"],
    ["suspend_role_forbidden", "suspend_account", 403, "项目管理员仅能停用标注员/审核员账号"],
    ["reactivate_role_forbidden", "reactivate_account", 403, "项目管理员不能恢复该角色"],
    ["reactivate_not_managed", "reactivate_account", 403, "该用户不在你管理的项目内"],
    ["suspend_scope_forbidden", "suspend_account", 403, "该用户跨多个项目，须由超级管理员处理"],
    ["reactivate_scope_forbidden", "reactivate_account", 403, "该用户存在不在你管理范围内的项目"],
    ["last_super_admin", "suspend_account", 400, "不能停用最后一名超级管理员"],
    ["already_active", "reactivate_account", 409, "账号当前已启用"],
    ["not_reactivatable", "reactivate_account", 409, "该停用状态不可恢复"],
    ["account_busy", "suspend_account", 409, "账号正在更新，请刷新后重试"],
    ["operation_conflict", "suspend_account", 409, null],
    ["unavailable", "reactivate_account", 503, null],
  ];
  for (const [code, operation, status, detail] of failures) {
    const failureTarget = ["self_suspend", "self_reactivate"].includes(code) ? actorId : targetId;
    const request = testRequest(operation, operationId, token, failureTarget);
    const raw = stringifyLossless({
      schema,
      ok: false,
      error: { code, status_code: status, detail },
    });
    if (decodeReply(request, raw, 200, "application/json").error.code !== code) fail();
    checks += 1;
  }
  for (const code of authFailures) {
    const request = testRequest("suspend_account", operationId, token, targetId);
    const raw = stringifyLossless({
      schema,
      ok: false,
      error: { code, status_code: null, detail: null },
    });
    if (decodeReply(request, raw, 200, "application/json").error.code !== code) fail();
    checks += 1;
  }
  const echoedReason = stringifyLossless({
    schema,
    ok: false,
    error: { code: "account_busy", status_code: 409, detail: "private raw reason" },
  });
  expectFailure(() =>
    decodeReply(
      testRequest("reactivate_account", operationId, token, targetId, "private raw reason"),
      echoedReason,
      200,
      "application/json",
    ),
  );
  checks += 1;
  const projectBusyRequest = testRequest("suspend_account", operationId, token, targetId);
  const projectBusy = stringifyLossless({
    schema,
    ok: false,
    error: { code: "project_busy", status_code: 409, detail: { reason: "project_busy" } },
  });
  decodeReply(projectBusyRequest, projectBusy, 200, "application/json");
  expectFailure(() =>
    decodeReply(
      testRequest("reactivate_account", operationId, token, targetId),
      projectBusy,
      200,
      "application/json",
    ),
  );
  checks += 2;

  expectFailure(() => parseStrictJson('{"schema":"a","schema":"b"}'));
  expectFailure(() => parseStrictJson('{"schema":'));
  expectFailure(() =>
    validateRequest({
      ...suspended.request,
      payload: { target_user_id: targetId, extra: true },
    }),
  );
  expectFailure(() =>
    validateRequest({
      ...reactivate.request,
      payload: { target_user_id: targetId, reason: "x".repeat(501) },
    }),
  );
  expectFailure(() =>
    validateRequest({
      ...suspended.request,
      operation: "unrecognized_operation",
    }),
  );
  checks += 5;

  expectFailure(() =>
    decodeReply(
      reactivate,
      reactivateRaw.replace(operationId, credentialId),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeReply(reactivate, reactivateRaw.replace(actorId, targetId), 200, "application/json"),
  );
  expectFailure(() =>
    decodeReply(
      reactivate,
      reactivateRaw.replace('"disabled_reason":null', '"disabled_reason":"reason leak"'),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeReply(
      reactivate,
      reactivateRaw.replace(
        '"created_at":"2024-01-01T00:00:00Z"',
        '"created_at":"2024-01-01T00:00:00Z","access_token":"leak"',
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() => new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from([0xc0, 0xaf])));
  checks += 5;
  return { ok: true, checks };
}

function validateCallInput(value) {
  if (!exact(value, ["mode", "url", "control_token", "request"])) fail();
  if (!["call", "discard"].includes(value.mode)) fail();
  const checkedRequest = validateRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: checkedRequest.request.token,
  });
  return {
    mode: value.mode,
    request: checkedRequest,
    origin: checked.origin,
    controlToken: checked.controlToken,
  };
}

async function callPeer() {
  const input = await readInput();
  const checked = validateCallInput(input);
  const expectedApiPrincipal = checked.request.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(checked.origin, checked.controlToken, checked.request.request.token)
    : null;
  const response = await postSuspensionRequest(
    checked.origin,
    checked.controlToken,
    checked.request.request,
  );
  const reply = decodeReply(
    checked.request,
    response.text,
    response.status,
    response.contentType,
    expectedApiPrincipal,
  );
  if (checked.mode === "discard") {
    process.stdout.write(`${JSON.stringify({ ok: true, response_discarded: true })}\n`);
    return;
  }
  process.stdout.write(`${stringifyLossless(reply)}\n`);
}

async function validatePublic() {
  const input = await readInput();
  const user = validatePublicInput(input);
  process.stdout.write(`${stringifyLossless(user)}\n`);
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
      process.stderr.write("Identity suspension peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    const runner = process.argv[2] === "--validate-public" ? validatePublic : callPeer;
    runner().catch(() => {
      process.stderr.write("Identity suspension peer failed\n");
      process.exitCode = 1;
    });
  }
}
