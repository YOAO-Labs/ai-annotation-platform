import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  uuid,
  validDateTime,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-deletions/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/deletions/execute";
const authEndpoint = "/v1/identity/authenticate";
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const managerRoles = new Set(["super_admin", "project_admin"]);
const operations = new Set(["delete_account"]);
const authFailures = new Set([
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);
const failureCodes = new Set([
  ...authFailures,
  "invalid_request",
  "manager_required",
  "target_not_found",
  "self_delete",
  "delete_role_forbidden",
  "delete_scope_forbidden",
  "last_super_admin",
  "has_pending_tasks",
  "receiver_unavailable",
  "receiver_is_target",
  "receiver_role_invalid",
  "receiver_scope_forbidden",
  "account_busy",
  "project_busy",
  "operation_conflict",
  "unavailable",
]);
const fixedFailures = {
  manager_required: [403n, "当前账号已无管理权限"],
  target_not_found: [404n, "用户不存在"],
  self_delete: [400n, "不能删除自己"],
  delete_role_forbidden: [403n, "项目管理员仅能删除员工账号"],
  delete_scope_forbidden: [403n, "该用户跨多个项目，须由超级管理员处理"],
  last_super_admin: [400n, "不能删除最后一名超级管理员"],
  receiver_unavailable: [400n, "转交目标用户不存在或已禁用"],
  receiver_is_target: [400n, "转交目标不能与被删用户相同"],
  receiver_role_invalid: [400n, "转交目标角色不合法"],
  receiver_scope_forbidden: [403n, "转交目标必须是待转交任务所在项目的成员"],
  account_busy: [409n, "账号正在更新，请刷新后重试"],
  operation_conflict: [409n, null],
  unavailable: [503n, null],
};
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
const maxInputBytes = 64 * 1024;
const maxResponseBytes = 4 * 1024 * 1024;

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity deletion request or response");
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

function validateAuditContext(value, targetId) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  if (
    value.method !== "DELETE" ||
    value.path !== `/api/v1/users/${targetId}` ||
    !nullableString(value.ip, 64) ||
    !nullableString(value.request_id, 36) ||
    (value.ip !== null && value.ip !== "" && isIP(value.ip) === 0)
  ) {
    fail();
  }
  uuid(value.request_id);
}

function validateDeletionRequest(value) {
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
  if (!exact(value.payload, ["target_user_id", "transfer_to_user_id"])) fail();
  const targetId = uuid(value.payload.target_user_id);
  const receiverId =
    value.payload.transfer_to_user_id === null ? null : uuid(value.payload.transfer_to_user_id);
  validateAuditContext(value.audit_context, targetId);
  return {
    request: {
      ...value,
      operation_id: operationId,
      audit_context: { ...value.audit_context, request_id: uuid(value.audit_context.request_id) },
      payload: { target_user_id: targetId, transfer_to_user_id: receiverId },
    },
    operationId,
    targetId,
    receiverId,
  };
}

function decodeTokenPart(part) {
  if (!/^[A-Za-z0-9_-]+$/.test(part) || part.length % 4 === 1) fail();
  const bytes = Buffer.from(part, "base64url");
  if (bytes.toString("base64url") !== part) fail();
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    fail();
  }
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3) fail();
  const header = decodeTokenPart(parts[0]);
  if (
    !isRecord(header) ||
    header.alg !== "HS256" ||
    (header.typ !== undefined && header.typ !== "JWT")
  )
    fail();
  const signature = Buffer.from(parts[2], "base64url");
  if (signature.length !== 32 || signature.toString("base64url") !== parts[2]) fail();
  const claims = decodeTokenPart(parts[1]);
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
    !managerRoles.has(value.role) ||
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
  return value;
}

