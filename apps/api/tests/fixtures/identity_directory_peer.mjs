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

const schema = "aap-identity-directory/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/directory/execute";
const authEndpoint = "/v1/identity/authenticate";
const operations = new Set(["list_users", "query_users", "user_stats", "export_users"]);
const managerRoles = new Set(["super_admin", "project_admin"]);
const platformRoles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
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
  "invalid_platform_role",
  "current_scope_busy",
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
const exportFields = [
  "id",
  "email",
  "name",
  "role",
  "group_name",
  "group_id",
  "status",
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
const managerRequired = "需要角色权限: super_admin或project_admin";

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity directory request or response");
}

function isRecord(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    !(value instanceof JsonNumber)
  );
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

function boundedString(value, max) {
  return typeof value === "string" && codePointLength(value) <= max;
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

function integer(value, min = 0n, max = maxInt64) {
  if (!(value instanceof JsonNumber) || !/^-?(?:0|[1-9][0-9]*)$/.test(value.raw)) {
    fail();
  }
  const parsed = BigInt(value.raw);
  if (parsed < min || parsed > max) fail();
  return parsed;
}

function nullableString(value, max = Number.MAX_SAFE_INTEGER) {
  return value === null || boundedString(value, max);
}

function nullableUuid(value) {
  return value === null ? null : uuid(value);
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  if (
    !nullableString(value.method, 8) ||
    !nullableString(value.path, 256) ||
    !nullableString(value.ip, 64) ||
    !nullableString(value.request_id, 36) ||
    (value.ip !== null && value.ip !== "" && isIP(value.ip) === 0)
  ) {
    fail();
  }
}

function validateFilters(payload, { list = false, paged = false, format = false } = {}) {
  const names = list
    ? ["role", "status", "project_id"]
    : [
        ...(paged ? ["page", "page_size"] : []),
        "status",
        "project_id",
        "group_id",
        "role",
        "search",
        ...(format ? ["format"] : []),
      ];
  if (!exact(payload, names) || !["active", "inactive", "all"].includes(payload.status)) fail();
  payload.project_id = nullableUuid(payload.project_id);
  if (list) {
    if (payload.role !== null && typeof payload.role !== "string") fail();
    return payload;
  }
  payload.group_id = nullableUuid(payload.group_id);
  if (payload.role !== null && typeof payload.role !== "string") fail();
  if (payload.search !== null && !boundedString(payload.search, 255)) fail();
  if (paged) {
    payload.page = integer(payload.page, 1n);
    payload.page_size = integer(payload.page_size, 1n, 500n);
  }
  if (format && !["csv", "json"].includes(payload.format)) fail();
  return payload;
}

export function validateDirectoryRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  value.operation_id = uuid(value.operation_id);
  if (
    typeof value.token !== "string" ||
    codePointLength(value.token) < 1 ||
    codePointLength(value.token) > 16_384
  ) {
    fail();
  }
  validateAuditContext(value.audit_context);
  switch (value.operation) {
    case "list_users":
      validateFilters(value.payload, { list: true });
      break;
    case "query_users":
      validateFilters(value.payload, { paged: true });
      break;
    case "user_stats":
      validateFilters(value.payload);
      break;
    case "export_users":
      validateFilters(value.payload, { format: true });
      break;
  }
  return { request: value, operationId: value.operation_id };
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
    !platformRoles.has(value.role) ||
    value.is_active !== true ||
    !["jwt", "api_key"].includes(value.credential_type)
  ) {
    fail();
  }
  value.actor_id = uuid(value.actor_id);
  value.credential_id = uuid(value.credential_id);
  if (value.credential_type === "jwt") {
    value.generation = integer(value.generation);
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

function validateUserRecord(value, fields = userFields) {
  if (!exact(value, fields)) fail();
  value.id = uuid(value.id);
  value.group_id = nullableUuid(value.group_id);
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

function validateExportRecord(value) {
  if (!exact(value, exportFields)) fail();
  value.id = uuid(value.id);
  value.group_id = nullableUuid(value.group_id);
  if (
    typeof value.email !== "string" ||
    typeof value.name !== "string" ||
    typeof value.role !== "string" ||
    !nullableString(value.group_name) ||
    typeof value.status !== "string" ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  return value;
}

function validateFailure(request, error) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    !failureCodes.has(error.code)
  ) {
    fail();
  }
  const code = error.code;
  const status = error.status_code === null ? null : integer(error.status_code, 100n, 599n);
  if (error.detail !== null && typeof error.detail !== "string") fail();
  if (error.detail && error.detail.includes(request.token)) fail();
  if (authFailures.has(code)) {
    if (status !== null || error.detail !== null) fail();
  } else if (code === "invalid_request") {
    if (status !== 400n || error.detail !== null) fail();
  } else if (code === "manager_required") {
    if (status !== 403n || error.detail !== managerRequired) fail();
  } else if (code === "invalid_platform_role") {
    const role = request.payload.role;
    if (
      role === null ||
      platformRoles.has(role) ||
      status !== 400n ||
      error.detail !== `非法平台角色: ${role}`
    ) {
      fail();
    }
  } else if (code === "current_scope_busy") {
    if (status !== 409n || error.detail !== null) fail();
  } else if (code === "operation_conflict") {
    if (request.operation !== "export_users" || status !== 409n || error.detail !== null) fail();
  } else if (code === "unavailable") {
    if (status !== 503n || error.detail !== null) fail();
  }
  return error;
}

function validateDirectoryData(request, data, principal) {
  switch (request.operation) {
    case "list_users":
      if (!exact(data, ["users"]) || !Array.isArray(data.users)) fail();
      data.users = data.users.map((user) => validateUserRecord(user));
      return data;
    case "query_users": {
      if (
        !exact(data, ["items", "total", "page", "page_size", "pages"]) ||
        !Array.isArray(data.items)
      )
        fail();
      data.total = integer(data.total);
      data.page = integer(data.page, 1n);
      data.page_size = integer(data.page_size, 1n, 500n);
      data.pages = integer(data.pages);
      data.items = data.items.map((item) => {
        if (!exact(item, [...userFields, "is_managed", "is_lifecycle_managed"])) fail();
        if (typeof item.is_managed !== "boolean" || typeof item.is_lifecycle_managed !== "boolean")
          fail();
        return validateUserRecord(item, [...userFields, "is_managed", "is_lifecycle_managed"]);
      });
      const pageSize = request.payload.page_size;
      const expectedPages = data.total === 0n ? 0n : (data.total + pageSize - 1n) / pageSize;
      if (
        data.page !== request.payload.page ||
        data.page_size !== pageSize ||
        data.pages !== expectedPages ||
        BigInt(data.items.length) > pageSize ||
        BigInt(data.items.length) > data.total ||
        (data.page > expectedPages && data.items.length !== 0)
      ) {
        fail();
      }
      validateCreatedOrder(data.items, true);
      for (const item of data.items) {
        if (principal.role === "super_admin") {
          if (!item.is_managed || !item.is_lifecycle_managed) fail();
        } else {
          if (item.id === principal.actor_id && !item.is_managed) fail();
          if (item.is_active && item.role === "employee" && !item.is_managed) fail();
          if (item.is_lifecycle_managed && !item.is_managed) fail();
        }
      }
      return data;
    }
    case "user_stats":
      if (!exact(data, ["total", "online", "weekly_active"])) fail();
      for (const field of ["total", "online", "weekly_active"]) data[field] = integer(data[field]);
      if (data.online > data.total || data.weekly_active > data.total) fail();
      return data;
    case "export_users":
      if (
        !exact(data, ["users", "exported_by", "exported_at", "request_id"]) ||
        !Array.isArray(data.users) ||
        typeof data.exported_by !== "string" ||
        !validDateTime(data.exported_at) ||
        (data.request_id !== null && typeof data.request_id !== "string")
      ) {
        fail();
      }
      data.users = data.users.map(validateExportRecord);
      validateCreatedOrder(data.users, true);
      return data;
  }
  fail();
}

function instantMicroseconds(value) {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(
    value,
  );
  if (!match) fail();
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) fail();
  const fraction = (match[2] ?? "").padEnd(6, "0");
  return BigInt(milliseconds) * 1000n + BigInt(fraction.slice(3) || "0");
}

function validateCreatedOrder(users, idTies) {
  for (let index = 1; index < users.length; index += 1) {
    const previous = users[index - 1];
    const current = users[index];
    const previousTime = instantMicroseconds(previous.created_at);
    const currentTime = instantMicroseconds(current.created_at);
    if (
      previousTime < currentTime ||
      (idTies &&
        previousTime === currentTime &&
        BigInt(`0x${previous.id.replaceAll("-", "")}`) <
          BigInt(`0x${current.id.replaceAll("-", "")}`))
    ) {
      fail();
    }
  }
}

function validateDataFilters(request, data) {
  const users = data.users ?? data.items;
  if (!Array.isArray(users)) return;
  if (request.payload.role !== null && users.some((user) => user.role !== request.payload.role))
    fail();
  if (
    users.length > 0 &&
    "is_active" in users[0] &&
    ((request.payload.status === "active" && users.some((user) => !user.is_active)) ||
      (request.payload.status === "inactive" && users.some((user) => user.is_active)))
  ) {
    fail();
  }
  if (
    request.payload.group_id !== undefined &&
    request.payload.group_id !== null &&
    users.some((user) => user.group_id !== request.payload.group_id)
  )
    fail();
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3) fail();
  const decode = (part) => {
    if (!/^[A-Za-z0-9_-]+$/.test(part) || part.length % 4 === 1) fail();
    const bytes = Buffer.from(part, "base64url");
    if (bytes.toString("base64url") !== part) fail();
    try {
      return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch {
      fail();
    }
  };
  if (!/^[A-Za-z0-9_-]+$/.test(parts[2]) || parts[2].length % 4 === 1) fail();
  const signature = Buffer.from(parts[2], "base64url");
  if (signature.toString("base64url") !== parts[2] || signature.length !== 32) fail();
  const header = decode(parts[0]);
  if (
    !isRecord(header) ||
    header.alg !== "HS256" ||
    (header.typ !== undefined && header.typ !== "JWT")
  )
    fail();
  const claims = decode(parts[1]);
  if (!isRecord(claims) || claims.iss !== issuer) fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = integer(claims.exp, 1n, maxPythonEpochSeconds);
  const generation = claims.gen === undefined ? 0n : integer(claims.gen);
  return { actorId, credentialId, expiry, generation };
}