function bindPrincipal(principal, request, expectedApiPrincipal) {
  if (!managerRoles.has(principal.role)) fail();
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

function validateUserRecord(value) {
  if (!exact(value, userFields)) fail();
  value.id = uuid(value.id);
  value.group_id = value.group_id === null ? null : uuid(value.group_id);
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

function validatePendingFailure(request, error, actorId) {
  const detail = error.detail;
  if (
    !exact(detail, [
      "reason",
      "pending_task_count",
      "locked_task_count",
      "sample_task_ids",
      "message",
    ])
  )
    fail();
  if (detail.reason !== "has_pending_tasks") fail();
  const pending = int64(detail.pending_task_count);
  const locked = int64(detail.locked_task_count);
  if (
    pending + locked === 0n ||
    !Array.isArray(detail.sample_task_ids) ||
    detail.sample_task_ids.length > 5
  )
    fail();
  const samples = detail.sample_task_ids.map(uuid);
  if (
    new Set(samples).size !== samples.length ||
    BigInt(samples.length) > pending ||
    (pending === 0n && samples.length !== 0) ||
    detail.message !== `该用户当前有 ${pending} 个未完成任务、${locked} 个锁定任务，需先转交`
  ) {
    fail();
  }
  if (actorId === null || request.receiverId !== null || request.targetId === actorId) {
    fail();
  }
  return { ...detail, sample_task_ids: samples };
}

function validateFailure(request, error, actorId) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    !failureCodes.has(error.code)
  ) {
    fail();
  }
  const status = error.status_code === null ? null : int64(error.status_code);
  const detail = error.detail;
  if (authFailures.has(error.code)) {
    if (status !== null || detail !== null) fail();
  } else if (error.code === "invalid_request") {
    if (status !== 400n || detail !== null) fail();
  } else if (error.code === "has_pending_tasks") {
    if (status !== 409n) fail();
    error.detail = validatePendingFailure(request, error, actorId);
  } else if (error.code === "project_busy") {
    if (
      actorId === null ||
      request.targetId === actorId ||
      status !== 409n ||
      !exact(detail, ["reason"]) ||
      detail.reason !== "project_busy"
    ) {
      fail();
    }
  } else {
    const pair = fixedFailures[error.code];
    if (!pair || status !== pair[0] || detail !== pair[1]) fail();
    if (error.code === "self_delete" && (actorId === null || request.targetId !== actorId)) fail();
    if (error.code === "target_not_found" && (actorId === null || request.targetId === actorId)) {
      fail();
    }
    if (
      (error.code === "delete_role_forbidden" || error.code === "delete_scope_forbidden") &&
      (actorId === null || request.targetId === actorId)
    ) {
      fail();
    }
    if (
      error.code === "receiver_is_target" &&
      (request.receiverId !== request.targetId || actorId === null || request.targetId === actorId)
    ) {
      fail();
    }
    if (
      ["receiver_unavailable", "receiver_role_invalid", "receiver_scope_forbidden"].includes(
        error.code,
      ) &&
      (request.receiverId === null ||
        request.receiverId === request.targetId ||
        request.targetId === actorId ||
        actorId === null ||
        (["receiver_unavailable", "receiver_scope_forbidden"].includes(error.code) &&
          request.receiverId === actorId))
    ) {
      fail();
    }
    if (error.code === "last_super_admin" && request.targetId === actorId) fail();
  }
  return error;
}