function bindPrincipal(rawPrincipal, principal, request, claims, expectedApiKey) {
  if (!managerRoles.has(principal.role)) fail();
  if (claims !== null) {
    if (
      principal.actor_id !== claims.actorId ||
      principal.credential_type !== "jwt" ||
      principal.credential_id !== claims.credentialId ||
      principal.generation !== claims.generation ||
      Date.parse(rawPrincipal.expires_at) !== Number(claims.expiry) * 1000
    ) {
      fail();
    }
    return;
  }
  if (
    expectedApiKey === null ||
    principal.actor_id !== expectedApiKey.actor_id ||
    principal.credential_type !== "api_key" ||
    principal.credential_id !== expectedApiKey.credential_id
  ) {
    fail();
  }
}

export function decodeDirectoryResponse(
  request,
  source,
  status,
  contentType,
  expectedApiKey = null,
) {
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
  if (document.ok === false) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    return {
      schema,
      ok: false,
      operation: request.request.operation,
      operation_id: request.operationId,
      error: validateFailure(request.request, document.error),
    };
  }
  if (request.request.payload.role !== null && !platformRoles.has(request.request.payload.role)) {
    fail();
  }
  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.request.operation ||
    uuid(document.operation_id) !== request.operationId
  ) {
    fail();
  }
  document.operation_id = uuid(document.operation_id);
  const principal = validatePrincipal(document.principal);
  const claims = request.request.token.startsWith("ak_")
    ? null
    : incomingJwtClaims(request.request.token);
  bindPrincipal(document.principal, principal, request, claims, expectedApiKey);
  document.data = validateDirectoryData(request.request, document.data, principal);
  validateDataFilters(request.request, document.data);
  return document;
}

function validatePeerInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  const request = validateDirectoryRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: request.request.token,
  });
  return { request, origin: checked.origin, controlToken: checked.controlToken };
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  let source;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail();
  }
  return parseStrictJson(source);
}

function postDirectoryRequest(origin, controlToken, payload) {
  const target = new URL(endpoint, origin);
  const transport = target.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(stringifyLossless(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
      target,
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
            rejectResponse(new Error("Invalid Identity directory response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity directory request failed")));
      },
    );
    request.on("error", () => rejectResponse(new Error("Identity directory request failed")));
    request.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  if (!/^ak_/.test(token)) fail();
  const response = await postIdentityRequest(origin, controlToken, authEndpoint, { token });
  const identity = parseIdentityReceipt(response.text, response.status, response.contentType);
  return identity.ok ? identity.principal : null;
}

async function main() {
  const input = await readInput();
  const { request, origin, controlToken } = validatePeerInput(input);
  const expectedApiKey = request.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(origin, controlToken, request.request.token)
    : null;
  const response = await postDirectoryRequest(origin, controlToken, request.request);
  const result = decodeDirectoryResponse(
    request,
    response.text,
    response.status,
    response.contentType,
    expectedApiKey,
  );
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

function testAuditContext() {
  return {
    method: "GET",
    path: "/acceptance/identity-directory",
    ip: "127.0.0.1",
    request_id: "00000000-0000-4000-8000-000000000001",
  };
}

function testPrincipal(actorId, credentialId, generation = 0n) {
  return {
    actor_id: actorId,
    name: "Directory manager",
    email: "directory@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testUser(id, overrides = {}) {
  return {
    id,
    email: "directory-user@example.test",
    name: "Historical user",
    role: "annotator",
    group_name: null,
    group_id: null,
    status: "legacy-paused",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    email_verified_at: null,
    last_login_at: null,
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences: {
      token: "opaque preference value",
      password: new JsonNumber("123456789012345678901"),
      credential: { unknown: [true, null, "preserve"] },
      future: {
        wide: new JsonNumber("9007199254740993"),
        decimal: new JsonNumber("0.1234567890123456789"),
      },
    },
    avatar_ref: null,
    created_at: "2030-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeRequest(operation, operationId, token, payload) {
  const request = {
    schema,
    operation,
    operation_id: operationId,
    token,
    audit_context: testAuditContext(),
    payload,
  };
  return validateDirectoryRequest(request);
}

function makeToken(actorId, credentialId, generation = 0n) {
  const header = Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url");
  const payload = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "super_admin",
      exp: 1893456000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
    }),
  ).toString("base64url");
  return `${header}.${payload}.${Buffer.alloc(32, 7).toString("base64url")}`;
}

function testReply(request, data, principal) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    principal,
    data,
  });
}

function expectFailure(callback) {
  let failed = false;
  try {
    callback();
  } catch {
    failed = true;
  }
  if (!failed) fail();
}