function decodeDeletionResponse(checked, source, status, contentType, expectedApiPrincipal = null) {
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
  if (document.ok === false) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    let actorId = null;
    if (request.token.startsWith("ak_")) actorId = expectedApiPrincipal?.actor_id ?? null;
    else {
      try {
        actorId = incomingJwtClaims(request.token).actorId;
      } catch {
        actorId = null;
      }
    }
    return {
      schema,
      ok: false,
      error: validateFailure(checked, document.error, actorId),
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
  document.data.user = validateUserRecord(document.data.user);
  if (
    document.data.user.id !== checked.targetId ||
    document.data.user.is_active !== false ||
    document.data.user.id === principal.actor_id
  ) {
    fail();
  }
  return document;
}

function validatePublicSuccess(body, status, contentType, targetId) {
  if (
    typeof body !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const user = validateUserRecord(parseStrictJson(body));
  if (user.id !== uuid(targetId) || user.is_active !== false) fail();
  return user;
}

function validateCallInput(value) {
  if (!exact(value, ["mode", "url", "control_token", "request"])) fail();
  if (!new Set(["call", "discard"]).has(value.mode)) fail();
  if (
    typeof value.url !== "string" ||
    value.url.length < 1 ||
    value.url.length > 2048 ||
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
  const checked = validateDeletionRequest(value.request);
  return { mode: value.mode, origin, controlToken: value.control_token, checked };
}

function postDeletionRequest(origin, controlToken, payload) {
  const endpointUrl = new URL(endpoint, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(payload), "utf8");
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
        let size = 0;
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > maxResponseBytes) {
            response.destroy();
            rejectResponse(new Error("Identity response exceeded the test bound"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Invalid Identity deletion response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity deletion request failed")));
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity deletion request failed")));
    outgoing.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  const response = await postIdentityRequest(origin, controlToken, authEndpoint, { token });
  scanJson(response.text);
  const admission = parseIdentityReceipt(response.text, response.status, response.contentType);
  if (!admission.ok || admission.principal.credential_type !== "api_key") return null;
  return admission.principal;
}

async function callPeer(value) {
  const { mode, origin, controlToken, checked } = validateCallInput(value);
  const response = await postDeletionRequest(origin, controlToken, checked.request);
  if (mode === "discard") return { ok: true, response_discarded: true };
  const apiPrincipal = checked.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(origin, controlToken, checked.request.token)
    : null;
  const result = decodeDeletionResponse(
    checked,
    response.text,
    response.status,
    response.contentType,
    apiPrincipal,
  );
  return result;
}

function validatePublicInput(value) {
  if (!exact(value, ["status", "content_type", "body", "target_user_id"])) fail();
  const status = int64(value.status);
  if (status !== 200n || typeof value.content_type !== "string" || typeof value.body !== "string")
    fail();
  return validatePublicSuccess(
    value.body,
    Number(status),
    value.content_type,
    value.target_user_id,
  );
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > maxInputBytes) fail();
    chunks.push(chunk);
  }
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

function testToken(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  const header = Buffer.from(JSON.stringify(jwtHeader)).toString("base64url");
  const payload = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "legacy-manager-role",
      exp: 1893456000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
      legacy_claim: { retained: true },
    }),
  ).toString("base64url");
  return `${header}.${payload}.${Buffer.alloc(32, 7).toString("base64url")}`;
}

function testPrincipal(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  return {
    actor_id: actorId,
    name: "Deletion manager",
    email: "deletion-manager@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation: new JsonNumber(generation.toString()),
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testRequest(actorId, credentialId, targetId, operationId) {
  return validateDeletionRequest({
    schema,
    operation: "delete_account",
    operation_id: operationId,
    token: testToken(actorId, credentialId),
    audit_context: {
      method: "DELETE",
      path: `/api/v1/users/${targetId}`,
      ip: "127.0.0.1",
      request_id: "44444444-4444-4444-8444-444444444444",
    },
    payload: { target_user_id: targetId, transfer_to_user_id: null },
  });
}

function testUserRecord(id, preferences = {}) {
  return {
    id,
    email: "historical-account@example.test",
    name: "Historical Account",
    role: "historical-employee",
    group_name: null,
    group_id: null,
    status: "legacy-offline",
    is_active: false,
    disabled_kind: "suspended",
    disabled_at: "2030-01-01T00:00:00Z",
    disabled_by: "55555555-5555-4555-8555-555555555555",
    disabled_reason: "earlier suspension",
    email_verified_at: null,
    last_login_at: null,
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences,
    avatar_ref: null,
    created_at: "2029-01-01T00:00:00Z",
  };
}

function testReply(checked, data, principal) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: checked.request.operation,
    operation_id: checked.operationId,
    principal,
    data,
  });
}

function expectFailure(callback) {
  try {
    callback();
  } catch {
    return;
  }
  fail();
}