function selfCheck() {
  let checks = 0;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const targetId = "33333333-3333-4333-8333-333333333333";
  const token = makeToken(actorId, credentialId, maxInt64);
  const principal = testPrincipal(actorId, credentialId, maxInt64);
  const filters = {
    status: "all",
    project_id: null,
    group_id: null,
    role: null,
    search: null,
  };
  const requests = [
    makeRequest("list_users", credentialId, token, {
      role: null,
      status: "active",
      project_id: null,
    }),
    makeRequest("query_users", credentialId, token, {
      ...filters,
      page: new JsonNumber("1"),
      page_size: new JsonNumber("50"),
    }),
    makeRequest("user_stats", credentialId, token, filters),
    makeRequest("export_users", credentialId, token, { ...filters, format: "csv" }),
  ];
  checks += requests.length;

  const user = testUser(targetId);
  const listData = { users: [user] };
  const listed = decodeDirectoryResponse(
    requests[0],
    testReply(requests[0], listData, principal),
    200,
    "application/json",
  );
  if (
    listed.data.users[0].role !== "annotator" ||
    listed.data.users[0].status !== "legacy-paused" ||
    !stringifyLossless(listed).includes('"wide":9007199254740993') ||
    !stringifyLossless(listed).includes('"decimal":0.1234567890123456789') ||
    !stringifyLossless(listed).includes('"token":"opaque preference value"') ||
    !stringifyLossless(listed).includes('"password":123456789012345678901')
  ) {
    fail();
  }
  checks += 1;

  const bigTotal = maxInt64;
  const queryData = {
    items: [],
    total: bigTotal,
    page: maxInt64,
    page_size: 500n,
    pages: (bigTotal + 499n) / 500n,
  };
  const queryRequest = makeRequest("query_users", credentialId, token, {
    ...filters,
    page: new JsonNumber(maxInt64.toString(10)),
    page_size: new JsonNumber("500"),
  });
  const query = decodeDirectoryResponse(
    queryRequest,
    testReply(queryRequest, queryData, principal),
    200,
    "application/json",
  );
  if (query.data.total !== maxInt64 || query.data.pages !== (maxInt64 + 499n) / 500n) fail();
  checks += 1;

  const statsRequest = requests[2];
  const stats = decodeDirectoryResponse(
    statsRequest,
    testReply(statsRequest, { total: maxInt64, online: 1n, weekly_active: 1n }, principal),
    200,
    "application/json",
  );
  if (stats.data.total !== maxInt64) fail();
  checks += 1;

  const exportRequest = requests[3];
  const exportedData = {
    users: [
      {
        id: targetId,
        email: user.email,
        name: user.name,
        role: user.role,
        group_name: null,
        group_id: null,
        status: user.status,
        created_at: user.created_at,
      },
    ],
    exported_by: principal.email,
    exported_at: "2030-01-01T00:00:00Z",
    request_id: exportRequest.request.audit_context.request_id,
  };
  if (
    !decodeDirectoryResponse(
      exportRequest,
      testReply(exportRequest, exportedData, principal),
      200,
      "application/json",
    ).ok
  )
    fail();
  checks += 1;
  const replayRequest = {
    ...exportRequest,
    request: {
      ...exportRequest.request,
      audit_context: { ...exportRequest.request.audit_context, request_id: "changed-context" },
    },
  };
  const changedPrincipal = { ...principal, email: "renamed@example.test" };
  if (
    !decodeDirectoryResponse(
      replayRequest,
      testReply(replayRequest, exportedData, changedPrincipal),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;

  const largeUsers = Array.from({ length: 1200 }, (_, index) =>
    testUser(`00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`, {
      email: `directory-${index}@example.test`,
    }),
  );
  const largeListReply = testReply(requests[0], { users: largeUsers }, principal);
  if (
    largeListReply.length <= 64 * 1024 ||
    decodeDirectoryResponse(requests[0], largeListReply, 200, "application/json").data.users
      .length !== 1200
  ) {
    fail();
  }
  checks += 1;

  const roleFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "invalid_platform_role", status_code: 400n, detail: "非法平台角色: annotator" },
  });
  const invalidRoleRequest = makeRequest("list_users", credentialId, token, {
    role: "annotator",
    status: "all",
    project_id: null,
  });
  if (
    decodeDirectoryResponse(invalidRoleRequest, roleFailure, 200, "application/json").error.code !==
    "invalid_platform_role"
  )
    fail();
  expectFailure(() =>
    decodeDirectoryResponse(
      invalidRoleRequest,
      roleFailure.replace("annotator", "reviewer"),
      200,
      "application/json",
    ),
  );
  checks += 2;
  expectFailure(() =>
    decodeDirectoryResponse(
      invalidRoleRequest,
      testReply(invalidRoleRequest, { users: [] }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const mismatch = { ...principal, actor_id: targetId };
  expectFailure(() =>
    decodeDirectoryResponse(
      requests[0],
      testReply(requests[0], { users: [user] }, mismatch),
      200,
      "application/json",
    ),
  );
  expectFailure(() => parseStrictJson('{"schema":"a","schema":"b"}'));
  expectFailure(() =>
    validateDirectoryRequest(
      parseStrictJson(
        `{"schema":"${schema}","operation":"query_users","operation_id":"${credentialId}","token":"x","audit_context":{"method":"GET","path":"/","ip":null,"request_id":null},"payload":{"status":"all","project_id":null,"group_id":null,"role":null,"search":null,"page":1.0,"page_size":50}}`,
      ),
    ),
  );
  expectFailure(() =>
    validateDirectoryRequest({
      ...requests[0].request,
      unexpected: true,
    }),
  );
  const invalidCredential = stringifyLossless({
    schema,
    ok: false,
    error: { code: "generation_outdated", status_code: null, detail: null },
  });
  if (
    decodeDirectoryResponse(requests[0], invalidCredential, 200, "application/json").error.code !==
    "generation_outdated"
  ) {
    fail();
  }
  const managerFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "manager_required", status_code: 403, detail: managerRequired },
  });
  if (
    decodeDirectoryResponse(requests[0], managerFailure, 200, "application/json").error.code !==
    "manager_required"
  ) {
    fail();
  }
  expectFailure(() =>
    decodeDirectoryResponse(
      requests[0],
      managerFailure.replace("需要角色权限", "权限不足"),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeDirectoryResponse(
      requests[0],
      testReply(requests[0], { users: [] }, principal).replace(
        `"operation_id":"${credentialId}"`,
        `"operation_id":"${targetId}"`,
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeDirectoryResponse(
      requests[0],
      testReply(requests[0], { users: [user, { ...user, password_hash: "forbidden" }] }, principal),
      200,
      "application/json",
    ),
  );
  checks += 9;
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
    } catch (error) {
      process.stderr.write(`Identity directory peer self-check failed: ${error.stack}\n`);
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity directory peer failed\n");
      process.exitCode = 1;
    });
  }
}