function selfCheck() {
  let checks = 0;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const targetId = "33333333-3333-4333-8333-333333333333";
  const operationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const checked = testRequest(actorId, credentialId, targetId, operationId);
  const principal = testPrincipal(actorId, credentialId);
  const preferences = {
    token: checked.request.token,
    password: new JsonNumber("123456789012345678901"),
    nested: {
      wide: new JsonNumber("9007199254740993"),
      decimal: new JsonNumber("0.1234567890123456789"),
      retained: [true, null, "legacy", new JsonNumber("-9223372036854775808")],
    },
  };
  const success = testReply(checked, { user: testUserRecord(targetId, preferences) }, principal);
  const decoded = decodeDeletionResponse(checked, success, 200, "application/json");
  const decodedJson = stringifyLossless(decoded);
  for (const literal of [
    '"wide":9007199254740993',
    '"decimal":0.1234567890123456789',
    '"password":123456789012345678901',
    `"token":${JSON.stringify(checked.request.token)}`,
  ]) {
    if (!decodedJson.includes(literal)) fail();
  }
  checks += 1;

  const legacyClaims = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "annotator",
      exp: 1893456000n,
      jti: credentialId,
      iss: issuer,
      legacy_extension: true,
    }),
  ).toString("base64url");
  const legacyToken = `${Buffer.from(JSON.stringify(jwtHeader)).toString("base64url")}.${legacyClaims}.${Buffer.alloc(32, 8).toString("base64url")}`;
  const legacyRequest = validateDeletionRequest({
    ...checked.request,
    token: legacyToken,
  });
  const legacyPrincipal = testPrincipal(actorId, credentialId, 0n);
  const legacyReply = testReply(legacyRequest, { user: testUserRecord(targetId) }, legacyPrincipal);
  if (
    decodeDeletionResponse(legacyRequest, legacyReply, 200, "application/json").principal
      .generation !== 0n
  ) {
    fail();
  }
  checks += 1;

  const apiKeyRequest = validateDeletionRequest({
    ...checked.request,
    token: "ak_deletion-peer-selfcheck",
  });
  const apiKeyPrincipal = {
    ...principal,
    credential_type: "api_key",
    generation: null,
    scopes: ["*"],
    expires_at: null,
  };
  const apiKeyReply = testReply(apiKeyRequest, { user: testUserRecord(targetId) }, apiKeyPrincipal);
  if (
    decodeDeletionResponse(apiKeyRequest, apiKeyReply, 200, "application/json", apiKeyPrincipal)
      .principal.credential_type !== "api_key"
  ) {
    fail();
  }
  checks += 1;

  const inactiveNoop = testReply(
    checked,
    { user: testUserRecord(targetId, { preserved: true }) },
    principal,
  );
  if (
    decodeDeletionResponse(checked, inactiveNoop, 200, "application/json").data.user
      .disabled_kind !== "suspended"
  )
    fail();
  checks += 1;

  const pendingDetail = {
    reason: "has_pending_tasks",
    pending_task_count: new JsonNumber("9007199254740993"),
    locked_task_count: new JsonNumber("2"),
    sample_task_ids: ["66666666-6666-4666-8666-666666666666"],
    message: "该用户当前有 9007199254740993 个未完成任务、2 个锁定任务，需先转交",
  };
  const pendingFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "has_pending_tasks", status_code: 409, detail: pendingDetail },
  });
  if (
    decodeDeletionResponse(checked, pendingFailure, 200, "application/json").error.code !==
    "has_pending_tasks"
  )
    fail();
  checks += 1;

  expectFailure(() =>
    parseStrictJson('{"operation":"delete_account","operation":"delete_account"}'),
  );
  checks += 1;
  expectFailure(() => parseStrictJson('{"value":NaN}'));
  checks += 1;
  expectFailure(() => new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from([0xff])));
  checks += 1;

  const missingReceiver = {
    ...checked.request,
    payload: { target_user_id: targetId },
  };
  expectFailure(() => validateDeletionRequest(missingReceiver));
  checks += 1;
  const badReceiver = {
    ...checked.request,
    payload: { target_user_id: targetId, transfer_to_user_id: "not-a-uuid" },
  };
  expectFailure(() => validateDeletionRequest(badReceiver));
  checks += 1;
  expectFailure(() => validateDeletionRequest({ ...checked.request, unexpected: "alias" }));
  checks += 1;

  const wrongOperationId = testReply(
    checked,
    { user: testUserRecord(targetId) },
    principal,
  ).replace(operationId, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
  expectFailure(() => decodeDeletionResponse(checked, wrongOperationId, 200, "application/json"));
  checks += 1;
  const wrongActor = testReply(
    checked,
    { user: testUserRecord(targetId) },
    testPrincipal("77777777-7777-4777-8777-777777777777", credentialId),
  );
  expectFailure(() => decodeDeletionResponse(checked, wrongActor, 200, "application/json"));
  checks += 1;
  const unexpectedCredential = testReply(
    checked,
    { user: { ...testUserRecord(targetId), access_token: "unexpected" } },
    principal,
  );
  expectFailure(() =>
    decodeDeletionResponse(checked, unexpectedCredential, 200, "application/json"),
  );
  checks += 1;

  const wrongError = stringifyLossless({
    schema,
    ok: false,
    error: { code: "manager_required", status_code: 404, detail: "用户不存在" },
  });
  expectFailure(() => decodeDeletionResponse(checked, wrongError, 200, "application/json"));
  checks += 1;

  const actorReceiverRequest = validateDeletionRequest({
    ...checked.request,
    payload: { target_user_id: targetId, transfer_to_user_id: actorId },
  });
  const impossibleUnavailable = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "receiver_unavailable",
      status_code: 400,
      detail: "转交目标用户不存在或已禁用",
    },
  });
  expectFailure(() =>
    decodeDeletionResponse(actorReceiverRequest, impossibleUnavailable, 200, "application/json"),
  );
  checks += 1;

  const impossibleScope = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "receiver_scope_forbidden",
      status_code: 403,
      detail: "转交目标必须是待转交任务所在项目的成员",
    },
  });
  expectFailure(() =>
    decodeDeletionResponse(actorReceiverRequest, impossibleScope, 200, "application/json"),
  );
  checks += 1;

  const selfReceiverRequest = validateDeletionRequest({
    ...checked.request,
    audit_context: {
      ...checked.request.audit_context,
      path: `/api/v1/users/${actorId}`,
    },
    payload: { target_user_id: actorId, transfer_to_user_id: actorId },
  });
  const impossibleSelfReceiver = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "receiver_is_target",
      status_code: 400,
      detail: "转交目标不能与被删用户相同",
    },
  });
  expectFailure(() =>
    decodeDeletionResponse(selfReceiverRequest, impossibleSelfReceiver, 200, "application/json"),
  );
  checks += 1;

  const unsafeCount = pendingFailure.replace(
    '"pending_task_count":9007199254740993',
    '"pending_task_count":9007199254740993.0',
  );
  expectFailure(() => decodeDeletionResponse(checked, unsafeCount, 200, "application/json"));
  checks += 1;
  const malformedSample = pendingFailure.replace(
    '"sample_task_ids":["66666666-6666-4666-8666-666666666666"]',
    '"sample_task_ids":["66666666-6666-4666-8666-666666666666","66666666-6666-4666-8666-666666666666"]',
  );
  expectFailure(() => decodeDeletionResponse(checked, malformedSample, 200, "application/json"));
  checks += 1;

  const publicUser = validatePublicSuccess(
    stringifyLossless(testUserRecord(targetId, preferences)),
    200,
    "application/json; charset=utf-8",
    targetId,
  );
  if (publicUser.id !== targetId || publicUser.is_active !== false) fail();
  checks += 1;
  return checks;
}

async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify({ ok: true, checks: selfCheck() })}\n`);
    return;
  }
  if (process.argv[2] === "--validate-public") {
    const result = validatePublicInput(await readInput());
    process.stdout.write(`${stringifyLossless(result)}\n`);
    return;
  }
  const result = await callPeer(await readInput());
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write("Identity deletion peer validation failed\n");
    process.exitCode = 1;
  });
}
